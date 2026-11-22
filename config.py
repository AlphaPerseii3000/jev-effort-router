"""Resolved plugin settings.

Every tunable comes from one place so a callback, a tool handler, a slash command and the
CLI all read the same values. ``ctx.get_config`` returns ``plugins.entries.<plugin>.settings.<key>``
**or the default the caller passes in** — it does *not* merge ``plugin.yaml``'s ``config_schema``
defaults (``core/hermes_cli/plugins.py``), so the manifest only documents and feeds the UI. The
effective default of every setting is the constant this module falls back to in
:func:`load_settings`, and :func:`load_settings` only adds coercion, validation and the "value is
missing or nonsense" backstops.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

#: The level normalisation is shared with the resolver on purpose: the parser and the resolver
#: must agree on what a level is, or a declaration would mean one thing in the YAML and another
#: on the wire. ``effort`` never imports ``config`` at module scope (only inside the alias
#: lookup), so this direction cannot cycle.
from .effort import DEFAULT_OFFERED_LEVELS, normalise_level, normalise_levels, normalise_overrides
from .grid import (
    ANTHROPIC_GRID,
    CODEX_GRID,
    COMMANDCODE_GRID,
    DEFAULT_GRID,
    OPENCODE_CONSOLE_GRID,
    OPENCODE_GO_GRID,
    Entry,
    parse_grid,
    parse_grid_strict,
)

logger = logging.getLogger(__name__)

#: Provider profile this router is benchmarked for. Anything else is skipped untouched.
ROUTED_PROVIDER = "ollama-cloud"

#: Provider profile name aliases that also count as routed.
ROUTED_PROVIDER_ALIASES = ("ollama_cloud",)

#: OpenCode Console provider and aliases.
OPENCODE_CONSOLE_PROVIDER = "opencode-console"
OPENCODE_CONSOLE_PROVIDER_ALIASES = ("opencode_console",)

#: OpenCode Go provider and aliases: the generation route for the opt-in Console decider.
OPENCODE_GO_PROVIDER = "opencode-go"
OPENCODE_GO_PROVIDER_ALIASES = ("opencode_go",)

#: CommandCode provider and aliases: an opt-in OpenAI-compatible generation route.
COMMANDCODE_PROVIDER = "commandcode"
COMMANDCODE_PROVIDER_ALIASES = ("commandcode-chat", "commandcode_chat")

#: OpenAI Codex provider and aliases: the OAuth-authenticated Responses-API route. Its payload
#: shape is ``codex_responses``, not ``chat_completions``, so the router has to know both the
#: route and the mode it speaks.
CODEX_PROVIDER = "openai-codex"
CODEX_PROVIDER_ALIASES = ("codex", "openai_codex")

#: Anthropic provider and aliases: the opt-in Claude route. Its payload is the Anthropic Messages
#: body (``api_mode="anthropic_messages"``), where the level rides in ``output_config.effort``,
#: so the router has to know a third payload shape. The aliases mirror the core's own provider
#: table (``core/hermes_cli/providers.py``: ``"anthropic": ("claude", "claude-code")``).
ANTHROPIC_PROVIDER = "anthropic"
ANTHROPIC_PROVIDER_ALIASES = ("claude", "claude-code")

#: Console serves generation for the one normal (non-free) id its public model list carries.
#: The free variant was refused by the owner and is absent from every catalog.
OPENCODE_CONSOLE_MODELS: Tuple[str, ...] = ("deepseek-v4.1-flash",)
OPENCODE_CONSOLE_ALLOWLIST = frozenset(OPENCODE_CONSOLE_MODELS)

#: The Go generation route's catalog: one authorized normal model. Every generation catalog
#: this plugin carries now names the same single model, so the model question is degenerate
#: by construction and the decider effectively chooses only the reasoning level.
OPENCODE_GO_MODELS: Tuple[str, ...] = ("deepseek-v4.1-flash",)
OPENCODE_GO_ALLOWLIST = frozenset(OPENCODE_GO_MODELS)

#: CommandCode namespaces its model ids, so the id here is **not** the bare OpenCode one of
#: the same model — they are different contracts. Verified against the provider's public,
#: unauthenticated model list (``https://api.commandcode.ai/provider/v1/models``), which
#: publishes ``deepseek/deepseek-v4.1-flash``; the ``-fast`` variant is not authorized.
COMMANDCODE_MODELS: Tuple[str, ...] = ("deepseek/deepseek-v4.1-flash",)
COMMANDCODE_ALLOWLIST = frozenset(COMMANDCODE_MODELS)

#: The ``openai-codex`` route's catalog: the ids its Responses API serves, taken from the route's
#: own grid so the cap and the built-in catalog cannot drift apart. The ids are bare (no
#: ``vendor/`` prefix) — that is how this provider's model cache lists them.
CODEX_MODELS: Tuple[str, ...] = tuple(entry.model_id for entry in CODEX_GRID)
CODEX_ALLOWLIST = frozenset(CODEX_MODELS)

#: The ``anthropic`` route's catalog: the ids its Messages API serves, taken from the route's own
#: grid so the cap and the built-in catalog cannot drift apart. The ids are the bare ones the
#: provider's model cache lists (no ``anthropic/`` prefix, no date suffix on the 5.5 families).
ANTHROPIC_MODELS: Tuple[str, ...] = tuple(entry.model_id for entry in ANTHROPIC_GRID)
ANTHROPIC_ALLOWLIST = frozenset(ANTHROPIC_MODELS)

#: The model a route degrades to when the operator's ``default_model`` is not in its catalog.
#: ``anthropic`` defaults to Sonnet — the middle family — instead of the first grid entry
#: (Haiku), because a turn that loses its decision should keep a general-purpose model rather
#: than the cheapest one. Every other route keeps the first-catalog-entry behaviour.
ROUTE_DEFAULT_MODELS: Mapping[str, str] = MappingProxyType(
    {
        ANTHROPIC_PROVIDER: "claude-sonnet-5-5",
    }
)

#: The provider/alias pairs an operator grid may be declared under. The same table drives the
#: alias normalisation in :meth:`Settings.is_provider_routable`, so a route cannot be known to
#: the grid lookups while its aliases are forgotten by the routable check.
_GRID_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (ROUTED_PROVIDER, ROUTED_PROVIDER_ALIASES),
    (OPENCODE_CONSOLE_PROVIDER, OPENCODE_CONSOLE_PROVIDER_ALIASES),
    (OPENCODE_GO_PROVIDER, OPENCODE_GO_PROVIDER_ALIASES),
    (COMMANDCODE_PROVIDER, COMMANDCODE_PROVIDER_ALIASES),
    (CODEX_PROVIDER, CODEX_PROVIDER_ALIASES),
    (ANTHROPIC_PROVIDER, ANTHROPIC_PROVIDER_ALIASES),
)

#: API modes this router understands, keyed by the payload shape each one hands the middleware.
#:
#: ``chat_completions`` builds a ``messages`` payload that takes a top-level ``reasoning_effort``.
#: ``codex_responses`` is the OpenAI Responses shape: the transport has already turned the
#: params-level ``reasoning_config`` into ``reasoning: {effort, summary}`` and the conversation
#: into ``input``, so the router reads and writes a different payload (see
#: ``router.conversation_messages`` / ``router.RESPONSES_REASONING_KEY``).
#: ``anthropic_messages`` is the Anthropic Messages body: the transport has already turned
#: ``reasoning_config`` into ``thinking`` plus ``output_config: {effort}``
#: (``core/agent/anthropic_adapter.py::_thinking_kwargs``), so the level is rewritten there and
#: nowhere else (see ``router._apply_anthropic_effort``). Bedrock Converse builds somewhere else
#: and is left alone.
ROUTED_API_MODES = ("chat_completions", "codex_responses", "anthropic_messages")

#: -- Payload styles ----------------------------------------------------------------------------
#:
#: The **branch** that writes the decided level into a payload, named once here and dispatched on by
#: the router (``router._apply``). A style is a property of the route, not of the provider id: the
#: router never asks "is this provider X?", it asks "which container does this route's body carry?".
#: Adding a route is therefore a declaration in this table, and no unrelated branch changes.
WIRE_STYLE_TOP_LEVEL = "top_level"
WIRE_STYLE_RESPONSES = "responses"
WIRE_STYLE_OUTPUT_CONFIG = "output_config"

#: The style each payload shape implies. ``chat_completions`` takes the top-level
#: ``reasoning_effort`` — the OpenAI-compatible contract every route of this plugin has used since
#: 0.1.0. A route whose body puts the level somewhere its ``api_mode`` does not imply is not
#: supported: the one that did carried it in a nested ``extra_body`` and was removed in 0.8.0, so
#: this table is the whole mapping.
API_MODE_WIRE_STYLES: Mapping[str, str] = MappingProxyType(
    {
        "chat_completions": WIRE_STYLE_TOP_LEVEL,
        "codex_responses": WIRE_STYLE_RESPONSES,
        "anthropic_messages": WIRE_STYLE_OUTPUT_CONFIG,
    }
)

#: -- Session selection reconciliation ------------------------------------------------------------
#:
#: The webui writes the operator's model/effort choice to the session's own row (``state.db``,
#: ``sessions.model_config``) *and* is supposed to hand it to the agent. When the choice arrives
#: while the agent is already being built (``tui_gateway``), the row carries the choice and the live
#: agent does not: the turn runs on the profile's default and this plugin sees that default.
#: ``session_selection`` decides what the plugin does with the session's choice when its **own**
#: routing fails open — never when the decider succeeds.
#:
#: ``off`` (the default) keeps the router byte-for-byte identical to the behaviour before the key
#: existed: the row is not read at all (no ``sqlite3.connect``). ``warn`` reads it, records the
#: divergence in the audit and leaves the request alone. ``apply`` also reconciles the fail-open
#: payload to the session's model and effort.
SESSION_SELECTION_OFF = "off"
SESSION_SELECTION_WARN = "warn"
SESSION_SELECTION_APPLY = "apply"
SESSION_SELECTION_MODES: Tuple[str, ...] = (
    SESSION_SELECTION_OFF,
    SESSION_SELECTION_WARN,
    SESSION_SELECTION_APPLY,
)

#: The surfaces whose session row is authoritative. Only the webui writes the flat
#: ``model_config`` shape this plugin reads (``tui_gateway/session_workdir.py``); every other
#: client keeps its own contract and stays out until it is declared here.
DEFAULT_SESSION_SELECTION_PLATFORMS: Tuple[str, ...] = ("webui",)

#: Selection provider → provider of the request it is reconciled onto ("also valid in"). A selection
#: is always eligible on its **own** route (identity, see ``Router._eligible_selection``), so the
#: default is empty: identity only. This map exists for an operator who wants a pick made on one
#: provider to apply to a request already going out on another (an alias or a second route for the
#: same catalog) — declaring one here is opt-in. What a turn *costs* is decided by the credential
#: the core resolves for that route, never by the provider id.
DEFAULT_SESSION_SELECTION_PROVIDER_MAP: Mapping[str, str] = MappingProxyType({})

#: Providers that mean "the live agent is on the native Anthropic route". Consulted only **out of
#: the race**: while the route is not in ``routable_providers`` the plugin has no lever there — the
#: request goes out exactly as built — and the only honest answer is the warning (audit event
#: ``selection_native_provider``). Once ``anthropic`` is routable the route is routed like any other
#: and the billing question belongs to the credential the core resolves: Claude Code's OAuth
#: consumes the subscription quota, an ``sk-ant-api*`` API key bills per token.
NATIVE_ANTHROPIC_PROVIDERS: frozenset = frozenset({ANTHROPIC_PROVIDER, *ANTHROPIC_PROVIDER_ALIASES})

#: The levels the router may *ask* Jev for. ``max`` is part of the vocabulary — it can be
#: requested, accepted and audited — but it is offered by a route only
#: where its effort map declares it (``effort.offered_levels``), so no answer is asked for and
#: then clamped away.
EFFORT_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "max")

DECIDERS: Tuple[str, ...] = ("jev", "openai_compatible", "jev_console")
DEFAULT_DECIDER = "jev"
DEFAULT_ROUTABLE_PROVIDERS: Tuple[str, ...] = ("ollama-cloud",)
DEFAULT_OPENAI_COMPATIBLE_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_OPENAI_COMPATIBLE_MODEL = "deepseek-v4.1-flash"
DEFAULT_OPENAI_COMPATIBLE_KEY_ENV = "OPENCODE_GO_API_KEY"

DEFAULT_CONSOLE_BASE_URL = "https://opencode.ai/inference/systemone/v1/systemone"
DEFAULT_CONSOLE_JEV_MODEL = "jev-1.13-free"
DEFAULT_CONSOLE_JEV_FALLBACK_MODEL = "jev-1.13"
DEFAULT_CONSOLE_KEY_ENV = "OPENCODE_CONSOLE_API_KEY"

#: The ``jev_console`` quota breaker. A 429 from SystemOne is deterministic (“free usage limit
#: reached”), not a transient hiccup: after ``threshold`` consecutive 429 answers the decider stops
#: sending HTTP for ``ttl_s`` and the audit records one ``decider_unavailable_quota`` line per
#: window instead of one per request. Any non-429 outcome clears the counter.
CONSOLE_BREAKER_THRESHOLD_SETTING = "console_jev_breaker_threshold"
CONSOLE_BREAKER_TTL_SETTING = "console_jev_breaker_ttl_s"
DEFAULT_CONSOLE_BREAKER_THRESHOLD = 3
DEFAULT_CONSOLE_BREAKER_TTL_S = 600.0

#: The opt-in secondary transport for the ``jev_console`` decider: a SystemOne endpoint, a model
#: and the name of the variable holding its key. The **URL** is the whole opt-in — the upstream has
#: no ZDR agreement, so no default may enable it: with an empty ``console_jev_commandcode_base_url``
#: the fallback is disabled and nothing else happens. The other two settings only say *what* to ask
#: that endpoint and default to the pair every deployment writes anyway, so a profile that opts in
#: only has to name the endpoint.
COMMANDCODE_BASE_URL_SETTING = "console_jev_commandcode_base_url"
COMMANDCODE_MODEL_SETTING = "console_jev_commandcode_model"
COMMANDCODE_KEY_ENV_SETTING = "console_jev_commandcode_api_key_env"

#: Applied only once :data:`COMMANDCODE_BASE_URL_SETTING` is non-empty (see
#: :func:`resolve_commandcode_fallback`). They are never enough to enable the fallback on their own.
DEFAULT_COMMANDCODE_MODEL = "typesafe/jev"
DEFAULT_COMMANDCODE_KEY_ENV = "COMMANDCODE_API_KEY"

DEFAULT_JEV_MODEL = "typesafe/jev-1.13"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_CONFIDENCE_THRESHOLD = 0.5
DEFAULT_MODEL = "deepseek-v4.1-flash"
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


def _provider_group(provider: str) -> frozenset:
    """Every name (canonical plus aliases) denoting the same route as ``provider``.

    Empty for a provider this router does not know, which callers treat as "match that name
    verbatim". One table backs this, so registering a route cannot leave an alias behind.
    """
    key = (provider or "").strip().lower()
    for name, aliases in _GRID_ALIASES:
        group = (name, *aliases)
        if key in group:
            return frozenset(group)
    return frozenset()


def canonical_provider_name(provider: str) -> Optional[str]:
    """The canonical name of a known route **alias**, or ``None``.

    Only an alias resolves: the canonical name itself answers ``None`` because the caller already
    tried it verbatim. This is the one table ``_GRID_ALIASES``; nothing else registers aliases.
    """
    key = (provider or "").strip().lower()
    for name, aliases in _GRID_ALIASES:
        if key in aliases:
            return name
    return None


def same_provider_route(first: str, second: str) -> bool:
    """Whether two provider ids denote the same route (a canonical name or any alias of it).

    The same table that backs :meth:`Settings.is_provider_routable`, reused wherever two ids have
    to be compared for *identity* instead of being translated: a name this router does not know is
    only equal to itself, never to another unknown name.
    """
    key = (first or "").strip().lower()
    other = (second or "").strip().lower()
    if not key or not other:
        return False
    group = _provider_group(key)
    return other in group if group else key == other


def route_allowlist(provider: str, decider: str = "") -> Optional[frozenset]:
    """The allowlist that pins ``provider``'s catalog, or ``None`` when it has none.

    ``opencode-console`` is pinned at all times: its models are the Console's own. The
    ``opencode-go``, ``commandcode``, ``openai-codex`` and ``anthropic`` caps are **opt-in** — they
    are in force only while the ``jev_console`` decider is active, so a legacy
    ``jev``/``openai_compatible`` setup keeps whatever grid the operator configured, unchanged.
    """
    key = (provider or "").strip().lower()
    if key in (OPENCODE_CONSOLE_PROVIDER, *OPENCODE_CONSOLE_PROVIDER_ALIASES):
        return OPENCODE_CONSOLE_ALLOWLIST
    if key in (OPENCODE_GO_PROVIDER, *OPENCODE_GO_PROVIDER_ALIASES):
        return OPENCODE_GO_ALLOWLIST if (decider or "").strip().lower() == "jev_console" else None
    if key in (COMMANDCODE_PROVIDER, *COMMANDCODE_PROVIDER_ALIASES):
        return COMMANDCODE_ALLOWLIST if (decider or "").strip().lower() == "jev_console" else None
    if key in (CODEX_PROVIDER, *CODEX_PROVIDER_ALIASES):
        return CODEX_ALLOWLIST if (decider or "").strip().lower() == "jev_console" else None
    if key in (ANTHROPIC_PROVIDER, *ANTHROPIC_PROVIDER_ALIASES):
        return ANTHROPIC_ALLOWLIST if (decider or "").strip().lower() == "jev_console" else None
    return None


def route_catalog(provider: str) -> Optional[Tuple[Entry, ...]]:
    """The built-in generation catalog of an opt-in route, or ``None`` for any other provider."""
    key = (provider or "").strip().lower()
    if key in (OPENCODE_CONSOLE_PROVIDER, *OPENCODE_CONSOLE_PROVIDER_ALIASES):
        return OPENCODE_CONSOLE_GRID
    if key in (OPENCODE_GO_PROVIDER, *OPENCODE_GO_PROVIDER_ALIASES):
        return OPENCODE_GO_GRID
    if key in (COMMANDCODE_PROVIDER, *COMMANDCODE_PROVIDER_ALIASES):
        return COMMANDCODE_GRID
    if key in (CODEX_PROVIDER, *CODEX_PROVIDER_ALIASES):
        return CODEX_GRID
    if key in (ANTHROPIC_PROVIDER, *ANTHROPIC_PROVIDER_ALIASES):
        return ANTHROPIC_GRID
    return None


def route_default_model(provider: str) -> Optional[str]:
    """The route's own degrade-to model, or ``None`` when it uses its first catalog entry.

    Only a route that declares one (see :data:`ROUTE_DEFAULT_MODELS`) overrides the generic
    "first entry of the catalog" rule that the other opt-in routes follow.
    """
    key = (provider or "").strip().lower()
    return ROUTE_DEFAULT_MODELS.get(key) or ROUTE_DEFAULT_MODELS.get(
        canonical_provider_name(key) or ""
    )


def wire_style(api_mode: str = "") -> str:
    """The payload branch :meth:`router.Router._apply` must use for this request.

    Answered by the ``api_mode`` the route speaks (:data:`API_MODE_WIRE_STYLES`): the router is told
    *which container* the body carries instead of branching on provider ids. An empty ``api_mode``
    or an unknown one falls to the top-level ``reasoning_effort`` — the OpenAI-compatible default of
    this plugin, which is what the router did before styles existed.
    """
    return API_MODE_WIRE_STYLES.get((api_mode or "").strip().lower(), WIRE_STYLE_TOP_LEVEL)


def _capped(
    grid: Tuple[Entry, ...],
    allowlist: Optional[frozenset],
    fallback: Optional[Tuple[Entry, ...]],
) -> Tuple[Entry, ...]:
    """``grid`` restricted to ``allowlist``; ``fallback`` when that leaves nothing on it."""
    if allowlist is None:
        return grid
    capped = tuple(entry for entry in grid if entry.model_id in allowlist)
    if capped:
        return capped
    return fallback if fallback is not None else grid


@dataclass(frozen=True)
class CommandCodeFallback:
    """The opt-in SystemOne secondary transport, resolved from three operator settings.

    ``configured`` and ``enabled`` are kept apart on purpose: a half-written trio is an operator
    mistake that must disable **only** the fallback, never the legacy decider chain, and the
    status surface has to be able to say "configured but not enabled" without guessing.

    ``reason`` is a sanitized explanation naming the setting at fault — never its value. A URL
    that carries credentials is dropped from ``url`` for the same reason: nothing that could hold
    a secret is kept around for a status line or a log record.
    """

    url: str = ""
    model: str = ""
    key_env: str = ""
    reason: str = ""

    @property
    def configured(self) -> bool:
        """Whether the operator set any of the three settings at all."""
        return bool(self.url or self.model or self.key_env or self.reason)

    @property
    def enabled(self) -> bool:
        """Whether the trio is complete and valid, so the transport may actually be consulted."""
        return self.configured and not self.reason


def _validated_fallback_url(raw: str) -> Tuple[str, str]:
    """``(url, problem)`` — the endpoint a fallback may POST to, or why it is unusable.

    A complete SystemOne endpoint is required, not a base URL with a suffix appended: the path is
    the provider's contract and is not this plugin's to compose. HTTPS only, and no userinfo,
    query or fragment — each of those would either leak a credential into an audit line or send
    the decision to something other than the documented endpoint.
    """
    text = (raw or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        # ``urlsplit`` validates the netloc itself and raises on a malformed host (an unclosed
        # IPv6 bracket, for instance) instead of returning something inspectable. A typo in the
        # fallback's endpoint must disable only the fallback: settings loading, the legacy chain
        # and the status surface all keep working.
        return "", "must be a valid https:// URL"
    if parts.scheme.strip().lower() != "https":
        return "", "must be an https:// URL"
    if not parts.netloc.strip():
        return "", "must name a host"
    if parts.username or parts.password:
        return "", "must not carry credentials"
    if parts.query or parts.fragment:
        return "", "must not carry a query or a fragment"
    return text, ""


def resolve_commandcode_fallback(*, base_url: Any, model: Any, api_key_env: Any) -> CommandCodeFallback:
    """Resolve the secondary-transport trio. Never raises; an invalid trio is simply disabled.

    ``base_url`` gates everything: it is the operator's explicit consent to send context to an
    upstream without a ZDR agreement, so an empty one returns the disabled fallback with no reason
    — a leftover ``model`` or ``api_key_env`` is then inert, not an operator mistake worth a
    warning. Once the URL is declared, the two fields that only say *what* to ask it complete
    themselves with the documented pair (:data:`DEFAULT_COMMANDCODE_MODEL`,
    :data:`DEFAULT_COMMANDCODE_KEY_ENV`), because there is nothing deployment-specific to choose.
    """
    url = str(base_url or "").strip()
    if not url:
        return CommandCodeFallback()

    chosen_model = str(model or "").strip() or DEFAULT_COMMANDCODE_MODEL
    key_env = str(api_key_env or "").strip() or DEFAULT_COMMANDCODE_KEY_ENV

    # The endpoint is validated (and an invalid one dropped) before anything else: a URL that
    # embedded a credential must never survive into a status line or a log record, not even on the
    # path that is about to be rejected.
    clean_url, problem = _validated_fallback_url(url)
    if problem:
        return CommandCodeFallback(
            model=chosen_model,
            key_env=key_env,
            reason=f"{COMMANDCODE_BASE_URL_SETTING} {problem}",
        )
    return CommandCodeFallback(url=clean_url, model=chosen_model, key_env=key_env)


@dataclass(frozen=True)
class SessionSelectionSettings:
    """How the webui session's own model/effort selection is treated, under ``session_selection``.

    Three answers, one per ``mode``:

    * ``off`` — the row is not read; every payload, audit record and log line stays what it was;
    * ``warn`` — the row is read, a divergence between it and the request becomes an audit event
      (``selection_divergence``) plus a log ``WARNING``, and the request is left alone;
    * ``apply`` — the same audit, and a turn this router **fails open** is rewritten to the
      session's model and effort. A successful decision is never overridden: this is fallback
      for the fail-open path, not a ceiling or a pin on the decider.

    ``platforms`` names the surfaces whose row is authoritative. A selection is always eligible on
    its own route; ``provider_map`` adds a **second** route the same choice is also valid on. It is
    empty by default, which means identity only: the plugin never translates a pick onto a route
    the row did not name unless an operator declares that translation.
    """

    mode: str = SESSION_SELECTION_OFF
    platforms: Tuple[str, ...] = DEFAULT_SESSION_SELECTION_PLATFORMS
    provider_map: Mapping[str, str] = field(
        default_factory=lambda: DEFAULT_SESSION_SELECTION_PROVIDER_MAP
    )

    @property
    def enabled(self) -> bool:
        """Whether the session row may be read at all (``warn`` and ``apply``)."""
        return self.mode != SESSION_SELECTION_OFF

    @property
    def applies(self) -> bool:
        """Whether a fail-open payload may be rewritten to the session's selection."""
        return self.mode == SESSION_SELECTION_APPLY

    @property
    def warns(self) -> bool:
        """Whether a divergence also raises a ``WARNING`` in the log, not just an audit event."""
        return self.mode in (SESSION_SELECTION_WARN, SESSION_SELECTION_APPLY)

    def applies_to(self, platform: str) -> bool:
        """Whether ``platform``'s session row is authoritative for this deployment."""
        key = (platform or "").strip().lower()
        return bool(key) and key in self.platforms

    def provider_for(self, provider: str) -> str:
        """The **additional** route a selection provider is valid on, beside its own.

        Identity (``provider`` itself, aliases included) is :func:`same_provider_route`'s answer and
        needs no entry here; an empty answer falls back to the name unchanged, which is what an
        unmapped provider means.
        """
        key = (provider or "").strip().lower()
        return self.provider_map.get(key, key)

    def __hash__(self) -> int:
        """Hash the policy, with the read-only provider map as a frozenset of its items.

        Same reason as :meth:`Settings.__hash__`: ``MappingProxyType`` is hashable only from
        Python 3.12, and this plugin's matrix includes 3.11.
        """
        return hash((self.mode, self.platforms, _make_hashable(self.provider_map)))


@dataclass(frozen=True)
class EffectiveSettings:
    """The frozen per-request snapshot the decision path consumes.

    Built once per request from the global :class:`Settings` and the provider's route catalog.
    """

    grid: Tuple[Entry, ...] = field(default_factory=lambda: tuple(DEFAULT_GRID))
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    default_model: str = DEFAULT_MODEL
    default_effort: str = DEFAULT_EFFORT

    def entry_for(self, model_id: str) -> Optional[Entry]:
        return _entry_for(self.grid, model_id)


def _make_hashable(val: Any) -> Any:
    if isinstance(val, (dict, Mapping)):
        return frozenset((k, _make_hashable(v)) for k, v in val.items())
    if isinstance(val, (list, tuple)):
        return tuple(_make_hashable(x) for x in val)
    if isinstance(val, (set, frozenset)):
        return frozenset(_make_hashable(x) for x in val)
    return val


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
    decider: str = DEFAULT_DECIDER
    decider_base_url: str = ""
    decider_model: str = ""
    decider_api_key_env: str = ""
    console_endpoint: str = ""
    console_jev_model: str = ""
    console_jev_fallback_model: str = DEFAULT_CONSOLE_JEV_FALLBACK_MODEL
    #: Quota breaker for the ``jev_console`` decider: N consecutive HTTP 429 answers open it and
    #: no request reaches SystemOne until the TTL expires (see :class:`decider.QuotaBreaker`).
    console_jev_breaker_threshold: int = DEFAULT_CONSOLE_BREAKER_THRESHOLD
    console_jev_breaker_ttl_s: float = DEFAULT_CONSOLE_BREAKER_TTL_S
    console_api_key_env: str = ""
    #: The opt-in SystemOne secondary transport. All three default to empty = disabled, and the
    #: trio is only ever consulted by the ``jev_console`` decider (see
    #: :meth:`active_commandcode_fallback`).
    console_jev_commandcode_base_url: str = ""
    console_jev_commandcode_api_key_env: str = ""
    console_jev_commandcode_model: str = ""
    routable_providers: Tuple[str, ...] = DEFAULT_ROUTABLE_PROVIDERS
    grids: Mapping[str, Tuple[Entry, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    effort_maps: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: The webui session-selection reconciliation (see :class:`SessionSelectionSettings`).
    #: Defaults to ``mode: off``, which leaves every existing deployment untouched.
    session_selection: SessionSelectionSettings = field(
        default_factory=SessionSelectionSettings
    )

    def __hash__(self) -> int:
        """Hash the snapshot, treating read-only mappings as frozensets.

        A frozen dataclass hashes its fields by default, but ``MappingProxyType`` is hashable
        only from Python 3.12 — and this plugin's matrix includes 3.11, where the default
        would make ``Settings`` unhashable. Hashing the mapping's items keeps the snapshot
        hashable on every supported interpreter without making it mutable.
        """
        parts = []
        for spec in fields(self):
            value = getattr(self, spec.name)
            parts.append((spec.name, _make_hashable(value)))
        return hash(tuple(parts))

    @property
    def grid_ids(self) -> Tuple[str, ...]:
        return tuple(entry.model_id for entry in self.grid)

    def entry_for(self, model_id: str) -> Optional[Entry]:
        return _entry_for(self.grid, model_id)

    def grid_for(self, provider: str = "") -> Tuple[Entry, ...]:
        """The grid for ``provider``, or the global grid when unlisted.

        An opt-in route is pinned by its catalog (see :func:`route_allowlist`): an entry
        outside the catalog is dropped, and a grid the cap empties falls back to the
        route's built-in catalog. Choice, fallback, alternatives and replay all read their
        options from here, so none of them can name a model the route does not serve.
        """
        key = (provider or "").strip().lower()
        configured: Optional[Tuple[Entry, ...]] = None
        if key in self.grids:
            configured = self.grids[key]
        else:
            for name, aliases in _GRID_ALIASES:
                if key in aliases and name in self.grids:
                    configured = self.grids[name]
                    break
        allowlist = route_allowlist(key, self.decider)
        catalog = route_catalog(key)
        if allowlist is None:
            return configured if configured is not None else self.grid
        return _capped(
            configured if configured is not None else (catalog or self.grid),
            allowlist,
            catalog,
        )

    def effort_map_for(self, provider: str = "") -> Optional[Any]:
        """The effort map for ``provider``, or None when unlisted."""
        key = (provider or "").strip().lower()
        if key in self.effort_maps:
            return self.effort_maps[key]
        if key in ROUTED_PROVIDER_ALIASES and ROUTED_PROVIDER in self.effort_maps:
            return self.effort_maps[ROUTED_PROVIDER]
        return None

    def api_key_env_for(self, decider: Optional[str] = None) -> str:
        """The environment variable name holding the API key for ``decider``."""
        target = (decider or self.decider).strip().lower()
        if target == "openai_compatible":
            return self.decider_api_key_env or DEFAULT_OPENAI_COMPATIBLE_KEY_ENV
        if target == "jev_console":
            return self.console_api_key_env or DEFAULT_CONSOLE_KEY_ENV
        return "OPENROUTER_API_KEY"

    def base_url_for(self, decider: Optional[str] = None) -> str:
        """The base URL for ``decider``."""
        target = (decider or self.decider).strip().lower()
        if target == "openai_compatible":
            return self.decider_base_url or DEFAULT_OPENAI_COMPATIBLE_BASE_URL
        if target == "jev_console":
            return self.console_endpoint or DEFAULT_CONSOLE_BASE_URL
        return self.endpoint

    @property
    def base_url(self) -> str:
        """The effective base URL for the active decider."""
        return self.base_url_for()

    def decider_model_for(self, decider: Optional[str] = None) -> str:
        """The decision model id for ``decider``."""
        target = (decider or self.decider).strip().lower()
        if target == "openai_compatible":
            return self.decider_model or DEFAULT_OPENAI_COMPATIBLE_MODEL
        if target == "jev_console":
            return self.console_jev_model or DEFAULT_CONSOLE_JEV_MODEL
        return self.jev_model

    def commandcode_fallback(self) -> CommandCodeFallback:
        """The configured SystemOne secondary transport, independent of the active decider."""
        return resolve_commandcode_fallback(
            base_url=self.console_jev_commandcode_base_url,
            model=self.console_jev_commandcode_model,
            api_key_env=self.console_jev_commandcode_api_key_env,
        )

    def active_commandcode_fallback(self) -> CommandCodeFallback:
        """The secondary transport as the **active** decider may use it.

        The trio belongs to ``jev_console``. Any other decider sees an empty fallback, so a legacy
        ``jev``/``openai_compatible`` deployment keeps byte-identical payloads, audit records and
        status output even if the three settings happen to be present in its config.
        """
        if (self.decider or "").strip().lower() != "jev_console":
            return CommandCodeFallback()
        return self.commandcode_fallback()

    def is_provider_routable(self, provider: str) -> bool:
        """Whether ``provider`` is in the configured routable providers list.

        A canonical name and its aliases denote the same route, so declaring either side
        counts for both (``opencode-go``/``opencode_go``, ``commandcode``/``commandcode-chat``,
        ...). Normalisation is driven by one table, so no route can be routable under its
        canonical name while its aliases are ignored.
        """
        key = (provider or "").strip().lower()
        if not key:
            return any(
                p.strip().lower() in (ROUTED_PROVIDER, *ROUTED_PROVIDER_ALIASES)
                for p in self.routable_providers
            )
        group = _provider_group(key) or {key}
        return any(p.strip().lower() in group for p in self.routable_providers)

    def effective(self, provider: str = "") -> EffectiveSettings:
        """Resolve one frozen snapshot for ``provider``'s route.

        The route's grid governs the snapshot; when the route pins a catalog, an off-catalog
        ``default_model`` is re-pinned to the route's own default so the decision path can never
        name a model the route does not serve.
        """
        base_grid = self.grid_for(provider)
        key = (provider or "").strip().lower()
        allowlist = route_allowlist(key, self.decider)
        default_model = self.default_model
        if allowlist is not None and default_model not in allowlist:
            default_model = (
                route_default_model(key) or (base_grid[0].model_id if base_grid else DEFAULT_MODEL)
            )
        return EffectiveSettings(
            grid=base_grid,
            confidence_threshold=self.confidence_threshold,
            default_model=default_model,
            default_effort=self.default_effort,
        )


def _parse_session_platforms(raw: Any) -> Tuple[str, ...]:
    """``session_selection.platforms``: the surfaces whose session row is authoritative.

    Absent/null keeps :data:`DEFAULT_SESSION_SELECTION_PLATFORMS`. A comma-separated string is
    accepted for parity with ``routable_providers``. A declaration that keeps no usable name —
    including an explicit empty list — falls back to the default **with a warning**: asking for
    ``mode: apply`` on no surface at all is an operator error, and silently reconciling nothing
    would hide it.
    """
    if raw is None:
        return DEFAULT_SESSION_SELECTION_PLATFORMS
    if isinstance(raw, str):
        declared = tuple(part.strip() for part in raw.split(",") if part.strip())
    elif isinstance(raw, Sequence):
        declared = tuple(str(part).strip() for part in raw if str(part).strip())
    else:
        logger.warning(
            "jev-effort-router: session_selection.platforms must be a list of surfaces; "
            "keeping %s",
            ", ".join(DEFAULT_SESSION_SELECTION_PLATFORMS),
        )
        return DEFAULT_SESSION_SELECTION_PLATFORMS
    if not declared:
        logger.warning(
            "jev-effort-router: session_selection.platforms names no surface; keeping %s",
            ", ".join(DEFAULT_SESSION_SELECTION_PLATFORMS),
        )
        return DEFAULT_SESSION_SELECTION_PLATFORMS
    normalised = tuple(dict.fromkeys(part.lower() for part in declared))
    return normalised


def _parse_session_provider_map(raw: Any) -> Mapping[str, str]:
    """``session_selection.provider_map``: selection provider → request provider.

    Absent/null keeps the built-in map, which is empty (identity only). A declared map keeps only
    entries whose both sides are non-empty strings, lower-cased; an explicitly empty declaration
    stays empty, and a value that is not a mapping at all falls back to the built-in map with a
    warning.
    """
    if raw is None:
        return DEFAULT_SESSION_SELECTION_PROVIDER_MAP
    if not isinstance(raw, Mapping):
        logger.warning(
            "jev-effort-router: session_selection.provider_map must be a mapping of "
            "provider to provider; keeping the identity-only default"
        )
        return DEFAULT_SESSION_SELECTION_PROVIDER_MAP
    parsed: Dict[str, str] = {}
    for key, value in raw.items():
        source = str(key or "").strip().lower()
        target = str(value or "").strip().lower() if isinstance(value, str) else ""
        if source and target:
            parsed[source] = target
    return MappingProxyType(parsed)


def parse_session_selection(raw: Any) -> SessionSelectionSettings:
    """Parse ``session_selection`` into a frozen policy. Never raises.

    ``mode`` is the only load-bearing knob, and an unusable one falls back to ``off`` with a
    warning: a typo must degrade to "the plugin behaves exactly as it did before", never to a
    request rewritten on a policy nobody declared.
    """
    if raw is None:
        return SessionSelectionSettings()
    if not isinstance(raw, Mapping):
        logger.warning(
            "jev-effort-router: session_selection must be a mapping with a mode; keeping reconciliation off"
        )
        return SessionSelectionSettings()

    raw_mode = raw.get("mode", SESSION_SELECTION_OFF)
    mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
    if mode not in SESSION_SELECTION_MODES:
        logger.warning(
            "jev-effort-router: session_selection.mode is not one of %s — keeping reconciliation off",
            "/".join(SESSION_SELECTION_MODES),
        )
        mode = SESSION_SELECTION_OFF

    return SessionSelectionSettings(
        mode=mode,
        platforms=_parse_session_platforms(raw.get("platforms")),
        provider_map=_parse_session_provider_map(raw.get("provider_map")),
    )


def parse_grids(raw: Any) -> Mapping[str, Tuple[Entry, ...]]:
    """Parse per-provider grid overrides."""
    if not isinstance(raw, dict):
        return MappingProxyType({})
    result: Dict[str, Tuple[Entry, ...]] = {}
    for key, value in raw.items():
        provider = str(key or "").strip().lower()
        if not provider:
            continue
        parsed = parse_grid_strict(value)
        if parsed:
            result[provider] = parsed
    return MappingProxyType(result)


def _freeze_effort_spec(spec: Any) -> Any:
    """Freeze an effort map spec into immutable, normalised structures.

    Levels are strings, ``strip()``ped and lower-cased. YAML ``null``, Python ``None``, numbers
    and booleans are **not** levels: they are dropped instead of being stringified — ``str(None)``
    used to become the valid level ``"none"``, which is exactly the bug that made a null entry
    look like a declared disable. The vocabulary is not filtered against the core's ladder: a
    route may declare its own words.

    ``min``/``max`` are carried through as levels (or ``None``), never as stringified nulls: an
    entry that declares bounds has its accepted set derived from them by
    :func:`effort.parse_effort_spec`, so the pair survives the freeze even when it is incomplete.
    """
    if isinstance(spec, (list, tuple, set, frozenset)):
        return normalise_levels(spec)
    if isinstance(spec, Mapping):
        return MappingProxyType({
            "accepted": normalise_levels(spec.get("accepted")),
            "overrides": normalise_overrides(spec.get("overrides")),
            "min": normalise_level(spec.get("min")),
            "max": normalise_level(spec.get("max")),
        })
    return spec


def parse_effort_maps(raw: Any) -> Mapping[str, Any]:
    """Parse per-provider effort maps into deep immutable structures.

    Provider and model keys go through the same normalisation as the levels: a key that is not
    a non-empty string (``null``, a number, a boolean) is dropped rather than turned into a name.
    """
    if not isinstance(raw, dict):
        return MappingProxyType({})
    result: Dict[str, Any] = {}
    for key, value in raw.items():
        provider = normalise_level(key)
        if provider is None:
            continue
        if isinstance(value, (dict, Mapping)):
            provider_map: Dict[str, Any] = {}
            for model_key, spec in value.items():
                model_name = normalise_level(model_key)
                if model_name is None:
                    continue
                provider_map[model_name] = _freeze_effort_spec(spec)
            result[provider] = MappingProxyType(provider_map)
        else:
            result[provider] = MappingProxyType({})
    return MappingProxyType(result)


def load_settings(get_config: Optional[Callable[..., Any]] = None) -> Settings:
    """Build a :class:`Settings` from ``ctx.get_config`` (or from no config at all)."""
    read = get_config if callable(get_config) else (lambda _key, default=None: default)

    raw_grid = read("grid", None)
    grid = parse_grid(raw_grid) if raw_grid else tuple(DEFAULT_GRID)

    raw_routable = read("routable_providers", None)
    if raw_routable is None:
        routable_providers = DEFAULT_ROUTABLE_PROVIDERS
    elif isinstance(raw_routable, str):
        routable_providers = tuple(p.strip() for p in raw_routable.split(",") if p.strip())
    elif isinstance(raw_routable, Sequence):
        routable_providers = tuple(str(p).strip() for p in raw_routable if str(p).strip())
    else:
        routable_providers = DEFAULT_ROUTABLE_PROVIDERS

    _marker = object()
    raw_console_model = read("console_jev_model", _marker)
    if raw_console_model is _marker or raw_console_model is None:
        console_jev_model = ""
    else:
        console_jev_model = str(raw_console_model).strip()

    raw_fallback = read("console_jev_fallback_model", _marker)
    if raw_fallback is _marker or raw_fallback is None:
        console_jev_fallback = DEFAULT_CONSOLE_JEV_FALLBACK_MODEL
    else:
        txt = str(raw_fallback).strip()
        if txt.lower() in ("none", "false", "off", "disabled") or txt == "":
            console_jev_fallback = ""
        else:
            console_jev_fallback = txt

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
        default_effort=_as_choice(
            read("default_effort", DEFAULT_EFFORT), DEFAULT_OFFERED_LEVELS, DEFAULT_EFFORT
        ),
        context_turns=_as_int(
            read("context_turns", DEFAULT_CONTEXT_TURNS), DEFAULT_CONTEXT_TURNS, minimum=0, maximum=50
        ),
        route_per_turn=_as_bool(read("route_per_turn", True), True),
        audit_enabled=_as_bool(read("audit_enabled", True), True),
        log_skips=_as_bool(read("log_skips", True), True),
        include_user_message_in_audit=_as_bool(read("include_user_message_in_audit", False), False),
        grid=grid,
        decider=_as_choice(read("decider", DEFAULT_DECIDER), DECIDERS, DEFAULT_DECIDER),
        decider_base_url=_as_text(read("decider_base_url", ""), ""),
        decider_model=_as_text(read("decider_model", ""), ""),
        decider_api_key_env=_as_text(read("decider_api_key_env", ""), ""),
        console_endpoint=_as_text(read("console_endpoint", ""), ""),
        console_jev_model=console_jev_model,
        console_jev_fallback_model=console_jev_fallback,
        console_jev_breaker_threshold=_as_int(
            read(CONSOLE_BREAKER_THRESHOLD_SETTING, DEFAULT_CONSOLE_BREAKER_THRESHOLD),
            DEFAULT_CONSOLE_BREAKER_THRESHOLD,
            minimum=1,
        ),
        console_jev_breaker_ttl_s=_as_float(
            read(CONSOLE_BREAKER_TTL_SETTING, DEFAULT_CONSOLE_BREAKER_TTL_S),
            DEFAULT_CONSOLE_BREAKER_TTL_S,
            minimum=1.0,
        ),
        console_api_key_env=_as_text(read("console_api_key_env", ""), ""),
        console_jev_commandcode_base_url=_as_text(read(COMMANDCODE_BASE_URL_SETTING, ""), ""),
        console_jev_commandcode_api_key_env=_as_text(read(COMMANDCODE_KEY_ENV_SETTING, ""), ""),
        console_jev_commandcode_model=_as_text(read(COMMANDCODE_MODEL_SETTING, ""), ""),
        routable_providers=routable_providers if routable_providers else DEFAULT_ROUTABLE_PROVIDERS,
        grids=parse_grids(read("grids", None)),
        effort_maps=parse_effort_maps(read("effort_maps", None)),
        session_selection=parse_session_selection(read("session_selection", None)),
    )
    # Sanitized on purpose: the message names the setting at fault and never its value, and it is
    # emitted once per settings load — even when the active decider is not jev_console, because a
    # malformed endpoint is an operator error worth surfacing. A broken trio disables only the
    # fallback, so the legacy chain keeps working exactly as it does without the three keys. An
    # empty ``console_jev_commandcode_base_url`` is *not* an error: it is the opt-in's default.
    fallback = settings.commandcode_fallback()
    if fallback.configured and not fallback.enabled:
        logger.warning(
            "jev-effort-router: the console_jev_commandcode_* fallback is disabled — %s",
            fallback.reason,
        )
    return settings


def api_key(env_name: Optional[str] = None) -> str:
    """The API key, from the environment only.

    Defaults to OPENROUTER_API_KEY when env_name is not provided.
    Never read from ``config.yaml`` and never returned anywhere it could be logged.
    """
    key_name = (env_name or "OPENROUTER_API_KEY").strip()
    return (os.environ.get(key_name) or "").strip()


def redact(value: Any, limit: int = 200, env_name: Optional[str] = None) -> str:
    """Render a value for a log line with any credential-looking substring removed."""
    text = str(value)
    keys_to_redact = set()
    if env_name:
        k = api_key(env_name)
        if k:
            keys_to_redact.add(k)
    k_default = api_key("OPENROUTER_API_KEY")
    if k_default:
        keys_to_redact.add(k_default)
    k_opencode = api_key("OPENCODE_GO_API_KEY")
    if k_opencode:
        keys_to_redact.add(k_opencode)
    k_console = api_key("OPENCODE_CONSOLE_API_KEY")
    if k_console:
        keys_to_redact.add(k_console)
    k_commandcode = api_key("COMMANDCODE_API_KEY")
    if k_commandcode:
        keys_to_redact.add(k_commandcode)

    for key in keys_to_redact:
        text = text.replace(key, "[REDACTED]")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text
