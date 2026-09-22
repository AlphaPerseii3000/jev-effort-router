"""Resolved plugin settings.

Every tunable comes from one place so a callback, a tool handler, a slash command and the
CLI all read the same values. ``ctx.get_config`` already resolves
``plugins.entries.jev-router.settings.<key>`` over the manifest default, so this module
only adds coercion, validation and the "value is missing or nonsense" backstops.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .grid import DEFAULT_GRID, Entry, parse_grid

#: Provider profile this router is benchmarked for. Anything else is skipped untouched.
ROUTED_PROVIDER = "ollama-cloud"

#: Provider profile name aliases that also count as routed.
ROUTED_PROVIDER_ALIASES = ("ollama_cloud",)

#: API modes this router understands. The Ollama:cloud profile is chat_completions;
#: a Responses or Anthropic-Messages route builds its payload elsewhere and is left alone.
ROUTED_API_MODES = ("chat_completions",)

EFFORT_LEVELS: Tuple[str, ...] = ("low", "medium", "high")

DEFAULT_JEV_MODEL = "typesafe/jev-1.13"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_EFFORT = "medium"
DEFAULT_CONTEXT_TURNS = 4


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_float(value: Any, default: float, *, minimum: Optional[float] = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    if minimum is not None and result < minimum:
        return default
    return result


def _as_int(value: Any, default: int, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and result < minimum:
        return default
    if maximum is not None and result > maximum:
        return maximum
    return result


def _as_choice(value: Any, allowed: Sequence[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _as_text(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text or default


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the plugin's effective settings."""

    enabled: bool = True
    jev_model: str = DEFAULT_JEV_MODEL
    endpoint: str = DEFAULT_ENDPOINT
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    timeout_s: float = DEFAULT_TIMEOUT_S
    default_model: str = DEFAULT_MODEL
    default_effort: str = DEFAULT_EFFORT
    context_turns: int = DEFAULT_CONTEXT_TURNS
    route_per_turn: bool = True
    audit_enabled: bool = True
    log_skips: bool = True
    include_user_message_in_audit: bool = False
    grid: Tuple[Entry, ...] = field(default_factory=lambda: tuple(DEFAULT_GRID))

    @property
    def grid_ids(self) -> Tuple[str, ...]:
        return tuple(entry.model_id for entry in self.grid)

    def entry_for(self, model_id: str) -> Optional[Entry]:
        wanted = (model_id or "").strip()
        for entry in self.grid:
            if entry.model_id == wanted:
                return entry
        return None


def load_settings(get_config: Optional[Callable[..., Any]] = None) -> Settings:
    """Build a :class:`Settings` from ``ctx.get_config`` (or from no config at all)."""
    read = get_config if callable(get_config) else (lambda _key, default=None: default)

    raw_grid = read("grid", None)
    grid = parse_grid(raw_grid) if raw_grid else tuple(DEFAULT_GRID)

    settings = Settings(
        enabled=_as_bool(read("enabled", True), True),
        jev_model=_as_text(read("jev_model", DEFAULT_JEV_MODEL), DEFAULT_JEV_MODEL),
        endpoint=_as_text(read("endpoint", DEFAULT_ENDPOINT), DEFAULT_ENDPOINT),
        confidence_threshold=_as_float(
            read("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD),
            DEFAULT_CONFIDENCE_THRESHOLD,
            minimum=0.0,
        ),
        timeout_s=_as_float(read("timeout_s", DEFAULT_TIMEOUT_S), DEFAULT_TIMEOUT_S, minimum=0.05),
        default_model=_as_text(read("default_model", DEFAULT_MODEL), DEFAULT_MODEL),
        default_effort=_as_choice(read("default_effort", DEFAULT_EFFORT), EFFORT_LEVELS, DEFAULT_EFFORT),
        context_turns=_as_int(
            read("context_turns", DEFAULT_CONTEXT_TURNS), DEFAULT_CONTEXT_TURNS, minimum=0, maximum=50
        ),
        route_per_turn=_as_bool(read("route_per_turn", True), True),
        audit_enabled=_as_bool(read("audit_enabled", True), True),
        log_skips=_as_bool(read("log_skips", True), True),
        include_user_message_in_audit=_as_bool(read("include_user_message_in_audit", False), False),
        grid=grid,
    )
    return settings


def api_key() -> str:
    """The OpenRouter key, from the environment only.

    Never read from ``config.yaml`` and never returned anywhere it could be logged.
    """
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def redact(value: Any, limit: int = 200) -> str:
    """Render a value for a log line with any credential-looking substring removed."""
    text = str(value)
    key = api_key()
    if key:
        text = text.replace(key, "[REDACTED]")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text
