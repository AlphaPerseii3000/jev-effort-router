"""The provider catalog: never put a model on the wire that the provider does not have.

A routing grid is static and a provider catalog is not. ``nemotron-3-nano`` aged out of the
Ollama:cloud catalog (the tier is ``nemotron-3-nano:30b``), and the stale grid entry turned a
confident decision into ``HTTP 404: model "nemotron-3-nano" not found`` — a dead turn, the one
outcome a router must never cause.
"""

from __future__ import annotations

import json

import pytest

from catalog import ModelCatalog
from stubs import StubResponse, StubTransport, decision_payload, ollama_request
from router import Router
from config import load_settings


def write_catalog(tmp_path, models, *, filename="ollama_cloud_models_cache.json"):
    payload = {"models": list(models), "cached_at": 0}
    (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path / filename


def catalog_for(tmp_path, models):
    """A catalog pinned to one file, so no test depends on the developer's real HERMES_HOME."""
    path = write_catalog(tmp_path, models) if models is not None else tmp_path / "absent.json"
    return ModelCatalog(path)


def build(tmp_path, transport, catalog):
    settings = load_settings(lambda _key, default=None: default)
    return (
        Router(
            lambda: settings,
            get_state=lambda: None,
            client_factory=lambda _settings: _Client(transport, settings),
            catalog=catalog,
        ),
        settings,
    )


def _Client(transport, settings):
    from client import JevClient

    return JevClient(settings, transport=transport)


def route(router, **overrides):
    kwargs = dict(
        turn_id="turn-1",
        session_id="session-1",
        platform="cli",
        model="deepseek-v4.1-flash",
        provider="ollama-cloud",
        api_mode="chat_completions",
        api_call_count=1,
        api_request_id="turn-1:api:1",
    )
    kwargs.update(overrides)
    request = ollama_request()
    return router.on_llm_request(request, dict(request), **kwargs)


# -- the catalog reader --------------------------------------------------------------


def test_reads_the_host_cache_shape(tmp_path):
    catalog = catalog_for(tmp_path, ["deepseek-v4.1-flash", "nemotron-3-nano:30b"])

    assert catalog.is_known("deepseek-v4.1-flash") is True
    assert catalog.is_known("nemotron-3-nano") is False


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"id": "a-model"}]},
        {"model_ids": ["a-model"]},
        ["a-model"],
        {"models": [{"model": "a-model"}]},
        {"models": [{"name": "a-model"}]},
    ],
)
def test_tolerates_the_shapes_a_catalog_might_take(tmp_path, payload):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert ModelCatalog(path).is_known("a-model") is True


def test_a_namespaced_entry_matches_through_the_prefix(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"models": ["nvidia/a-model"]}), encoding="utf-8")

    assert ModelCatalog(path).is_known("a-model", provider_prefixes=("nvidia",)) is True


@pytest.mark.parametrize("absent_or_broken", [True, False])
def test_no_evidence_is_not_a_verdict(tmp_path, absent_or_broken):
    """An absent or unreadable cache must never make the router start refusing models."""
    path = tmp_path / "cache.json"
    if not absent_or_broken:
        path.write_text("{ not json", encoding="utf-8")

    assert ModelCatalog(path).is_known("anything") is None


# -- the router's use of it ----------------------------------------------------------


def test_a_model_the_provider_lacks_leaves_the_turn_untouched(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("6", 0.99, "high", 0.9))])
    router, _ = build(tmp_path, transport, catalog_for(tmp_path, ["deepseek-v4.1-flash"]))

    assert route(router, model="deepseek-v4.1-flash") is None


def test_the_rejected_choice_is_recorded_with_its_reason(tmp_path, monkeypatch):
    transport = StubTransport([StubResponse(decision_payload("6", 0.99, "high", 0.9))])
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    catalog = ModelCatalog(write_catalog(tmp_path, ["deepseek-v4.1-flash"]))
    settings = load_settings(lambda _key, default=None: default)
    router = Router(
        lambda: settings,
        get_state=lambda: _State(audit_dir),
        client_factory=lambda _settings: _Client(transport, settings),
        catalog=catalog,
    )

    assert route(router) is None

    record = json.loads((audit_dir / "routes.jsonl").read_text(encoding="utf-8").strip())
    assert record["event"] == "skip"
    assert record["reason"] == "model_not_in_provider_catalog"
    assert record["chosen_model"] == "nemotron-3-nano:30b"


def test_a_model_the_provider_has_is_routed(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.9))])
    router, _ = build(tmp_path, transport, catalog_for(tmp_path, ["deepseek-v4.1-flash", "kimi-k3"]))

    result = route(router)

    assert result["request"]["model"] == "kimi-k3"


def test_an_unreadable_catalog_does_not_stop_routing(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.9))])
    router, _ = build(tmp_path, transport, catalog_for(tmp_path, None))

    result = route(router)

    assert result["request"]["model"] == "kimi-k3"


def test_replay_does_not_re_check_the_catalog(tmp_path):
    """The decision was already vetted when it was made; a replay must not call the catalog."""
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.9))])
    router, _ = build(tmp_path, transport, catalog_for(tmp_path, ["deepseek-v4.1-flash", "kimi-k3"]))

    first = route(router, api_call_count=1)
    router._catalog._models = ("deepseek-v4.1-flash",)  # catalog moves under us mid-turn
    second = route(router, api_call_count=2, api_request_id="turn-1:api:2")

    assert first["request"]["model"] == "kimi-k3"
    assert second["request"]["model"] == "kimi-k3"


def test_grid_report_flags_the_entries_the_provider_lacks(tmp_path):
    settings = load_settings(lambda _key, default=None: default)
    router = Router(
        lambda: settings,
        get_state=lambda: None,
        catalog=catalog_for(tmp_path, ["deepseek-v4.1-flash", "kimi-k3"]),
    )

    report = router.grid_report(settings)

    assert [row["model"] for row in report if row["available"]] == ["deepseek-v4.1-flash", "kimi-k3"]


def test_grid_report_is_empty_without_evidence(tmp_path):
    settings = load_settings(lambda _key, default=None: default)
    router = Router(
        lambda: settings,
        get_state=lambda: None,
        catalog=catalog_for(tmp_path, None),
    )

    assert router.grid_report(settings) == []


class _State:
    def __init__(self, data_dir):
        self.data_dir = data_dir
