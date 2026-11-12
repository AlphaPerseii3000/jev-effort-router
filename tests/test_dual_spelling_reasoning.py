"""Regression: the router's effort decision must own the ONE reasoning knob on the wire.

OpenRouter answers HTTP 400 ("reasoning_effort and reasoning.effort are both provided
with conflicting values") when the payload carries both a top-level ``reasoning_effort``
and a nested ``extra_body.reasoning``. The OpenRouter provider profile emits the nested
form; when this router then writes its top-level decision, the nested form must be
dropped — the router's decision is authoritative. Same class as the fleet-jev-router
dual-spelling fix (2026-10-21/22); this twin lacked the reconcile block and 400'd the
quota-fallback leg on 2026-10-22.
"""

from __future__ import annotations

from stubs import StubResponse, StubTransport, decision_payload, ollama_request

from test_router import build, route


def test_decision_replaces_nested_reasoning_conflict(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, _ = build(tmp_path, transport)

    request = ollama_request()
    # What the OpenRouter provider profile puts on the wire (build_api_kwargs_extras,
    # supports_reasoning=True): nested-only spelling.
    request["extra_body"] = {"reasoning": {"enabled": True, "effort": "max"}}

    result = route(router, request)

    assert result is not None
    assert result["request"]["reasoning_effort"] == "high"
    extra_body = result["request"].get("extra_body")
    assert isinstance(extra_body, dict), "unrelated extra_body keys must survive"
    assert "reasoning" not in extra_body, (
        "nested extra_body.reasoning must be dropped when the router writes its own "
        "top-level reasoning_effort — both spellings together are an OpenRouter HTTP 400"
    )
    # Other extra_body content is untouched.
    extra_body["keep_me"] = True


def test_no_effort_decision_leaves_nested_reasoning_alone(tmp_path):
    # Without a router effort decision there is no top-level field, hence no conflict:
    # the profile's nested spelling must NOT be stripped.
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8, include_effort=False))])
    router, _ = build(tmp_path, transport)

    request = ollama_request()
    request["extra_body"] = {"reasoning": {"enabled": True, "effort": "max"}}

    result = route(router, request)

    assert result is not None
    assert "reasoning_effort" not in result["request"]
    assert result["request"]["extra_body"]["reasoning"]["effort"] == "max"
