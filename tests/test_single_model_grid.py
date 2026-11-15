"""A one-model grid asks no model question.

A Choice over one option has one possible answer and a confidence of 1.0 by construction, so the
question spends tokens and latency to learn nothing. The model is fixed to the grid's only entry,
the audit records ``model_choice: "single"``, and a two-model grid keeps the question unchanged.
"""

from __future__ import annotations

import json

from config import load_settings
from decider import JevConsoleDecider, JevDecider, OpenAICompatibleDecider, REASON_MALFORMED
from grid import Entry
from state import MODEL_QUESTION_ID, SINGLE_MODEL_CHOICE, build_questions
from stubs import StubResponse, StubTransport, decision_payload

from test_router import build, route

SINGLE = (Entry("deepseek-v4.1-flash", "the usual choice"),)
PAIR = (
    Entry("deepseek-v4.1-flash", "the usual choice"),
    Entry("kimi-k3", "complex code"),
)

MESSAGES = [{"role": "user", "content": "refactor the payment module"}]


# -- state ---------------------------------------------------------------------------


def test_a_one_model_grid_omits_the_model_question():
    questions = build_questions(SINGLE)

    assert MODEL_QUESTION_ID not in questions
    assert questions["reasoning_effort"]["type"] == "choice"
    assert set(questions["reasoning_effort"]["criteria"]) == {"low", "medium", "high"}


def test_a_two_model_grid_keeps_both_questions():
    questions = build_questions(PAIR)

    assert set(questions[MODEL_QUESTION_ID]["criteria"]) == {"1", "2"}
    assert "reasoning_effort" in questions


def test_the_default_still_asks_the_model_question():
    """A grid with a choice keeps the model question: the default asks when ``len(grid) > 1``."""
    from state import build_payload

    payload = build_payload(
        MESSAGES,
        PAIR,
        jev_model="typesafe/jev-1.13",
        context_turns=2,
        platform="cli",
        provider="ollama-cloud",
    )
    assert MODEL_QUESTION_ID in payload["questions"]


# -- jev_console ---------------------------------------------------------------------


def _console_client(settings):
    return JevConsoleDecider(settings)


def test_jev_console_fixes_the_model_and_audits_it(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-key")
    settings = load_settings(
        lambda key, default=None: {
            "decider": "jev_console",
            "grids": {"opencode-go": ["deepseek-v4.1-flash: the usual choice"]},
        }.get(key, default)
    )
    transport = StubTransport([StubResponse(decision_payload(model_choice="1", model_confidence=0.95, grid_size=1))])
    client = JevConsoleDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="opencode-go"),
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    assert decision.model == "deepseek-v4.1-flash"
    assert decision.model_confidence == 1.0
    assert decision.model_choice == SINGLE_MODEL_CHOICE
    assert decision.model_probabilities == {}
    assert "model_route" not in transport.last_payload["questions"]


def test_a_volunteered_model_answer_is_ignored_on_a_one_model_grid(monkeypatch):
    """The question was not asked; an answer naming another id must not steer the route."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-key")
    settings = load_settings(
        lambda key, default=None: {
            "decider": "jev_console",
            "grids": {"opencode-go": ["deepseek-v4.1-flash: the usual choice"]},
        }.get(key, default)
    )
    payload = decision_payload(model_choice="1", model_confidence=0.95, grid_size=1)
    payload["answers"]["model_route"]["choice"] = "kimi-k3"
    payload["answers"]["model_route"]["probabilities"] = {"1": 1.0}
    transport = StubTransport([StubResponse(payload)])
    client = JevConsoleDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="opencode-go"),
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    assert decision.model == "deepseek-v4.1-flash"


def test_jev_console_two_model_grid_still_requires_and_reads_the_choice(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-key")
    settings = load_settings(
        lambda key, default=None: {
            "decider": "jev_console",
            "grids": {"ollama-cloud": ["deepseek-v4.1-flash: the usual choice", "kimi-k3: complex code"]},
        }.get(key, default)
    )
    transport = StubTransport([StubResponse(decision_payload(model_choice="2", model_confidence=0.95, grid_size=2))])
    client = JevConsoleDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="ollama-cloud"),
        provider="ollama-cloud",
    )

    assert reason is None
    assert decision is not None
    assert decision.model == "kimi-k3"
    assert decision.model_choice == "2"
    assert decision.model_confidence == 0.95
    assert "model_route" in transport.last_payload["questions"]


def test_jev_console_two_model_grid_without_a_model_answer_is_malformed(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-key")
    settings = load_settings(
        lambda key, default=None: {
            "decider": "jev_console",
            "console_jev_fallback_model": "",
            "grids": {"ollama-cloud": ["deepseek-v4.1-flash: the usual choice", "kimi-k3: complex code"]},
        }.get(key, default)
    )
    payload = decision_payload(model_choice="2", model_confidence=0.95, grid_size=2)
    payload["answers"].pop("model_route")
    transport = StubTransport([StubResponse(payload)])
    client = JevConsoleDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="ollama-cloud"),
        provider="ollama-cloud",
    )

    assert decision is None
    assert reason == REASON_MALFORMED


# -- router / audit ------------------------------------------------------------------


def test_the_route_record_says_the_model_was_fixed_by_the_grid(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-key")
    transport = StubTransport(
        [StubResponse(decision_payload(model_choice="1", model_confidence=0.95, grid_size=1))]
    )
    router, settings = build(
        tmp_path,
        transport,
        config={"decider": "jev_console", "routable_providers": ["opencode-console"]},
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = route(router, provider="opencode-console")

    assert result is not None
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["model_choice"] == "single"
    assert record["model_confidence"] == 1.0

    # An intra-turn replay re-emits the same "single" choice without consulting the decider.
    replay = route(router, provider="opencode-console", api_call_count=1)
    assert replay is not None
    assert transport.call_count == 1
    replay_record = router.tail(settings, limit=1)[0]
    assert replay_record["replayed"] is True
    assert replay_record["model_choice"] == "single"
    assert replay_record["model_confidence"] == 1.0


# -- openai_compatible ---------------------------------------------------------------


def _openai_settings(grid):
    return load_settings(
        lambda key, default=None: {
            "decider": "openai_compatible",
            "decider_base_url": "https://decider.test/v1",
            "decider_api_key_env": "TEST_DECIDER_KEY",
            "grids": {"opencode-go": grid},
        }.get(key, default)
    )


def _openai_body(body):
    return {"choices": [{"message": {"content": json.dumps(body)}}]}


def test_openai_compatible_one_model_grid_asks_only_for_effort(monkeypatch):
    monkeypatch.setenv("TEST_DECIDER_KEY", "test-key")
    settings = _openai_settings(["deepseek-v4.1-flash: the usual choice"])
    transport = StubTransport([StubResponse(_openai_body({"effort": "high", "confidence": 0.9}))])
    client = OpenAICompatibleDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="opencode-go"),
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    assert decision.model == "deepseek-v4.1-flash"
    assert decision.model_confidence == 1.0
    assert decision.model_choice == SINGLE_MODEL_CHOICE
    assert decision.model_probabilities == {}
    assert decision.effort == "high"
    assert decision.effort_confidence == 0.9

    system_prompt, user_prompt = [message["content"] for message in transport.last_payload["messages"]]
    assert '"model"' not in system_prompt
    assert "Available Models" not in user_prompt
    assert "Pick the effort." in user_prompt


def test_openai_compatible_two_model_grid_keeps_the_model_question(monkeypatch):
    monkeypatch.setenv("TEST_DECIDER_KEY", "test-key")
    settings = _openai_settings(
        ["deepseek-v4.1-flash: the usual choice", "kimi-k3: complex code"]
    )
    transport = StubTransport(
        [StubResponse(_openai_body({"model": "kimi-k3", "effort": "low", "confidence": 0.8}))]
    )
    client = OpenAICompatibleDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="opencode-go"),
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    assert decision.model == "kimi-k3"
    assert decision.model_choice == "kimi-k3"
    assert decision.model_confidence == 0.8
    system_prompt, user_prompt = [message["content"] for message in transport.last_payload["messages"]]
    assert '"model"' in system_prompt
    assert "Available Models" in user_prompt


def test_openai_compatible_one_model_grid_never_invents_a_model_answer(monkeypatch):
    """Only the effort is asked, so a body without ``model`` is well formed."""
    monkeypatch.setenv("TEST_DECIDER_KEY", "test-key")
    settings = _openai_settings(["deepseek-v4.1-flash: the usual choice"])
    transport = StubTransport([StubResponse(_openai_body({"effort": "medium", "confidence": 0.7}))])
    client = OpenAICompatibleDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="opencode-go"),
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    assert decision.model == "deepseek-v4.1-flash"
    assert decision.effort == "medium"


# -- legacy OpenRouter wire ----------------------------------------------------------


def test_the_legacy_jev_wire_still_asks_the_model_on_a_one_model_grid(monkeypatch):
    """The one-model rule scopes to the wires implemented here, not to a frozen contract.

    The legacy Decisions API payload is pinned byte-for-byte (see ``test_client``), so
    ``JevDecider`` passes ``ask_model=True`` explicitly: with a single model in the grid the
    model question stays in the payload. Dropping that argument has to fail here, or the frozen
    payload would drift silently.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    settings = load_settings(
        lambda key, default=None: {
            "decider": "jev",
            "grid": ["only-model: the only choice"],
        }.get(key, default)
    )
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, grid_size=1))])
    client = JevDecider(settings, transport=transport)

    decision, reason = client.decide(
        MESSAGES,
        settings.effective(),
        platform="cli",
        provider="ollama-cloud",
    )

    assert reason is None
    assert decision is not None
    assert decision.model == "only-model"
    assert MODEL_QUESTION_ID in transport.last_payload["questions"]
