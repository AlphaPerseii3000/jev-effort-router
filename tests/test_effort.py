"""Effort translation per model family."""

from __future__ import annotations

from effort import clamp, family_for, is_omitted, resolve_effort


def test_family_detection():
    assert family_for("kimi-k3").name == "kimi-k3"
    assert family_for("kimi-k3-256k").name == "kimi-k3"
    assert family_for("vendor/kimi-k3").name == "kimi-k3"
    assert family_for("kimi-k2.6").name == "ollama-cloud"
    assert family_for("deepseek-v4.1-flash").name == "deepseek"
    assert family_for("glm-5.3").name == "glm-53"
    assert family_for("glm-5.3-flash").name == "glm-53"
    assert family_for("glm-5.2").name == "glm-52"
    assert family_for("minimax-m3").name == "minimax"
    assert family_for("nemotron-3-nano:30b").name == "nemotron"
    assert family_for("mystery-model").name == "ollama-cloud"
    assert family_for("").name == "ollama-cloud"


def test_deepseek_passes_the_three_levels_through():
    assert resolve_effort("deepseek-v4.1-flash", "low") == "low"
    assert resolve_effort("deepseek-v4.1-flash", "medium") == "medium"
    assert resolve_effort("deepseek-v4.1-flash", "high") == "high"


def test_kimi_k3_medium_lands_on_high_via_the_declared_override():
    # K3's documented set is low/high/max and `high` is its positional middle.
    assert resolve_effort("kimi-k3", "low") == "low"
    assert resolve_effort("kimi-k3", "medium") == "high"
    assert resolve_effort("kimi-k3", "high") == "high"


def test_glm53_is_graded():
    for level in ("low", "medium", "high"):
        assert resolve_effort("glm-5.3", level) == level
        assert resolve_effort("glm-5.3-flash", level) == level


def test_glm52_never_escalates_below_its_floor():
    # The older knob only has high and max; every request lands on high, never on a
    # level the route would reject.
    assert resolve_effort("glm-5.2", "low") == "high"
    assert resolve_effort("glm-5.2", "medium") == "high"
    assert resolve_effort("glm-5.2", "high") == "high"


def test_ollama_cloud_family_accepts_the_three_levels():
    assert resolve_effort("minimax-m3", "high") == "high"
    assert resolve_effort("nemotron-3-nano", "low") == "low"
    assert resolve_effort("mystery-model", "medium") == "medium"


def test_clamp_never_escalates():
    assert clamp("minimal", ("low", "medium", "high")) == "low"
    assert clamp("xhigh", ("low", "medium", "high")) == "high"
    assert clamp("high", ("low", "medium")) == "medium"
    assert clamp("low", ("high", "max")) == "high"


def test_clamp_prefers_a_declared_override():
    assert clamp("medium", ("low", "high"), {"medium": "high"}) == "high"
    assert clamp("xhigh", ("low", "max"), {"xhigh": "max"}) == "max"


def test_clamp_never_lands_on_none():
    assert clamp("low", ("none", "medium")) == "medium"


def test_clamp_passes_a_bespoke_vocabulary_through():
    assert clamp("thinking", ("low", "high")) == "thinking"


def test_empty_effort_means_omit():
    assert resolve_effort("deepseek-v4.1-flash", None) is None
    assert resolve_effort("deepseek-v4.1-flash", "") is None
    assert is_omitted("deepseek-v4.1-flash", "")
    assert not is_omitted("deepseek-v4.1-flash", "medium")
