"""The opt-in CommandCode SystemOne fallback: when it is asked, in what order, and how it fails.

The fallback is the *last* transport of the ``jev_console`` decider. These tests pin the four rules
that make it safe to enable:

* it is only consulted after the console chain fails **technically** — never because a valid answer
  was below the threshold or named an option the grid does not carry;
* it is one request, with the same per-call timeout, never a retry loop;
* its credential, its headers and its payload are its own: nothing from the generation provider is
  inherited and no secret reaches a log line;
* the URL is the whole opt-in: an empty one disables it silently, while the model and the key
  variable complete themselves with the documented pair once the endpoint is named. A malformed
  endpoint disables it and leaves the legacy chain exactly as it was.
"""

from __future__ import annotations

import logging

import pytest

import decider as decider_module
from client import JevClient
from config import load_settings
from decider import (
    PROHIBITED_DECIDER_HEADERS,
    REASON_LOW_CONFIDENCE,
    REASON_MALFORMED,
    REASON_NO_API_KEY,
    REASON_TIMEOUT,
    REASON_UNKNOWN_CHOICE,
    REASON_UPSTREAM_ERROR,
)
from state import build_payload
from stubs import ReadTimeout, StubResponse, StubTransport, decision_payload

CONSOLE_URL = "https://opencode.ai/inference/systemone/v1/systemone"
CONSOLE_PRIMARY = "jev-1.13-free"
CONSOLE_PAID = "jev-1.13"
CC_URL = "https://api.commandcode.ai/provider/v1/systemone"
CC_MODEL = "typesafe/jev"
CONSOLE_KEY = "console-key-secret-777"
CC_KEY = "cc-key-secret-888"

MESSAGES = [
    {"role": "user", "content": "première question"},
    {"role": "assistant", "content": "première réponse"},
    {"role": "user", "content": "refactore le module de paiement"},
]
PROVIDER = "opencode-console"


def cc_config(**overrides):
    """A `jev_console` deployment with the fallback fully configured and the paid leg off.

    The paid console leg is disabled by default so a test that scripts one console failure gets
    exactly one console call; the ordering test turns it back on explicitly.
    """
    config = {
        "decider": "jev_console",
        "routable_providers": [PROVIDER],
        "console_jev_fallback_model": "",
        "console_jev_commandcode_base_url": CC_URL,
        "console_jev_commandcode_api_key_env": "COMMANDCODE_API_KEY",
        "console_jev_commandcode_model": CC_MODEL,
    }
    config.update(overrides)
    return config


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", CONSOLE_KEY)
    monkeypatch.setenv("COMMANDCODE_API_KEY", CC_KEY)


def make(transport, config):
    settings = load_settings(lambda key, default=None: config.get(key, default))
    return JevClient(settings, transport=transport), settings


def ask(client, settings):
    return client.decide(
        MESSAGES,
        settings.effective(provider=PROVIDER),
        provider=PROVIDER,
    )


def console_answer(choice: str = "1", confidence: float = 0.95):
    return decision_payload(choice, confidence, "medium", 0.9, grid_size=1)


def console_weak_effort_answer():
    """A confident model line and a weak *effort* line.

    The console route's grid carries one model, so the model question is not asked (the model
    answer is the grid's by construction) and the only dimension an answer can be unusable on is
    the effort. A ``confidence`` lowered on the model line alone would therefore still be a valid
    decision here.
    """
    return decision_payload("1", 0.95, "medium", 0.2, grid_size=1)


def console_unknown_effort_answer():
    """An answer whose *effort* names a level outside the vocabulary, with valid probabilities."""
    return decision_payload(
        "1", 0.95, "bogus", 0.9, effort_probabilities={"low": 1.0}, grid_size=1
    )


class _StepClock:
    """A monotonic clock that advances a fixed amount per read, so latency is exact."""

    def __init__(self, step_ms: int = 5) -> None:
        self._now = 0.0
        self._step = step_ms / 1000.0

    def monotonic(self) -> float:
        self._now += self._step
        return self._now

    @staticmethod
    def time() -> float:
        return 1_700_000_000.0


# -- the happy path of the fallback ------------------------------------------------


def test_a_console_http_failure_hands_the_turn_to_the_fallback():
    transport = StubTransport(
        [
            StubResponse({"error": "rate limited"}, status_code=429),
            StubResponse(console_answer()),
        ]
    )
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert reason is None
    assert decision is not None
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CC_URL]
    assert [call["json"]["model"] for call in transport.calls] == [CONSOLE_PRIMARY, CC_MODEL]
    # Provenance: the fallback answered, the reason says why it had to.
    assert decision.decider_model == CC_MODEL
    assert decision.decider_endpoint == CC_URL
    assert decision.decider_fallback_reason == REASON_UPSTREAM_ERROR
    assert decision.decider == "jev_console"


def test_the_chain_order_is_free_paid_then_fallback():
    transport = StubTransport(
        [
            StubResponse({"error": "free quota"}, status_code=500),
            StubResponse({"error": "paid quota"}, status_code=502),
            StubResponse(console_answer()),
        ]
    )
    client, settings = make(transport, cc_config(console_jev_fallback_model=CONSOLE_PAID))

    decision, reason = ask(client, settings)

    assert reason is None
    assert [call["json"]["model"] for call in transport.calls] == [CONSOLE_PRIMARY, CONSOLE_PAID, CC_MODEL]
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CONSOLE_URL, CC_URL]
    assert decision.decider_model == CC_MODEL
    assert decision.decider_fallback_reason == REASON_UPSTREAM_ERROR


def test_a_primary_answer_never_consults_the_fallback():
    transport = StubTransport([StubResponse(console_answer())])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert reason is None
    assert transport.call_count == 1
    assert decision.decider_model == CONSOLE_PRIMARY
    assert decision.decider_endpoint == CONSOLE_URL
    assert decision.decider_fallback_reason is None


def test_the_fallback_payload_is_the_systemone_payload_with_only_the_model_changed():
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), StubResponse(console_answer())])
    client, settings = make(transport, cc_config())

    assert ask(client, settings)[0] is not None

    effective = settings.effective(provider=PROVIDER)
    expected = build_payload(
        MESSAGES,
        effective.grid,
        jev_model=CC_MODEL,
        context_turns=settings.context_turns,
        platform="",
        provider=PROVIDER,
    )
    assert transport.calls[1]["json"] == expected
    assert transport.calls[0]["json"] == build_payload(
        MESSAGES,
        effective.grid,
        jev_model=CONSOLE_PRIMARY,
        context_turns=settings.context_turns,
        platform="",
        provider=PROVIDER,
    )


def test_the_fallback_headers_are_its_own():
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), StubResponse(console_answer())])
    client, settings = make(transport, cc_config())

    assert ask(client, settings)[0] is not None

    headers = transport.calls[1]["headers"]
    assert headers == {
        "Authorization": f"Bearer {CC_KEY}",
        "Content-Type": "application/json",
        "X-Title": "hermes-jev-effort-router",
    }
    # No inherited user agent and no chat-only marker, whatever the operator's generation route sets.
    assert not any(name.lower() == "x-cmd-zdr" for name in headers)
    assert not any(name.lower() == "user-agent" for name in headers)
    # The primary's credential never travels to the second host.
    assert CONSOLE_KEY not in str(headers)
    # Every call keeps the one configured budget: no per-attempt increase, no retry.
    assert [call["timeout"] for call in transport.calls] == [settings.timeout_s, settings.timeout_s]


def test_the_prohibited_header_set_covers_x_cmd_zdr_case_insensitively(monkeypatch):
    assert "x-cmd-zdr" in PROHIBITED_DECIDER_HEADERS
    # The filter is what keeps a future merged header dict honest, so exercise it directly.
    monkeypatch.setattr(decider_module, "PROHIBITED_DECIDER_HEADERS", frozenset({"x-title"}))
    headers = decider_module._secondary_headers("k")
    assert "X-Title" not in headers
    assert headers["Authorization"] == "Bearer k"


# -- what must NOT trigger the fallback ---------------------------------------------


@pytest.mark.parametrize(
    "answer, expected_reason",
    [
        (console_weak_effort_answer(), REASON_LOW_CONFIDENCE),
        (console_unknown_effort_answer(), REASON_UNKNOWN_CHOICE),
    ],
)
def test_a_valid_but_unusable_console_answer_does_not_consult_the_fallback(answer, expected_reason):
    transport = StubTransport([StubResponse(answer)])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == expected_reason
    assert transport.call_count == 1
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL]


# -- credentials --------------------------------------------------------------------


def test_a_missing_fallback_key_sends_nothing_and_keeps_the_console_reason(monkeypatch, caplog):
    monkeypatch.delenv("COMMANDCODE_API_KEY")
    transport = StubTransport([StubResponse({"error": "rate limited"}, status_code=429)])
    client, settings = make(transport, cc_config())

    with caplog.at_level(logging.INFO):
        decision, reason = ask(client, settings)

    assert decision is None
    # Not "no_api_key": that would blame the fallback's configuration for the console's 429.
    assert reason == REASON_UPSTREAM_ERROR
    assert transport.call_count == 1
    assert "COMMANDCODE_API_KEY" in caplog.text
    assert CC_KEY not in caplog.text


def test_both_credentials_missing_is_the_legacy_no_api_key_answer(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONSOLE_API_KEY")
    monkeypatch.delenv("COMMANDCODE_API_KEY")
    transport = StubTransport([])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_NO_API_KEY
    assert transport.call_count == 0


def test_without_the_console_key_the_fallback_is_the_only_transport(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONSOLE_API_KEY")
    transport = StubTransport([StubResponse(console_answer())])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert reason is None
    assert decision is not None
    assert [call["url"] for call in transport.calls] == [CC_URL]
    assert decision.decider_model == CC_MODEL
    # The chain's last reason before the successful attempt is the missing console key.
    assert decision.decider_fallback_reason == REASON_NO_API_KEY


def test_no_credential_reaches_the_logs_or_an_exception_message(caplog):
    transport = StubTransport(
        [
            StubResponse({"error": "rate limited"}, status_code=429),
            StubResponse({"error": f"invalid key {CC_KEY}"}, status_code=403),
        ]
    )
    client, settings = make(transport, cc_config())

    with caplog.at_level(logging.DEBUG):
        decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR
    assert CC_KEY not in caplog.text
    assert CONSOLE_KEY not in caplog.text
    assert "[REDACTED]" in caplog.text


# -- failure classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "primary_failure, expected_fallback_reason",
    [
        (StubResponse({"error": "free quota"}, status_code=400), REASON_UPSTREAM_ERROR),
        (StubResponse({"error": "free quota"}, status_code=429), REASON_UPSTREAM_ERROR),
        (StubResponse({"error": "free quota"}, status_code=500), REASON_UPSTREAM_ERROR),
        (ReadTimeout("timed out"), REASON_TIMEOUT),
        (RuntimeError("connection reset"), REASON_UPSTREAM_ERROR),
        # No response object at all: the host failed technically, with no reason of its own.
        (None, REASON_UPSTREAM_ERROR),
        (StubResponse(None, status_code=200, text="<html>gateway</html>"), REASON_MALFORMED),
        (StubResponse({"invalid": "structure"}), REASON_MALFORMED),
    ],
    ids=["http-400", "http-429", "http-500", "timeout", "transport", "no-response", "non-json", "wire"],
)
def test_every_technical_console_failure_hands_the_turn_to_the_fallback(
    primary_failure, expected_fallback_reason
):
    transport = StubTransport([primary_failure, StubResponse(console_answer())])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert reason is None
    assert decision is not None
    assert decision.decider_model == CC_MODEL
    assert decision.decider_endpoint == CC_URL
    assert decision.decider_fallback_reason == expected_fallback_reason
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CC_URL]
    assert [call["json"]["model"] for call in transport.calls] == [CONSOLE_PRIMARY, CC_MODEL]
    # Both queued answers were consumed: the rescue really happened on the second host.
    assert transport.responses == []


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422, 429, 500, 503])
def test_every_http_failure_of_the_fallback_is_an_upstream_error(status_code):
    transport = StubTransport(
        [
            StubResponse({"error": "free quota"}, status_code=429),
            StubResponse({"error": "no"}, status_code=status_code),
        ]
    )
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR
    # The status under test came from the second host, not from the console leg.
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CC_URL]
    assert [call["json"]["model"] for call in transport.calls] == [CONSOLE_PRIMARY, CC_MODEL]
    assert transport.responses == []


def test_a_fallback_timeout_is_a_timeout():
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), ReadTimeout("timed out")])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_TIMEOUT


def test_a_fallback_non_json_body_is_malformed():
    transport = StubTransport(
        [
            StubResponse({"error": "quota"}, status_code=429),
            StubResponse(None, status_code=200, text="<html>gateway</html>"),
        ]
    )
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_MALFORMED


def test_a_fallback_wire_failure_is_malformed():
    transport = StubTransport(
        [StubResponse({"error": "quota"}, status_code=429), StubResponse({"invalid": "structure"})]
    )
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_MALFORMED


@pytest.mark.parametrize(
    "answer, expected_reason",
    [
        (console_weak_effort_answer(), REASON_LOW_CONFIDENCE),
        (console_unknown_effort_answer(), REASON_UNKNOWN_CHOICE),
    ],
)
def test_a_valid_but_unroutable_fallback_answer_keeps_the_semantic_reason(
    answer, expected_reason
):
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), StubResponse(answer)])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == expected_reason
    # The semantic answer was produced by the fallback, after a real console failure.
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CC_URL]
    assert [call["json"]["model"] for call in transport.calls] == [CONSOLE_PRIMARY, CC_MODEL]
    assert transport.responses == []


# -- the last console attempt is what decides ---------------------------------------


def test_a_missing_paid_answer_still_hands_the_turn_to_the_fallback():
    # A missing response has no reason of its own, so the chain's last *reason* stays semantic —
    # but the paid attempt failed technically, and the contract requires asking the fallback.
    transport = StubTransport(
        [
            StubResponse(console_weak_effort_answer()),
            None,
            StubResponse(console_answer()),
        ]
    )
    client, settings = make(transport, cc_config(console_jev_fallback_model=CONSOLE_PAID))

    decision, reason = ask(client, settings)

    assert reason is None
    assert decision is not None
    assert decision.decider_model == CC_MODEL
    assert decision.decider_endpoint == CC_URL
    # The reason recorded before the successful rescue is the last console reason, not the default.
    assert decision.decider_fallback_reason == REASON_LOW_CONFIDENCE
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CONSOLE_URL, CC_URL]
    assert [call["json"]["model"] for call in transport.calls] == [
        CONSOLE_PRIMARY,
        CONSOLE_PAID,
        CC_MODEL,
    ]
    assert transport.responses == []


def test_a_semantic_paid_answer_does_not_consult_the_fallback():
    transport = StubTransport(
        [
            StubResponse(console_weak_effort_answer()),
            StubResponse(console_unknown_effort_answer()),
        ]
    )
    client, settings = make(transport, cc_config(console_jev_fallback_model=CONSOLE_PAID))

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_UNKNOWN_CHOICE
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CONSOLE_URL]
    assert transport.responses == []


def test_a_missing_paid_answer_without_the_fallback_key_keeps_the_console_reason(monkeypatch):
    monkeypatch.delenv("COMMANDCODE_API_KEY")
    transport = StubTransport([StubResponse(console_weak_effort_answer()), None])
    client, settings = make(transport, cc_config(console_jev_fallback_model=CONSOLE_PAID))

    decision, reason = ask(client, settings)

    assert decision is None
    # No request goes to a host without a key, and the console chain's reason stays final.
    assert reason == REASON_LOW_CONFIDENCE
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CONSOLE_URL]
    assert transport.responses == []


def test_without_the_opt_in_a_missing_paid_answer_keeps_the_legacy_reason():
    # The technical flag exists for the second-host decision only: with the trio empty the returned
    # reason is exactly what 0.4.0 produced for the same two responses.
    transport = StubTransport([StubResponse(console_weak_effort_answer()), None])
    client, settings = make(
        transport,
        cc_config(
            console_jev_fallback_model=CONSOLE_PAID,
            console_jev_commandcode_base_url="",
            console_jev_commandcode_api_key_env="",
            console_jev_commandcode_model="",
        ),
    )

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_LOW_CONFIDENCE
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CONSOLE_URL]
    assert transport.responses == []


@pytest.mark.parametrize(
    "second_failure, expected_reason",
    [
        (StubResponse({"error": "boom"}, status_code=500), REASON_UPSTREAM_ERROR),
        (ReadTimeout("timed out"), REASON_TIMEOUT),
        (StubResponse(None, status_code=200, text="not json"), REASON_MALFORMED),
    ],
)
def test_when_both_hosts_fail_the_fallback_reason_wins(second_failure, expected_reason):
    transport = StubTransport([StubResponse({"error": "429"}, status_code=429), second_failure])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == expected_reason
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CC_URL]
    assert [call["json"]["model"] for call in transport.calls] == [CONSOLE_PRIMARY, CC_MODEL]
    assert transport.responses == []


# -- configuration ------------------------------------------------------------------


def test_the_endpoint_alone_is_a_complete_trio_with_the_documented_defaults(caplog):
    # ``COMMANDCODE_API_KEY`` / ``typesafe/jev`` are the documented pair for the URL opt-in, so an
    # operator who names only the endpoint still gets a working fallback — no warning, and the
    # defaulted model is the one that actually goes on the wire.
    config = cc_config(
        console_jev_commandcode_api_key_env="", console_jev_commandcode_model=""
    )
    transport = StubTransport(
        [StubResponse({"error": "rate limited"}, status_code=429), StubResponse(console_answer())]
    )

    with caplog.at_level(logging.WARNING, logger="config"):
        client, settings = make(transport, config)
        decision, reason = ask(client, settings)

    assert reason is None
    assert decision is not None
    assert decision.decider_endpoint == CC_URL
    assert decision.decider_model == CC_MODEL
    assert (settings.commandcode_fallback().key_env, settings.commandcode_fallback().model) == (
        "COMMANDCODE_API_KEY",
        CC_MODEL,
    )
    assert [call["url"] for call in transport.calls] == [CONSOLE_URL, CC_URL]
    assert [call["json"]["model"] for call in transport.calls] == [CONSOLE_PRIMARY, CC_MODEL]
    assert transport.calls[1]["headers"]["Authorization"] == f"Bearer {CC_KEY}"
    # No "fallback is disabled" warning: the URL alone is a complete opt-in.
    assert not [record for record in caplog.records if "fallback is disabled" in record.getMessage()]


@pytest.mark.parametrize(
    "url",
    [
        "http://api.commandcode.ai/provider/v1/systemone",
        "https://user:pass@api.commandcode.ai/provider/v1/systemone",
        "https://api.commandcode.ai/provider/v1/systemone?x=1",
        "https://api.commandcode.ai/provider/v1/systemone#frag",
        "https:///provider/v1/systemone",
        "not-a-url",
        # ``urlsplit`` raises on this one instead of returning a bad parse: the loader must stay
        # usable and the legacy chain must keep its single call and its reason.
        "https://[broken/provider/v1/systemone",
    ],
)
def test_an_invalid_endpoint_disables_the_fallback(url):
    transport = StubTransport([StubResponse({"error": "rate limited"}, status_code=429)])
    client, settings = make(transport, cc_config(console_jev_commandcode_base_url=url))

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR
    assert transport.call_count == 1
    assert settings.active_commandcode_fallback().enabled is False


def test_an_empty_trio_is_the_legacy_transport_with_a_zero_extra_call():
    transport = StubTransport([StubResponse({"error": "rate limited"}, status_code=429)])
    client, settings = make(
        transport,
        cc_config(
            console_jev_commandcode_base_url="",
            console_jev_commandcode_api_key_env="",
            console_jev_commandcode_model="",
        ),
    )

    decision, reason = ask(client, settings)

    assert decision is None
    assert reason == REASON_UPSTREAM_ERROR
    assert transport.call_count == 1
    assert settings.commandcode_fallback().configured is False


# -- latency ------------------------------------------------------------------------


def test_the_recorded_latency_covers_the_whole_chain(monkeypatch):
    # 125 ms steps are binary-exact, so the expected value cannot drift on float rounding.
    clock = _StepClock(step_ms=125)
    monkeypatch.setattr(decider_module, "time", clock)
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), StubResponse(console_answer())])
    client, settings = make(transport, cc_config())

    decision, reason = ask(client, settings)

    assert reason is None
    # Three clock reads on two attempts: start, after the 429, after the fallback answered — the
    # decision reports the whole chain, not just the call that succeeded.
    assert decision.latency_ms == 250
    assert transport.call_count == 2
