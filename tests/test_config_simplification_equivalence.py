"""The 50-line profile block and the 9-line one must route identically.

``docs/SPEC-simplificacao-config.md`` §7 makes the simplification admissible: the operator's
``grids`` and ``effort_maps`` may leave every profile **only** because the code already holds the
same declarations — the route catalogs in ``grid.py`` and the built-in effort policies in
``effort.py``. This module is that equivalence proof, and it is what keeps the promise honest:
if a catalog id, a description, an offered level or a resolved wire value ever drifts between the
two spellings, one profile keeps working and the other silently changes behaviour.

The fixtures are the real blocks: ``jev_settings_legacy.json`` transcribes
``/home/rafael/hermes/home/profiles/planner/config.yaml`` lines 170-219 verbatim (the YAML block is
JSON-shaped, so the comparison does not need PyYAML), and ``jev_settings_target_minimal.json`` is
§2's target block. They are compared setting by setting, provider by provider, and model × level
for the whole :data:`effort.LADDER`.

The single permitted difference is the ``description`` of the two DeepSeek entries: the operator's
grid says "general work, explanation, ordinary coding and tool use", the catalog says "the usual
choice for general work: …". That text is a criterion Jev reads, and it never reaches the wire,
because a one-model grid sends no model question (``decider.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import load_settings
from effort import LADDER, offered_levels, offered_levels_for_grid, resolve_effort
from router import wire_model_for

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEGACY = "jev_settings_legacy.json"
MINIMAL = "jev_settings_target_minimal.json"

#: Every route the profiles list, so the comparison covers the whole `routable_providers` gate.
PROVIDERS = ("opencode-go", "commandcode", "openai-codex", "anthropic")

#: The two descriptions allowed to differ, as ``(provider, model id)``.
DEEPSEEK_DESCRIPTION_EXCEPTION = frozenset(
    {
        ("opencode-go", "deepseek-v4.1-flash"),
        ("commandcode", "deepseek/deepseek-v4.1-flash"),
    }
)

ANTHROPIC_CAPPED = ("claude-opus-5-5", "claude-sonnet-5-5")
ANTHROPIC_HAIKU = "claude-haiku-5-5"


def settings_from(name: str):
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    # ``_comment`` is documentation, not a setting: dropping it here keeps the loader honest.
    raw.pop("_comment", None)
    return load_settings(lambda key, default=None: raw.get(key, default))


@pytest.fixture(scope="module")
def legacy():
    return settings_from(LEGACY)


@pytest.fixture(scope="module")
def minimal():
    return settings_from(MINIMAL)


# -- the fixtures really are the two blocks ------------------------------------------


def test_the_legacy_block_declares_what_the_target_block_drops(legacy, minimal):
    """Premise guard: the comparison is between a block that declares everything and one that
    declares only the operator's own choices — otherwise an equality test proves nothing."""
    assert set(legacy.grids) == set(PROVIDERS)
    assert set(legacy.effort_maps) == set(PROVIDERS) - {"anthropic"}
    assert not minimal.grids
    assert not minimal.effort_maps
    # The seven settings that stay are the ones the operator must choose, plus the three that the
    # code's own defaults would get wrong for this deployment (`decider`, the credential name).
    assert minimal.decider == legacy.decider == "jev_console"
    assert minimal.console_api_key_env == legacy.console_api_key_env == "OPENCODE_GO_API_KEY"


# -- §7, item by item ----------------------------------------------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_grid_for_yields_the_same_model_ids(provider, legacy, minimal):
    assert [entry.model_id for entry in legacy.grid_for(provider)] == [
        entry.model_id for entry in minimal.grid_for(provider)
    ]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_only_the_two_deepseek_descriptions_differ(provider, legacy, minimal):
    old = {entry.model_id: entry.description for entry in legacy.grid_for(provider)}
    new = {entry.model_id: entry.description for entry in minimal.grid_for(provider)}
    assert old.keys() == new.keys()

    for model_id, description in new.items():
        if (provider, model_id) in DEEPSEEK_DESCRIPTION_EXCEPTION:
            assert old[model_id] != description
            assert old[model_id].startswith("general work, explanation")
            continue
        assert old[model_id] == description


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_model_reaches_the_same_wire_id(provider, legacy, minimal):
    """The id the router *writes*, not the parsed grid's own ``wire_id``.

    ``router.wire_model_for`` reads the route catalog first by design (a parsed operator grid
    carries descriptions only, so it cannot say what an id means on the wire — the Codex ``-900k``
    label is stripped by the transport, and writing the label back would send an id the backend
    does not serve). The catalog answers in both configurations, which is exactly the equality
    that matters: same picker id in ⇒ same provider id out.
    """
    legacy_effective = legacy.effective(provider)
    minimal_effective = minimal.effective(provider)

    for entry in minimal.grid_for(provider):
        assert wire_model_for(provider, legacy_effective, entry.model_id) == wire_model_for(
            provider, minimal_effective, entry.model_id
        )


def test_the_codex_picker_alias_still_maps_to_the_served_id(legacy, minimal):
    """The parsed legacy grid carries no ``wire_id`` for ``gpt-6-luna-900k``; the catalog does."""
    for settings in (legacy, minimal):
        effective = settings.effective("openai-codex")
        assert (
            wire_model_for("openai-codex", effective, "gpt-6-luna-900k") == "gpt-6-luna"
        )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_degraded_model_is_the_same(provider, legacy, minimal):
    assert (
        legacy.effective(provider).default_model
        == minimal.effective(provider).default_model
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_configured_default_effort_is_the_same(provider, legacy, minimal):
    assert (
        legacy.effective(provider).default_effort
        == minimal.effective(provider).default_effort
        == "medium"
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_effort_question_offers_the_same_levels(provider, legacy, minimal):
    assert offered_levels_for_grid(
        legacy.effective(provider).grid, provider, legacy.effort_maps
    ) == offered_levels_for_grid(
        minimal.effective(provider).grid, provider, minimal.effort_maps
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_every_model_and_level_resolves_to_the_same_wire_value(provider, legacy, minimal):
    """The omission case is ``None`` in both — a level above a cap is dropped, never clamped."""
    models = [entry.model_id for entry in minimal.grid_for(provider)]
    assert models

    for model_id in models:
        for level in LADDER:
            assert resolve_effort(
                model_id, level, provider=provider, effort_maps=legacy.effort_maps
            ) == resolve_effort(
                model_id, level, provider=provider, effort_maps=minimal.effort_maps
            ), (provider, model_id, level)


def test_the_commandcode_fallback_is_the_same(legacy, minimal):
    old = legacy.active_commandcode_fallback()
    new = minimal.active_commandcode_fallback()

    assert (old.url, old.model, old.key_env, old.enabled) == (
        new.url,
        new.model,
        new.key_env,
        new.enabled,
    )
    assert new.enabled is True
    assert (new.url, new.model, new.key_env) == (
        "https://api.commandcode.ai/provider/v1/systemone",
        "typesafe/jev",
        "COMMANDCODE_API_KEY",
    )


def test_the_decider_identity_is_the_same(legacy, minimal):
    assert legacy.decider_model_for() == minimal.decider_model_for()
    assert legacy.base_url_for() == minimal.base_url_for()
    assert legacy.api_key_env_for() == minimal.api_key_env_for()
    assert legacy.console_jev_fallback_model == minimal.console_jev_fallback_model == ""
    assert legacy.timeout_s == minimal.timeout_s == 3.0
    # ``''`` is load-bearing: it is what keeps the paid jev-1.13 leg off.
    assert minimal.console_jev_fallback_model == ""


def test_the_session_selection_and_the_routable_gate_are_the_same(legacy, minimal):
    assert (
        legacy.session_selection.mode,
        legacy.session_selection.platforms,
    ) == (
        minimal.session_selection.mode,
        minimal.session_selection.platforms,
    )
    assert (minimal.session_selection.mode, minimal.session_selection.platforms) == (
        "apply",
        ("webui",),
    )
    assert legacy.routable_providers == minimal.routable_providers
    for provider in PROVIDERS:
        assert legacy.is_provider_routable(provider) is minimal.is_provider_routable(provider) is True


# -- the caps, spelled out -----------------------------------------------------------


def test_the_anthropic_caps_hold_in_both_configurations(legacy, minimal):
    for settings in (legacy, minimal):
        for model_id in ANTHROPIC_CAPPED:
            assert offered_levels("anthropic", model_id, settings.effort_maps) == (
                "low",
                "medium",
                "high",
            )
        assert offered_levels("anthropic", ANTHROPIC_HAIKU, settings.effort_maps) == (
            "low",
            "medium",
            "high",
            "max",
        )


def test_a_level_above_the_anthropic_cap_is_omitted_and_never_clamped(legacy, minimal):
    for settings in (legacy, minimal):
        for model_id in ANTHROPIC_CAPPED:
            for level in ("xhigh", "max"):
                assert (
                    resolve_effort(
                        model_id, level, provider="anthropic", effort_maps=settings.effort_maps
                    )
                    is None
                )
        # The level the family does accept still goes out verbatim.
        assert (
            resolve_effort(
                "claude-opus-5-5", "high", provider="anthropic", effort_maps=settings.effort_maps
            )
            == "high"
        )
