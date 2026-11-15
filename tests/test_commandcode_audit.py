"""Audit, replay and status for the opt-in CommandCode fallback.

Three contracts live here:

* the record names the model **and** the endpoint that actually answered — for the primary leg, the
  paid console leg and the fallback alike — and adds those two columns only where the trio is
  enabled, so a legacy deployment keeps the record shape 0.4.0 wrote;
* the trio is part of the memo's configuration identity: changing any of the three fields (or
  turning it on/off) invalidates a replay and fails open, and a replay never re-emits a provenance
  it did not measure;
* the status surface reports the fallback's configuration, endpoint, model and the *name* of its key
  variable — never the value.
"""

from __future__ import annotations

import json

import pytest

from client import JevClient
from config import load_settings
from decider import REASON_SKIPPED_PROVIDER, REASON_UPSTREAM_ERROR
from router import Router
from stubs import StubResponse, StubState, StubTransport, decision_payload, ollama_request
from tools import _status

CONSOLE_URL = "https://opencode.ai/inference/systemone/v1/systemone"
CONSOLE_PRIMARY = "jev-1.13-free"
CONSOLE_PAID = "jev-1.13"
CC_URL = "https://api.commandcode.ai/provider/v1/systemone"
CC_MODEL = "typesafe/jev"
CONSOLE_KEY = "console-key-secret-777"
CC_KEY = "cc-key-secret-888"
PROVIDER = "opencode-console"


def cc_config(**overrides):
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


class _NoCatalog:
    def is_known(self, *args, **kwargs):
        return None


def build(tmp_path, transport, config):
    """A real router whose settings can be swapped mid-test (to prove replay invalidation)."""
    holder = {"settings": load_settings(lambda key, default=None: config.get(key, default))}
    router = Router(
        lambda: holder["settings"],
        get_state=lambda: StubState(tmp_path),
        client_factory=lambda settings: JevClient(settings, transport=transport),
        catalog=_NoCatalog(),
    )
    return router, holder


def route(router, request=None, **overrides):
    kwargs = dict(
        turn_id="turn-1",
        session_id="session-1",
        platform="cli",
        model="deepseek-v4.1-flash",
        provider=PROVIDER,
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-1:api:0",
    )
    kwargs.update(overrides)
    request = request if request is not None else ollama_request()
    return router.on_llm_request(request, dict(request), **kwargs)


def replay(router, **overrides):
    overrides.setdefault("api_call_count", 1)
    overrides.setdefault("api_request_id", "turn-1:api:1")
    return route(router, **overrides)


def console_answer():
    return decision_payload("1", 0.95, "medium", 0.9, grid_size=1)


def records(router, holder):
    return router.tail(holder["settings"], limit=50)


def last(router, holder):
    found = records(router, holder)
    assert found, "the audit file must hold the attempt"
    return found[-1]


# -- provenance on the record --------------------------------------------------------


def test_the_primary_leg_records_the_console_model_and_endpoint(tmp_path):
    transport = StubTransport([StubResponse(console_answer())])
    router, holder = build(tmp_path, transport, cc_config())

    assert route(router) is not None

    record = last(router, holder)
    assert record["jev_model"] == CONSOLE_PRIMARY
    assert record["decider_model"] == CONSOLE_PRIMARY
    assert record["decider_endpoint"] == CONSOLE_URL
    assert record["decider_fallback_reason"] is None


def test_the_paid_console_leg_records_the_console_endpoint(tmp_path):
    transport = StubTransport([StubResponse({"error": "free quota"}, status_code=500), StubResponse(console_answer())])
    router, holder = build(tmp_path, transport, cc_config(console_jev_fallback_model=CONSOLE_PAID))

    assert route(router) is not None

    record = last(router, holder)
    assert record["jev_model"] == CONSOLE_PAID
    assert record["decider_model"] == CONSOLE_PAID
    assert record["decider_endpoint"] == CONSOLE_URL
    assert record["decider_fallback_reason"] == REASON_UPSTREAM_ERROR


def test_a_commandcode_rescue_records_the_effective_model_and_endpoint(tmp_path):
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), StubResponse(console_answer())])
    router, holder = build(tmp_path, transport, cc_config())

    assert route(router) is not None

    record = last(router, holder)
    assert record["decider"] == "jev_console"
    assert record["jev_model"] == CC_MODEL
    assert record["decider_model"] == CC_MODEL
    assert record["decider_endpoint"] == CC_URL
    assert record["decider_fallback_reason"] == REASON_UPSTREAM_ERROR
    assert record["replayed"] is False
    # The route itself is untouched: the fallback decides *which* model and effort, not what runs.
    assert record["model"] == "deepseek-v4.1-flash"


def test_a_legacy_decider_gains_no_columns_even_with_the_trio_configured(tmp_path):
    # `decider: jev` keeps its payload, its audit shape and its status output: the trio is a
    # jev_console feature, so configuring it elsewhere must not add a column to the record.
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, holder = build(
        tmp_path,
        transport,
        {
            "console_jev_commandcode_base_url": CC_URL,
            "console_jev_commandcode_api_key_env": "COMMANDCODE_API_KEY",
            "console_jev_commandcode_model": CC_MODEL,
        },
    )

    assert route(router, provider="ollama-cloud") is not None

    record = last(router, holder)
    assert "decider_model" not in record
    assert "decider_endpoint" not in record


def test_the_audit_never_carries_a_credential(tmp_path):
    transport = StubTransport(
        [
            StubResponse({"error": f"quota for key {CONSOLE_KEY}"}, status_code=429),
            StubResponse({"error": f"quota for key {CC_KEY}"}, status_code=429),
        ]
    )
    router, holder = build(tmp_path, transport, cc_config())
    assert route(router) is None

    text = (tmp_path / "routes.jsonl").read_text(encoding="utf-8")
    assert CONSOLE_KEY not in text
    assert CC_KEY not in text


# -- replay --------------------------------------------------------------------------


def test_a_replay_keeps_the_provenance_and_consults_nothing(tmp_path):
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), StubResponse(console_answer())])
    router, holder = build(tmp_path, transport, cc_config())

    assert route(router) is not None
    assert transport.call_count == 2
    assert replay(router) is not None

    record = last(router, holder)
    assert record["replayed"] is True
    assert record["jev_model"] == CC_MODEL
    assert record["decider_endpoint"] == CC_URL
    assert record["decider_fallback_reason"] == REASON_UPSTREAM_ERROR
    assert transport.call_count == 2


def test_a_memo_from_the_fallback_replays_without_the_console_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_CONSOLE_API_KEY")
    transport = StubTransport([StubResponse(console_answer())])
    router, holder = build(tmp_path, transport, cc_config())

    assert route(router) is not None
    assert transport.call_count == 1
    # The primary credential is absent, but this configuration's decision came from a transport it
    # can still reach, so the turn's memo stays replayable instead of re-deciding per API call.
    assert replay(router) is not None

    record = last(router, holder)
    assert record["replayed"] is True
    assert transport.call_count == 1


@pytest.mark.parametrize(
    "field, value",
    [
        ("console_jev_commandcode_base_url", "https://other.example.com/provider/v1/systemone"),
        ("console_jev_commandcode_model", "typesafe/jev-other"),
        ("console_jev_commandcode_api_key_env", "OTHER_COMMANDCODE_KEY"),
    ],
)
def test_changing_any_opt_in_field_invalidates_the_replay(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("OTHER_COMMANDCODE_KEY", CC_KEY)
    transport = StubTransport(
        [
            StubResponse({"error": "quota"}, status_code=429),
            StubResponse(console_answer()),
            # A fresh decision after the invalidation asks the console again, then the fallback.
            StubResponse({"error": "quota"}, status_code=429),
            StubResponse(console_answer()),
        ]
    )
    router, holder = build(tmp_path, transport, cc_config())

    assert route(router) is not None
    calls_before = transport.call_count

    holder["settings"] = load_settings(lambda key, default=None: {**cc_config(), field: value}.get(key, default))

    # The memo no longer matches the active configuration: the call falls open, it is not replayed
    # against the new endpoint and no request is rewritten from stale provenance.
    assert replay(router) is None
    skip = last(router, holder)
    assert skip["event"] == "skip"
    assert skip["reason"] == REASON_SKIPPED_PROVIDER
    assert transport.call_count == calls_before

    # The next turn decides again under the new configuration.
    assert route(router, turn_id="turn-2") is not None
    assert transport.call_count > calls_before


def test_turning_the_trio_off_invalidates_a_memo_written_with_it(tmp_path):
    transport = StubTransport([StubResponse({"error": "quota"}, status_code=429), StubResponse(console_answer())])
    router, holder = build(tmp_path, transport, cc_config())
    assert route(router) is not None

    off = cc_config(
        console_jev_commandcode_base_url="",
        console_jev_commandcode_api_key_env="",
        console_jev_commandcode_model="",
    )
    holder["settings"] = load_settings(lambda key, default=None: off.get(key, default))

    assert replay(router) is None
    assert last(router, holder)["reason"] == REASON_SKIPPED_PROVIDER


# -- status --------------------------------------------------------------------------


def test_status_reports_the_fallback_without_its_value(tmp_path):
    transport = StubTransport([])
    router, holder = build(tmp_path, transport, cc_config())

    payload = json.loads(_status(router, holder["settings"]))

    assert payload["commandcode_fallback"] == {
        "configured": True,
        "enabled": True,
        "endpoint": CC_URL,
        "model": CC_MODEL,
        "api_key_env": "COMMANDCODE_API_KEY",
        "api_key_present": True,
        "reason": None,
    }
    assert CC_KEY not in json.dumps(payload)
    assert CONSOLE_KEY not in json.dumps(payload)


def test_status_omits_the_block_when_the_trio_is_empty(tmp_path):
    transport = StubTransport([])
    router, holder = build(
        tmp_path,
        transport,
        cc_config(
            console_jev_commandcode_base_url="",
            console_jev_commandcode_api_key_env="",
            console_jev_commandcode_model="",
        ),
    )

    payload = json.loads(_status(router, holder["settings"]))

    assert "commandcode_fallback" not in payload


def test_status_shows_the_completed_defaults_for_a_url_only_trio(tmp_path):
    transport = StubTransport([])
    router, holder = build(tmp_path, transport, cc_config(console_jev_commandcode_api_key_env=""))

    block = json.loads(_status(router, holder["settings"]))["commandcode_fallback"]

    # The URL opted in, so the pair completes with the documented defaults and the fallback runs.
    assert block["configured"] is True
    assert block["enabled"] is True
    assert block["endpoint"] == CC_URL
    assert block["model"] == CC_MODEL
    assert block["api_key_env"] == "COMMANDCODE_API_KEY"
    assert block["reason"] is None


def test_status_shows_a_malformed_endpoint_as_configured_but_disabled(tmp_path):
    transport = StubTransport([])
    router, holder = build(
        tmp_path, transport, cc_config(console_jev_commandcode_base_url="http://api.commandcode.ai/provider/v1/systemone")
    )

    block = json.loads(_status(router, holder["settings"]))["commandcode_fallback"]

    assert block["configured"] is True
    assert block["enabled"] is False
    assert block["endpoint"] is None
    # Sanitized: the reason names the setting, never a value.
    assert "console_jev_commandcode_base_url" in block["reason"]


def test_status_reports_a_complete_trio_as_disabled_for_a_decider_that_cannot_use_it(tmp_path):
    transport = StubTransport([])
    router, holder = build(tmp_path, transport, cc_config(decider="jev", routable_providers=["ollama-cloud"]))

    block = json.loads(_status(router, holder["settings"]))["commandcode_fallback"]

    assert block["configured"] is True
    assert block["enabled"] is False
    assert block["reason"] is None
