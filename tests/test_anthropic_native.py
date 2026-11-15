"""The native ``anthropic`` route.

The decision from card t_ec2453b1 (spec §2, §3, §5) is pinned here:

* the router routes the native ``anthropic`` route like any other routable provider. Its turns are
  billed against whatever credential the core resolved — Claude Code's OAuth on this host, a
  per-token API key elsewhere, which is the credential's business, not this plugin's.
* the webui selection naming ``anthropic`` is eligible on a native request because identity needs
  no translation, and ``session_selection.provider_map`` (empty by default since 0.8.0) only *adds*
  a route a choice is also valid on.

Test IDs, as numbered by the spec (see ``docs/routing-grid.md``):

===================  ==============================================================
N1..N5               native route: routed decision, family effort cap, reconciliation,
                     and the regression that keeps an unrouted native turn untouched
F1..F3               fail-open, Hermes fallback, and ``mode: off``
===================  ==============================================================
"""

from __future__ import annotations

import pytest
from stubs import (
    ReadTimeout,
    StubResponse,
    StubTransport,
    anthropic_request,
    decision_payload,
)
from test_router import build, route
from test_session_selection import write_row

import selection
from config import (
    ANTHROPIC_MODELS,
    COMMANDCODE_MODELS,
    same_provider_route,
)

NATIVE = "anthropic"
COMMANDCODE = "commandcode"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

SESSION_ID = "20261111_233528_179895"


# -- helpers -------------------------------------------------------------------


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A profile home plus the console decider's key; the row is written per test."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(selection, "_context_home", lambda: home)
    return home


def config(*routable, mode="", **overrides):
    settings = {"decider": "jev_console", "routable_providers": list(routable)}
    if mode:
        settings["session_selection"] = {"mode": mode}
    settings.update(overrides)
    return settings


def selection_row(model, provider, effort="medium", *, enabled=True):
    return {"model": model, "provider": provider, "reasoning_config": {"enabled": enabled, "effort": effort}}


def native_call(router, request=None, *, model=HAIKU, **overrides):
    kwargs = dict(
        model=model,
        provider=NATIVE,
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages",
    )
    kwargs.update(overrides)
    request = request if request is not None else anthropic_request(model=model)
    return route(router, request, **kwargs)


def records(router, settings, limit=50):
    return router.tail(settings, limit=limit)


def events(router, settings, name):
    return [record for record in records(router, settings) if record.get("event") == name]


def last_skip(router, settings):
    skips = events(router, settings, "skip")
    assert skips, "expected a skip record"
    return skips[-1]


# -- N1..N3: the native route's wiring -----------------------------------------


def test_n1_native_route_writes_the_model_and_output_config_effort(profile, tmp_path):
    """N1 — ``anthropic`` + ``anthropic_messages``: Sonnet/``high`` in ``output_config``."""
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=config(NATIVE))

    result = native_call(router, anthropic_request(model=HAIKU, effort="low"))

    assert result is not None
    routed = result["request"]
    assert routed["model"] == SONNET
    assert routed["output_config"]["effort"] == "high"
    # Neither of the other routes' containers may appear on a Messages body.
    assert "extra_body" not in routed
    assert "reasoning_effort" not in routed
    assert routed["thinking"] == {"type": "adaptive", "display": "summarized"}

    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["provider"] == NATIVE
    assert record["model"] == SONNET
    assert record["wire_model"] == SONNET
    assert record["effort"] == "high"


def test_n2_the_native_route_can_land_on_haiku(profile, tmp_path):
    """N2 — the same route, Haiku/``medium``: no route-specific special case."""
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "medium", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=config(NATIVE))

    result = native_call(router, anthropic_request(model=SONNET, effort="high"))

    assert result["request"]["model"] == HAIKU
    assert result["request"]["output_config"]["effort"] == "medium"


def test_n3_sonnet_plus_max_is_omitted_not_invented():
    """N3 — the family cap omits ``max`` on Sonnet, and the question never offers it.

    An end-to-end turn cannot produce this answer: the level question is built from
    ``offered_levels_for_grid``, which intersects the families' caps, so ``max`` is not among the
    options while Sonnet can win. The cap is therefore pinned where it lives — the resolver that
    turns an answer into a wire value, and the writer that has to cope with the omission.
    """
    from config import ANTHROPIC_PROVIDER
    from effort import offered_levels_for_grid, resolve_effort
    from grid import ANTHROPIC_GRID

    assert "max" not in offered_levels_for_grid(ANTHROPIC_GRID, ANTHROPIC_PROVIDER)
    assert resolve_effort(SONNET, "max", provider=ANTHROPIC_PROVIDER) is None
    # Haiku's family cap keeps ``max``: the omission is per family, not per route.
    assert resolve_effort(HAIKU, "max", provider=ANTHROPIC_PROVIDER) == "max"

    from router import Router

    body = {"model": SONNET, "output_config": {"effort": "max"}}
    assert Router._apply_anthropic_effort(dict(body), None) == {"model": SONNET}


# -- N4/N5: reconciliation on the native route, and the untouched regression -----


def test_n4_a_webui_selection_reconciles_a_failed_native_turn(profile, tmp_path):
    """N4 — identity is eligible: the native pick lands on the native request."""
    write_row(profile, selection_row(HAIKU, NATIVE, "medium"), session_id=SESSION_ID)
    router, settings = build(
        tmp_path,
        StubTransport([StubResponse(decision_payload("2", 0.10, "high", 0.12, grid_size=3))] * 2),
        config=config(NATIVE, mode="apply"),
    )

    result = native_call(
        router,
        anthropic_request(model=SONNET, effort="high"),
        platform="webui",
        session_id=SESSION_ID,
        turn_id="n4",
    )

    assert result is not None
    routed = result["request"]
    assert routed["model"] == HAIKU
    assert routed["output_config"]["effort"] == "medium"
    assert events(router, settings, "selection_unmappable") == []
    skip = last_skip(router, settings)
    assert skip["reconciled_model"] == HAIKU
    assert skip["reconciled_effort"] == "medium"
    assert skip["selection_source"] == "session_row"


def test_n5_native_stays_untouched_while_the_route_is_not_routable(profile, tmp_path):
    """N5 — regression: without ``anthropic`` in ``routable_providers`` nothing changes.

    The record is asserted field by field, because "byte for byte" is the whole promise of the
    rollback: ``provider_not_routed``, no decider call, and exactly the fields a deployment without
    the native route wrote.
    """
    router, settings = build(tmp_path, StubTransport([]), config=config(COMMANDCODE))
    request = anthropic_request(model=HAIKU, effort="medium")

    assert native_call(router, request, model=HAIKU) is None

    record = last_skip(router, settings)
    # ``ts`` is the audit's own timestamp; every other field is part of the contract.
    assert {key: value for key, value in record.items() if key != "ts"} == {
        "event": "skip",
        "reason": "provider_not_routed",
        "provider": NATIVE,
        "configured_model": HAIKU,
        "turn_id": "turn-1",
        "session_id": "session-1",
        "api_request_id": "turn-1:api:0",
        "platform": "cli",
    }


# -- F1..F3: fail-open, Hermes fallback, and ``mode: off`` ---------------------


def test_f1_a_failed_native_decision_fails_open_and_reconciles(profile, tmp_path):
    """F1 — a timeout on the native route: skip + reconciliation when a row exists."""
    write_row(profile, selection_row(OPUS, NATIVE, "high"), session_id=SESSION_ID)
    router, settings = build(
        tmp_path,
        StubTransport([ReadTimeout("timed out")] * 2),
        config=config(NATIVE, mode="apply"),
    )

    result = native_call(
        router,
        anthropic_request(model=SONNET, effort="medium"),
        platform="webui",
        session_id=SESSION_ID,
        turn_id="f1-with-row",
    )

    assert result is not None
    assert result["request"]["model"] == OPUS
    assert result["request"]["output_config"]["effort"] == "high"
    skip = last_skip(router, settings)
    assert skip["reason"] in {"timeout", "exception"}
    assert skip["reconciled_model"] == OPUS


def test_f1_without_a_row_the_failed_native_turn_is_untouched(profile, tmp_path):
    """F1 — same failure, no row: the request leaves exactly as built (``None``)."""
    router, settings = build(
        tmp_path,
        StubTransport([ReadTimeout("timed out")] * 2),
        config=config(NATIVE, mode="apply"),
    )

    assert native_call(
        router,
        anthropic_request(model=SONNET, effort="medium"),
        platform="webui",
        session_id=SESSION_ID,
        turn_id="f1-no-row",
    ) is None
    assert last_skip(router, settings)["reason"] in {"timeout", "exception"}
    assert "reconciled_model" not in last_skip(router, settings)


def test_f2_a_fallback_request_is_routed_by_its_own_grid(profile, tmp_path):
    """F2 — after a native 500, the fallback route uses its own grid; ``anthropic`` never leaks."""
    transport = StubTransport(
        [
            # ``jev_console`` tries its free model and then its paid one; both have to fail before
            # the turn fails open, which is the state a Hermes provider fallback runs out of.
            StubResponse(None, status_code=500, text="upstream error"),
            StubResponse(None, status_code=500, text="upstream error"),
            StubResponse(decision_payload("1", 0.95, "medium", 0.9, grid_size=1)),
        ]
    )
    router, settings = build(tmp_path, transport, config=config(NATIVE, COMMANDCODE))

    assert native_call(
        router,
        anthropic_request(model=SONNET, effort="high"),
        turn_id="f2-native",
    ) is None
    fallback = route(
        router,
        {"model": "deepseek/deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hello"}]},
        model="deepseek/deepseek-v4.1-flash",
        provider=COMMANDCODE,
        base_url="https://commandcode.invalid/v1",
        api_mode="chat_completions",
        turn_id="f2-native",
        session_id="session-1",
    )

    assert fallback is not None
    assert fallback["request"]["model"] == COMMANDCODE_MODELS[0]
    assert fallback["request"]["model"] not in ANTHROPIC_MODELS

    # A provider outside ``routable_providers`` is still untouched, and costs no decider call.
    calls_before = transport.call_count
    untouched = route(
        router,
        {"model": "openrouter/auto", "messages": [{"role": "user", "content": "hello"}]},
        model="openrouter/auto",
        provider="openrouter",
        base_url="https://openrouter.invalid/api/v1",
        api_mode="chat_completions",
        turn_id="f2-native",
        session_id="session-1",
    )
    assert untouched is None
    assert last_skip(router, settings)["reason"] == "provider_not_routed"
    assert transport.call_count == calls_before


def test_f3_mode_off_is_byte_for_byte_the_behaviour_without_the_key(profile, tmp_path, monkeypatch):
    """F3 — ``mode: off``: the row is never read, and neither payload nor audit changes."""
    write_row(profile, selection_row(HAIKU, NATIVE, "medium"), session_id=SESSION_ID)

    def forbidden(*args, **kwargs):
        raise AssertionError("mode: off must not open the session database")

    transport = StubTransport([ReadTimeout("timed out")] * 2)
    off_router, off_settings = build(
        tmp_path / "off", transport, config=config(NATIVE, mode="off")
    )
    reference_router, reference_settings = build(
        tmp_path / "ref", StubTransport([ReadTimeout("timed out")] * 2), config=config(NATIVE)
    )
    monkeypatch.setattr(selection.sqlite3, "connect", forbidden)

    payload = anthropic_request(model=SONNET, effort="medium")
    off_result = native_call(
        off_router, payload, platform="webui", session_id=SESSION_ID, turn_id="f3"
    )
    reference_result = native_call(
        reference_router, payload, platform="webui", session_id=SESSION_ID, turn_id="f3"
    )

    assert off_result is None and reference_result is None
    assert off_router.tail(off_settings, limit=10) == reference_router.tail(reference_settings, limit=10)
    assert events(off_router, off_settings, "selection_divergence") == []
    assert events(off_router, off_settings, "selection_unmappable") == []


def test_identity_is_the_routes_own_alias_table():
    """``same_provider_route`` is the alias normalisation, not a string comparison."""
    assert same_provider_route("anthropic", "claude")
    assert same_provider_route("claude-code", "anthropic")
    assert not same_provider_route(NATIVE, COMMANDCODE)
    assert not same_provider_route("some-route", "another-route")
    assert same_provider_route("some-route", "some-route")

