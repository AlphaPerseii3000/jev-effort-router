"""Client wire contract: payload shape, answer interpretation, failure containment."""

from __future__ import annotations

import hashlib
import json

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

    client.decide(MESSAGES, settings.effective(), platform="cli", provider="ollama-cloud")

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


def test_default_jev_payload_is_frozen(monkeypatch):
    """The legacy Jev request body must not drift when the decider becomes pluggable.

    The hash pins the wire contract of this build. It moved once, deliberately, with the
    four-step rubric and the route-scoped effort levels; any further move is drift and has to be
    justified by a decision, not by a refactor.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload())])
    client, settings = make(transport)

    client.decide(MESSAGES, settings.effective(), platform="cli", provider="ollama-cloud")

    canonical = json.dumps(
        transport.last_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == (
        "3e67ac642c4ee5183cf26b321e056c46b3a4a5371f21cc82bd57deaf44bbe62e"
    )


def test_payload_is_stated_in_one_language(monkeypatch):
    """The whole payload must be English — instructions, criteria and effort levels alike.

    A half-translated payload is a half-measured one: the grid profiles, the two question
    instructions and the effort criteria all travel in the same request and are all weighed.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload())])
    client, settings = make(transport)

    client.decide(MESSAGES, settings.effective(), platform="cli", provider="ollama-cloud")

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

    client.decide(long_messages, settings.effective())

    state = transport.last_payload["state"]
    assert "tour 0" not in state["recent_context"]
    assert "tour 5" not in state["recent_context"]  # the last user turn is `user_message`
    assert len(state["recent_context"]) <= 12001
    assert len(state["user_message"]) <= 4001


def test_decision_interpretation(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("2", 0.91, "high", 0.66))])
    client, settings = make(transport)

    decision, reason = client.decide(MESSAGES, settings.effective())

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

    decision, _ = client.decide(MESSAGES, settings.effective())

    assert decision.effort_requested == "medium"
    assert decision.effort == "high"


def test_missing_api_key_short_circuits(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    transport = StubTransport([])
    client, settings = make(transport)

    decision, reason = client.decide(MESSAGES, settings.effective())

    assert decision is None
    assert reason == REASON_NO_API_KEY
    assert transport.call_count == 0


def test_timeout(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def handler(*_args, **_kwargs):
        raise ReadTimeout("slow")

    client, settings = make(StubTransport(handler=handler))

    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_TIMEOUT


def test_upstream_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(status_code=500, text="boom")]))

    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR


def test_non_json_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(payload=None, status_code=200, text="<html>")]))

    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_MALFORMED


def test_missing_answers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse({"answers": {}})]))

    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_MALFORMED


def test_unknown_model_choice_degrades_to_the_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(decision_payload("99", 0.99))]))

    decision, reason = client.decide(MESSAGES, settings.effective())

    assert reason is None
    assert decision.model == "deepseek-v4.1-flash"
    assert REASON_UNKNOWN_CHOICE in decision.fallback_reasons


def test_low_confidence_degrades(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(decision_payload("2", 0.2, "high", 0.2))]))

    decision, _ = client.decide(MESSAGES, settings.effective())

    assert decision.model == "deepseek-v4.1-flash"
    assert decision.effort == "medium"
    assert decision.fallback_reasons.count(REASON_LOW_CONFIDENCE) == 2


def test_a_confident_model_with_an_unconfident_effort_only_degrades_the_effort(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    client, settings = make(StubTransport([StubResponse(decision_payload("2", 0.95, "high", 0.10))]))

    decision, _ = client.decide(MESSAGES, settings.effective())

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

    decision, _ = client.decide(MESSAGES, settings.effective())

    # `choice` is empty on a wrongly-typed answer, so the model is off-grid and the turn
    # falls back rather than reading a probability as a model name.
    assert decision.model == "deepseek-v4.1-flash"
    assert REASON_UNKNOWN_CHOICE in decision.fallback_reasons


def test_custom_endpoint_is_honoured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload())])
    client, settings = make(transport, config={"endpoint": "https://example.invalid/decisions"})

    client.decide(MESSAGES, settings.effective())

    assert transport.calls[0]["url"] == "https://example.invalid/decisions"


def test_an_absent_effort_answer_is_omitted(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, include_effort=False))])
    client, settings = make(transport)

    decision, _ = client.decide(MESSAGES, settings.effective())

    assert decision.effort is None
    assert decision.effort_requested is None


def test_an_off_grid_default_model_is_emitted_verbatim(monkeypatch):
    """The operator's global grid is a catalog, never a cap on ``default_model``.

    An operator whose grid excludes the configured ``default_model`` must keep that configured
    model on the wire when the decision degrades: the grid only bounds what Jev is *offered*.
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

    effective = settings.effective()
    decision, _ = client.decide(MESSAGES, effective)

    assert decision.model == "deepseek-v4.1-flash"
    assert decision.model != "kimi-k3"


# -- openai_compatible backend tests ------------------------------------------


def test_openai_compatible_decider_happy_path(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-opencode-secret-123")
    resp_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "model": "deepseek-v4.1-flash",
                            "effort": "high",
                            "confidence": 0.95,
                        }
                    )
                }
            }
        ]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_model": "deepseek-v4.1-flash",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "routable_providers": ["opencode-go"],
        },
    )
    effective = settings.effective()
    decision, reason = client.decide(MESSAGES, effective, provider="opencode-go")

    assert reason is None
    assert decision is not None
    assert decision.model == "deepseek-v4.1-flash"
    assert decision.effort_requested == "high"
    assert decision.model_confidence == 0.95
    assert decision.decider == "openai_compatible"

    # Verify wire request
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-opencode-secret-123"
    assert call["json"]["model"] == "deepseek-v4.1-flash"
    assert call["json"]["response_format"] == {"type": "json_object"}


def test_openai_compatible_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    transport = StubTransport([])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_NO_API_KEY
    assert transport.call_count == 0


def test_openai_compatible_timeout(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    transport = StubTransport([ReadTimeout("timed out")])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_TIMEOUT


def test_openai_compatible_upstream_error(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    transport = StubTransport([StubResponse(status_code=500, text="Internal Error")])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR


def test_openai_compatible_malformed_json_content(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    resp_payload = {"choices": [{"message": {"content": "not-valid-json"}}]}
    transport = StubTransport([StubResponse(resp_payload)])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_MALFORMED


def test_openai_compatible_markdown_json_fence_handled(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    fenced = "```json\n{\"model\": \"kimi-k3\", \"effort\": \"medium\", \"confidence\": 0.88}\n```"
    resp_payload = {"choices": [{"message": {"content": fenced}}]}
    transport = StubTransport([StubResponse(resp_payload)])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert reason is None
    assert decision is not None
    assert decision.model == "kimi-k3"


def test_openai_compatible_off_grid_model_fails_open(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    resp_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"model": "non-existent-model", "effort": "high", "confidence": 0.99}
                    )
                }
            }
        ]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "default_model": "deepseek-v4.1-flash",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_UNKNOWN_CHOICE


def test_openai_compatible_low_confidence_fails_open(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    resp_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"model": "kimi-k3", "effort": "high", "confidence": 0.2}
                    )
                }
            }
        ]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    client, settings = make(
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "confidence_threshold": 0.5,
            "default_model": "deepseek-v4.1-flash",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_LOW_CONFIDENCE


def test_openai_compatible_invalid_confidence_fails_open(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    for bad_conf in ("garbage", -0.1, 1.5, None):
        resp_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"model": "kimi-k3", "effort": "high", "confidence": bad_conf}
                        )
                    }
                }
            ]
        }
        transport = StubTransport([StubResponse(resp_payload)])
        client, settings = make(
            transport,
            config={
                "decider": "openai_compatible",
                "decider_base_url": "https://opencode.ai/zen/go/v1",
                "decider_api_key_env": "OPENCODE_GO_API_KEY",
            },
        )
        decision, reason = client.decide(MESSAGES, settings.effective())
        assert decision is None
        assert reason == REASON_MALFORMED


# -- jev_console backend tests ------------------------------------------------

def test_jev_console_decider_happy_path(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    resp_payload = decision_payload(model_choice="1", model_confidence=0.9, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "confidence_threshold": 0.5,
        },
    )
    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="opencode-console"),
        provider="opencode-console",
    )
    assert reason is None
    assert decision is not None
    assert decision.decider == "jev_console"
    assert decision.model == "deepseek-v4.1-flash"
    # One model on the grid is not a choice: the payload carries no model question, the model is
    # the only option, and the audit says so instead of echoing a key nobody asked for.
    assert decision.model_confidence == 1.0
    assert decision.model_choice == "single"
    assert decision.model_probabilities == {}
    assert decision.decider_model == "jev-1.13-free"
    assert decision.decider_fallback_reason is None

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://opencode.ai/inference/systemone/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer test-console-key-123"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["json"]["model"] == "jev-1.13-free"
    assert "state" in call["json"]
    questions = call["json"]["questions"]
    assert "model_route" not in questions
    assert questions["reasoning_effort"]["type"] == "choice"


def test_jev_console_decider_fallback_to_paid_jev(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    # First call (free jev) fails with 500; second call (paid jev) succeeds with 200
    resp_payload = decision_payload(model_choice="1", model_confidence=0.88, grid_size=1)
    transport = StubTransport([
        StubResponse({"error": "free quota exhausted"}, status_code=500),
        StubResponse(resp_payload, status_code=200),
    ])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
        },
    )
    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="opencode-console"),
        provider="opencode-console",
    )
    assert reason is None
    assert decision is not None
    assert decision.model == "deepseek-v4.1-flash"
    assert len(transport.calls) == 2
    assert transport.calls[0]["json"]["model"] == "jev-1.13-free"
    assert transport.calls[1]["json"]["model"] == "jev-1.13"
    # Provenance: the fallback answered, and the decision says so instead of claiming the primary.
    assert decision.decider_model == "jev-1.13"
    assert decision.decider_fallback_reason == "upstream_error"


def test_jev_console_decider_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONSOLE_API_KEY", raising=False)
    transport = StubTransport([])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_NO_API_KEY
    assert len(transport.calls) == 0


def test_jev_console_decider_both_models_fail(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    transport = StubTransport([
        StubResponse({"error": "server error 1"}, status_code=500),
        StubResponse({"error": "server error 2"}, status_code=502),
    ])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR
    assert len(transport.calls) == 2


def test_jev_console_decider_malformed_json(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    transport = StubTransport([StubResponse({"invalid": "structure"})])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "console_jev_fallback_model": "",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_MALFORMED


def test_jev_console_decider_low_confidence_degrades(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    # A multi-model grid is where the model question exists at all — with one model the model
    # confidence is 1.0 by construction and the threshold can only bite on the effort answer.
    resp_payload = decision_payload(model_choice="2", model_confidence=0.2, grid_size=6)
    transport = StubTransport([StubResponse(resp_payload)])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "confidence_threshold": 0.5,
            "console_jev_fallback_model": "",
        },
    )
    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="ollama-cloud"),
        provider="ollama-cloud",
    )
    assert decision is None
    assert reason == REASON_LOW_CONFIDENCE


def test_jev_console_decider_fallback_model(monkeypatch):
    # Explicit alias matching EVIDENCIAS_OPERACIONAIS.md
    return test_jev_console_decider_fallback_to_paid_jev(monkeypatch)


def test_jev_console_decider_network_failure_fail_open(monkeypatch):
    # Explicit alias matching EVIDENCIAS_OPERACIONAIS.md
    return test_jev_console_decider_both_models_fail(monkeypatch)


def test_jev_console_decider_timeout(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    transport = StubTransport([ReadTimeout("connection timeout")])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "console_jev_fallback_model": "",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_TIMEOUT


@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
def test_jev_console_decider_http_error_matrix(monkeypatch, status_code, caplog):
    key = "test-console-key-secret-999"
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", key)
    transport = StubTransport([StubResponse({"error": f"error with {key}"}, status_code=status_code)])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "console_jev_fallback_model": "",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR
    # Verify secret is never logged in plaintext
    assert key not in caplog.text


def test_jev_console_decider_real_non_json_body(monkeypatch, caplog):
    key = "test-console-key-secret-888"
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", key)
    transport = StubTransport([StubResponse(payload=None, status_code=200, text=f"<html>Bad Gateway {key}</html>")])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "console_jev_fallback_model": "",
        },
    )
    decision, reason = client.decide(MESSAGES, settings.effective())
    assert decision is None
    assert reason == REASON_MALFORMED
    # Verify secret is redacted
    assert key not in caplog.text


def test_jev_console_decider_response_defined_probability_domain(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    payload = decision_payload("2", 0.99, "high", 0.8, grid_size=2)
    answer = payload["answers"]["model_route"]
    answer["criteria"] = {"outsider": "unrequested option"}
    answer["probabilities"] = {"outsider": 1.0}
    transport = StubTransport([StubResponse(payload)])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "console_jev_fallback_model": "",
            "grids": {"ollama-cloud": ["deepseek-v4.1-flash: routine", "kimi-k3: code"]},
        },
    )
    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider="ollama-cloud"),
        provider="ollama-cloud",
    )
    assert decision is None
    assert reason == REASON_MALFORMED


def test_jev_console_decider_invalid_types_and_float_choice(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-123")
    # The model question only exists on a multi-model grid.
    # Float choice
    payload_float = decision_payload("1", 0.99, "high", 0.8, grid_size=6)
    payload_float["answers"]["model_route"]["choice"] = 1.0
    # Non-choice type
    payload_bad_type = decision_payload("1", 0.99, "high", 0.8, grid_size=6)
    payload_bad_type["answers"]["model_route"]["type"] = "free_text"
    # Numeric effort choice
    payload_bad_effort = decision_payload("1", 0.99, "high", 0.8, grid_size=6)
    payload_bad_effort["answers"]["reasoning_effort"]["choice"] = 3
    payload_bad_effort["answers"]["reasoning_effort"]["probabilities"] = {"3": 1.0}

    transport = StubTransport([
        StubResponse(payload_float),
        StubResponse(payload_bad_type),
        StubResponse(payload_bad_effort),
    ])
    client, settings = make(
        transport,
        config={
            "decider": "jev_console",
            "console_jev_fallback_model": "",
        },
    )
    # 1: float choice -> unknown choice
    dec1, r1 = client.decide(MESSAGES, settings.effective(provider="ollama-cloud"), provider="ollama-cloud")
    assert dec1 is None
    assert r1 == REASON_UNKNOWN_CHOICE

    # 2: bad type -> malformed
    dec2, r2 = client.decide(MESSAGES, settings.effective(provider="ollama-cloud"), provider="ollama-cloud")
    assert dec2 is None
    assert r2 == REASON_MALFORMED

    # 3: bad effort -> unknown choice
    dec3, r3 = client.decide(MESSAGES, settings.effective(provider="ollama-cloud"), provider="ollama-cloud")
    assert dec3 is None
    assert r3 == REASON_UNKNOWN_CHOICE

