"""The ``jev_console`` quota breaker: N consecutive HTTP 429s stop the endpoint for a TTL.

SystemOne answers a spent free quota with HTTP 429 (``FreeUsageLimitError``) — deterministic, not
transient. Before this breaker, every turn asked again and logged again: 671 ``returned HTTP 429``
lines in four profiles on 06/11/2026. These tests pin the five contracts of the fix:

1. N consecutive 429s open the breaker and no further request reaches the endpoint until the TTL
   expires (the stub counts the calls, so "no HTTP" is measured, not inferred);
2. exactly one ``decider_unavailable_quota`` audit line per open window — never one per request;
3. an expired TTL closes the breaker and the next turn asks the endpoint again;
4. a lone 429 followed by a success does not open it (the counter restarts);
5. the state is visible in ``jev_effort_router_status``: ``closed``/``open``, opening timestamp,
   reason and counter.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import selection
from client import JevClient
from config import load_settings
from decider import (
    REASON_DECIDER_UNAVAILABLE,
    REASON_SKIPPED_MODEL,
    REASON_SKIPPED_NO_MESSAGE,
    REASON_UPSTREAM_ERROR,
    QuotaBreaker,
)
from router import Router, wire_effort
from selection import SELECTION_SOURCE_SESSION_ROW
from stubs import StubResponse, StubState, StubTransport, decision_payload, ollama_request
from tools import _status

CONSOLE_URL = "https://opencode.ai/inference/systemone/v1/systemone"
CONSOLE_KEY = "console-key-secret-777"
PROVIDER = "opencode-console"

MESSAGES = [
    {"role": "user", "content": "première question"},
    {"role": "assistant", "content": "première réponse"},
    {"role": "user", "content": "refactore le module de paiement"},
]


def quota_config(**overrides):
    """A ``jev_console`` deployment with the paid leg and the opt-in fallback off.

    One console request per ``decide`` call keeps the stub's call count equal to the number of
    turns that actually reached SystemOne — which is the whole point of the breaker.
    """
    config = {
        "decider": "jev_console",
        "routable_providers": [PROVIDER],
        "console_jev_fallback_model": "",
    }
    config.update(overrides)
    return config


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", CONSOLE_KEY)


def make(transport, config=None):
    settings = load_settings(lambda key, default=None: (config or quota_config()).get(key, default))
    return JevClient(settings, transport=transport), settings


def ask(client, settings):
    return client.decide(
        MESSAGES,
        settings.effective(provider=PROVIDER),
        provider=PROVIDER,
    )


def console_answer():
    return decision_payload("1", 0.95, "medium", 0.9, grid_size=1)


def http(status_code: int):
    return StubResponse({"error": "FreeUsageLimitError"}, status_code=status_code)


class _NoCatalog:
    """No provider catalog evidence: every model is "unknown", never "missing"."""

    def is_known(self, *args, **kwargs):
        return None


def build_router(tmp_path, transport, config=None):
    holder = {"settings": load_settings(lambda key, default=None: (config or quota_config()).get(key, default))}
    router = Router(
        lambda: holder["settings"],
        get_state=lambda: StubState(tmp_path),
        client_factory=lambda settings: JevClient(settings, transport=transport),
        catalog=_NoCatalog(),
    )
    return router, holder


def route(router, *, turn: str, request=None, model: str = "deepseek-v4.1-flash", platform: str = "cli"):
    request = dict(request if request is not None else ollama_request(model=model))
    return router.on_llm_request(
        request,
        dict(request),
        turn_id=turn,
        session_id="session-1",
        platform=platform,
        model=model,
        provider=PROVIDER,
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id=f"{turn}:api:0",
    )


def records(router, holder):
    return router.tail(holder["settings"], limit=200)


def write_selection_row(
    home,
    provider: str,
    *,
    session_id: str = "session-1",
    model: str = "deepseek-v4.1-flash",
) -> None:
    """The session row the gateway writes: the model/effort the operator picked in the webui."""
    connection = sqlite3.connect(str(home / "state.db"))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, model_config TEXT)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO sessions (id, source, model, model_config) VALUES (?, ?, ?, ?)",
            (
                session_id,
                "webui",
                model,
                json.dumps({"model": model, "provider": provider}),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def reconciled(result) -> tuple:
    """``(model, wire_effort)`` of a rewritten request — what actually went on the wire."""
    request = result["request"]
    return request.get("model"), wire_effort(request, "chat_completions")


def breaker_of(client):
    """The decider's breaker, with its clock replaced by the caller's."""
    return client._decider._breaker


def state_of(client) -> dict:
    state = client.breaker_state()
    assert state is not None, "the jev_console decider always reports a breaker"
    return state


# -- 1. N 429s open the breaker and the HTTP calls stop ---------------------------------


def test_n_consecutive_429_open_the_breaker_and_stop_the_http_calls():
    transport = StubTransport([http(429) for _ in range(8)])
    client, settings = make(transport)

    decisions = [ask(client, settings) for _ in range(6)]

    # Exactly the three answers that opened the window reached SystemOne; the rest are local.
    assert transport.call_count == 3
    assert [decision for decision, _ in decisions] == [None] * 6
    assert decisions[2][1] == REASON_UPSTREAM_ERROR
    assert decisions[3][1] == REASON_DECIDER_UNAVAILABLE
    assert decisions[5][1] == REASON_DECIDER_UNAVAILABLE

    state = state_of(client)
    assert state["state"] == "open"
    assert state["reason"] == "quota"
    assert state["consecutive_429"] == 3
    assert state["threshold"] == 3
    assert state["ttl_s"] == 600.0
    assert state["opened_at"], "the opening timestamp is part of the exposed state"
    assert state["window_id"] == 1, "a window has an identity two same-second openings cannot share"


def test_the_threshold_is_configurable():
    transport = StubTransport([http(429) for _ in range(6)])
    client, settings = make(transport, quota_config(console_jev_breaker_threshold=2))

    ask(client, settings)
    assert transport.call_count == 1
    ask(client, settings)
    assert transport.call_count == 2
    ask(client, settings)
    assert transport.call_count == 2, "the second consecutive 429 opened the window"
    assert state_of(client)["threshold"] == 2


# -- 2. one audit line per window -------------------------------------------------------


def test_exactly_one_audit_line_per_open_window(tmp_path):
    transport = StubTransport([http(429) for _ in range(8)])
    router, holder = build_router(tmp_path, transport)

    for index in range(7):
        assert route(router, turn=f"turn-{index}") is None

    assert transport.call_count == 3
    quota_lines = [r for r in records(router, holder) if r.get("reason") == REASON_DECIDER_UNAVAILABLE]
    assert len(quota_lines) == 1, "one line for the window, not one per request"

    line = quota_lines[0]
    assert line["event"] == "skip"
    assert line["audit_detail"] == "decider unavailable (quota)"
    assert line["upstream_reason"] == REASON_UPSTREAM_ERROR
    assert line["quota_breaker"]["state"] == "open"
    assert line["quota_breaker"]["reason"] == "quota"
    assert line["quota_breaker"]["consecutive_429"] == 3
    assert line["quota_breaker"]["opened_at"]
    # The two failed attempts that did not open the window keep their own honest reason.
    upstream = [r for r in records(router, holder) if r.get("reason") == REASON_UPSTREAM_ERROR]
    assert len(upstream) == 2


def test_a_skip_that_does_not_come_from_the_decider_keeps_its_own_reason(tmp_path):
    """The breaker is per decider and lasts its whole TTL: a gate's skip must survive it.

    ``model_not_in_grid`` is decided *before* the client is asked — it has nothing to do with a
    spent quota. Folding it into the window line would either hide the skip or relabel it
    ``decider_unavailable_quota``, both of which make the audit say something that did not happen.
    """
    transport = StubTransport([http(429) for _ in range(6)])
    router, holder = build_router(tmp_path, transport)

    # Three 429s open the window through the only path that may use it.
    for index in range(4):
        assert route(router, turn=f"turn-{index}") is None
    assert state_of(router.client(holder["settings"]))["state"] == "open"
    assert transport.call_count == 3

    # A configured model outside the surface's grid, with the window still open.
    assert route(router, turn="turn-4", model="model-outside-any-grid") is None

    reasons = [r.get("reason") for r in records(router, holder)]
    assert reasons.count(REASON_DECIDER_UNAVAILABLE) == 1, "still one line for the window"
    assert REASON_SKIPPED_MODEL in reasons, "the grid skip is never swallowed by the breaker"
    grid_skip = [r for r in records(router, holder) if r.get("reason") == REASON_SKIPPED_MODEL][-1]
    assert "quota_breaker" not in grid_skip and "upstream_reason" not in grid_skip
    # And the gate skip asked no host: the window kept the endpoint closed.
    assert transport.call_count == 3


def test_a_refusal_before_the_client_is_asked_keeps_its_own_reason(tmp_path):
    """``no_user_message`` is raised inside ``_decide``, but before ``client.decide`` runs."""
    transport = StubTransport([http(429) for _ in range(6)])
    router, holder = build_router(tmp_path, transport)

    for index in range(4):
        route(router, turn=f"turn-{index}")
    assert transport.call_count == 3

    request = ollama_request(messages=[{"role": "assistant", "content": "nothing to route"}])
    assert route(router, turn="turn-4", request=request) is None

    reasons = [r.get("reason") for r in records(router, holder)]
    assert reasons.count(REASON_DECIDER_UNAVAILABLE) == 1
    assert REASON_SKIPPED_NO_MESSAGE in reasons
    assert transport.call_count == 3


def test_the_window_line_carries_what_a_reconciled_turn_sent(tmp_path, monkeypatch):
    """A reconciled turn is rewritten on the wire; the audit must still say so.

    The window's single line replaces that turn's skip, so it has to carry the reconciliation
    detail (``reconciled_model``/``reconciled_effort``/``selection_source``) — otherwise the only
    record of a turn that *was* rewritten from the session row disappears. Requests inside the
    same open window are reconciled too and deliberately write nothing (one line per window).
    """
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(selection, "_context_home", lambda: home)
    write_selection_row(home, PROVIDER)

    transport = StubTransport([http(429) for _ in range(8)])
    router, holder = build_router(
        tmp_path,
        transport,
        quota_config(session_selection={"mode": "apply"}),
    )

    routed_turns = []
    for index in range(5):
        routed_turns.append(route(router, turn=f"turn-{index}", platform="webui"))

    # Every turn of the window rewrote the request to the session's row.
    assert all(result is not None for result in routed_turns)
    for result in routed_turns:
        wire_model, wire_effort = reconciled(result)
        assert wire_model == "deepseek-v4.1-flash"
        assert wire_effort is None, "the console route declares no effort field"

    windows = [r for r in records(router, holder) if r.get("reason") == REASON_DECIDER_UNAVAILABLE]
    assert len(windows) == 1
    line = windows[0]
    assert line["upstream_reason"] == REASON_UPSTREAM_ERROR
    assert line["reconciled_model"] == "deepseek-v4.1-flash"
    assert "reconciled_effort" in line and line["reconciled_effort"] is None
    assert line["selection_source"] == SELECTION_SOURCE_SESSION_ROW

    # The two 429s that did not open the window carry the same detail, with their own reason.
    upstream = [r for r in records(router, holder) if r.get("reason") == REASON_UPSTREAM_ERROR]
    assert len(upstream) == 2
    assert all(record["reconciled_model"] == "deepseek-v4.1-flash" for record in upstream)


# -- 3. TTL expiry closes the breaker ---------------------------------------------------


def test_ttl_expiry_closes_the_breaker_and_the_next_call_asks_again():
    now = {"t": 1000.0}
    transport = StubTransport([http(429) for _ in range(8)] + [console_answer()])
    client, settings = make(transport, quota_config(console_jev_breaker_ttl_s=60.0))
    breaker_of(client).clock = lambda: now["t"]

    for _ in range(4):
        ask(client, settings)
    assert transport.call_count == 3
    assert state_of(client)["state"] == "open"

    now["t"] += 61.0
    decision, reason = ask(client, settings)

    assert transport.call_count == 4, "the expired window must let the next call through"
    assert decision is None and reason == REASON_UPSTREAM_ERROR
    state = state_of(client)
    assert state["state"] == "closed"
    assert state["consecutive_429"] == 1, "a new window starts from the first 429 again"


def test_a_new_window_writes_its_own_audit_line(tmp_path):
    now = {"t": 1000.0}
    transport = StubTransport([http(429) for _ in range(8)])
    router, holder = build_router(
        tmp_path, transport, quota_config(console_jev_breaker_ttl_s=60.0)
    )
    breaker_of(router.client(holder["settings"])).clock = lambda: now["t"]

    for index in range(5):
        route(router, turn=f"turn-{index}")
    assert len([r for r in records(router, holder) if r.get("reason") == REASON_DECIDER_UNAVAILABLE]) == 1

    # The window expires: three more 429s open a second one, announced once.
    now["t"] += 61.0
    for index in range(5, 10):
        route(router, turn=f"turn-{index}")

    windows = [r for r in records(router, holder) if r.get("reason") == REASON_DECIDER_UNAVAILABLE]
    assert len(windows) == 2
    assert windows[0]["quota_breaker"]["window_id"] == 1
    assert windows[1]["quota_breaker"]["window_id"] == 2
    assert transport.call_count == 6


# -- 4. an isolated 429 never opens it ---------------------------------------------------


def test_one_429_followed_by_success_does_not_open_the_breaker():
    transport = StubTransport([http(429), StubResponse(console_answer())])
    client, settings = make(transport)

    assert ask(client, settings)[0] is None
    decision, reason = ask(client, settings)

    assert decision is not None and reason is None
    assert transport.call_count == 2
    state = state_of(client)
    assert state["state"] == "closed"
    assert state["consecutive_429"] == 0 and state["opened_at"] is None


def test_a_non_429_error_restarts_the_counter():
    transport = StubTransport([http(429), http(429), http(500), http(429), http(429), http(429)])
    client, settings = make(transport)

    for _ in range(5):
        ask(client, settings)

    assert transport.call_count == 5
    assert state_of(client)["consecutive_429"] == 2
    assert state_of(client)["state"] == "closed"

    ask(client, settings)
    assert state_of(client)["state"] == "open"


def test_a_timeout_restarts_the_counter():
    import stubs

    transport = StubTransport([http(429), http(429), stubs.ReadTimeout("slow"), http(429)])
    client, settings = make(transport)

    for _ in range(4):
        ask(client, settings)

    assert client.breaker_state() is not None
    assert state_of(client)["consecutive_429"] == 1
    assert state_of(client)["state"] == "closed"


# -- 5. the state is visible in the status surface ---------------------------------------


def test_status_exposes_the_open_breaker(tmp_path):
    transport = StubTransport([http(429) for _ in range(4)])
    router, holder = build_router(tmp_path, transport)

    for index in range(3):
        route(router, turn=f"turn-{index}")

    payload = json.loads(_status(router, holder["settings"]))
    breaker = payload["quota_breaker"]
    assert breaker["state"] == "open"
    assert breaker["reason"] == "quota"
    assert breaker["consecutive_429"] == 3
    assert breaker["threshold"] == 3
    assert breaker["ttl_s"] == 600.0
    assert breaker["opened_at"]


def test_status_reports_a_closed_breaker_on_a_healthy_deployment(tmp_path):
    transport = StubTransport([StubResponse(console_answer())])
    router, holder = build_router(tmp_path, transport)

    assert route(router, turn="turn-0") is not None

    breaker = json.loads(_status(router, holder["settings"]))["quota_breaker"]
    assert breaker["state"] == "closed"
    assert breaker["consecutive_429"] == 0
    assert breaker["opened_at"] is None
    assert breaker["reason"] is None


def test_legacy_deciders_gain_no_breaker_surface(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, holder = build_router(tmp_path, transport, {"routable_providers": ["ollama-cloud"]})

    request = ollama_request()
    assert (
        router.on_llm_request(
            request,
            dict(request),
            turn_id="turn-0",
            session_id="session-1",
            platform="cli",
            model="deepseek-v4.1-flash",
            provider="ollama-cloud",
            base_url="https://ollama.com/v1",
            api_mode="chat_completions",
            api_call_count=0,
            api_request_id="turn-0:api:0",
        )
        is not None
    )

    payload = json.loads(_status(router, holder["settings"]))
    assert "quota_breaker" not in payload
    assert router.breaker_state(holder["settings"]) is None


# -- the breaker itself ------------------------------------------------------------------


def test_breaker_unit_contract():
    now = {"t": 0.0}
    breaker = QuotaBreaker(threshold=3, ttl_s=600.0, clock=lambda: now["t"])

    assert breaker.snapshot()["state"] == "closed"
    assert breaker.record_429() is False
    assert breaker.record_429() is False
    assert breaker.record_429() is True, "the third consecutive 429 opens the window"
    assert breaker.is_open() is True

    now["t"] = 599.0
    assert breaker.is_open() is True
    now["t"] = 600.0
    assert breaker.is_open() is False
    assert breaker.consecutive_429 == 0, "the window resets when it expires"


@pytest.mark.parametrize(
    "setting, expected",
    [
        ({"console_jev_breaker_threshold": 5}, 5),
        ({"console_jev_breaker_ttl_s": 120.0}, 120.0),
    ],
)
def test_the_breaker_settings_are_configurable(setting, expected):
    settings = load_settings(lambda key, default=None: quota_config(**setting).get(key, default))
    field = next(iter(setting))
    assert getattr(settings, field) == expected


def test_the_breaker_defaults_are_three_429_and_ten_minutes():
    settings = load_settings(lambda key, default=None: quota_config().get(key, default))
    assert settings.console_jev_breaker_threshold == 3
    assert settings.console_jev_breaker_ttl_s == 600.0
