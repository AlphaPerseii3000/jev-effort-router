"""The four-step rubric, the route-scoped effort levels, and the previous-turn signal.

Three properties are pinned here, all of them cost-relevant:

1. ``max`` is part of the vocabulary but is only *offered* where the route's ``effort_maps``
   entry declares it — never offered and then clamped away.
2. A cap on the way to the wire is audited: ``effort_requested`` keeps what was asked for and
   ``effort`` keeps what was applied.
3. The effort question and the accepted answer keys come from the same offered set, and the
   payload still fits the budget with a full context.
"""

from __future__ import annotations

import json

import pytest

from config import EFFORT_LEVELS, load_settings
from effort import DEFAULT_OFFERED_LEVELS, offered_levels, offered_levels_for_grid
from grid import DEFAULT_GRID, Entry
from state import (
    EFFORT_CRITERIA,
    EFFORT_INSTRUCTIONS,
    EFFORT_QUESTION_ID,
    TOOL_RESULTS_CHAR_LIMIT,
    build_payload,
    build_questions,
    build_state,
    normalise_effort_choice,
)

PAIR = (Entry("deepseek-v4.1-flash", "the usual choice"), Entry("kimi-k3", "complex code"))
SINGLE = (Entry("deepseek-v4.1-flash", "the usual choice"),)


def settings_for(config):
    return load_settings(lambda key, default=None: (config or {}).get(key, default))


# -- the rubric ---------------------------------------------------------------------


def test_the_default_rubric_has_four_rungs_and_the_lowest_covering_rule():
    assert set(EFFORT_CRITERIA) == set(EFFORT_LEVELS) == {"low", "medium", "high", "max"}
    assert "LOWEST" in EFFORT_INSTRUCTIONS
    assert "uncertainty" in EFFORT_INSTRUCTIONS
    assert all(text.strip() for text in EFFORT_CRITERIA.values())
    # Each rung carries an example, so the choice is grounded instead of adjectival.
    assert "E.g." in EFFORT_CRITERIA["low"]
    assert "E.g." in EFFORT_CRITERIA["medium"]
    assert "E.g." in EFFORT_CRITERIA["high"]


# -- offered levels -----------------------------------------------------------------


def test_a_route_without_a_map_is_offered_the_three_levels():
    assert offered_levels("ollama-cloud", "kimi-k3", None) == DEFAULT_OFFERED_LEVELS
    assert offered_levels("ollama-cloud", "kimi-k3", {"other": {"kimi-k3": ["low", "max"]}}) == (
        DEFAULT_OFFERED_LEVELS
    )
    assert offered_levels("ollama-cloud", "kimi-k3", {"ollama-cloud": {}}) == (
        DEFAULT_OFFERED_LEVELS
    )


def test_max_is_offered_only_when_the_map_accepts_it():
    maps = {"ollama-cloud": {"kimi-k3": {"accepted": ["low", "high", "max"]}}}

    assert offered_levels("ollama-cloud", "kimi-k3", maps) == ("low", "high", "max")
    # A map keyed on the bare slug answers for its longer id too.
    assert offered_levels("ollama-cloud", "kimi-k3-256k", maps) == ("low", "high", "max")
    # A model the map does not name keeps the default, so ``max`` is not offered for it.
    assert offered_levels("ollama-cloud", "glm-5.3", maps) == DEFAULT_OFFERED_LEVELS


def test_a_map_declaring_no_usable_level_keeps_the_default():
    maps = {"ollama-cloud": {"kimi-k3": {"accepted": ["none", "minimal"]}}}

    assert offered_levels("ollama-cloud", "kimi-k3", maps) == DEFAULT_OFFERED_LEVELS


def test_an_empty_intersection_falls_back_to_the_default_and_documents_it():
    """Two models whose declarations share no level cannot be asked one question.

    The plugin's answer is the default three levels (a declaration unusable for this question is
    treated as no declaration); ``resolve_effort`` then omits whatever is answered, never clamps.
    """
    maps = {"p": {"a": ["high"], "b": ["low"]}}

    assert offered_levels_for_grid((Entry("a", "x"), Entry("b", "y")), "p", maps) == (
        "low",
        "medium",
        "high",
    )


def test_the_shared_question_offers_only_what_every_model_accepts():
    maps = {
        "p": {
            "a": ["low", "medium", "high", "max"],
            "b": ["low", "medium", "high"],
        }
    }

    assert offered_levels_for_grid((Entry("a", "x"), Entry("b", "y")), "p", maps) == (
        "low",
        "medium",
        "high",
    )
    assert offered_levels_for_grid((Entry("a", "x"),), "p", maps) == (
        "low",
        "medium",
        "high",
        "max",
    )


def test_a_level_outside_the_declaration_is_omitted_not_clamped():
    """The answer is validated as a level; the route's declaration decides whether it applies.

    ``max`` is answerable in principle, but a map that does not declare it makes
    ``resolve_effort`` omit the field — never clamp it to a weaker rung (main's declared-vocabulary
    rule). Failing the whole decision instead would punish a level the route was never offered.
    """
    from effort import resolve_effort

    assert normalise_effort_choice("max") == "max"
    maps = {"opencode-go": {"deepseek-v4.1-flash": {"accepted": ["low", "medium", "high"]}}}
    assert resolve_effort("deepseek-v4.1-flash", "max", provider="opencode-go", effort_maps=maps) is None

    with_max = {"opencode-go": {"deepseek-v4.1-flash": {"accepted": ["low", "medium", "high", "max"]}}}
    assert resolve_effort("deepseek-v4.1-flash", "max", provider="opencode-go", effort_maps=with_max) == "max"


# -- questions ----------------------------------------------------------------------


def test_the_effort_question_lists_only_the_offered_levels():
    offered = build_questions(SINGLE, levels=("low", "high", "max"))
    default = build_questions(SINGLE, levels=DEFAULT_OFFERED_LEVELS)

    assert set(offered[EFFORT_QUESTION_ID]["criteria"]) == {"low", "high", "max"}
    assert set(default[EFFORT_QUESTION_ID]["criteria"]) == {"low", "medium", "high"}


def test_the_rubric_knobs_rewrite_the_question_without_losing_the_other_rungs():
    settings = settings_for(
        {
            "effort_criteria": {"high": "our own high-effort text"},
            "effort_instructions": "our own routing rule",
        }
    )

    questions = build_questions(
        SINGLE,
        levels=("low", "high"),
        criteria_override=settings.effort_criteria,
        instructions_override=settings.effort_instructions,
    )

    criteria = questions[EFFORT_QUESTION_ID]["criteria"]
    assert criteria["high"] == "our own high-effort text"
    assert criteria["low"] == EFFORT_CRITERIA["low"]
    assert questions[EFFORT_QUESTION_ID]["instructions"] == "our own routing rule"


@pytest.mark.parametrize(
    "knob",
    [
        {"ultra": "a level the vocabulary does not have"},
        {"low": ""},
        {"low": 3},
        {"low": "ok", "high": None},
        ["low", "high"],
    ],
)
def test_an_invalid_criteria_knob_falls_back_to_the_built_in_rubric(knob):
    settings = settings_for({"effort_criteria": knob})

    assert settings.effort_criteria == {}
    questions = build_questions(
        SINGLE, levels=DEFAULT_OFFERED_LEVELS, criteria_override=settings.effort_criteria
    )
    assert questions[EFFORT_QUESTION_ID]["criteria"] == {
        level: EFFORT_CRITERIA[level] for level in DEFAULT_OFFERED_LEVELS
    }


@pytest.mark.parametrize("knob", [7, "", "   ", None])
def test_an_invalid_instructions_knob_falls_back_to_the_built_in_rule(knob):
    settings = settings_for({"effort_instructions": knob})

    questions = build_questions(
        SINGLE, levels=DEFAULT_OFFERED_LEVELS, instructions_override=settings.effort_instructions
    )
    assert questions[EFFORT_QUESTION_ID]["instructions"] == EFFORT_INSTRUCTIONS


# -- previous turn signal ------------------------------------------------------------


def _history_with_tools(tool_calls, tool_content="tool output"):
    return [
        {"role": "user", "content": "first request"},
        {
            "role": "assistant",
            "content": "working on it",
            "tool_calls": [
                {"id": str(index), "function": {"name": name}}
                for index, name in enumerate(tool_calls)
            ],
        },
        {"role": "tool", "content": tool_content},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "now the next request"},
    ]


def test_the_previous_turn_shape_reaches_the_state():
    state = build_state(
        _history_with_tools(["terminal", "terminal", "read_file"]), context_turns=4
    )

    assert state["previous_turn"] == {
        "tool_calls": 3,
        "tools": ["terminal", "read_file"],
    }
    assert state["last_tool_results_excerpt"] == "tool output"


def test_the_previous_turn_names_at_most_eight_distinct_tools():
    state = build_state(_history_with_tools([f"tool-{index}" for index in range(12)]), context_turns=2)

    assert state["previous_turn"]["tool_calls"] == 12
    assert state["previous_turn"]["tools"] == [f"tool-{index}" for index in range(8)]


def test_a_first_turn_carries_no_turn_signal():
    state = build_state([{"role": "user", "content": "hello"}], context_turns=4)

    assert "previous_turn" not in state
    assert "last_tool_results_excerpt" not in state


def test_the_tool_results_excerpt_is_a_bounded_redacted_tail(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-for-this-test")
    tail = "END-OF-THE-TOOL-OUTPUT"
    content = "x" * 5000 + " sk-or-secret-for-this-test " + tail

    state = build_state(_history_with_tools(["terminal"], tool_content=content), context_turns=2)
    excerpt = state["last_tool_results_excerpt"]

    assert len(excerpt) <= TOOL_RESULTS_CHAR_LIMIT
    assert excerpt.endswith(tail)
    assert "[REDACTED]" in excerpt
    assert "sk-or-secret-for-this-test" not in excerpt


def test_a_history_without_a_user_message_keeps_only_the_trailing_assistant_run():
    messages = [
        {"role": "assistant", "content": "old", "tool_calls": [{"function": {"name": "terminal"}}]},
        {"role": "tool", "content": "old output"},
        {"role": "system", "content": "notice"},
        {"role": "assistant", "content": "recent", "tool_calls": [{"function": {"name": "read_file"}}]},
        {"role": "tool", "content": "recent output"},
        {"role": "user", "content": "next"},
    ]

    state = build_state(messages, context_turns=4)

    assert state["previous_turn"] == {"tool_calls": 1, "tools": ["read_file"]}
    assert state["last_tool_results_excerpt"] == "recent output"


def test_a_plain_assistant_turn_reports_no_tool_shape():
    """No tool call and no tool result: nothing to report, and no invented ``tool_calls: 0``.

    On the Responses wire the transport has already projected ``function_call`` items away, so an
    eager zero would claim the previous turn used no tools when it may have used several.
    """
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a plain answer"},
        {"role": "user", "content": "second"},
    ]

    state = build_state(messages, context_turns=4)

    assert "previous_turn" not in state
    assert "last_tool_results_excerpt" not in state


def test_a_tool_output_larger_than_the_window_still_reports_its_tail(monkeypatch):
    """The excerpt is the tail of the output, not the tail of the first megabyte."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-for-this-test")
    tail = "TRUE-END-OF-OUTPUT"
    content = "x" * 1_200_000 + " sk-or-secret-for-this-test " + tail

    state = build_state(_history_with_tools(["terminal"], tool_content=content), context_turns=2)
    excerpt = state["last_tool_results_excerpt"]

    assert len(excerpt) <= TOOL_RESULTS_CHAR_LIMIT
    assert excerpt.endswith(tail)
    assert "sk-or-secret-for-this-test" not in excerpt
    assert "[REDACTED]" in excerpt


def test_a_tool_result_without_a_visible_call_reports_no_fake_zero():
    """A preview projection can drop the call while keeping the result: no invented count."""
    messages = [
        {"role": "user", "content": "first"},
        {"role": "tool", "content": "output of an unseen call"},
        {"role": "user", "content": "second"},
    ]

    state = build_state(messages, context_turns=4)

    assert "previous_turn" not in state
    assert state["last_tool_results_excerpt"] == "output of an unseen call"


def test_the_plugin_own_commandcode_key_is_redacted_from_the_excerpt(monkeypatch):
    monkeypatch.setenv("COMMANDCODE_API_KEY", "cc-secret-for-this-test")
    content = "x" * 4000 + " cc-secret-for-this-test END"

    state = build_state(_history_with_tools(["terminal"], tool_content=content), context_turns=2)

    assert "cc-secret-for-this-test" not in state["last_tool_results_excerpt"]
    assert "[REDACTED]" in state["last_tool_results_excerpt"]


def test_reasoning_is_never_forwarded():
    messages = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "visible answer",
            "reasoning": "private chain of thought",
            "reasoning_content": "another private chain",
        },
        {"role": "user", "content": "second"},
    ]

    payload = json.dumps(build_state(messages, context_turns=4))

    assert "chain of thought" not in payload
    assert "another private chain" not in payload


# -- payload budget -----------------------------------------------------------------


def test_the_payload_fits_the_budget_with_a_full_context():
    messages = []
    for index in range(12):
        messages.append({"role": "user", "content": f"turn {index} " + "u" * 5000})
        messages.append({"role": "assistant", "content": "a" * 5000})
        messages.append({"role": "tool", "content": "t" * 5000})
    # The worst legal turn signal: eight tool calls whose names are far longer than any real one,
    # plus a full-length excerpt. The name cap is what keeps this inside the budget.
    messages.append(
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {"id": str(index), "function": {"name": f"t{index}" + "n" * 200}}
                for index in range(8)
            ],
        }
    )
    messages.append({"role": "tool", "content": "t" * 5000})
    messages.append({"role": "user", "content": "final request " + "f" * 5000})

    payload = build_payload(
        messages,
        DEFAULT_GRID,
        jev_model="typesafe/jev-1.13",
        context_turns=50,
        platform="cli",
        provider="ollama-cloud",
        levels=EFFORT_LEVELS,
    )

    # Eight distinct names, each truncated to the cap.
    tools = payload["state"]["previous_turn"]["tools"]
    assert len(tools) == 8
    assert tools[0] == "t0" + "n" * 62
    assert len(json.dumps(payload, ensure_ascii=False)) <= 20000


# -- routing, end to end -------------------------------------------------------------

#: The live shape this feature exists for: one model, a map that declares ``max``. The Console
#: decider is the one in use, and the fallback model is disabled so one answer is one call.
GO_CONFIG = {
    "decider": "jev_console",
    "routable_providers": ["opencode-go"],
    "console_jev_fallback_model": "",
    "effort_maps": {
        "opencode-go": {"deepseek-v4.1-flash": {"accepted": ["low", "medium", "high", "max"]}}
    },
}


def _go_router(tmp_path, monkeypatch, transport, config):
    from test_router import build

    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    router, settings = build(tmp_path, transport, config=dict(config))
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    return router, settings


def _max_answer():
    from stubs import decision_payload

    return decision_payload(
        model_choice="1",
        model_confidence=1.0,
        effort_choice="max",
        effort_confidence=1.0,
        effort_probabilities={"max": 1.0},
        grid_size=1,
    )


def test_max_is_offered_and_applied_where_the_map_accepts_it(tmp_path, monkeypatch):
    from stubs import StubResponse, StubTransport
    from test_router import route

    transport = StubTransport([StubResponse(_max_answer())])
    router, settings = _go_router(tmp_path, monkeypatch, transport, GO_CONFIG)

    result = route(router, provider="opencode-go", turn_id="turn-max")

    criteria = transport.last_payload["questions"][EFFORT_QUESTION_ID]["criteria"]
    assert set(criteria) == {"low", "medium", "high", "max"}
    assert result["request"]["reasoning_effort"] == "max"
    record = router.tail(settings, limit=1)[0]
    assert (record["effort_requested"], record["effort"]) == ("max", "max")


def test_dropping_the_effort_map_keeps_the_max_option_and_the_wire_value(tmp_path, monkeypatch):
    """The built-in policy now declares what the operator's ``effort_maps`` used to.

    This is the config simplification's core promise: deleting the ``effort_maps`` entry from a
    profile must not change the effort question, the decision it accepts, or the wire value.
    """
    from stubs import StubResponse, StubTransport
    from test_router import route

    config = {key: value for key, value in GO_CONFIG.items() if key != "effort_maps"}
    transport = StubTransport([StubResponse(_max_answer())])
    router, settings = _go_router(tmp_path, monkeypatch, transport, config)

    result = route(router, provider="opencode-go", turn_id="turn-no-map")

    criteria = transport.last_payload["questions"][EFFORT_QUESTION_ID]["criteria"]
    assert set(criteria) == {"low", "medium", "high", "max"}
    assert result is not None
    assert result["request"]["reasoning_effort"] == "max"
    record = router.tail(settings, limit=1)[0]
    assert (record["effort_requested"], record["effort"]) == ("max", "max")


def test_a_capped_max_is_audited_with_the_level_that_was_asked(tmp_path, monkeypatch):
    from stubs import StubResponse, StubTransport
    from test_router import route

    config = dict(GO_CONFIG, platform_overrides={"cli": {"max_effort": "high"}})
    transport = StubTransport([StubResponse(_max_answer())])
    router, settings = _go_router(tmp_path, monkeypatch, transport, config)

    result = route(router, provider="opencode-go", turn_id="turn-capped")

    # The question still offers ``max`` — the map accepts it; the owner's cap decides what runs.
    criteria = transport.last_payload["questions"][EFFORT_QUESTION_ID]["criteria"]
    assert "max" in criteria
    assert result["request"]["reasoning_effort"] == "high"
    record = router.tail(settings, limit=1)[0]
    assert record["effort_requested"] == "max"
    assert record["effort"] == "high"


def test_the_replay_keeps_the_asked_level_and_the_cap_applies_to_the_wire(tmp_path, monkeypatch):
    """The memo replay re-applies the cap to the wire, never to the recorded request."""
    from stubs import StubResponse, StubTransport
    from test_router import route

    config = dict(GO_CONFIG, platform_overrides={"cli": {"max_effort": "high"}})
    transport = StubTransport([StubResponse(_max_answer())])
    router, settings = _go_router(tmp_path, monkeypatch, transport, config)

    first = route(router, provider="opencode-go", turn_id="turn-replay")
    second = route(router, provider="opencode-go", turn_id="turn-replay", api_call_count=1)

    assert first is not None and second is not None
    # One decider call for the turn: the second request is the memo's replay.
    assert transport.call_count == 1
    records = router.tail(settings, limit=5)
    assert len(records) == 2
    assert records[0].get("replayed") is not True
    assert records[1]["replayed"] is True
    for record in records:
        assert record["effort_requested"] == "max"
        assert record["effort"] == "high"


def test_the_rubric_knobs_reach_the_question_the_decider_sends(tmp_path, monkeypatch):
    """The knobs are wired end to end: settings → decider → the payload on the wire."""
    from stubs import StubResponse, StubTransport, decision_payload
    from test_router import route

    config = dict(
        GO_CONFIG,
        effort_criteria={"high": "our own high-effort text"},
        effort_instructions="our own routing rule",
    )
    transport = StubTransport(
        [
            StubResponse(
                decision_payload(
                    model_choice="1",
                    model_confidence=1.0,
                    effort_choice="medium",
                    effort_confidence=1.0,
                    effort_probabilities={"medium": 1.0},
                    grid_size=1,
                )
            )
        ]
    )
    router, _ = _go_router(tmp_path, monkeypatch, transport, config)

    assert route(router, provider="opencode-go", turn_id="turn-knobs") is not None

    question = transport.last_payload["questions"][EFFORT_QUESTION_ID]
    assert question["instructions"] == "our own routing rule"
    assert question["criteria"]["high"] == "our own high-effort text"
    assert question["criteria"]["low"] == EFFORT_CRITERIA["low"]


def test_the_openai_compatible_cap_keeps_the_requested_level(monkeypatch):
    """The third decider's cap follows the same audit contract: asked ``max``, applied ``high``."""
    from client import JevClient
    from stubs import StubResponse, StubTransport

    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-ope...-123")
    transport = StubTransport(
        [
            StubResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"model": "deepseek-v4.1-flash", "effort": "max", "confidence": 0.95}
                                )
                            }
                        }
                    ]
                }
            )
        ]
    )
    config = {
        "decider": "openai_compatible",
        "decider_base_url": "https://opencode.ai/zen/go/v1",
        "decider_model": "deepseek-v4.1-flash",
        "decider_api_key_env": "OPENCODE_GO_API_KEY",
        "routable_providers": ["opencode-go"],
        "platform_overrides": {"cron": {"max_effort": "high"}},
    }
    settings = settings_for(config)
    client = JevClient(settings, transport=transport)

    decision, reason = client.decide(
        [{"role": "user", "content": "plan the migration"}],
        settings.effective(settings.policy_for("cron"), provider="opencode-go"),
        platform="cron",
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    assert decision.effort_requested == "max"
    assert decision.effort == "high"


def test_max_effort_policy_accepts_max():
    settings = settings_for({"platform_overrides": {"cron": {"max_effort": "max"}}})

    assert settings.policy_for("cron").max_effort == "max"
    assert settings.effective(settings.policy_for("cron")).max_effort == "max"


def test_the_openai_compatible_schema_names_the_offered_levels(monkeypatch):
    from client import JevClient
    from stubs import StubResponse, StubTransport

    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-ope...-123")
    transport = StubTransport([StubResponse({"choices": [{"message": {"content": json.dumps(
        {"model": "deepseek-v4.1-flash", "effort": "high", "confidence": 0.95}
    )}}]})])
    config = dict(
        {
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_model": "deepseek-v4.1-flash",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "routable_providers": ["opencode-go"],
            # One model, so the prompt takes the effort-only shape.
            "grid": ["deepseek-v4.1-flash: the Go route's only model"],
        },
        effort_maps=GO_CONFIG["effort_maps"],
    )
    settings = settings_for(config)
    client = JevClient(settings, transport=transport)

    decision, reason = client.decide(
        [{"role": "user", "content": "plan the migration"}],
        settings.effective(None),
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    system_prompt = transport.last_payload["messages"][0]["content"]
    assert "<low|medium|high|max>" in system_prompt
    assert "low, medium, high, or max" in system_prompt


def test_the_default_effort_refuses_max():
    """``default_effort`` is the fallback of a degraded turn, never the expensive rung.

    ``max`` is a vocabulary level, so validating the knob against ``EFFORT_LEVELS`` would let a
    hand-written config turn the decider's failure into the priciest turn — and the manifest's
    ``choices`` never protected a config edited outside the plugin's own write path.
    """
    settings = settings_for({"default_effort": "max"})

    assert settings.default_effort == "medium"
    # The vocabulary still accepts it where it is a genuine choice: the operator's ceiling.
    policy = settings_for({"platform_overrides": {"cli": {"max_effort": "max"}}}).policy_for("cli")
    assert policy is not None
    assert policy.max_effort == "max"
