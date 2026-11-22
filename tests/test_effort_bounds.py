"""Per-model effort **bounds** (``min``/``max``) on a declared route.

Two properties the older contracts could not express, both cost-relevant:

1. **The question offers the union, not the intersection.** The effort question is asked once,
   before the model is known. Under a vocabulary contract only the levels every model accepts
   verbatim can be offered, so a grid of Haiku ``medium..max``, Sonnet ``medium..high`` and Opus
   ``low..medium`` would offer a single option (``medium``) and every turn would land there. With
   bounds declared, each model clamps whatever it cannot take, so the union is safe to offer.
2. **The bounds are applied by clamping, never by omission.** An omitted level falls back to the
   transport's own default — a declared ceiling that is dropped is not a ceiling. A request above
   ``max`` lands on ``max``; below ``min`` it lands on ``min``; inside the pair it goes verbatim.

The audit has to make that provable: ``effort_requested`` is what the decider asked for,
``effort`` what reached the wire, and ``effort_bounds`` / ``effort_adjusted`` explain the
difference. The routes that declare no bounds keep the declarative omit rule unchanged — that
compatibility is pinned here too, on the same functions.
"""

from __future__ import annotations

import pytest
from stubs import StubResponse, StubTransport, anthropic_request, decision_payload
from test_router import build, route

from config import EFFORT_LEVELS, load_settings, parse_effort_maps
from effort import (
    bounds_adjustment,
    declared_bounds,
    offered_levels,
    offered_levels_for_grid,
    parse_effort_spec,
    resolve_effort,
)
from grid import ANTHROPIC_GRID, Entry
from state import EFFORT_QUESTION_ID, build_questions

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

#: The owner's declaration for the ``anthropic`` route (card t_b477e209): the per-family caps
#: that used to omit a level now bound it — Haiku ``medium..max``, Sonnet ``medium..high``, Opus
#: ``low..medium``.
BOUNDS_RAW = {
    "anthropic": {
        HAIKU: {"min": "medium", "max": "max"},
        SONNET: {"min": "medium", "max": "high"},
        OPUS: {"min": "low", "max": "medium"},
    }
}
BOUNDS = parse_effort_maps(BOUNDS_RAW)

ANTHROPIC_CONFIG = {"decider": "jev_console", "routable_providers": ["anthropic"]}
BOUNDED_CONFIG = dict(ANTHROPIC_CONFIG, effort_maps=BOUNDS_RAW)


def _settings(config=None):
    return load_settings(lambda key, default=None: (config or {}).get(key, default))


def anthropic_route(router, request=None, **overrides):
    """``route`` with the Anthropic Messages payload shape and provider."""
    kwargs = dict(
        model=SONNET,
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages",
    )
    kwargs.update(overrides)
    return route(router, request if request is not None else anthropic_request(), **kwargs)


# -- the declaration -------------------------------------------------------------


def test_a_min_max_entry_derives_its_accepted_set_from_the_pair():
    """The pair *is* the contract; ``accepted`` is what it covers, not a second declaration."""
    assert parse_effort_spec({"min": "medium", "max": "max"}).accepted == ("medium", "high", "max")
    assert parse_effort_spec({"min": "medium", "max": "high"}).accepted == ("medium", "high")
    assert parse_effort_spec({"min": "low", "max": "medium"}).accepted == ("low", "medium")
    # One open side is filled from the askable vocabulary, so a single key still bounds the route.
    assert parse_effort_spec({"max": "medium"}).accepted == ("low", "medium")
    assert parse_effort_spec({"min": "high"}).accepted == ("high", "max")


def test_a_contradictory_or_unorderable_declaration_declares_nothing():
    """Silently letting one key win would ship a route nobody declared."""
    # ``accepted`` disagreeing with the bounds is refused, not resolved by precedence.
    assert not parse_effort_spec({"min": "low", "max": "medium", "accepted": ["low", "high"]}).valid
    # An inverted pair covers nothing.
    assert not parse_effort_spec({"min": "high", "max": "low"}).valid
    # A bound outside the askable vocabulary has no position to clamp onto.
    assert not parse_effort_spec({"min": "minimal", "max": "high"}).valid
    # An ``accepted`` list alone is still the vocabulary contract: no bounds.
    assert parse_effort_spec({"accepted": ["low", "high"]}).bounds is None


def test_the_bounds_are_read_per_model_from_the_operator_map():
    assert declared_bounds("anthropic", HAIKU, BOUNDS) == ("medium", "max")
    assert declared_bounds("anthropic", SONNET, BOUNDS) == ("medium", "high")
    assert declared_bounds("anthropic", OPUS, BOUNDS) == ("low", "medium")
    # A model the map does not name keeps its built-in policy, which declares no bounds.
    assert declared_bounds("anthropic", "claude-opus-4-1", BOUNDS) is None
    assert declared_bounds("anthropic", HAIKU, None) is None


# -- the question ----------------------------------------------------------------


def test_a_bounded_grid_offers_the_union_of_its_models():
    """One question, four rungs: any answer has somewhere to land on every model."""
    assert offered_levels_for_grid(ANTHROPIC_GRID, "anthropic", BOUNDS) == (
        "low",
        "medium",
        "high",
        "max",
    )
    # Each model on its own still offers only what it can be given verbatim.
    assert offered_levels("anthropic", HAIKU, BOUNDS) == ("medium", "high", "max")
    assert offered_levels("anthropic", SONNET, BOUNDS) == ("medium", "high")
    assert offered_levels("anthropic", OPUS, BOUNDS) == ("low", "medium")


def test_the_question_lists_all_four_levels_under_bounds():
    levels = offered_levels_for_grid(ANTHROPIC_GRID, "anthropic", BOUNDS)
    questions = build_questions(ANTHROPIC_GRID, levels=levels)

    assert set(questions[EFFORT_QUESTION_ID]["criteria"]) == set(EFFORT_LEVELS) == {
        "low",
        "medium",
        "high",
        "max",
    }


def test_an_unbounded_grid_still_intersects():
    """Compatibility: without bounds the old rule stands — never offer what a model would omit."""
    assert offered_levels_for_grid(ANTHROPIC_GRID, "anthropic") == ("low", "medium", "high")
    accepted_only = parse_effort_maps({"p": {"a": ["low", "max"], "b": ["low", "high", "max"]}})
    assert offered_levels_for_grid(
        (Entry("a", "x"), Entry("b", "y")), "p", accepted_only
    ) == ("low", "max")


def test_one_bounded_model_is_enough_to_widen_the_question():
    """The grid is offered the union as soon as a clamp can catch what a model cannot take."""
    mixed = parse_effort_maps(
        {
            "p": {
                "a": {"min": "medium", "max": "max"},
                "b": {"accepted": ["low", "medium", "high"]},
            }
        }
    )
    assert offered_levels_for_grid((Entry("a", "x"), Entry("b", "y")), "p", mixed) == (
        "low",
        "medium",
        "high",
        "max",
    )


# -- the applied level -----------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "level", "expected", "reason"),
    [
        # Haiku: floor medium, ceiling max.
        (HAIKU, "low", "medium", "clamped_min"),
        (HAIKU, "medium", "medium", None),
        (HAIKU, "high", "high", None),
        (HAIKU, "max", "max", None),
        # Sonnet: floor medium, ceiling high.
        (SONNET, "low", "medium", "clamped_min"),
        (SONNET, "medium", "medium", None),
        (SONNET, "high", "high", None),
        (SONNET, "max", "high", "clamped_max"),
        # Opus: floor low, ceiling medium.
        (OPUS, "low", "low", None),
        (OPUS, "medium", "medium", None),
        (OPUS, "high", "medium", "clamped_max"),
        (OPUS, "max", "medium", "clamped_max"),
    ],
)
def test_every_level_is_clamped_into_the_models_own_bounds(model, level, expected, reason):
    applied = resolve_effort(model, level, provider="anthropic", effort_maps=BOUNDS)

    assert applied == expected
    assert bounds_adjustment(level, applied, declared_bounds("anthropic", model, BOUNDS)) == reason


def test_a_bounded_model_never_drops_the_field():
    """The point of the pair: a level inside the ladder always lands somewhere."""
    for model in (HAIKU, SONNET, OPUS):
        for level in EFFORT_LEVELS:
            assert resolve_effort(model, level, provider="anthropic", effort_maps=BOUNDS) is not None


def test_a_level_outside_the_ladder_is_still_not_invented():
    """Bounds clamp rungs, they do not manufacture a level the vocabulary never had."""
    for level in ("none", "bogus", "", None):
        for model in (HAIKU, SONNET, OPUS):
            assert (
                resolve_effort(model, level, provider="anthropic", effort_maps=BOUNDS) is None
            )


def test_a_rung_inside_the_bounds_but_absent_from_the_vocabulary_lands_weaker():
    """``xhigh`` sits between ``high`` and ``max``, which Haiku's set does not carry."""
    applied = resolve_effort(HAIKU, "xhigh", provider="anthropic", effort_maps=BOUNDS)

    assert applied == "high"
    assert bounds_adjustment("xhigh", applied, ("medium", "max")) == "nearest_accepted"


# -- compatibility: the omit rule for routes with no bounds -----------------------


def test_an_accepted_only_map_still_omits_an_out_of_set_level():
    maps = parse_effort_maps(
        {"opencode-go": {"deepseek-v4.1-flash": {"accepted": ["low", "medium", "high"]}}}
    )
    assert (
        resolve_effort("deepseek-v4.1-flash", "max", provider="opencode-go", effort_maps=maps)
        is None
    )


def test_the_built_in_anthropic_caps_still_omit():
    """No ``effort_maps`` entry: the built-in per-family caps keep the old rule."""
    assert resolve_effort(HAIKU, "max", provider="anthropic") == "max"
    assert resolve_effort(SONNET, "max", provider="anthropic") is None
    assert resolve_effort(OPUS, "max", provider="anthropic") is None


def test_the_legacy_ollama_clamp_is_untouched():
    """The one pre-existing clamp keeps its nearest-weaker rule; bounds do not reach it."""
    assert resolve_effort("kimi-k3", "medium", provider="ollama-cloud") == "high"
    assert declared_bounds("ollama-cloud", "kimi-k3", None) is None


def test_a_floor_above_the_operator_cap_wins_and_is_visible():
    """The cap runs before the bounds, so a contradictory pair is resolved, not hidden.

    ``max_effort: low`` cannot be honoured by a model whose floor is ``medium``: the level is capped
    to ``low`` and the bounds raise it back to ``medium``. The audit shows the applied level, so the
    conflict surfaces instead of silently running below the declared floor.
    """
    from decider import _cap_effort

    capped = _cap_effort("high", "low")

    assert capped == "low"
    assert resolve_effort(HAIKU, capped, provider="anthropic", effort_maps=BOUNDS) == "medium"


# -- the wire and the audit ------------------------------------------------------


def test_the_clamp_reaches_output_config_effort(tmp_path, monkeypatch):
    """Opus asked for ``high``: the Messages body carries ``medium``, and the container stays."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("3", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=BOUNDED_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(router, turn_id="bounds-opus", session_id="bounds-opus")

    assert result is not None
    assert result["request"]["model"] == OPUS
    assert result["request"]["output_config"] == {"effort": "medium"}
    assert result["request"]["thinking"] == {"type": "adaptive", "display": "summarized"}

    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["effort_requested"] == "high"
    assert record["effort"] == "medium"
    assert record["effort_bounds"] == ["low", "medium"]
    assert record["effort_adjusted"] == "clamped_max"
    assert transport.call_count == 1


def test_a_level_below_the_floor_is_raised_on_the_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "low", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=BOUNDED_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(
        router, anthropic_request(model=HAIKU), model=HAIKU, turn_id="bounds-haiku", session_id="bounds-haiku"
    )

    assert result is not None
    assert result["request"]["output_config"] == {"effort": "medium"}
    record = router.tail(settings, limit=1)[0]
    assert record["effort_requested"] == "low"
    assert record["effort"] == "medium"
    assert record["effort_bounds"] == ["medium", "max"]
    assert record["effort_adjusted"] == "clamped_min"


def test_the_audit_keeps_the_request_when_nothing_was_adjusted(tmp_path, monkeypatch):
    """A level inside the pair is verbatim, and the record says so by carrying only the pair."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "medium", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=BOUNDED_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = anthropic_route(router, turn_id="bounds-verbatim", session_id="bounds-verbatim")

    assert result is not None
    record = router.tail(settings, limit=1)[0]
    assert record["effort_requested"] == "medium"
    assert record["effort"] == "medium"
    assert record["effort_bounds"] == ["medium", "high"]
    assert "effort_adjusted" not in record


def test_a_route_without_bounds_keeps_the_records_old_shape(tmp_path, monkeypatch):
    """The two new keys appear only where a pair was declared."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    anthropic_route(router, turn_id="bounds-none", session_id="bounds-none")

    record = router.tail(settings, limit=1)[0]
    assert "effort_bounds" not in record
    assert "effort_adjusted" not in record


def test_the_replay_reproduces_the_applied_level_not_the_request(tmp_path, monkeypatch):
    """The memo stores the request; the replay re-applies the *declaration* to it."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("3", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=BOUNDED_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    request = anthropic_request()

    first = anthropic_route(router, request, turn_id="bounds-replay", session_id="bounds-replay")
    second = anthropic_route(
        router, request, turn_id="bounds-replay", session_id="bounds-replay", api_call_count=1
    )

    assert first is not None and second is not None
    assert second["request"]["output_config"] == {"effort": "medium"}
    assert second["request"] == first["request"]
    assert transport.call_count == 1  # the replay never asks the decider again

    record = router.tail(settings, limit=1)[0]
    assert record["replayed"] is True
    assert record["model"] == OPUS
    assert record["effort_requested"] == "high"
    assert record["effort"] == "medium"
    assert record["effort_adjusted"] == "clamped_max"


def test_the_replay_reapplies_the_declaration_to_the_memoised_request(tmp_path, monkeypatch):
    """The memo written before the bounds existed must not carry ``high`` onto the wire."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("3", 0.95, "high", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=ANTHROPIC_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    request = anthropic_request()

    first = anthropic_route(router, request, turn_id="bounds-declared-late", session_id="s")
    assert first is not None
    # Unbounded route: ``high`` was verbatim, so that is what the memo holds as applied.
    assert first["request"]["output_config"] == {"effort": "high"}

    # The operator declares the bounds between the turn's two requests. The memo still carries
    # ``high`` as the request; the replay resolves it against the declaration that will run now.
    settings2 = _settings(BOUNDED_CONFIG)
    monkeypatch.setattr(router, "_get_settings", lambda: settings2)
    second = anthropic_route(
        router, request, turn_id="bounds-declared-late", session_id="s", api_call_count=1
    )

    assert second is not None
    assert second["request"]["output_config"] == {"effort": "medium"}
    assert transport.call_count == 1  # the replay never asks the decider again
    record = router.tail(settings2, limit=1)[0]
    assert record["replayed"] is True
    assert record["effort_requested"] == "high"
    assert record["effort"] == "medium"
    assert record["effort_adjusted"] == "clamped_max"


def test_the_settings_view_of_the_bounds_matches_the_parsed_map():
    """``load_settings`` round-trips the pair, so the audit reads what the operator wrote."""
    settings = _settings(BOUNDED_CONFIG)

    assert settings.effort_maps["anthropic"][OPUS]["min"] == "low"
    assert settings.effort_maps["anthropic"][OPUS]["max"] == "medium"
    assert declared_bounds("anthropic", OPUS, settings.effort_maps) == ("low", "medium")
