"""The native ``anthropic`` route, and the subscription route's ``[1m]`` context variant.

Two decisions from card t_ec2453b1 (spec §2, §3, §5) are pinned here, and nothing else:

* the router routes **both** Claude routes. The native ``anthropic`` route is a normal routable
  provider now (its turns are billed against whatever credential the core resolved — Claude Code's
  OAuth on this host, a per-token API key elsewhere, which is the credential's business, not this
  plugin's), and the webui selection naming ``anthropic`` is eligible on a native request because
  identity needs no translation. ``provider_map`` stays as the *additional* route a selection is
  also valid on, which is what keeps the t_9bda5849 race reconciled on the subscription route.
* the subscription route's ``[1m]`` suffix is a context-window variant, not another model. It is
  normalised away for every grid/catalog/memo/selection comparison and re-appended on the wire.
  The native route has **no** variant: its 1M window comes from the beta header, so an unexpected
  ``[1m]`` there stays a ``model_not_in_grid`` skip (M3).

Test IDs, as numbered by the spec (see ``docs/routing-grid.md``):

===================  ==============================================================
N1..N5               native route: routed decision, family effort cap, reconciliation,
                     and the regression that keeps an unrouted native turn untouched
M1..M4               ``[1m]`` on the subscription route: wire, selection, native
                     (no normalisation) and the memo replay
B1, B2               both routes in one session, and the t_9bda5849 race
F1..F3               fail-open, Hermes fallback, and ``mode: off``
===================  ==============================================================
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from stubs import (
    ReadTimeout,
    StubResponse,
    StubTransport,
    anthropic_request,
    decision_payload,
    subscription_request,
)
from test_router import build, route
from test_session_selection import write_row

import selection
from config import (
    ANTHROPIC_MODELS,
    COMMANDCODE_MODELS,
    CONTEXT_VARIANT_SUFFIXES,
    same_provider_route,
    split_context_variant,
)
from effort import SUBSCRIPTION_MODEL_LEVELS
from grid import SUBSCRIPTION_GRID

PROVIDER = "claude-subscription-directsdk-experimental"
NATIVE = "anthropic"
COMMANDCODE = "commandcode"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
ONE_M = "[1m]"

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


def subscription_call(router, request=None, *, model=SONNET, **overrides):
    kwargs = dict(
        model=model,
        provider=PROVIDER,
        base_url="https://subscription.invalid",
        api_mode="chat_completions",
    )
    kwargs.update(overrides)
    request = request if request is not None else subscription_request(model=model)
    return route(router, request, **kwargs)


def records(router, settings, limit=50):
    return router.tail(settings, limit=limit)


def events(router, settings, name):
    return [record for record in records(router, settings) if record.get("event") == name]


def last_skip(router, settings):
    skips = events(router, settings, "skip")
    assert skips, "expected a skip record"
    return skips[-1]


def subscription_effort(routed):
    return (routed.get("extra_body") or {}).get("reasoning", {}).get("effort")


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
        config=config(NATIVE, PROVIDER, mode="apply"),
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
    rollback: ``provider_not_routed``, no decider call, and no ``normalized_model`` beside the
    configured id.
    """
    router, settings = build(tmp_path, StubTransport([]), config=config(PROVIDER))
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


# -- M1..M4: the subscription route's ``[1m]`` ----------------------------------


def test_m1_the_suffix_is_normalised_for_the_grid_and_put_back_on_the_wire(profile, tmp_path):
    """M1 — configured ``Sonnet[1m]``, decided Haiku: the wire carries ``Haiku[1m]``."""
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=config(PROVIDER))
    configured = SONNET + ONE_M

    result = subscription_call(router, subscription_request(model=configured, effort="low"), model=configured)

    assert result is not None, "a ``[1m]`` request must not be a model_not_in_grid skip"
    routed = result["request"]
    assert routed["model"] == HAIKU + ONE_M
    assert subscription_effort(routed) == "high"

    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["model"] == HAIKU
    assert record["wire_model"] == HAIKU + ONE_M
    assert record["provider"] == PROVIDER
    # The configured id is kept verbatim; the bare id every lookup used is beside it.
    assert record["configured_model"] == configured
    assert record["normalized_model"] == SONNET
    assert events(router, settings, "skip") == []


def test_m1_the_subscription_providers_native_model_accepts_that_wire_id():
    """M1 — contract check against the provider's own ``model_catalog.native_model``.

    The wire id this plugin writes has to be one the provider accepts and resolves back to itself:
    ``native_model`` strips ``[1m]``, routes the short names and re-appends the suffix to every
    family it pins at 1M. Skipped only when the provider plugin is not installed on this machine —
    the rest of M1 does not depend on it.
    """
    module = _subscription_model_catalog()
    for entry in SUBSCRIPTION_GRID:
        # The provider pins these three families at 1M (``model_catalog.CONTEXT_WINDOWS``); an id
        # outside that table would reach the CLI unchanged and the suffix would not be its own.
        assert module.CONTEXT_WINDOWS[entry.model_id] == 1_000_000
        wire = entry.model_id + ONE_M
        assert module.native_model(wire) == wire


def test_m2_a_suffixed_selection_is_eligible_on_the_subscription_route(profile, tmp_path):
    """M2 — the row's ``Sonnet[1m]`` is the grid's ``Sonnet``: not ``model_outside_catalog``."""
    write_row(
        profile,
        selection_row(SONNET + ONE_M, PROVIDER, "medium"),
        session_id=SESSION_ID,
    )
    router, settings = build(
        tmp_path,
        StubTransport([StubResponse(decision_payload("2", 0.10, "high", 0.12, grid_size=3))] * 2),
        config=config(PROVIDER, mode="apply"),
    )

    result = subscription_call(
        router,
        subscription_request(model=SONNET, effort="high"),
        platform="webui",
        session_id=SESSION_ID,
        turn_id="m2",
    )

    assert result is not None
    assert result["request"]["model"] == SONNET + ONE_M
    assert events(router, settings, "selection_unmappable") == []
    skip = last_skip(router, settings)
    assert skip["reconciled_model"] == SONNET + ONE_M
    assert skip["selection_source"] == "session_row"


def test_m3_the_native_route_does_not_normalise_the_suffix(profile, tmp_path):
    """M3 — the native 1M window is the beta header's: ``[1m]`` there is still an off-grid id."""
    router, settings = build(tmp_path, StubTransport([]), config=config(NATIVE))

    assert native_call(router, anthropic_request(model=SONNET + ONE_M), model=SONNET + ONE_M) is None

    record = last_skip(router, settings)
    assert record["reason"] == "model_not_in_grid"
    assert record["configured_model"] == SONNET + ONE_M
    # No suffix is declared for this route, so there is nothing to normalise and nothing to claim.
    assert "normalized_model" not in record


def test_m4_a_suffixed_turn_replays_its_own_memo(profile, tmp_path):
    """M4 — a ``[1m]`` request replays the turn's decision instead of skipping on provenance."""
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=config(PROVIDER))
    configured = SONNET + ONE_M

    first = subscription_call(
        router,
        subscription_request(model=configured, effort="high"),
        model=configured,
        turn_id="m4",
    )
    second = subscription_call(
        router,
        subscription_request(model=configured, effort="high"),
        model=configured,
        turn_id="m4",
    )

    assert first["request"]["model"] == SONNET + ONE_M
    assert second["request"]["model"] == SONNET + ONE_M
    assert transport.call_count == 1, "the second request must replay, not re-decide"
    route_records = events(router, settings, "route")
    assert [record["replayed"] for record in route_records] == [False, True]
    assert events(router, settings, "skip") == []


# -- B1/B2: both routes in one session, and the t_9bda5849 race -----------------


def test_b1_both_routes_are_routed_in_one_session(profile, tmp_path):
    """B1 — turn 1 subscription ``Sonnet[1m]``, turn 2 native Haiku: both routed, no leak."""
    transport = StubTransport(
        [
            StubResponse(decision_payload("2", 0.95, "high", 0.9, grid_size=3)),
            StubResponse(decision_payload("1", 0.95, "medium", 0.9, grid_size=3)),
        ]
    )
    router, settings = build(tmp_path, transport, config=config(NATIVE, PROVIDER))
    configured = SONNET + ONE_M

    first = subscription_call(
        router,
        subscription_request(model=configured, effort="high"),
        model=configured,
        session_id=SESSION_ID,
        turn_id="b1-sub",
    )
    second = native_call(
        router,
        anthropic_request(model=HAIKU, effort="medium"),
        session_id=SESSION_ID,
        turn_id="b1-native",
    )

    assert first["request"]["model"] == SONNET + ONE_M
    assert second["request"]["model"] == HAIKU
    assert "extra_body" not in second["request"] or "reasoning" not in second["request"]["extra_body"]
    route_records = events(router, settings, "route")
    assert [record["provider"] for record in route_records] == [PROVIDER, NATIVE]
    # The native turn asked the decider itself: the subscription memo did not leak into it.
    assert [record["replayed"] for record in route_records] == [False, False]
    assert transport.call_count == 2


def test_b2_the_race_still_reconciles_on_the_subscription_route(profile, tmp_path):
    """B2 — t_9bda5849: the row says ``anthropic``, the request is on the subscription route."""
    write_row(profile, selection_row(HAIKU, NATIVE, "medium"), session_id=SESSION_ID)
    router, settings = build(
        tmp_path,
        StubTransport([StubResponse(decision_payload("2", 0.10, "high", 0.12, grid_size=3))] * 2),
        config=config(NATIVE, PROVIDER, mode="apply"),
    )

    result = subscription_call(
        router,
        subscription_request(model=SONNET, effort="high"),
        platform="webui",
        session_id=SESSION_ID,
        turn_id="b2",
    )

    assert result is not None
    assert result["request"]["model"] == HAIKU
    assert subscription_effort(result["request"]) == "medium"
    assert events(router, settings, "selection_unmappable") == []
    skip = last_skip(router, settings)
    assert skip["provider"] == PROVIDER
    assert skip["reconciled_model"] == HAIKU


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


# -- shared contracts ----------------------------------------------------------


def test_the_variant_table_is_declared_only_where_the_id_carries_the_window():
    """``[1m]`` is declared for the subscription route and for nothing else."""
    assert CONTEXT_VARIANT_SUFFIXES == {PROVIDER: (ONE_M,)}
    assert split_context_variant(PROVIDER, SONNET + ONE_M) == (SONNET, ONE_M)
    assert split_context_variant(PROVIDER, SONNET) == (SONNET, "")
    # The native route's 1M window rides in the beta header, so its id is never split.
    assert split_context_variant(NATIVE, SONNET + ONE_M) == (SONNET + ONE_M, "")
    # A suffix is only split when something is left of it.
    assert split_context_variant(PROVIDER, ONE_M) == (ONE_M, "")


def test_identity_is_the_routes_own_alias_table():
    """``same_provider_route`` is the alias normalisation, not a string comparison."""
    assert same_provider_route("anthropic", "claude")
    assert same_provider_route("claude-code", "anthropic")
    assert not same_provider_route(NATIVE, PROVIDER)
    assert not same_provider_route("some-route", "another-route")
    assert same_provider_route("some-route", "some-route")


def test_the_subscription_families_keep_their_declared_caps():
    """The variant does not move a family's cap: Sonnet stops at ``high``, Haiku keeps ``max``."""
    caps = dict(SUBSCRIPTION_MODEL_LEVELS)
    assert caps[SONNET] == ("low", "medium", "high")
    assert "max" in caps[HAIKU]


def _subscription_model_catalog():
    """The provider's own ``model_catalog`` module, or a skip when it is not installed here."""
    candidates = []
    if os.environ.get("JEV_SUBSCRIPTION_PLUGIN_DIR"):
        candidates.append(Path(os.environ["JEV_SUBSCRIPTION_PLUGIN_DIR"]))
    candidates.extend(
        [
            Path.home() / ".hermes" / "plugins" / PROVIDER,
            Path.home() / "hermes" / "home" / "plugins" / PROVIDER,
        ]
    )
    for directory in candidates:
        path = directory / "model_catalog.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("jev_subscription_model_catalog", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    pytest.skip("the claude-subscription provider plugin is not installed on this machine")
