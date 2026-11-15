"""The webui session's own model/effort selection, reconciled on a fail-open turn.

Why this file exists: the webui's model picker reaches the backend twice — into the session's row
(``sessions.model_config``) and into the agent being built. When the pick lands while the pre-warmed
build is already running, only the row gets it, the turn runs on the profile's default, and the
router sees a request that disagrees with what the operator chose. This suite pins what the plugin
does with the row, and — more importantly — what it must not do.

The five properties under test:

1. ``mode: off`` (the default) is byte-for-byte the old behaviour: the row is not even read.
2. ``warn`` reports a divergence in the audit and in the log, and leaves the request alone.
3. ``apply`` reconciles a **fail-open** turn only. A successful decision wins, and the gates the
   router may not override (``disabled``, ``provider_not_routed``, ``api_mode_not_routed``) never
   reconcile.
4. The reconciled level goes through the same route policy as a decided one: ``output_config.effort``
   on the native ``anthropic`` route, caps respected by omission, and never a level written into a
   container the transport did not build.
5. Every unreadable row — absent, locked, invalid JSON, another component's shape — fails open
   silently (``DEBUG`` only), never raising.

The row fixture is the **real** one the job-search session wrote
(``profiles/job-search/state.db``, session ``20261111_180552_34faa4``): the UI chose
``claude-haiku-5-5`` on ``anthropic`` with effort ``medium``, while the agent ran
``claude-sonnet-5-5`` with effort ``high`` on the same route.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
from stubs import StubResponse, StubTransport, anthropic_request, decision_payload
from test_router import build, route

import selection
from config import (
    DEFAULT_SESSION_SELECTION_PLATFORMS,
    DEFAULT_SESSION_SELECTION_PROVIDER_MAP,
    SESSION_SELECTION_APPLY,
    SESSION_SELECTION_MODES,
    SESSION_SELECTION_OFF,
    SESSION_SELECTION_WARN,
    load_settings,
    parse_session_selection,
)
from router import wire_effort

NATIVE = "anthropic"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

#: The session the planner card diagnosed.
SESSION_ID = "20261111_180552_34faa4"

#: Verbatim ``sessions.model_config`` of that session: the flat shape ``tui_gateway`` writes, with
#: ``base_url``/``api_mode`` from the pin and the host's own ``_usage_anchor`` sibling.
JOB_SEARCH_MODEL_CONFIG = {
    "model": HAIKU,
    "provider": NATIVE,
    "base_url": "https://api.anthropic.com",
    "api_mode": "anthropic_messages",
    "reasoning_config": {"enabled": True, "effort": "medium"},
    "_usage_anchor": {
        "prompt_tokens": 40087,
        "completion_tokens": 31,
        "base_count": 1,
        "base_last_role": "user",
    },
}


# -- helpers -------------------------------------------------------------------


def _config(mode: str, **overrides):
    config = {
        "decider": "jev_console",
        "routable_providers": [NATIVE],
        "session_selection": {"mode": mode},
    }
    config.update(overrides)
    return config


def write_row(home, model_config, *, session_id: str = SESSION_ID, model: str = "") -> None:
    """Create/refresh the ``sessions`` table the reader queries and upsert one row.

    Only the columns this feature reads are created: the reader's contract is ``id`` and
    ``model_config``, and a test that needed the whole host schema would be pinning more than the
    plugin depends on.
    """
    if not isinstance(model_config, str):
        model_config = json.dumps(model_config)
    connection = sqlite3.connect(str(home / "state.db"))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, source TEXT, model TEXT, model_config TEXT)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO sessions (id, source, model, model_config) VALUES (?, ?, ?, ?)",
            (session_id, "webui", model, model_config),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A profile home with no ``state.db`` yet, resolved through the host hook."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(selection, "_context_home", lambda: home)
    return home


def call(
    router, request=None, *, model=SONNET, effort="high", provider=NATIVE, platform="webui",
    turn_id="turn-1", session_id=SESSION_ID, **overrides
):
    """One provider request on the native ``anthropic`` route (``output_config`` shape)."""
    request = request if request is not None else anthropic_request(model=model, effort=effort)
    kwargs = {
        "model": model,
        "provider": provider,
        "base_url": "https://api.anthropic.com",
        "api_mode": "anthropic_messages",
        "platform": platform,
        "turn_id": turn_id,
        "session_id": session_id,
    }
    kwargs.update(overrides)
    return route(router, request, **kwargs)


def low_confidence_transport():
    """A console answer below the threshold, for both models of its free-then-paid chain.

    ``jev_console`` treats a semantic answer as host-healthy and re-asks with its paid fallback
    before it fails open, so a low-confidence turn costs two exchanges with the stub.
    """
    payload = decision_payload("2", 0.10, "high", 0.12, grid_size=3)
    return StubTransport([StubResponse(payload), StubResponse(payload)])


def records(router, settings, limit: int = 50):
    return router.tail(settings, limit=limit)


def events(router, settings, name: str):
    return [record for record in records(router, settings) if record.get("event") == name]


def last_skip(router, settings):
    skips = events(router, settings, "skip")
    assert skips, "expected a skip record"
    return skips[-1]


def wire_level(routed):
    return wire_effort(routed, "anthropic_messages")


# -- the configuration ---------------------------------------------------------


def test_session_selection_defaults_keep_the_plugin_out_of_the_database():
    """The default has to be ``off``, on the webui, with an identity-only provider map."""
    parsed = load_settings(lambda key, default=None: default).session_selection

    assert parsed.mode == SESSION_SELECTION_OFF == "off"
    assert parsed.platforms == DEFAULT_SESSION_SELECTION_PLATFORMS == ("webui",)
    assert dict(parsed.provider_map) == dict(DEFAULT_SESSION_SELECTION_PROVIDER_MAP) == {}
    assert parsed.provider_for(NATIVE) == NATIVE
    assert not parsed.enabled and not parsed.applies and not parsed.warns


def test_an_unusable_mode_falls_back_to_off_with_a_warning(caplog):
    caplog.set_level(logging.WARNING)
    for declared in ("applyy", "", 3, None, ["apply"]):
        parsed = parse_session_selection({"mode": declared})
        assert parsed.mode == SESSION_SELECTION_OFF
    assert any("session_selection.mode" in record.message for record in caplog.records)


def test_the_two_opted_in_modes_read_the_row_and_only_apply_rewrites():
    for mode in SESSION_SELECTION_MODES:
        parsed = parse_session_selection({"mode": mode})
        assert parsed.enabled is (mode != SESSION_SELECTION_OFF)
        assert parsed.applies is (mode == SESSION_SELECTION_APPLY)
        assert parsed.warns is (mode in (SESSION_SELECTION_WARN, SESSION_SELECTION_APPLY))


def test_an_unusable_platform_list_or_provider_map_falls_back_with_a_warning(caplog):
    caplog.set_level(logging.WARNING)

    assert parse_session_selection({"mode": "apply", "platforms": []}).platforms == (
        DEFAULT_SESSION_SELECTION_PLATFORMS
    )
    assert parse_session_selection({"mode": "apply", "platforms": 7}).platforms == (
        DEFAULT_SESSION_SELECTION_PLATFORMS
    )
    assert parse_session_selection({"mode": "apply", "platforms": "cli, WebUI"}).platforms == (
        "cli",
        "webui",
    )
    # A map that is not a mapping keeps the built-in default; an explicitly empty one is a
    # declaration, not an error.
    assert dict(parse_session_selection({"provider_map": "anthropic"}).provider_map) == dict(
        DEFAULT_SESSION_SELECTION_PROVIDER_MAP
    )
    assert dict(parse_session_selection({"provider_map": {"Anthropic": "opencode-go"}}).provider_map) == {
        NATIVE: "opencode-go"
    }
    assert dict(parse_session_selection({"provider_map": {}}).provider_map) == {}
    assert any("platforms" in record.message for record in caplog.records)


# -- the row reader ------------------------------------------------------------


def test_the_reader_returns_the_flat_selection_the_gateway_writes(profile):
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)

    read = selection.read_selection(profile, SESSION_ID)

    assert read == selection.Selection(
        model=HAIKU, provider=NATIVE, effort="medium", enabled=True
    )


def test_the_reader_opens_the_database_read_only(profile, monkeypatch):
    """``mode=ro`` through a URI: the plugin must never be able to write the host's state."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    seen = {}
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        seen["database"] = database
        seen["uri"] = kwargs.get("uri")
        seen["timeout"] = kwargs.get("timeout")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(selection.sqlite3, "connect", spy)

    assert selection.read_selection(profile, SESSION_ID) is not None
    assert seen["database"].startswith("file:")
    assert seen["database"].endswith("?mode=ro")
    assert seen["uri"] is True
    assert seen["timeout"] == pytest.approx(0.05)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        json.dumps({"model": HAIKU}),
        json.dumps({"model": "", "provider": NATIVE}),
        json.dumps({"provider": NATIVE}),
        json.dumps({"browser_model_lock": {"model": HAIKU, "provider": NATIVE}}),
        json.dumps({"follow_profile_config": True, "model": HAIKU}),
        json.dumps({"room_plumbing": {"x": 1}, "model": HAIKU, "provider": NATIVE}),
    ],
)
def test_unusable_rows_are_no_evidence(raw, caplog):
    caplog.set_level(logging.DEBUG)
    assert selection.parse_model_config(raw) is None


def test_a_row_without_reasoning_reports_no_level_rather_than_a_default():
    parsed = selection.parse_model_config(json.dumps({"model": SONNET, "provider": NATIVE}))

    assert parsed == selection.Selection(model=SONNET, provider=NATIVE, effort=None, enabled=None)


def test_a_missing_database_or_row_is_no_evidence(profile):
    assert selection.read_selection(profile, SESSION_ID) is None
    write_row(profile, {"model": HAIKU, "provider": NATIVE}, session_id="another-session")
    assert selection.read_selection(profile, SESSION_ID) is None


def test_a_locked_database_fails_open_within_the_timeout(profile):
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    writer = sqlite3.connect(str(profile / "state.db"), isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        assert selection.read_selection(profile, SESSION_ID, timeout_s=0.05) is None
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_the_reader_caches_by_turn_so_a_tool_loop_reads_the_row_once(profile, monkeypatch):
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    calls = []
    real_reader = selection.read_selection

    def counting(home, session_id, **kwargs):
        calls.append(session_id)
        return real_reader(home, session_id, **kwargs)

    reader = selection.SelectionReader(reader=counting)

    first = reader.read(profile, SESSION_ID, "turn-a")
    again = reader.read(profile, SESSION_ID, "turn-a")
    other = reader.read(profile, SESSION_ID, "turn-b")

    assert first == again == other
    assert calls == [SESSION_ID, SESSION_ID]  # one read per turn, not per request


def test_the_profile_home_comes_from_the_host_and_falls_back_to_the_data_directory(
    tmp_path, monkeypatch
):
    host = tmp_path / "host-profile"
    host.mkdir()
    data_dir = tmp_path / "profiles" / "job-search" / "plugin-data" / "agent-plugin-jev-1"
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(selection, "_context_home", lambda: host)
    assert selection.profile_home(data_dir) == host

    monkeypatch.setattr(selection, "_context_home", lambda: None)
    assert selection.profile_home(data_dir) == tmp_path / "profiles" / "job-search"
    assert selection.profile_home(None) is None
    assert selection.profile_home("relative/plugin-data/x") is None


# -- T1: the diagnosed race ----------------------------------------------------


def test_the_job_search_race_turn_runs_on_the_sessions_haiku_not_the_profiles_sonnet(
    profile, tmp_path, caplog
):
    """T1 — request Sonnet/high, decider low_confidence, row Haiku/anthropic/medium.

    The turn must go out as ``claude-haiku-5-5`` with ``output_config.effort=medium`` on the native
    route, and the audit must say the skip was reconciled and why.
    """
    caplog.set_level(logging.WARNING)
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    result = call(router)

    assert result is not None
    routed = result["request"]
    assert routed["model"] == HAIKU
    assert routed["output_config"] == {"effort": "medium"}
    assert "reasoning_effort" not in routed
    assert routed["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert result["source"] == "jev-effort-router"

    skip = last_skip(router, settings)
    assert skip["reason"] == "low_confidence"
    assert skip["reconciled_model"] == HAIKU
    assert skip["reconciled_effort"] == "medium"
    assert skip["selection_source"] == "session_row"
    assert skip["session_id"] == SESSION_ID

    divergence = events(router, settings, "selection_divergence")[-1]
    assert divergence["session_model"] == HAIKU
    assert divergence["session_provider"] == NATIVE
    assert divergence["session_effort"] == "medium"
    assert divergence["mapped_provider"] == NATIVE
    assert divergence["request_model"] == SONNET
    assert divergence["request_effort"] == "high"
    assert divergence["action"] == "reconcile_on_fail_open"
    assert any(
        "the session chose" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_the_reconciled_body_keeps_the_transports_containers(profile, tmp_path):
    """T1b — the rewritten body keeps the Messages shape the transport built."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    router, _ = build(tmp_path, transport, config=_config("apply"))

    routed = call(router)["request"]

    # The transport authored ``thinking`` and ``output_config``; the plugin rewrites the level
    # inside the object that already exists and adds no other container.
    assert routed["output_config"] == {"effort": "medium"}
    assert routed["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "extra_body" not in routed and "reasoning_effort" not in routed
    assert set(routed) == {
        "model",
        "messages",
        "max_tokens",
        "system",
        "tools",
        "thinking",
        "output_config",
    }


def test_the_second_call_of_the_turn_replays_the_same_rewrite(profile, tmp_path):
    """T2 — the negative memo replays the skip; the replay reconciles identically."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    first = call(router)
    after_first = transport.call_count
    second = call(router, turn_id="turn-1", api_call_count=1, api_request_id="turn-1:api:1")

    assert json.dumps(second["request"], sort_keys=True) == json.dumps(first["request"], sort_keys=True)
    assert transport.call_count == after_first
    replays = [record for record in events(router, settings, "skip") if record.get("replayed")]
    assert replays and replays[-1]["reconciled_model"] == HAIKU
    assert len(events(router, settings, "selection_divergence")) == 1


# -- T3/T4: the modes ----------------------------------------------------------


def test_mode_off_never_reads_the_database_and_records_nothing_new(profile, tmp_path, monkeypatch):
    """T3 — the default must be indistinguishable from the plugin without this feature."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)

    def forbidden(*args, **kwargs):
        raise AssertionError("mode: off must not open the state database")

    monkeypatch.setattr(selection.sqlite3, "connect", forbidden)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("off"))

    assert call(router) is None
    assert events(router, settings, "selection_divergence") == []
    assert events(router, settings, "selection_unmappable") == []
    assert events(router, settings, "selection_native_provider") == []

    skip = last_skip(router, settings)
    assert set(skip) == {
        "ts",
        "event",
        "reason",
        "provider",
        "configured_model",
        "turn_id",
        "session_id",
        "api_request_id",
        "platform",
    }


def test_mode_warn_reports_the_divergence_and_leaves_the_request_alone(
    profile, tmp_path, caplog
):
    """T4 — the audit event plus a WARNING, and no rewrite."""
    caplog.set_level(logging.WARNING)
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("warn"))

    assert call(router) is None

    divergence = events(router, settings, "selection_divergence")[-1]
    assert divergence["action"] == "watch"
    assert divergence["session_model"] == HAIKU
    assert divergence["request_model"] == SONNET
    skip = last_skip(router, settings)
    assert "reconciled_model" not in skip and "selection_source" not in skip
    assert any(
        "the session chose" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )


# -- T5: the decider still wins ------------------------------------------------


def test_a_successful_decision_wins_and_the_divergence_is_only_audited(profile, tmp_path):
    """T5 — the decider's answer is the route; the row is a fallback for fail-open turns."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = StubTransport([StubResponse(decision_payload("3", 0.95, "low", 0.9, grid_size=3))])
    router, settings = build(tmp_path, transport, config=_config("apply"))

    result = call(router)

    assert result["request"]["model"] == OPUS
    assert result["request"]["output_config"]["effort"] == "low"
    assert events(router, settings, "route")[-1]["model"] == OPUS
    assert events(router, settings, "selection_divergence")[-1]["action"] == "reconcile_on_fail_open"
    assert all("reconciled_model" not in record for record in records(router, settings))


# -- T6/T7: eligibility and silence -------------------------------------------


def test_a_selection_for_another_provider_is_unmappable_and_changes_nothing(profile, tmp_path):
    """T6a — the provider the row names does not translate to the request's provider."""
    write_row(
        profile,
        {"model": HAIKU, "provider": "openai-codex", "reasoning_config": {"effort": "medium"}},
    )
    transport = low_confidence_transport()
    config = _config("apply")
    config["session_selection"] = {
        "mode": "apply",
        "provider_map": {"openai-codex": "some-other-route"},
    }
    router, settings = build(tmp_path, transport, config=config)

    assert call(router) is None

    unmappable = events(router, settings, "selection_unmappable")[-1]
    assert unmappable["reason"] == "provider_mismatch"
    assert unmappable["session_model"] == HAIKU
    assert unmappable["mapped_provider"] == "some-other-route"
    assert "reconciled_model" not in last_skip(router, settings)


def test_a_model_outside_the_routes_catalog_is_unmappable(profile, tmp_path):
    """T6b — the route pins its own catalog; a model it does not serve is never put on the wire."""
    write_row(profile, {"model": "claude-sonnet-4-0", "provider": NATIVE, "reasoning_config": {"effort": "medium"}})
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    assert call(router) is None
    assert events(router, settings, "selection_unmappable")[-1]["reason"] == "model_outside_catalog"


def test_a_model_outside_the_surfaces_grid_is_unmappable(profile, tmp_path):
    """T6c — the grid is the choice set: a model the route serves but this surface forbids stays out."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    config = _config("apply", grids={NATIVE: [f"{SONNET}: the only model this surface offers"]})
    router, settings = build(tmp_path, transport, config=config)

    assert call(router) is None
    assert events(router, settings, "selection_unmappable")[-1]["reason"] == "model_outside_grid"


def test_a_row_that_agrees_with_the_request_produces_no_divergence(profile, tmp_path):
    """T7 — silence when there is nothing to report.

    ``apply`` still routes the fail-open turn through the session's selection (one rule per turn,
    not one rule per divergence), but with the row agreeing with the request the payload comes out
    as it went in, and no divergence is claimed.
    """
    write_row(
        profile,
        {
            "model": SONNET,
            "provider": NATIVE,
            "reasoning_config": {"enabled": True, "effort": "high"},
        },
    )
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    result = call(router)

    assert events(router, settings, "selection_divergence") == []
    assert result["request"]["model"] == SONNET
    assert wire_level(result["request"]) == "high"


# -- T8: every unreadable row fails open ---------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"browser_model_lock": {"model": HAIKU}}),
        json.dumps({"follow_profile_config": True}),
        json.dumps({"room_plumbing": {}, "model": HAIKU, "provider": NATIVE}),
    ],
)
def test_an_unusable_row_keeps_todays_fail_open(raw, profile, tmp_path):
    """T8 — the payload and the record are what a deployment without the row produces."""
    write_row(profile, raw)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    assert call(router) is None
    assert events(router, settings, "selection_divergence") == []
    assert events(router, settings, "selection_unmappable") == []
    assert "reconciled_model" not in last_skip(router, settings)


def test_a_missing_database_keeps_todays_fail_open(profile, tmp_path):
    """T8 — no ``state.db`` at all is the ordinary case on a fresh profile."""
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    assert call(router) is None
    assert "reconciled_model" not in last_skip(router, settings)


def test_a_locked_database_keeps_todays_fail_open(profile, tmp_path):
    """T8 — a database the dashboard is writing to must cost the turn nothing."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    writer = sqlite3.connect(str(profile / "state.db"), isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        assert call(router) is None
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert "reconciled_model" not in last_skip(router, settings)


# -- T9/T10: what the level may and may not do ---------------------------------


def test_a_payload_without_an_effort_container_gets_the_model_and_no_invented_field(
    profile, tmp_path
):
    """T9 — the transport built no ``output_config`` (reasoning disabled): nothing is invented.

    The reconciled model still reaches the wire; the level does not, because the container the
    level lives in is the transport's and this router never creates it. The audit reports the
    omission rather than a level nobody wrote.
    """
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    routed = call(router, anthropic_request(model=SONNET, thinking=False))["request"]

    assert routed["model"] == HAIKU
    assert "output_config" not in routed and "thinking" not in routed
    assert wire_level(routed) is None
    assert last_skip(router, settings)["reconciled_effort"] is None


def test_a_level_above_the_routes_family_cap_is_omitted_not_clamped(profile, tmp_path):
    """T10 — Sonnet's cap is ``high``; the row's ``max`` is omitted, and the audit says so."""
    write_row(
        profile,
        {
            "model": SONNET,
            "provider": NATIVE,
            "reasoning_config": {"enabled": True, "effort": "max"},
        },
    )
    transport = StubTransport([StubResponse(decision_payload("3", 0.10, "high", 0.12, grid_size=3))])
    router, settings = build(tmp_path, transport, config=_config("apply"))

    routed = call(router, model=OPUS)["request"]

    assert routed["model"] == SONNET
    # The omitted level leaves an empty ``output_config``, which is dropped rather than sent as {}.
    assert "output_config" not in routed
    assert wire_level(routed) is None
    skip = last_skip(router, settings)
    assert skip["reconciled_model"] == SONNET
    assert skip["reconciled_effort"] is None


def test_a_surface_max_effort_still_caps_the_sessions_level(profile, tmp_path):
    """The operator's ceiling outranks the row: ``high`` becomes ``medium``."""
    write_row(
        profile,
        {
            "model": HAIKU,
            "provider": NATIVE,
            "reasoning_config": {"enabled": True, "effort": "high"},
        },
    )
    transport = low_confidence_transport()
    config = _config("apply", platform_overrides={"webui": {"max_effort": "medium"}})
    router, _ = build(tmp_path, transport, config=config)

    routed = call(router)["request"]

    assert routed["model"] == HAIKU
    assert wire_level(routed) == "medium"


# -- T11/T12: what is not reconciled -------------------------------------------


def test_a_native_anthropic_turn_is_only_reported(profile, tmp_path, caplog):
    """T11 — out of the race the plugin has no lever: warn, record, and touch nothing."""
    caplog.set_level(logging.WARNING)
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = StubTransport([])
    router, settings = build(
        tmp_path, transport, config=_config("apply", routable_providers=["openai-codex"])
    )
    request = anthropic_request(model=SONNET, effort="high")

    assert call(router, request, provider=NATIVE) is None

    native = events(router, settings, "selection_native_provider")[-1]
    assert native["session_model"] == HAIKU
    assert native["session_provider"] == NATIVE
    assert native["session_effort"] == "medium"
    assert native["mapped_provider"] == NATIVE
    assert native["action"] == "none"
    assert last_skip(router, settings)["reason"] == "provider_not_routed"
    assert "reconciled_model" not in last_skip(router, settings)
    assert transport.call_count == 0
    assert any("native" in record.message and record.levelno == logging.WARNING for record in caplog.records)


def test_a_surface_outside_the_platforms_list_never_reads_the_row(
    profile, tmp_path, monkeypatch
):
    """T12 — the row is authoritative only on the surfaces that declared it."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)

    def forbidden(*args, **kwargs):
        raise AssertionError("a surface outside session_selection.platforms must not read the row")

    monkeypatch.setattr(selection.sqlite3, "connect", forbidden)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    assert call(router, platform="cli") is None
    assert events(router, settings, "selection_divergence") == []
    assert "reconciled_model" not in last_skip(router, settings)


def test_an_alias_selection_reconciles_on_the_native_route(profile, tmp_path):
    """``provider_map`` is additive: identity, aliases included, needs no translation.

    The owner's decision (card t_ec2453b1) routes the native ``anthropic`` route, so a selection
    naming ``anthropic`` — or any alias of it — must be usable on an ``anthropic`` request. Reading
    the map as "only valid in" turned it into ``selection_unmappable/provider_mismatch`` and left
    the turn on the profile's default.
    """
    write_row(profile, {"model": HAIKU, "provider": "claude", "reasoning_config": {"enabled": True, "effort": "medium"}})
    transport = low_confidence_transport()
    config = _config("apply")
    router, settings = build(tmp_path, transport, config=config)

    result = call(router, anthropic_request(model=SONNET, effort="high"))

    assert result is not None
    assert result["request"]["model"] == HAIKU
    assert events(router, settings, "selection_unmappable") == []
    assert last_skip(router, settings)["reconciled_model"] == HAIKU
    assert last_skip(router, settings)["selection_source"] == "session_row"


def test_the_gates_are_never_overridden_by_a_selection(profile, tmp_path, monkeypatch):
    """``disabled`` and ``api_mode_not_routed`` are decisions, not failures to patch over.

    Both gates also answer *before* the row is read, which is what keeps them authoritative: a
    turn the router does not own must not even look at the session's choice.
    """
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()

    def forbidden(*args, **kwargs):
        raise AssertionError("a gated turn must not read the session row")

    monkeypatch.setattr(selection.sqlite3, "connect", forbidden)

    disabled, disabled_settings = build(tmp_path / "a", transport, config=_config("apply", enabled=False))
    assert call(disabled) is None
    assert last_skip(disabled, disabled_settings)["reason"] == "disabled"

    other_mode, other_settings = build(tmp_path / "b", StubTransport([]), config=_config("apply"))
    assert call(other_mode, api_mode="bedrock_converse") is None
    record = last_skip(other_mode, other_settings)
    assert record["reason"] == "api_mode_not_routed"
    assert "reconciled_model" not in record


def test_a_failed_reconciliation_still_records_the_skip(profile, tmp_path, monkeypatch):
    """A bug in the reconciliation degrades to today's fail-open, never to an exception."""
    write_row(profile, JOB_SEARCH_MODEL_CONFIG)
    transport = low_confidence_transport()
    router, settings = build(tmp_path, transport, config=_config("apply"))

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(router, "_reconciled", explode)

    assert call(router) is None
    record = last_skip(router, settings)
    assert record["reason"] == "low_confidence"
    assert "reconciled_model" not in record
