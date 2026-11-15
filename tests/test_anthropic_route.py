"""The opt-in ``anthropic`` route: Haiku/Sonnet/Opus, and the level in ``output_config.effort``.

Three properties are pinned here, all of them wire-relevant:

1. The route's catalog is the three Claude families the provider's own model list carries, with
   the descriptions the planner fixed — they are the criteria strings Jev is sent.
2. The effort caps are family-scoped: Opus and Sonnet stop at ``high``, Haiku keeps ``max``. A
   level outside a family's cap is *omitted*, never clamped onto a neighbour — the same rule the
   other strict routes follow.
3. On the Anthropic Messages body the level lands in ``output_config.effort``, inside an
   ``output_config`` the transport already built. The plugin never creates that object, never adds
   the ``effort`` key to a container the transport left without one, and never writes a top-level
   ``reasoning_effort`` (not a Messages field).
"""

from __future__ import annotations

import copy
import json

import pytest
from stubs import StubResponse, StubTransport, anthropic_request, decision_payload
from test_router import build, route

from config import (
    ANTHROPIC_ALLOWLIST,
    ANTHROPIC_MODELS,
    ROUTED_API_MODES,
    load_settings,
    route_allowlist,
    route_catalog,
)
from effort import (
    ANTHROPIC_MODEL_LEVELS,
    ANTHROPIC_REASONING_POLICIES,
    DEFAULT_OFFERED_LEVELS,
    offered_levels,
    offered_levels_for_grid,
    resolve_effort,
)
from grid import ANTHROPIC_GRID

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

ANTHROPIC_CONFIG = {"decider": "jev_console", "routable_providers": ["anthropic"]}


def _settings(config=None):
    return load_settings(lambda key, default=None: (config or {}).get(key, default))


def anthropic_route(router, request=None, **overrides):
    """``route`` with the Anthropic Messages payload shape and provider."""
    kwargs = dict(
        model=SONNET,
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages",
    )
    kwargs.update(overrides)
    return route(router, request if request is not None else anthropic_request(), **kwargs)


# -- the catalog ---------------------------------------------------------------


def test_the_route_carries_the_three_claude_families_the_planner_fixed():
    assert [entry.model_id for entry in ANTHROPIC_GRID] == [HAIKU, SONNET, OPUS]
    descriptions = {entry.model_id: entry.description for entry in ANTHROPIC_GRID}
    assert descriptions == {
        HAIKU: "routine and well-scoped agentic coding, quick edits, summaries, extraction, "
        "classification and other high-volume chores",
        SONNET: "human-facing messages and writing, careful single-file or refined code changes, "
        "code review",
        OPUS: "planning, orchestration, architecture, multi-step debugging and deep analysis",
    }
    # The ids are the ones the provider's model cache lists, verbatim: no ``anthropic/`` prefix
    # and no picker alias, so what the grid names is what the endpoint is asked for.
    assert all("/" not in entry.model_id for entry in ANTHROPIC_GRID)
    assert all(entry.wire_model == entry.model_id for entry in ANTHROPIC_GRID)


def test_the_catalog_and_the_allowlist_are_the_same_three_ids():
    assert ANTHROPIC_MODELS == (HAIKU, SONNET, OPUS)
    assert ANTHROPIC_ALLOWLIST == frozenset(ANTHROPIC_MODELS)
    assert route_catalog("anthropic") == ANTHROPIC_GRID
    # Both aliases the core uses for this provider resolve to the same route.
    for alias in ("claude", "claude-code"):
        assert route_catalog(alias) == ANTHROPIC_GRID
        assert route_catalog(alias.upper()) == ANTHROPIC_GRID


def test_the_route_is_opt_in_and_the_cap_only_applies_under_the_console_decider():
    """Same contract as ``openai-codex``: the cap is a ``jev_console`` feature."""
    assert route_allowlist("anthropic") is None
    assert route_allowlist("claude", decider="jev") is None
    assert route_allowlist("anthropic", decider="jev_console") == ANTHROPIC_ALLOWLIST
    assert route_allowlist("claude", decider="jev_console") == ANTHROPIC_ALLOWLIST

    console = _settings(ANTHROPIC_CONFIG)
    grid = console.effective(provider="anthropic").grid
    assert [entry.model_id for entry in grid] == [HAIKU, SONNET, OPUS]
    # Sonnet is the route's default model, not the first entry (Haiku).
    assert console.effective(provider="anthropic").default_model == SONNET
    assert console.effective(provider="claude").default_model == SONNET


def test_an_operator_grid_outside_the_catalog_is_capped_back_to_the_route_catalog():
    settings = _settings(
        {
            "decider": "jev_console",
            "routable_providers": ["anthropic"],
            "grids": {"anthropic": ["claude-opus-4-1: old opus", "gpt-9-ultra: nonsense"]},
        }
    )

    assert [entry.model_id for entry in settings.grid_for("anthropic")] == [HAIKU, SONNET, OPUS]


def test_the_messages_mode_is_routed():
    assert "anthropic_messages" in ROUTED_API_MODES


# -- offered levels and caps ---------------------------------------------------


def test_opus_and_sonnet_offer_only_up_to_high_and_haiku_offers_max():
    assert offered_levels("anthropic", OPUS) == ("low", "medium", "high")
    assert offered_levels("anthropic", SONNET) == ("low", "medium", "high")
    assert offered_levels("anthropic", HAIKU) == ("low", "medium", "high", "max")
    # The aliases are the same route, so they answer the same way.
    assert offered_levels("claude", HAIKU) == ("low", "medium", "high", "max")


def test_the_shared_question_never_offers_a_level_a_family_caps_away():
    """The effort question is asked once, before the model is known.

    With Haiku on the grid the intersection is still ``low..high``: offering ``max`` there would
    spend the decision on a level Opus and Sonnet cannot be given. A Haiku-only grid can be asked
    for ``max``.
    """
    assert offered_levels_for_grid(ANTHROPIC_GRID, "anthropic") == ("low", "medium", "high")
    haiku_only = tuple(entry for entry in ANTHROPIC_GRID if entry.model_id == HAIKU)
    assert offered_levels_for_grid(haiku_only, "anthropic") == ("low", "medium", "high", "max")


def test_the_question_follows_each_routes_own_offered_declaration():
    """A policy that declares an offered set widens its question; nothing else does.

    The three OpenAI-compatible generation routes carry their contract in code (each model's
    accepted set covers ``low``/``medium``/``high``/``max``), so an operator no longer repeats it
    in ``effort_maps``. A policy that leaves the field empty keeps the three-level default.
    """
    assert offered_levels("openai-codex", "gpt-6-luna") == ("low", "medium", "high", "max")
    assert offered_levels("openai-codex", "gpt-6-luna-900k") == ("low", "medium", "high", "max")
    assert offered_levels("openai-codex", "gpt-6.1-sol") == ("low", "medium", "high", "max")
    assert offered_levels("opencode-go", "deepseek-v4.1-flash") == ("low", "medium", "high", "max")
    assert offered_levels("commandcode", "deepseek/deepseek-v4.1-flash") == (
        "low",
        "medium",
        "high",
        "max",
    )
    # No declaration, no widening: the legacy route keeps the three-level question.
    assert offered_levels("ollama-cloud", "kimi-k3") == DEFAULT_OFFERED_LEVELS
    # A model outside the route's catalog is a deliberate omission, not an offer.
    assert offered_levels("opencode-go", "kimi-k3") == DEFAULT_OFFERED_LEVELS


def test_an_operator_map_still_wins_over_the_built_in_offer():
    maps = {"anthropic": {SONNET: ["low", "high"]}}

    assert offered_levels("anthropic", SONNET, maps) == ("low", "high")
    # A model the map does not name falls back to its own built-in declaration.
    assert offered_levels("anthropic", HAIKU, maps) == ("low", "medium", "high", "max")


def test_the_caps_and_the_policies_cannot_drift_apart():
    assert dict(ANTHROPIC_MODEL_LEVELS) == {
        model_id: policy.accepted_levels
        for model_id, policy in ANTHROPIC_REASONING_POLICIES.items()
    }
    for model_id, policy in ANTHROPIC_REASONING_POLICIES.items():
        assert policy.provider == "anthropic"
        assert policy.wire_parameter == "output_config.effort"
        assert policy.omitted is False
        assert policy.legacy_clamp is False
        assert policy.offered == policy.accepted_levels
        assert "output_config.effort" in policy.evidence
        assert "ADAPTIVE_EFFORT_MAP" in policy.evidence
        assert model_id in policy.decision


# -- the wire value ------------------------------------------------------------


@pytest.mark.parametrize("level", ["low", "medium", "high"])
@pytest.mark.parametrize("model", [HAIKU, SONNET, OPUS])
def test_every_accepted_level_goes_out_verbatim(model, level):
    assert resolve_effort(model, level, provider="anthropic") == level


def test_max_is_sent_on_haiku_and_omitted_on_opus_and_sonnet():
    """A level the family caps away is dropped, never clamped to ``high``."""
    assert resolve_effort(HAIKU, "max", provider="anthropic") == "max"
    assert resolve_effort(SONNET, "max", provider="anthropic") is None
    assert resolve_effort(OPUS, "max", provider="anthropic") is None


@pytest.mark.parametrize("level", ["xhigh", "minimal", "ultra", "none", "bogus", "", None])
def test_an_undeclared_level_is_omitted(level):
    for model in (HAIKU, SONNET, OPUS):
        assert resolve_effort(model, level, provider="anthropic") is None


def test_a_model_outside_the_catalog_is_omitted():
    assert resolve_effort("claude-opus-4-1", "high", provider="anthropic") is None
    assert resolve_effort("gpt-6-luna", "high", provider="anthropic") is None


# -- the payload ---------------------------------------------------------------


def test_the_level_lands_in_output_config_and_nowhere_else(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(router, turn_id="anthropic-1", session_id="anthropic")

    assert result is not None
    routed = result["request"]
    assert routed["model"] == SONNET
    assert routed["output_config"] == {"effort": "high"}
    # The thinking block, the system prompt and the token cap are the transport's.
    assert routed["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert routed["system"] == "you are a test"
    assert routed["max_tokens"] == 64000
    # A Messages body takes neither of these; adding one is a dead turn.
    assert "reasoning_effort" not in routed
    assert "reasoning_config" not in routed

    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["provider"] == "anthropic"
    assert record["model"] == SONNET
    assert record["wire_model"] == SONNET
    assert record["effort"] == "high"
    assert record["effort_requested"] == "high"
    assert record["fallback_reasons"] == []
    assert transport.call_count == 1


def test_the_request_is_not_mutated_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("3", 0.95, "low", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    request = anthropic_request(effort="medium")
    snapshot = copy.deepcopy(request)

    result = anthropic_route(router, request, turn_id="anthropic-immutable", session_id="anthropic")

    assert result is not None
    assert result["request"] is not request
    assert request == snapshot
    # The transport's own level is replaced by Jev's, not merged with it.
    assert result["request"]["output_config"] == {"effort": "low"}


def test_a_haiku_only_grid_can_put_max_on_the_wire(tmp_path, monkeypatch):
    """The one route-level ``max`` the caps allow, end to end."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [
            StubResponse(
                decision_payload(
                    "1",
                    0.97,
                    "max",
                    0.93,
                    effort_probabilities={"low": 0.02, "medium": 0.02, "high": 0.03, "max": 0.93},
                    grid_size=1,
                )
            )
        ]
    )
    config = dict(ANTHROPIC_CONFIG, grids={"anthropic": [f"{HAIKU}: chores"]})
    router, settings = build(tmp_path, transport, config=config)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(
        router,
        anthropic_request(model=HAIKU),
        model=HAIKU,
        turn_id="anthropic-haiku-max",
        session_id="anthropic-haiku-max",
    )

    assert result is not None
    assert result["request"]["model"] == HAIKU
    assert result["request"]["output_config"] == {"effort": "max"}
    record = router.tail(settings, limit=1)[0]
    assert record["effort"] == "max"
    assert record["effort_requested"] == "max"


def test_an_unoffered_max_is_recorded_and_dropped_on_the_messages_wire(tmp_path, monkeypatch):
    """Opus/Sonnet cap at ``high``: the answer survives in the audit, not on the wire."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [
            StubResponse(
                decision_payload(
                    "3",
                    0.95,
                    "max",
                    0.9,
                    effort_probabilities={"low": 0.03, "medium": 0.03, "high": 0.04, "max": 0.9},
                    grid_size=3,
                )
            )
        ]
    )
    router, settings = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(
        router, turn_id="anthropic-opus-max", session_id="anthropic-opus-max"
    )

    assert result is not None
    assert result["request"]["model"] == OPUS
    # The transport's own level is removed with the decision (a null ``effort`` in the audit must
    # mean "no level on the wire"); nothing is substituted for it — no ``high`` clamp, and an
    # ``output_config`` that is left empty is dropped instead of sent as ``{}``.
    assert "output_config" not in result["request"]
    assert "reasoning_effort" not in result["request"]
    record = router.tail(settings, limit=1)[0]
    assert record["effort_requested"] == "max"
    assert record["effort"] is None


def test_a_body_the_transport_left_without_an_effort_field_gets_none_invented(
    tmp_path, monkeypatch
):
    """Pre-adaptive-thinking Claude (or reasoning disabled): no ``output_config`` is created."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(
        router,
        anthropic_request(thinking=False),
        turn_id="anthropic-noeffort",
        session_id="anthropic-noeffort",
    )

    assert result is not None
    assert "output_config" not in result["request"]
    assert "reasoning_effort" not in result["request"]
    record = router.tail(settings, limit=1)[0]
    # The wire carried no level; ``effort_requested`` still names Jev's choice.
    assert record["effort"] is None
    assert record["effort_requested"] == "high"


def test_an_output_config_without_an_effort_key_is_not_given_one(tmp_path, monkeypatch):
    """Structured output puts ``format`` in the same object; that is not permission to add effort."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    request = anthropic_request(thinking=False)
    request["output_config"] = {"format": {"type": "json_schema"}}

    result = anthropic_route(
        router, request, turn_id="anthropic-format-only", session_id="anthropic-format-only"
    )

    assert result is not None
    assert result["request"]["output_config"] == {"format": {"type": "json_schema"}}
    assert router.tail(settings, limit=1)[0]["effort"] is None


def test_other_output_config_keys_survive_the_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "high", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(
        router,
        anthropic_request(output_config_extra={"format": {"type": "json_schema"}}),
        turn_id="anthropic-format-shared",
        session_id="anthropic-format-shared",
    )

    assert result is not None
    assert result["request"]["output_config"] == {
        "format": {"type": "json_schema"},
        "effort": "high",
    }


def test_the_route_replays_the_same_body_without_a_second_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "high", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    request = anthropic_request()

    first = anthropic_route(router, request, turn_id="anthropic-replay", session_id="anthropic-replay")
    second = anthropic_route(
        router, request, turn_id="anthropic-replay", session_id="anthropic-replay", api_call_count=1
    )

    assert first is not None and second is not None
    assert json.dumps(second["request"], sort_keys=True) == json.dumps(
        first["request"], sort_keys=True
    )
    assert transport.call_count == 1


# -- the opt-in gate -----------------------------------------------------------


def test_the_provider_is_left_untouched_when_it_is_not_declared(tmp_path, monkeypatch):
    """Without ``anthropic`` in ``routable_providers`` the turn is byte-identical."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport([])
    router, settings = build(
        tmp_path, transport, config={"decider": "jev_console", "routable_providers": ["ollama-cloud"]}
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    request = anthropic_request()

    assert anthropic_route(router, request, turn_id="anthropic-off", session_id="anthropic-off") is None
    assert transport.call_count == 0
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "skip"
    assert record["reason"] == "provider_not_routed"
    assert record["provider"] == "anthropic"


def test_the_core_alias_names_the_same_routable_route(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "low", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(
        router, provider="claude", turn_id="anthropic-alias", session_id="anthropic-alias"
    )

    assert result is not None
    assert result["request"]["model"] == HAIKU
    assert result["request"]["output_config"] == {"effort": "low"}
