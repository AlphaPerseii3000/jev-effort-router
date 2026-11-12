"""Resolved plugin settings.

Every tunable comes from one place so a callback, a tool handler, a slash command and the
CLI all read the same values. ``ctx.get_config`` already resolves
``plugins.entries.jev-effort-router.settings.<key>`` over the manifest default, so this module
only adds coercion, validation and the "value is missing or nonsense" backstops.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .grid import DEFAULT_GRID, Entry, parse_grid, parse_grid_strict

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
#: Requesty's Decisions endpoint. It is a different API from OpenRouter's: the path is
#: ``/v1/systemone`` and the current Jev id carries a patch component (``jev-1.14.0``).
REQUESTY_ENDPOINT = "https://router.requesty.ai/v1/systemone"
REQUESTY_DEFAULT_JEV_MODEL = "typesafe/jev-1.14.0"
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
#: Low-confidence fallback model. Was ``deepseek-v4.1-flash``, which resolves to OpenRouter
#: per-token billing: during the 2026-10-22 zai 429 storm, fallback turns re-sent a ~330K-token
#: working set on every retry with no cache pricing, and the prepaid balance drained to zero.
#: ``glm-5.3-flash`` is flat-quota and already on the grid, so fallback resolves on-grid.
DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_EFFORT = "medium"
DEFAULT_CONTEXT_TURNS = 4


def _as_optional_bool(value: Any) -> Optional[bool]:
    """Coerce to a bool, or ``None`` when the value is absent or unreadable.

    ``None`` is the "inherit the global setting" sentinel, so it has to stay distinguishable
    from a parsed ``False``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _as_bool(value: Any, default: bool) -> bool:
    parsed = _as_optional_bool(value)
    return default if parsed is None else parsed


def _as_optional_float(value: Any, *, minimum: Optional[float] = None) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    if minimum is not None and result < minimum:
        return None
    return result


def _as_float(value: Any, default: float = 0.0, *, minimum: Optional[float] = None) -> float:
    parsed = _as_optional_float(value, minimum=minimum)
    return default if parsed is None else parsed


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


def _as_optional_choice(value: Any, allowed: Sequence[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in allowed else None


def _as_choice(value: Any, allowed: Sequence[str], default: str) -> str:
    parsed = _as_optional_choice(value, allowed)
    return default if parsed is None else parsed


def _as_text(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _entry_for(grid: Sequence[Entry], model_id: str) -> Optional[Entry]:
    """The grid entry whose model id is ``model_id``, or ``None``."""
    wanted = (model_id or "").strip()
    for entry in grid:
        if entry.model_id == wanted:
            return entry
    return None



@dataclass(frozen=True)
class PlatformPolicy:
    """One surface's routing policy, as declared under ``platform_overrides``.

    Every field is optional and ``None`` means "inherit the global setting", deliberately
    distinct from a parsed ``False``/``0.0``. ``grid`` is ``None`` unless a policy grid both
    was declared and parsed; the resolved grid then falls to the effective global one.
    """

    enabled: Optional[bool] = None
    grid: Optional[Tuple[Entry, ...]] = None
    confidence_threshold: Optional[float] = None
    max_effort: Optional[str] = None


@dataclass(frozen=True)
class EffectiveSettings:
    """The frozen per-request snapshot the decision path consumes.

    Built once per request from the global :class:`Settings` and, when a surface has one, its
    :class:`PlatformPolicy`. A policy field overrides the global value; everything else is the
    global setting unchanged.
    """

    grid: Tuple[Entry, ...] = field(default_factory=lambda: tuple(DEFAULT_GRID))
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    default_model: str = DEFAULT_MODEL
    default_effort: str = DEFAULT_EFFORT
    max_effort: Optional[str] = None
    #: Whether ``grid`` is the surface's own parsed policy grid (``True``) or the global grid
    #: (``False``, whether from an absent policy or one whose grid did not parse). The decision
    #: path needs the distinction: an off-grid ``default_model`` resolves to the effective grid
    #: only when a policy grid is genuinely in force, so a surface the policy does not name keeps
    #: today's configured default verbatim.
    grid_from_policy: bool = False

    def entry_for(self, model_id: str) -> Optional[Entry]:
        return _entry_for(self.grid, model_id)


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
    #: A read-only mapping, so the frozen snapshot cannot be mutated through the field.
    platform_overrides: Mapping[str, PlatformPolicy] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __hash__(self) -> int:
        """Hash the snapshot, treating the read-only mapping as its frozenset of items.

        A frozen dataclass hashes its fields by default, but ``MappingProxyType`` is hashable
        only from Python 3.12 — and this plugin's matrix includes 3.11, where the default
        would make ``Settings`` unhashable. Hashing the mapping's items keeps the snapshot
        hashable on every supported interpreter without making it mutable.
        """
        parts = []
        for spec in fields(self):
            value = getattr(self, spec.name)
            if spec.name == "platform_overrides":
                value = frozenset(value.items())
            parts.append((spec.name, value))
        return hash(tuple(parts))

    @property
    def grid_ids(self) -> Tuple[str, ...]:
        return tuple(entry.model_id for entry in self.grid)

    def entry_for(self, model_id: str) -> Optional[Entry]:
        return _entry_for(self.grid, model_id)

    def policy_for(self, platform: str) -> Optional[PlatformPolicy]:
        """The policy declared for ``platform``, or ``None`` when it is unlisted."""
        key = (platform or "").strip().lower()
        return self.platform_overrides.get(key)

    def effective(self, policy: Optional[PlatformPolicy]) -> EffectiveSettings:
        """Resolve one frozen snapshot, applying ``policy`` over the global settings.

        A policy field of ``None`` inherits the global value; a policy ``grid`` that did not
        parse also inherits, so a bad policy grid never replaces the operator's global grid.
        """
        if policy is None:
            return EffectiveSettings(
                grid=self.grid,
                confidence_threshold=self.confidence_threshold,
                default_model=self.default_model,
                default_effort=self.default_effort,
                max_effort=None,
                grid_from_policy=False,
            )
        grid = policy.grid if policy.grid is not None else self.grid
        threshold = (
            policy.confidence_threshold
            if policy.confidence_threshold is not None
            else self.confidence_threshold
        )
        return EffectiveSettings(
            grid=grid,
            confidence_threshold=threshold,
            default_model=self.default_model,
            default_effort=self.default_effort,
            max_effort=policy.max_effort,
            grid_from_policy=policy.grid is not None,
        )


def parse_platform_overrides(raw: Any) -> Mapping[str, PlatformPolicy]:
    """Parse ``platform_overrides`` into frozen per-platform policies.

    An entry is kept only when its value is a mapping with at least one recognised key that
    parsed. Anything else — a non-map value, an unknown key, or a value that does not coerce —
    is dropped outright, so "unusable" and "unparseable grid alone" are observably the same
    thing: no policy, the global settings in force. Never raises.

    Returns a read-only ``MappingProxyType``: ``Settings`` is frozen, so the mapping it carries
    must be too, or the snapshot is silently mutable through the field.
    """
    if not isinstance(raw, dict):
        return MappingProxyType({})

    policies: Dict[str, PlatformPolicy] = {}
    for key, value in raw.items():
        platform = str(key or "").strip().lower()
        if not platform or not isinstance(value, dict):
            continue

        enabled = _as_optional_bool(value["enabled"]) if "enabled" in value else None
        threshold = (
            _as_optional_float(value["confidence_threshold"], minimum=0.0)
            if "confidence_threshold" in value
            else None
        )
        max_effort = (
            _as_optional_choice(value["max_effort"], EFFORT_LEVELS) if "max_effort" in value else None
        )
        grid = parse_grid_strict(value["grid"]) if "grid" in value else None

        if enabled is None and threshold is None and max_effort is None and grid is None:
            continue
        policies[platform] = PlatformPolicy(
            enabled=enabled,
            grid=grid,
            confidence_threshold=threshold,
            max_effort=max_effort,
        )
    return MappingProxyType(policies)


def endpoint_host(endpoint: str) -> str:
    """The lowercase hostname of a decision endpoint, or ``""`` when it cannot be parsed."""
    try:
        return (urlsplit(str(endpoint or "").strip()).hostname or "").lower()
    except (TypeError, ValueError):
        return ""


def is_requesty_endpoint(endpoint: str) -> bool:
    return "requesty" in endpoint_host(endpoint)


def resolve_endpoint(configured: str = DEFAULT_ENDPOINT) -> str:
    """The endpoint to call, given the configured one and the keys present.

    The two Decisions surfaces are not interchangeable: OpenRouter's is
    ``/api/alpha/decisions`` and Requesty's is ``/v1/systemone``. Sending a Requesty key to
    OpenRouter (or the reverse) is a 401, so the endpoint has to follow the key. An explicit
    non-default ``endpoint`` always wins, which keeps the custom-endpoint test path and
    self-hosted proxies working.

    Requesty takes precedence when both keys are present, matching the 2026-10-20 cutover
    carried in 0.3.1. Operators who want the OpenRouter surface while a Requesty key also
    exists should remove the Requesty key (the endpoint setting is deliberately not part of
    the public config schema).
    """
    text = str(configured or "").strip()
    if text and text != DEFAULT_ENDPOINT:
        return text
    if (os.environ.get("REQUESTY_API_KEY") or "").strip():
        return REQUESTY_ENDPOINT
    return DEFAULT_ENDPOINT


def default_jev_model(endpoint: str) -> str:
    """The decision-model id the endpoint actually serves.

    OpenRouter serves ``typesafe/jev-1.13`` (and 1.14); Requesty serves ``typesafe/jev-1.14.0``
    and the ``typesafe/jev-latest`` alias. The patch-level spelling matters: Requesty's docs
    name the model as ``typesafe/jev-1.14.0``, so the OpenRouter spelling cannot be reused.
    """
    return REQUESTY_DEFAULT_JEV_MODEL if is_requesty_endpoint(endpoint) else DEFAULT_JEV_MODEL


def load_settings(get_config: Optional[Callable[..., Any]] = None) -> Settings:
    """Build a :class:`Settings` from ``ctx.get_config`` (or from no config at all)."""
    read = get_config if callable(get_config) else (lambda _key, default=None: default)

    raw_grid = read("grid", None)
    grid = parse_grid(raw_grid) if raw_grid else tuple(DEFAULT_GRID)

    endpoint = resolve_endpoint(_as_text(read("endpoint", DEFAULT_ENDPOINT), DEFAULT_ENDPOINT))
    jev_model = _as_text(read("jev_model", DEFAULT_JEV_MODEL), DEFAULT_JEV_MODEL)
    if is_requesty_endpoint(endpoint) and jev_model == DEFAULT_JEV_MODEL:
        # The OpenRouter spelling is not a model Requesty serves; only override the *default*,
        # so a deliberately pinned id is left alone (and reports its own 404 if it is wrong).
        jev_model = REQUESTY_DEFAULT_JEV_MODEL

    settings = Settings(
        enabled=_as_bool(read("enabled", True), True),
        jev_model=jev_model,
        endpoint=endpoint,
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
        platform_overrides=parse_platform_overrides(read("platform_overrides", None)),
    )
    return settings


def _secret(name: str) -> str:
    """Read a credential through Hermes' profile-scoped resolver when it is available.

    A bare ``os.environ`` read is wrong under the multiplexing gateway: profile A's key would
    be visible to profile B's turn. ``agent.secret_scope.get_secret`` is the host's resolver —
    it honours the active profile's scope and fails closed when multiplexing is on with no
    scope installed. Single-profile deployments still fall through to the process environment,
    which is how cron and systemd inject keys.

    The import is lazy and the fallback keeps this module testable outside a Hermes install.
    """
    try:
        from agent.secret_scope import get_secret  # type: ignore
    except Exception:  # noqa: BLE001 - no host, no scope: the environment is the only source
        return (os.environ.get(name) or "").strip()
    try:
        return str(get_secret(name, "") or "").strip()
    except Exception:  # noqa: BLE001 - multiplexed with no scope: fail closed, never leak env
        return ""


def api_key(endpoint: str = "") -> str:
    """The key for ``endpoint``, from the active profile's secret scope.

    Never read from ``config.yaml`` and never returned anywhere it could be logged. The key is
    chosen by endpoint host, because the two Decisions surfaces authenticate separately:
    Requesty's ``/v1/systemone`` wants ``REQUESTY_API_KEY`` and OpenRouter's
    ``/api/alpha/decisions`` wants ``OPENROUTER_API_KEY``. An unknown/self-hosted endpoint gets
    Requesty first, then OpenRouter, preserving the 2026-10-20 cutover precedence.
    """
    host = endpoint_host(endpoint)
    if "requesty" in host:
        return _secret("REQUESTY_API_KEY")
    if "openrouter" in host:
        return _secret("OPENROUTER_API_KEY")
    return _secret("REQUESTY_API_KEY") or _secret("OPENROUTER_API_KEY")


def redact(value: Any, limit: int = 200) -> str:
    """Render a value for a log line with any credential-looking substring removed."""
    text = str(value)
    # Scrub every configured decision key, not just the one this endpoint selects: a log line
    # may quote a body produced while the other surface was active (or a key rotated mid-run).
    for key in {_secret("REQUESTY_API_KEY"), _secret("OPENROUTER_API_KEY")}:
        if key:
            text = text.replace(key, "[REDACTED]")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text
