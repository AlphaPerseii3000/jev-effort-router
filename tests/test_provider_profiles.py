"""Provider profiles: slot mapping, the provider gate, per-profile fallback and the catalog.

The regression that matters is identity: with no ``provider_profiles`` configured, the request is
the same six Ollama:Cloud entries in the same order with the same criteria as 0.3.2. The new
behaviour is that an operator can declare a profile for another provider and have that provider's
own models chosen — without the router ever switching providers or sending Ollama ids elsewhere.
"""

from __future__ import annotations

import json

import pytest

from config import DEFAULT_MODEL, load_settings
from grid import DEFAULT_GRID, SLOT_DESCRIPTIONS, SLOTS, Entry
from profiles import (
    DEFAULT_PROVIDER,
    ProviderProfile,
    normalize_provider,
    parse_provider_profile,
    parse_provider_profiles,
)
from stubs import StubResponse, StubTransport, decision_payload
from test_router import build, route
from tools import _route, _status
from commands import _route_text


OPENROUTER = {
    "provider_profiles": {
        "openrouter": {
            "models": {
                "general": "vendor/general",
                "deep-code": "vendor/code",
                "rigorous": "vendor/rigorous",
                "long-text": "vendor/long",
                "tool-throughput": "vendor/tools",
                "trivial": "vendor/trivial",
            },
            "fallback": "vendor/trivial",
        }
    }
}


def settings_for(config=None):
    return load_settings(lambda key, default=None: (config or {}).get(key, default))


def audit_records(tmp_path):
    path = tmp_path / "routes.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- identity ------------------------------------------------------------------------


def test_the_default_profile_is_the_measured_grid_in_the_measured_order():
    settings = settings_for()
    profile = settings.profile_for(DEFAULT_PROVIDER)

    assert profile is not None
    assert profile.source == "builtin"
    assert profile.entries == DEFAULT_GRID
    assert profile.slots == SLOTS
    assert profile.fallback_model == settings.default_model
    # The payload is unchanged: the criterion still starts with the model id and the same text.
    assert profile.entries[0].criterion == DEFAULT_GRID[0].criterion


def test_the_default_provider_spellings_resolve_to_the_same_profile():
    settings = settings_for()

    assert normalize_provider("ollama_cloud") == DEFAULT_PROVIDER
    assert settings.profile_for("ollama_cloud") is not None
    assert settings.profile_for("OLLAMA-CLOUD") is not None


def test_an_unknown_provider_has_no_profile():
    settings = settings_for()

    assert settings.profile_for("openrouter") is None
    assert settings.profile_for("") is not None  # empty resolves to the default


def test_adding_another_profile_does_not_move_the_default_profile():
    settings = settings_for(OPENROUTER)

    default = settings.profile_for(DEFAULT_PROVIDER)
    assert default is not None
    assert default.entries == DEFAULT_GRID
    assert settings.grid == DEFAULT_GRID
    assert settings.default_model == DEFAULT_MODEL


# -- parsing an operator profile -----------------------------------------------------


def test_a_full_profile_maps_each_slot_and_keeps_the_measured_descriptions():
    profile = parse_provider_profile("openrouter", OPENROUTER["provider_profiles"]["openrouter"])

    assert profile is not None
    assert profile.name == "openrouter"
    assert profile.slots == SLOTS
    assert [entry.model_id for entry in profile.entries] == [
        "vendor/general",
        "vendor/code",
        "vendor/rigorous",
        "vendor/long",
        "vendor/tools",
        "vendor/trivial",
    ]
    assert profile.entries[1].description == SLOT_DESCRIPTIONS["deep-code"]
    assert profile.fallback_model == "vendor/trivial"


def test_a_bare_slot_map_is_accepted():
    profile = parse_provider_profile("openrouter", {"general": "vendor/general"})

    assert profile is not None
    assert profile.entries[0].model_id == "vendor/general"
    assert profile.fallback_model == "vendor/general"


def test_a_profile_can_override_a_description():
    profile = parse_provider_profile(
        "openrouter",
        {"models": {"deep-code": "vendor/code: handles multi-file refactors"}},
    )

    assert profile is not None
    assert profile.entries[0] == Entry("vendor/code", "handles multi-file refactors")


def test_a_model_id_with_a_colon_survives_parsing():
    profile = parse_provider_profile(
        "openrouter", {"models": {"trivial": "nemotron-3-nano:30b"}}
    )

    assert profile is not None
    assert profile.entries[0].model_id == "nemotron-3-nano:30b"
    assert profile.entries[0].description == SLOT_DESCRIPTIONS["trivial"]


def test_a_list_is_read_positionally():
    profile = parse_provider_profile("openrouter", ["vendor/general", "vendor/code"])

    assert profile is not None
    assert profile.slots == ("general", "deep-code")
    assert [entry.model_id for entry in profile.entries] == ["vendor/general", "vendor/code"]


def test_an_off_grid_or_absent_fallback_resolves_inside_the_profile():
    general = parse_provider_profile(
        "openrouter",
        {"models": {"general": "vendor/general", "deep-code": "vendor/code"}},
    )
    first = parse_provider_profile("openrouter", {"models": {"deep-code": "vendor/code"}})

    assert general is not None and general.fallback_model == "vendor/general"
    assert first is not None and first.fallback_model == "vendor/code"


def test_duplicate_models_keep_their_first_slot():
    profile = parse_provider_profile(
        "openrouter",
        {"models": {"general": "vendor/one", "deep-code": "vendor/one", "rigorous": "vendor/two"}},
    )

    assert profile is not None
    assert [(slot, entry.model_id) for slot, entry in zip(profile.slots, profile.entries)] == [
        ("general", "vendor/one"),
        ("rigorous", "vendor/two"),
    ]


@pytest.mark.parametrize(
    "spec",
    [
        None,
        {},
        {"models": {}},
        {"models": []},
        {"models": {"general": "???"}},
        {"models": {"general": ""}},
        {"models": 42},
    ],
)
def test_an_unusable_profile_is_dropped_not_guessed(spec):
    assert parse_provider_profile("openrouter", spec) is None


def test_only_the_usable_profiles_survive():
    profiles = parse_provider_profiles(
        {
            "openrouter": OPENROUTER["provider_profiles"]["openrouter"],
            "broken": {"models": {"general": "???"}},
            "ollama_cloud": {"models": {"general": "ollama/general"}},
        }
    )

    assert set(profiles) == {"openrouter", DEFAULT_PROVIDER}
    assert profiles[DEFAULT_PROVIDER].entries[0].model_id == "ollama/general"


# -- the router ----------------------------------------------------------------------


def test_a_configured_provider_routes_to_its_own_slot_mapping(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, _ = build(tmp_path, transport, config=OPENROUTER)

    result = route(router, provider="openrouter", model="vendor/general")

    assert result is not None
    assert result["request"]["model"] == "vendor/code"
    # The effort path is unchanged in phase 1; only the model mapping moved.
    assert result["request"]["reasoning_effort"] == "high"


def test_an_unknown_provider_still_passes_through_untouched(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, _ = build(tmp_path, transport, config=OPENROUTER)

    result = route(router, provider="anthropic", model="claude-sonnet-5")

    assert result is None
    assert audit_records(tmp_path)[-1]["reason"] == "provider_not_routed"
    assert transport.calls == []


def test_the_configured_model_must_belong_to_that_profile(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, _ = build(tmp_path, transport, config=OPENROUTER)

    result = route(router, provider="openrouter", model="deepseek-v4.1-flash")

    assert result is None
    assert audit_records(tmp_path)[-1]["reason"] == "model_not_in_grid"
    assert transport.calls == []


def test_a_low_confidence_decision_falls_back_inside_the_profile(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("99", 0.99, "high", 0.8))])
    router, _ = build(tmp_path, transport, config=OPENROUTER)

    result = route(router, provider="openrouter", model="vendor/general")

    assert result is not None
    assert result["request"]["model"] == "vendor/trivial"
    assert "unknown_choice" in audit_records(tmp_path)[-1]["fallback_reasons"]


def test_a_surface_policy_still_caps_that_providers_grid(tmp_path):
    config = dict(OPENROUTER)
    config["platform_overrides"] = {
        "cron": {"grid": "vendor/general: general work\nvendor/code: handles multi-file refactors"}
    }
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "medium", 0.8))])
    router, _ = build(tmp_path, transport, config=config)

    result = route(
        router,
        provider="openrouter",
        model="vendor/general",
        platform="cron",
    )

    assert result is not None
    assert result["request"]["model"] == "vendor/code"


def test_the_router_checks_the_catalog_for_the_active_provider(tmp_path):
    calls = []

    class RecordingCatalog:
        def is_known(self, model_id, provider_prefixes=(), *, provider=""):
            calls.append((model_id, provider))
            return True

    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, _ = build(tmp_path, transport, config=OPENROUTER, catalog=RecordingCatalog())

    route(router, provider="openrouter", model="vendor/general")

    assert ("vendor/code", "openrouter") in calls


# -- the observability surfaces ------------------------------------------------------


def test_status_lists_the_default_profile_and_the_configured_one(tmp_path):
    transport = StubTransport([])
    router, settings = build(tmp_path, transport, config=OPENROUTER)

    payload = json.loads(_status(router, settings))

    assert payload["default_provider"] == "ollama-cloud"
    profiles = {row["provider"]: row for row in payload["provider_profiles"]}
    assert set(profiles) == {"ollama-cloud", "openrouter"}
    assert profiles["openrouter"]["fallback"] == "vendor/trivial"
    assert profiles["openrouter"]["slots"]["deep-code"] == "vendor/code"
    # The legacy `grid`/`fallback` view still describes the default profile.
    assert payload["grid"][0]["model"] == "deepseek-v4.1-flash"


def test_the_route_tool_can_target_a_provider(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, settings = build(tmp_path, transport, config=OPENROUTER)

    payload = json.loads(_route(router, settings, "refactor the payment module", provider="openrouter"))

    assert payload["routed"] is True
    assert payload["provider"] == "openrouter"
    assert payload["model"] == "vendor/code"


def test_the_route_tool_refuses_an_undeclared_provider(tmp_path):
    transport = StubTransport([])
    router, settings = build(tmp_path, transport, config=OPENROUTER)

    payload = json.loads(_route(router, settings, "anything", provider="anthropic"))

    assert payload["routed"] is False
    assert payload["reason"] == "provider_not_routed"
    assert transport.calls == []


def test_cli_route_text_names_the_provider(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, settings = build(tmp_path, transport, config=OPENROUTER)

    text = _route_text(router, settings, "refactor", provider="openrouter")

    assert text.startswith("provider: openrouter")
    assert "vendor/code" in text


# -- the catalog reader --------------------------------------------------------------


def test_the_generic_provider_cache_is_read_per_provider(tmp_path, monkeypatch):
    from catalog import ModelCatalog

    (tmp_path / "provider_models_cache.json").write_text(
        json.dumps(
            {
                "openrouter": {"models": ["vendor/code", "vendor/general"]},
                "ollama-cloud": {"models": ["glm-5.3"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The home resolver reads the host module first; force the environment fallback.
    monkeypatch.setattr("catalog._hermes_home", lambda: tmp_path)

    openrouter = ModelCatalog()
    assert openrouter.is_known("vendor/code", provider="openrouter") is True
    assert openrouter.is_known("glm-5.3", provider="openrouter") is False
    assert openrouter.is_known("glm-5.3", provider="ollama-cloud") is True


def test_the_legacy_ollama_cache_still_works(tmp_path, monkeypatch):
    from catalog import ModelCatalog

    (tmp_path / "ollama_cloud_models_cache.json").write_text(
        json.dumps({"models": ["glm-5.3"]}), encoding="utf-8"
    )
    (tmp_path / "provider_models_cache.json").write_text(
        json.dumps({"other": {"models": ["x"]}}), encoding="utf-8"
    )
    monkeypatch.setattr("catalog._hermes_home", lambda: tmp_path)

    assert ModelCatalog().is_known("glm-5.3") is True
    assert ModelCatalog().is_known("glm-5.3", provider="ollama-cloud") is True


def test_an_absent_catalog_is_no_evidence(tmp_path):
    from catalog import ModelCatalog

    assert ModelCatalog(tmp_path / "absent.json").is_known("anything", provider="openrouter") is None
