"""Regression: the low-confidence fallback must not resolve to a per-token OpenRouter model.

On 2026-10-22 a zai 429 storm pushed low-confidence fallback routes onto
``deepseek-v4.1-flash``, which resolves to OpenRouter per-token billing. The affected
session re-sent its ~330K-token working set on every retry (no cache pricing on the
fallback path) and the prepaid OpenRouter balance drained to zero within 36 hours.
The default fallback is now ``glm-5.3-flash``: flat-quota, and already on the grid.
"""

from __future__ import annotations

import json

from stubs import StubResponse, StubTransport, decision_payload
from config import DEFAULT_MODEL, api_key
from test_router import build, route


def test_default_fallback_is_glm_5_3_flash(tmp_path):
    """A below-threshold decision routes to the flat-quota fallback, not deepseek."""
    # decision_payload("99", ...) names an off-grid model -> unknown_choice fallback.
    transport = StubTransport([StubResponse(decision_payload("99", 0.99, "medium", 0.99))])
    router, settings = build(tmp_path, transport)

    assert settings.default_model == "glm-5.3-flash"
    assert settings.entry_for(settings.default_model) is not None, (
        "the fallback model must resolve on the default grid"
    )
    assert DEFAULT_MODEL == "glm-5.3-flash"

    result = route(router)
    assert result is not None
    assert result["request"]["model"] == "glm-5.3-flash"

    record = json.loads(
        (tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1]
    )
    assert "unknown_choice" in record["fallback_reasons"]


def test_fallback_default_survives_a_policy_surface(tmp_path):
    """A surface the policy does not name keeps the configured (glm) default verbatim."""
    transport = StubTransport([StubResponse(decision_payload("99", 0.99, "medium", 0.99))])
    router, _ = build(
        tmp_path,
        transport,
        config={"platform_overrides": {"cron": {"enabled": False}}},
    )

    result = route(router, platform="cli")
    assert result is not None
    assert result["request"]["model"] == "glm-5.3-flash"


def test_requesty_key_takes_precedence_over_openrouter(monkeypatch):
    """The 2026-10-20 Requesty cutover: REQUESTY_API_KEY wins when both are set."""
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    assert api_key() == "rq-key"

    monkeypatch.delenv("REQUESTY_API_KEY")
    assert api_key() == "or-key"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert api_key() == ""
