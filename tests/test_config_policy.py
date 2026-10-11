"""The ``platform_overrides`` settings side: parse, resolve, and the compatibility backstops.

Every row of the ticket's I/O matrix that is about settings lands here. The router-side rows
(a disabled surface skips, an unlisted surface still routes) live in ``tests/test_router.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from config import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MODEL,
    EFFORT_LEVELS,
    PlatformPolicy,
    Settings,
    load_settings,
)
from grid import DEFAULT_GRID, Entry


def settings_for(config):
    return load_settings(lambda key, default=None: config.get(key, default))


# -- happy path --------------------------------------------------------------------


def test_a_cron_policy_reads_all_four_keys_and_resolves_once():
    settings = settings_for(
        {
            "platform_overrides": {
                "cron": {
                    "enabled": True,
                    "grid": "glm-5.3: rigorous reasoning",
                    "confidence_threshold": 0.7,
                    "max_effort": "medium",
                }
            }
        }
    )

    policy = settings.policy_for("cron")

    assert policy is not None
    assert policy.enabled is True
    assert policy.grid == (Entry("glm-5.3", "rigorous reasoning"),)
    assert policy.confidence_threshold == 0.7
    assert policy.max_effort == "medium"

    effective = settings.effective(policy)
    assert effective.grid == (Entry("glm-5.3", "rigorous reasoning"),)
    assert effective.confidence_threshold == 0.7
    assert effective.max_effort == "medium"
    # A policy that carried a parsed grid is a policy grid.
    assert effective.grid_from_policy is True
    # The rest of the snapshot is the global setting unchanged.
    assert effective.default_model == settings.default_model
    assert effective.default_effort == settings.default_effort


def test_a_policy_field_may_inherit_and_the_policy_key_is_normalised():
    settings = settings_for({"platform_overrides": {"cron": {"enabled": True}}})

    assert settings.policy_for("CRON") is not None
    policy = settings.policy_for("cron")
    assert policy.enabled is True
    assert policy.grid is None
    assert policy.confidence_threshold is None
    assert policy.max_effort is None

    effective = settings.effective(policy)
    assert effective.grid == settings.grid
    assert effective.confidence_threshold == settings.confidence_threshold
    assert effective.max_effort is None
    # No policy grid was declared, so this is the global grid, not a policy grid.
    assert effective.grid_from_policy is False


# -- unlisted / absent / null / unusable -------------------------------------------


def test_unlisted_platform_has_no_policy():
    settings = settings_for({"platform_overrides": {"cron": {"enabled": True}}})

    assert settings.policy_for("telegram") is None
    assert settings.effective(None).grid == settings.grid
    assert settings.effective(None).grid_from_policy is False


@pytest.mark.parametrize(
    "config",
    [
        {},  # no key
        {"platform_overrides": None},  # null
        {"platform_overrides": []},  # not a map
        {"platform_overrides": {"cron": "off"}},  # value is not a map
        {"platform_overrides": {"cron": {}}},  # map with no recognised key
        {"platform_overrides": {"cron": {"nope": 1}}},  # map with only unknown keys
        {"platform_overrides": {"cron": {"enabled": "banana"}}},  # uncoercible
        {"platform_overrides": {"cron": {"grid": "???"}}},  # grid alone, unparseable
        {"platform_overrides": {"cron": {"grid": 123}}},  # grid alone, unparseable
        {"platform_overrides": {"cron": {"grid": []}}},  # grid alone, empty
    ],
)
def test_absent_null_unlisted_and_unusable_leave_settings_default(config):
    settings = settings_for(config)

    assert settings.platform_overrides == {}
    # Field for field equal to a default-constructed Settings.
    assert settings == Settings()
    for field in dataclasses.fields(Settings):
        assert getattr(settings, field.name) == getattr(Settings(), field.name)


def test_an_unusable_policy_grid_falls_to_the_global_grid_not_the_builtin():
    custom = "custom-model: does the custom thing"
    settings = settings_for(
        {
            "grid": custom,
            "platform_overrides": {"cron": {"grid": "???"}},
        }
    )

    assert settings.grid == (Entry("custom-model", "does the custom thing"),)
    # The entry was dropped, so no policy and the effective grid is the operator's own.
    assert settings.policy_for("cron") is None
    assert settings.effective(None).grid == settings.grid
    assert settings.effective(None).grid != DEFAULT_GRID


# -- an unparseable policy grid alongside a readable key ---------------------------


@pytest.mark.parametrize("bad_grid", [123, [], "", None, "   "])
def test_unparseable_policy_grid_keeps_the_other_keys_and_drops_only_the_grid(bad_grid):
    settings = settings_for(
        {
            "grid": "custom-model: does the custom thing",
            "platform_overrides": {
                "cron": {"enabled": True, "grid": bad_grid, "confidence_threshold": 0.8}
            },
        }
    )

    policy = settings.policy_for("cron")
    assert policy is not None
    assert policy.enabled is True
    assert policy.confidence_threshold == 0.8
    assert policy.grid is None

    effective = settings.effective(policy)
    # The grid falls to the *global* grid, never to parse_grid's built-in six.
    assert effective.grid == settings.grid
    assert effective.grid != DEFAULT_GRID
    # A policy whose grid alone was unparseable is not a policy grid.
    assert effective.grid_from_policy is False


# -- vocabulary --------------------------------------------------------------------


def test_max_effort_is_the_effort_vocabulary_and_rejects_anything_else():
    for level in EFFORT_LEVELS:
        settings = settings_for({"platform_overrides": {"cron": {"max_effort": level}}})
        assert settings.policy_for("cron").max_effort == level

    settings = settings_for({"platform_overrides": {"cron": {"max_effort": "xhigh"}}})
    assert settings.policy_for("cron") is None


def test_confidence_threshold_rejects_a_negative():
    settings = settings_for({"platform_overrides": {"cron": {"confidence_threshold": -0.1}}})

    assert settings.policy_for("cron") is None


def test_platform_policy_is_frozen_and_keeps_none_distinct_from_false():
    policy = PlatformPolicy(enabled=False)
    assert policy.enabled is False

    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.enabled = True  # type: ignore[misc]


def test_the_settings_snapshot_cannot_be_mutated_through_its_mapping():
    settings = settings_for({"platform_overrides": {"cron": {"enabled": True}}})
    assert settings.policy_for("cron") is not None

    with pytest.raises(TypeError):
        settings.platform_overrides["cli"] = PlatformPolicy(enabled=False)  # type: ignore[index]

    # The empty default is immutable too.
    with pytest.raises(TypeError):
        Settings().platform_overrides["cli"] = None  # type: ignore[index]


def test_the_settings_snapshot_stays_hashable():
    settings = settings_for({"platform_overrides": {"cron": {"enabled": True}}})

    assert hash(settings) == hash(settings)
    assert hash(Settings()) == hash(Settings())
    # Equal snapshots hash equal, different ones need not.
    assert len({settings, Settings()}) == 2


def test_defaults_are_unchanged_when_the_key_is_absent():
    settings = settings_for({})

    assert settings.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD
    assert settings.default_model == DEFAULT_MODEL
