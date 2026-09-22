"""Agent-facing tools: ask the router what it sees and what it would do."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Tuple

from .client import JevClient
from .config import Settings, api_key
from .state import build_questions, build_state

STATUS_SCHEMA = {
    "name": "jev_router_status",
    "description": (
        "Report the Jev router's state: whether routing is enabled, which decision model and "
        "endpoint it uses, the confidence threshold, the models it may choose between, and the "
        "most recent routing decisions. Use it when the user asks how model routing is set up, "
        "which model was picked recently, or why a turn was not routed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recent": {
                "type": "integer",
                "description": "How many recent routing records to include (default 5, max 50).",
            }
        },
    },
}

ROUTE_SCHEMA = {
    "name": "jev_router_route",
    "description": (
        "Ask the Jev decision model which Ollama:cloud model and reasoning effort it would "
        "choose for a given task description, without changing the current session. Use it to "
        "test the routing grid, or to show the user what the router would pick. Returns the "
        "choice, its probabilities, the confidence, and the alternatives."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task or user request to route, in natural language.",
            },
            "context": {
                "type": "string",
                "description": "Optional recent context to help the decision.",
            },
        },
        "required": ["task"],
    },
}


def _status(router, settings: Settings, recent: int = 5) -> str:
    records = router.tail(settings, limit=max(1, min(int(recent or 5), 50)))
    payload = {
        "enabled": settings.enabled,
        "api_key_present": bool(api_key()),
        "endpoint": settings.endpoint,
        "jev_model": settings.jev_model,
        "confidence_threshold": settings.confidence_threshold,
        "timeout_s": settings.timeout_s,
        "route_per_turn": settings.route_per_turn,
        "fallback": {"model": settings.default_model, "effort": settings.default_effort},
        "grid": [
            {"model": entry.model_id, "profile": entry.description} for entry in settings.grid
        ],
        "audit": {
            "enabled": settings.audit_enabled,
            "records": router.count(settings),
        },
        "recent": records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _route(router, settings: Settings, task: str, context: str = "") -> str:
    if not str(task or "").strip():
        return json.dumps({"error": "task must not be empty"})
    messages = [{"role": "user", "content": str(task)}]
    if str(context).strip():
        messages = [
            {"role": "user", "content": str(context).strip()},
            {"role": "assistant", "content": "(context)"},
            {"role": "user", "content": str(task)},
        ]

    decision, reason = router.client(settings).decide(
        messages, settings.grid, current_model=settings.default_model
    )
    if decision is None:
        return json.dumps(
            {
                "routed": False,
                "reason": reason,
                "hint": _hint_for(reason),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "routed": True,
            "model": decision.model,
            "effort": decision.effort,
            "effort_requested": decision.effort_requested,
            "model_choice": decision.model_choice,
            "model_confidence": round(decision.model_confidence, 4),
            "model_probabilities": decision.model_probabilities,
            "effort_confidence": round(decision.effort_confidence, 4),
            "effort_probabilities": decision.effort_probabilities,
            "alternatives": list(decision.alternatives),
            "latency_ms": decision.latency_ms,
            "degradation": list(decision.fallback_reasons),
        },
        ensure_ascii=False,
        indent=2,
    )


def _hint_for(reason: Any) -> str:
    from .client import REASON_NO_API_KEY, REASON_TIMEOUT, REASON_UPSTREAM_ERROR

    if reason == REASON_NO_API_KEY:
        return "Set OPENROUTER_API_KEY in the Hermes .env file, then restart the session."
    if reason == REASON_TIMEOUT:
        return "Jev did not answer inside the configured budget; raise timeout_s or retry."
    if reason == REASON_UPSTREAM_ERROR:
        return "The Decisions API returned an error — check credits and network access."
    return "The decision was unusable; the turn would keep the configured model."


def build_tool_registrations(router, get_settings: Callable[[], Settings]) -> List[Tuple[Dict[str, Any], Callable[..., str]]]:
    """``(schema, handler)`` pairs for ``ctx.register_tool``."""

    def status_handler(recent: int = 5, **_: Any) -> str:
        return _status(router, get_settings(), recent=recent)

    def route_handler(task: str = "", context: str = "", **_: Any) -> str:
        return _route(router, get_settings(), task, context)

    return [(STATUS_SCHEMA, status_handler), (ROUTE_SCHEMA, route_handler)]
