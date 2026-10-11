"""Client wire contract: payload shape, answer interpretation, failure containment."""

from __future__ import annotations

import pytest

from client import (
    REASON_LOW_CONFIDENCE,
    REASON_MALFORMED,
    REASON_NO_API_KEY,
    REASON_TIMEOUT,
    REASON_UNKNOWN_CHOICE,
    REASON_UPSTREAM_ERROR,
    JevClient,
)
from stubs import ReadTimeout, StubResponse, StubTransport, decision_payload
from config import load_settings

MESSAGES = [
    {"role": "user", "content": "première question"},
    {"role": "assistant", "content": "première réponse"},
    {"role": "user", "content": "refactore le module de paiement"},
]


def make(transport, config=None):
    settings = load_settings(lambda key, default=None: (config or {}).get(key, default))
    return JevClient(settings, transport=transport), settings


def test_payload_matches_the_documented_shape(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload())])
    client, settings = make(transport)

    client.decide(MESSAGES, settings.effective(None), platform="cli", provider="ollama-cloud")

    payload = transport.last_payload
    assert payload["model"] == settings.jev_model == "typesafe/jev-1.13"
    assert transport.calls[0]["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer sk-or-test"

    state = payload["state"]
    assert state["user_message"] == "refactore le module de paiement"
    assert "première question" in state["recent_context"]
    assert state["surface"] == "cli"
    assert state["provider"] == "ollama-cloud"
    # The configured model is deliberately NOT sent: it anchors the decision on itself.
    assert "currently_configured_model" not in state

    questions = payload["questions"]
    assert questions["model_route"]["type"] == "choice"
    assert set(questions["model_route"]["criteria"]) == {"1", "2", "3", "4", "5", "6"}
    assert questions["model_route"]["criteria"]["1"].startswith("deepseek-v4.1-flash: ")
    assert questions["reasoning_effort"]["type"] == "choice"
    assert set(questions["reasoning_effort"]["criteria"]) == {"low", "medium", "high"}


def test_payload_is_stated_in_one_language(monkeypatch):
    """The whole payload must be English — instructions, criteria and effort levels alike.

    A half-translated payload is a half-measured one: the grid profiles, the two question
    instructions and the effort criteria all travel in the same request and are all weighed.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload())])
    client, settings = make(transport)

    client.decide(MESSAGES, settings.effective(None), platform="cli", provider="ollama-cloud")

    french = set("àâäçéèêëîïôöùûüœ")
    questions = transport.last_payload["questions"]
    for question in questions.values():
        strings = [question["instructions"], *question["criteria"].values()]
        for text in strings:
            offenders = sorted(french & set(text.lower()))
            assert not offenders, f"French text in the Jev payload: {offenders} in {text!r}"


def test_context_window_is_bounded(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload())])
    client, settings = make(transport, config={"context_turns": 1})
    long_messages = [
        {"role": "user", "content": f"tour {index} " + "x" * 5000} for index in range(6)
    ]

    client.decide(long_messages, settings.effective(None))

    state = transport.last_payload["state"]
    assert "tour 0" not in state["recent_context"]
    assert "tour 5" not in state["recent_context"]  # the last user turn is `user_message`
    assert len(state["recent_context"]) <= 12001
    assert len(state["user_message"]) <= 4001


def test_decision_interpretation(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("2", 0.91, "high", 0.66))])
    client, settings = make(transport)

    decision, reason = client.decide(MESSAGES, settings.effective(None))

    assert reason is None
    assert decision.model == "kimi-k3"
    assert decision.effort == "high"
    assert decision.model_confidence == pytest.approx(0.91)
    assert decision.effort_confidence == pytest.approx(0.66)
    assert decision.model_choice == "2"
    assert decision.latency_ms >= 0

    # The grid position is offered to Jev; `choice` returns that key, and `probabilities`
    # is the full distribution over the criteria we sent (docs/jev-decisions-api.md).
    assert set(decision.model_probabilities) == {"1", "2", "3", "4", "5", "6"}
    assert decision.model_probabilities["2"] == pytest.approx(0.91)


def test_kimi_effort_override_is_applied(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "medium", 0.9))])
    client, settings = make(transport)

    decision, _ = client.decide(MESSAGES, settings.effective(None))

    assert decision.effort_requested == "medium"
    assert decision.effort == "high"


def test_missing_api_key_short_circuits(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    transport = StubTransport([])
    client, settings = make(transport)

    decision, reason = client.decide(MESSAGES, settings.effective(None))

    assert decision is None
    assert reason == REASON_NO_API_KEY
    assert transport.call_count == 0


def test_timeout(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def handler(*_args, **_kwargs):
        raise ReadTimeout("slow")

    client, settings = make(StubTransport(handler=handler))

    decision, reason = client.decide(MESSAGES, settings.effective(None))
    assert decision is None
    assert reason == REASON_TIMEOUT


def test_upstream_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(status_code=500, text="boom")]))

    decision, reason = client.decide(MESSAGES, settings.effective(None))
    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR


def test_non_json_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(payload=None, status_code=200, text="<html>")]))

    decision, reason = client.decide(MESSAGES, settings.effective(None))
    assert decision is None
    assert reason == REASON_MALFORMED


def test_missing_answers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse({"answers": {}})]))

    decision, reason = client.decide(MESSAGES, settings.effective(None))
    assert decision is None
    assert reason == REASON_MALFORMED


def test_unknown_model_choice_degrades_to_the_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(decision_payload("99", 0.99))]))

    decision, reason = client.decide(MESSAGES, settings.effective(None))

    assert reason is None
    assert decision.model == "deepseek-v4.1-flash"
    assert REASON_UNKNOWN_CHOICE in decision.fallback_reasons


def test_low_confidence_degrades(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(decision_payload("2", 0.2, "high", 0.2))]))

    decision, _ = client.decide(MESSAGES, settings.effective(None))

    assert decision.model == "deepseek-v4.1-flash"
    assert decision.effort == "medium"
    assert decision.fallback_reasons.count(REASON_LOW_CONFIDENCE) == 2


def test_a_confident_model_with_an_unconfident_effort_only_degrades_the_effort(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(decision_payload("2", 0.95, "high", 0.10))]))

    decision, _ = client.decide(MESSAGES, settings.effective(None))

    assert decision.model == "kimi-k3"
    # Degraded to the configured default ("medium"), then translated for the model family:
    # Kimi K3's documented middle is `high`, so that is what goes on the wire.
    assert decision.effort_requested == "medium"
    assert decision.effort == "high"
    assert decision.fallback_reasons == (REASON_LOW_CONFIDENCE,)


def test_non_choice_answer_types_are_treated_as_malformed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(
        StubTransport(
            [StubResponse(decision_payload("1", 0.9, "medium", 0.9, model_type="noul", effort_type="noul"))]
        )
    )

    decision, _ = client.decide(MESSAGES, settings.effective(None))

    # `choice` is empty on a wrongly-typed answer, so the model is off-grid and the turn
    # falls back rather than reading a probability as a model name.
    assert decision.model == "deepseek-v4.1-flash"
    assert REASON_UNKNOWN_CHOICE in decision.fallback_reasons


def test_custom_endpoint_is_honoured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload())])
    client, settings = make(transport, config={"endpoint": "https://example.invalid/decisions"})

    client.decide(MESSAGES, settings.effective(None))

    assert transport.calls[0]["url"] == "https://example.invalid/decisions"


# -- a surface policy drives the decision path -------------------------------------
#
# The snapshot the router builds once per request reaches the client in place of the grid:
# the grid offered to Jev, both threshold reads, the fallback model and the fallback effort
# all come from it, and `max_effort` caps the level the router emits before the per-family
# table resolves it.


def _policy_config(**policy):
    return {"platform_overrides": {"cron": policy}}


def test_a_policy_grid_is_the_criteria_sent_to_jev(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, "medium", 0.9, grid_size=1))])
    client, settings = make(transport, config=_policy_config(grid="glm-5.3: rigorous reasoning"))

    effective = settings.effective(settings.policy_for("cron"))
    client.decide(MESSAGES, effective, platform="cron", provider="ollama-cloud")

    criteria = transport.last_payload["questions"]["model_route"]["criteria"]
    assert criteria == {"1": "glm-5.3: rigorous reasoning"}


def test_the_policy_threshold_degrades_below_it(monkeypatch):
    """A one-entry policy grid and a confidence under the policy threshold degrade the model."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("1", 0.2, "medium", 0.9, grid_size=1))])
    client, settings = make(
        transport,
        config=_policy_config(grid="glm-5.3: rigorous reasoning", confidence_threshold=0.7),
    )

    effective = settings.effective(settings.policy_for("cron"))
    decision, reason = client.decide(MESSAGES, effective, platform="cron")

    assert reason is None
    # The single-entry grid is the fallback grid too, so the degraded answer stays inside it.
    assert decision.model == "glm-5.3"
    assert REASON_LOW_CONFIDENCE in decision.fallback_reasons


def test_an_off_grid_answer_degrades_to_the_policy_grid(monkeypatch):
    """A one-entry policy grid with Jev answering an off-grid position falls back inside it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "medium", 0.9, grid_size=1))])
    client, settings = make(transport, config=_policy_config(grid="glm-5.3: rigorous reasoning"))

    effective = settings.effective(settings.policy_for("cron"))
    decision, _ = client.decide(MESSAGES, effective, platform="cron")

    assert decision.model == "glm-5.3"
    assert REASON_UNKNOWN_CHOICE in decision.fallback_reasons


def test_a_cap_lowers_a_high_answer_and_leaves_low_and_medium_untouched(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    cases = [("low", "low"), ("medium", "medium"), ("high", "medium")]
    for answer, expected in cases:
        transport = StubTransport([StubResponse(decision_payload("1", 0.9, answer, 0.9))])
        client, settings = make(transport, config=_policy_config(max_effort="medium"))

        effective = settings.effective(settings.policy_for("cron"))
        decision, _ = client.decide(MESSAGES, effective, platform="cron")

        assert decision.model == "deepseek-v4.1-flash"
        assert decision.effort_requested == expected, answer
        # DeepSeek V4.1 accepts the three levels, so the emitted value is the requested one.
        assert decision.effort == expected


def test_a_cap_lowers_the_default_effort_fallback_too(monkeypatch):
    """`max_effort` caps the fallback path, not only Jev's answer."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, "high", 0.10))])
    client, settings = make(
        transport, config={**_policy_config(max_effort="low"), "default_effort": "high"}
    )

    effective = settings.effective(settings.policy_for("cron"))
    decision, _ = client.decide(MESSAGES, effective, platform="cron")

    assert decision.effort_requested == "low"
    assert decision.effort == "low"
    assert REASON_LOW_CONFIDENCE in decision.fallback_reasons


def test_a_cap_never_raises_a_confident_low_answer(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, "low", 0.9))])
    client, settings = make(transport, config=_policy_config(max_effort="medium"))

    effective = settings.effective(settings.policy_for("cron"))
    decision, _ = client.decide(MESSAGES, effective, platform="cron")

    assert decision.effort_requested == "low"
    assert decision.effort == "low"


def test_an_absent_effort_answer_is_omitted_without_a_cap(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, include_effort=False))])
    client, settings = make(transport)

    decision, _ = client.decide(MESSAGES, settings.effective(None))

    assert decision.effort is None
    assert decision.effort_requested is None


def test_an_absent_effort_answer_emits_the_cap_when_one_is_in_force(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, include_effort=False))])
    client, settings = make(transport, config=_policy_config(max_effort="medium"))

    effective = settings.effective(settings.policy_for("cron"))
    decision, _ = client.decide(MESSAGES, effective, platform="cron")

    assert decision.effort_requested == "medium"
    assert decision.effort == "medium"


def test_a_policy_grid_resolves_the_fallback_inside_it_when_the_default_is_off_grid(monkeypatch):
    """A policy surface never emits the off-grid global default; it re-pins inside its grid."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("99", 0.99, "medium", 0.9, grid_size=1))])
    client, settings = make(
        transport,
        config={
            **_policy_config(grid="glm-5.3: rigorous reasoning"),
            "default_model": "deepseek-v4.1-flash",  # absent from the one-entry cron grid
        },
    )

    effective = settings.effective(settings.policy_for("cron"))
    assert effective.grid_from_policy is True
    decision, _ = client.decide(MESSAGES, effective, platform="cron")

    assert decision.model == "glm-5.3"
    assert REASON_UNKNOWN_CHOICE in decision.fallback_reasons


def test_a_snapshot_without_a_policy_grid_emits_the_off_grid_default_verbatim(monkeypatch):
    """Review finding 1: the off-grid re-pin is a policy-surface behaviour only.

    An operator whose global grid excludes the configured ``default_model`` and who never sets
    ``platform_overrides`` must keep that configured model on the wire, exactly as before this
    change — the global grid is not a policy grid.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("99", 0.99, "medium", 0.9, grid_size=1))])
    client, settings = make(
        transport,
        config={
            "grid": "kimi-k3: code",  # a global grid that excludes the configured default
            "default_model": "deepseek-v4.1-flash",
        },
    )

    effective = settings.effective(None)
    assert effective.grid_from_policy is False
    decision, _ = client.decide(MESSAGES, effective)

    assert decision.model == "deepseek-v4.1-flash"
    assert decision.model != "kimi-k3"
