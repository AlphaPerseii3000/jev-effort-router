"""Reasoning-effort translation: declared vocabularies, and the legacy Ollama fallback.

Two contracts live here, and only one of them guesses.

* **Declared** — an operator ``effort_maps`` entry and every strict built-in route
  (``opencode-console``, ``opencode-go``, ``commandcode``, ``openai-codex``, ``anthropic``). The declaration is
  authoritative: a requested level that the declaration accepts goes out verbatim; otherwise a
  *declared* override whose target is itself accepted is used; otherwise the field is omitted.
  There is no ordering, no nearest-weaker clamp and no anti-escalation rule: ``xhigh → max``
  works because it is written down, and a level nobody declared is never invented.
  ``"none"`` is an ordinary level here — valid when (and only when) it is declared — while
  Python ``None`` (and empty/whitespace) means "omit the field".

* **Bounded** — an operator map entry that declares ``min``/``max`` per model instead of a
  level list. The pair *is* the contract: the accepted set is the levels between the bounds, a
  request inside them goes out verbatim and a request outside them is **clamped to the nearest
  bound** (:func:`resolve_bounded_effort`) — never omitted, because a declared ceiling that
  cannot hold is not a ceiling. A bounded grid also changes the effort question: it offers the
  **union** of its models' levels (:func:`offered_levels_for_grid`), since any level can now be
  taken by every model in it, one way or another.

* **Legacy Ollama:cloud families** — only for an ``ollama-cloud`` route with **no** map entry.
  That route keeps the historical nearest-weaker clamp (:func:`clamp`) for compatibility; it is
  outside the declarative contract on purpose.

The vocabularies below mirror ``agent/reasoning_effort.py`` in hermes-agent, which is the
reference implementation of the clamping rule ("nearest weaker level, never stronger") that the
legacy path still uses. `docs/routing-grid.md` records where each row came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: Hermes' ladder, low to high. Used only for nearest-weaker arithmetic.
LADDER: Tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

#: Levels offered when the route declares neither an ``effort_maps`` entry nor an ``offered`` set on
#: its built-in policy. ``max`` is deliberately absent: it is only offered where the route's own
#: contract proves the wire takes it (see :func:`offered_levels`).
DEFAULT_OFFERED_LEVELS: Tuple[str, ...] = ("low", "medium", "high")


def normalise_level(value: Any) -> Optional[str]:
    """A declared or requested level, or ``None`` when the value is not one.

    Levels are non-empty strings, normalised with ``strip().lower()``. YAML ``null``, Python
    ``None``, numbers and booleans are **not** levels: they are dropped rather than stringified
    (``str(None)`` used to become the perfectly valid level ``"none"``). The vocabulary is not
    filtered against :data:`LADDER` — a declaration may use the endpoint's own words.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def normalise_levels(values: Any) -> Tuple[str, ...]:
    """A declared level sequence, normalised, in declaration order, without duplicates.

    A bare string is not a sequence of levels — it is refused instead of being iterated over its
    characters. Anything that is not a list/tuple/set is refused too.
    """
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    levels: list[str] = []
    for item in values:
        level = normalise_level(item)
        if level is not None and level not in levels:
            levels.append(level)
    return tuple(levels)


def normalise_overrides(raw: Any) -> Mapping[str, str]:
    """A declared ``requested -> sent`` mapping, normalised on both sides.

    Entries whose key or target is not a level (``null``, a number, a boolean, an empty string)
    are dropped: an override is never a way to smuggle a non-level onto the wire.
    """
    if not isinstance(raw, Mapping):
        return MappingProxyType({})
    pairs: Dict[str, str] = {}
    for key, value in raw.items():
        source = normalise_level(key)
        target = normalise_level(value)
        if source is None or target is None or source in pairs:
            continue
        pairs[source] = target
    return MappingProxyType(pairs)


@dataclass(frozen=True)
class EffortContract:
    """One model's declared reasoning vocabulary, in either supported shape."""

    accepted: Tuple[str, ...] = ()
    overrides: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    #: The declared ``(min, max)`` pair, when the entry declared one. ``accepted`` is then the
    #: levels between the bounds and :func:`resolve_bounded_effort` clamps a request onto them;
    #: ``None`` keeps the vocabulary contract (verbatim, declared override, else omit).
    bounds: Optional[Tuple[str, str]] = None

    @property
    def valid(self) -> bool:
        """True when the contract declares at least one level.

        An entry that matched the model but declares nothing is a *deliberate* shadow of the
        built-in policy, not an absence — callers must omit rather than fall back.
        """
        return bool(self.accepted)

    def resolve(self, requested: Any) -> Optional[str]:
        """The level to put on the wire, or ``None`` when the contract does not cover it."""
        if self.bounds is not None:
            return resolve_bounded_effort(requested, self.accepted, self.overrides, self.bounds)
        return resolve_declared_effort(requested, self.accepted, self.overrides)


EMPTY_CONTRACT = EffortContract()


def askable_levels() -> Tuple[str, ...]:
    """The vocabulary a question can offer, in order — ``config.EFFORT_LEVELS``.

    Imported lazily: ``config`` imports this module for the level normalisation, so a
    module-level import here would be a cycle.
    """
    from .config import EFFORT_LEVELS

    return EFFORT_LEVELS


def parse_effort_bounds(minimum: Any, maximum: Any) -> Optional[Tuple[str, str]]:
    """The ``(min, max)`` pair an entry declares, or ``None`` when it declares none.

    One open side is filled from the askable vocabulary (``low`` for a missing ``min``, ``max``
    for a missing ``max``), so ``max: medium`` means "the floor up to ``medium``". Both sides
    must be levels the question can *offer*: a bound outside that vocabulary has no position to
    clamp onto, so the pair is refused rather than guessed. An inverted pair is refused too —
    :func:`levels_within` would be empty and the entry would silently accept nothing.
    """
    low = normalise_level(minimum)
    high = normalise_level(maximum)
    if low is None and high is None:
        return None
    levels = askable_levels()
    if low is None:
        low = levels[0]
    if high is None:
        high = levels[-1]
    if low not in levels or high not in levels:
        return None
    if levels.index(low) > levels.index(high):
        return None
    return (low, high)


def levels_within(bounds: Tuple[str, str]) -> Tuple[str, ...]:
    """The askable levels between two bounds, inclusive — the bounded contract's accepted set."""
    levels = askable_levels()
    return levels[levels.index(bounds[0]) : levels.index(bounds[1]) + 1]


def parse_effort_spec(spec: Any) -> EffortContract:
    """Read an effort spec in either supported shape: a level sequence or ``{accepted, ...}``.

    Accepts the immutable structures ``config.parse_effort_maps`` produces as well as the raw
    YAML shapes. A mapping that declares ``min``/``max`` is a *bounded* contract: its accepted
    set is derived from the pair (:func:`levels_within`), so an ``accepted`` list beside the
    bounds is redundant and must agree with them — a contradictory declaration accepts nothing
    (the deliberate-shadow branch), rather than letting one key silently win over the other.
    Anything else — ``None``, a scalar, a mapping with no usable ``accepted`` — yields an empty
    contract.
    """
    if isinstance(spec, Mapping):
        bounds = parse_effort_bounds(spec.get("min"), spec.get("max"))
        accepted = normalise_levels(spec.get("accepted"))
        if bounds is not None:
            derived = levels_within(bounds)
            if accepted and tuple(accepted) != derived:
                return EMPTY_CONTRACT
            accepted = derived
        return EffortContract(
            accepted=accepted,
            overrides=normalise_overrides(spec.get("overrides")),
            bounds=bounds,
        )
    if isinstance(spec, (list, tuple, set, frozenset)):
        return EffortContract(accepted=normalise_levels(spec))
    return EMPTY_CONTRACT


def resolve_declared_effort(
    requested: Any,
    accepted: Any,
    overrides: Any = None,
) -> Optional[str]:
    """The single declarative algorithm, shared by maps and strict built-in policies.

    Order, and nothing else:

    1. a request that is not a level, or a contract that accepts nothing → omit (``None``);
    2. the request is one of the declared levels → return it verbatim (an accepted level always
       wins, even when a conflicting override names another one);
    3. ``overrides[requested]`` → its target, but only when that target is itself accepted;
    4. otherwise → omit.

    Overrides are never chained, and no level outside ``accepted`` is ever produced. Position on
    :data:`LADDER` plays no part: ``xhigh → max`` happens because it is written down.
    """
    level = normalise_level(requested)
    if level is None:
        return None
    declared = normalise_levels(accepted)
    if not declared:
        return None
    if level in declared:
        return level
    target = normalise_overrides(overrides).get(level)
    if target is not None and target in declared:
        return target
    return None


def resolve_bounded_effort(
    requested: Any,
    accepted: Any,
    overrides: Any,
    bounds: Tuple[str, str],
) -> Optional[str]:
    """The level to send for a *bounded* contract: verbatim inside, clamped outside.

    A declared ceiling that turns into an omission is not a ceiling — the transport then applies
    its own default, which is the exact cost this contract exists to bound. So a request the
    bounds do not contain lands on the nearest bound instead of being dropped:

    1. a request that is not a level → omit (``None``); the bounds never invent a request;
    2. a request the contract accepts → verbatim;
    3. a declared override with an accepted target → that target;
    4. a request above the ceiling → the ceiling (``clamped_max``);
    5. a request below the floor → the floor (``clamped_min``);
    6. a rung *inside* the bounds that this vocabulary does not carry (``xhigh`` between ``high``
       and ``max``) → the nearest accepted level at or below it, never a stronger one;
    7. ``none`` and any word outside :data:`LADDER` → omit: disabling reasoning is not a level to
       raise onto the floor, and a bespoke word has no position to clamp from.
    """
    level = normalise_level(requested)
    if level is None:
        return None
    declared = normalise_levels(accepted)
    if not declared or bounds is None:
        return None
    if level in declared:
        return level
    target = normalise_overrides(overrides).get(level)
    if target is not None and target in declared:
        return target
    if level == "none" or level not in LADDER:
        return None
    index = LADDER.index(level)
    floor, ceiling = LADDER.index(bounds[0]), LADDER.index(bounds[1])
    if index > ceiling:
        return bounds[1]
    if index < floor:
        return bounds[0]
    weaker = [candidate for candidate in declared if LADDER.index(candidate) <= index]
    if weaker:
        return max(weaker, key=LADDER.index)
    return bounds[0]


def bounds_adjustment(
    requested: Any,
    applied: Any,
    bounds: Optional[Tuple[str, str]],
) -> Optional[str]:
    """Why ``applied`` differs from ``requested``, in the bounded contract's own words.

    ``clamped_max`` / ``clamped_min`` for the two contract cases the audit has to prove, and
    ``nearest_accepted`` for a rung inside the bounds that this vocabulary does not carry.
    ``None`` when nothing was adjusted, or when no bounds were declared at all.
    """
    level = normalise_level(requested)
    sent = normalise_level(applied)
    if level is None or sent is None or level == sent or bounds is None:
        return None
    if level not in LADDER:
        return "nearest_accepted"
    index = LADDER.index(level)
    if index > LADDER.index(bounds[1]):
        return "clamped_max"
    if index < LADDER.index(bounds[0]):
        return "clamped_min"
    return "nearest_accepted"


def select_model_spec(entries: Any, model_id: Optional[str]) -> Tuple[bool, Any]:
    """Pick the entry for ``model_id`` from a model-keyed mapping: ``(found, spec)``.

    The result is deterministic and independent of insertion order:

    1. the full model id exactly (``deepseek/deepseek-v4.1-flash``);
    2. a namespace-free key exactly equal to the slug;
    3. the longest true prefix — the id continues with ``-``, so ``gpt-6-luna`` matches
       ``gpt-6-luna-900k`` but a key merely contained in the middle of the id never matches.

    A qualified key is compared with the full id only, so it cannot claim another namespace's
    model; a bare key is compared with the slug. Prefix ties are broken by the length of the
    key's model part (longer first), then by qualified-before-bare, then lexically.

    ``found`` is returned separately from ``spec`` on purpose: a matched entry whose spec is
    empty or ``None`` is a deliberate shadow, not an absence.
    """
    if not isinstance(entries, Mapping):
        return False, None
    full_id = normalise_level(model_id)
    if not full_id:
        return False, None
    slug = full_id.rsplit("/", 1)[-1]

    best: Optional[Tuple[Tuple[Any, ...], Any]] = None
    for key, spec in entries.items():
        candidate = normalise_level(key)
        if candidate is None:
            continue
        qualified = "/" in candidate
        scope = full_id if qualified else slug
        model_part = candidate.rsplit("/", 1)[-1]
        if candidate == full_id:
            rank: Tuple[Any, ...] = (0, -len(model_part), 0 if qualified else 1, candidate)
        elif not qualified and candidate == slug:
            rank = (1, -len(model_part), 1, candidate)
        elif scope.startswith(candidate + "-"):
            rank = (2, -len(model_part), 0 if qualified else 1, candidate)
        else:
            continue
        if best is None or rank < best[0]:
            best = (rank, spec)
    if best is None:
        return False, None
    return True, best[1]


def _canonical_provider(provider: str) -> str:
    """The route's canonical name: the alias table's answer, else the name as given.

    Imported lazily: ``config`` itself imports this module for the level normalisation, so a
    module-level import here would be a cycle. The alias table is never duplicated.
    """
    key = (provider or "").strip().lower()
    from .config import canonical_provider_name as _canonical

    return _canonical(key) or key


#: The field a custom contract's level rides in, keyed by the route's **canonical** name.
#:
#: Metadata for the operator, not a transport selector: ``reasoning`` for the Codex Responses route
#: (the transport nests the level there), ``output_config.effort`` for the Anthropic Messages route,
#: ``reasoning_effort`` (the default, see :func:`_custom_wire_parameter`) for every other custom
#: contract. The router still picks the payload shape from ``config.wire_style``; nothing here can
#: create a ``reasoning`` object or an ``output_config`` one.
WIRE_PARAMETERS: Mapping[str, str] = MappingProxyType(
    {
        "openai-codex": "reasoning",
        "anthropic": "output_config.effort",
    }
)


def _custom_wire_parameter(provider: str) -> str:
    """The field a custom contract's level rides in — metadata, not a transport selector.

    Answered from :data:`WIRE_PARAMETERS` for the routes that declare an exception, and
    ``reasoning_effort`` for every other custom contract.
    """
    return WIRE_PARAMETERS.get(_canonical_provider(provider), "reasoning_effort")


def _effort_map_entry(
    effort_maps: Optional[Mapping[str, Any]],
    provider_key: str,
) -> Tuple[Optional[str], Any]:
    """The provider's map: the exact name first, then the canonical name of a known alias."""
    if not effort_maps or not isinstance(effort_maps, Mapping):
        return None, None
    if provider_key in effort_maps:
        return provider_key, effort_maps[provider_key]
    canonical = _canonical_provider(provider_key)
    if canonical and canonical != provider_key and canonical in effort_maps:
        return canonical, effort_maps[canonical]
    return None, None


@dataclass(frozen=True)
class Family:
    """One model family's effort contract on its wire."""

    name: str
    accepted: Tuple[str, ...]
    overrides: Dict[str, str]


#: Ollama:cloud /v1/chat/completions. ``max`` is undocumented but real; ``minimal`` 400s.
OLLAMA_CLOUD = Family(
    name="ollama-cloud",
    accepted=("none", "low", "medium", "high", "max"),
    overrides={"xhigh": "max"},
)

FAMILIES: Tuple[Family, ...] = (
    # DeepSeek V4 / V4.1 — graded, xhigh rounds up to max.
    Family("deepseek", ("low", "medium", "high", "max"), {"xhigh": "max"}),
    # Kimi K3 — documented set is low/high/max; `high` is its positional middle, so a
    # `medium` request maps onto `high` rather than down to `low`.
    Family("kimi-k3", ("low", "high", "max"), {"medium": "high", "xhigh": "max"}),
    # GLM-5.3 — graded low/medium/high/max, live-verified monotonic.
    Family("glm-53", ("low", "medium", "high", "max"), {"xhigh": "max"}),
    # GLM-5.2 — the older knob only has high and max.
    Family("glm-52", ("high", "max"), {"xhigh": "max"}),
    # MiniMax M3 — no narrower vocabulary published; the Ollama:cloud set applies.
    Family("minimax", OLLAMA_CLOUD.accepted, dict(OLLAMA_CLOUD.overrides)),
    # Nemotron 3 Nano — same, and `low` is the honest landing for its "simple tasks" profile.
    Family("nemotron", OLLAMA_CLOUD.accepted, dict(OLLAMA_CLOUD.overrides)),
)

#: Table of default families by provider
PROVIDER_FAMILIES: Dict[str, Tuple[Family, ...]] = {
    "ollama-cloud": FAMILIES,
    "ollama_cloud": FAMILIES,
}


def family_for(model_id: Optional[str], provider: str = "") -> Family:
    """Pick the family contract for a model id.

    Matching is a substring test on the bare slug, so ``kimi-k3``, ``kimi-k3-256k`` and a
    ``vendor/kimi-k3`` prefix all land on the same row. Unknown models get the
    Ollama:cloud vocabulary, which is the widest set this router's provider accepts.
    """
    slug = (model_id or "").strip().lower().rsplit("/", 1)[-1]
    if not slug:
        return OLLAMA_CLOUD
    if "kimi" in slug and _token(slug, "k3"):
        return FAMILIES[1]
    if "deepseek" in slug:
        return FAMILIES[0]
    if slug.startswith("glm"):
        return FAMILIES[3] if _token(slug, "5.2") else FAMILIES[2]
    if "minimax" in slug:
        return FAMILIES[4]
    if "nemotron" in slug:
        return FAMILIES[5]
    return OLLAMA_CLOUD


def _token(slug: str, needle: str) -> bool:
    """Boundary-ish containment so ``k2.6`` never matches ``k3`` and ``5.3`` never ``5.2``."""
    return needle in slug.replace("-", " ").replace("_", " ").split() or needle in slug


def clamp(
    effort: Optional[str],
    accepted: Sequence[str],
    overrides: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Clamp a requested level onto ``accepted``; ``None`` means "omit the field".

    **Legacy path only**: the ``ollama-cloud`` family table for a route with no
    ``effort_maps`` entry. Declared policies — operator maps and the strict built-in routes — use
    :func:`resolve_declared_effort` and never clamp.

    Order: declared override, verbatim when accepted, else the nearest **weaker** level, and
    the provider's floor when nothing weaker exists. A level that is not a ladder rung at all
    (a bespoke provider vocabulary) is passed through untouched.
    """
    requested = str(effort or "").strip().lower()
    if not requested:
        return None

    normalised = [level for level in (str(item).strip().lower() for item in accepted) if level in LADDER]
    if not normalised:
        return requested
    if requested in normalised:
        return requested
    if overrides and overrides.get(requested) in normalised:
        return overrides[requested]
    if requested not in LADDER:
        return requested

    # `none` disables reasoning outright — never a landing target for an enabled request.
    candidates = [level for level in normalised if level != "none"]
    if not candidates:
        return requested
    request_index = LADDER.index(requested)
    weaker = [level for level in candidates if LADDER.index(level) < request_index]
    if weaker:
        return max(weaker, key=LADDER.index)
    return min(candidates, key=LADDER.index)


@dataclass(frozen=True)
class ModelReasoningPolicy:
    """Explicit reasoning effort policy for a model under a target provider."""

    provider: str
    model_id: str
    wire_parameter: Optional[str]
    accepted_levels: Tuple[str, ...]
    evidence: str
    omitted: bool
    decision: str
    #: Declared vendor mapping consulted when the request is not itself accepted, mirroring the
    #: core's `DEEPSEEK_V4_OVERRIDES` (``{"xhigh": "max"}``). Only a target that is itself an
    #: accepted level is honoured; the mapping never adds a level the policy did not declare.
    overrides: Mapping[str, str] = field(default_factory=dict)
    #: True only for the legacy Ollama:cloud family fallback (a route with **no** map entry).
    #: Such a policy is resolved with :func:`clamp` — nearest weaker, never stronger — while every
    #: declared policy (operator map or strict built-in) uses :func:`resolve_declared_effort`.
    legacy_clamp: bool = False
    #: The ``(min, max)`` pair an operator entry declared for this model, or ``None``.
    #:
    #: Only an operator map declares bounds; the built-in policies declare vocabularies. With
    #: bounds present the policy is resolved with :func:`resolve_bounded_effort` (clamp to the
    #: nearest bound) instead of the omit rule, and the route's question offers the union of the
    #: grid's levels (see :func:`offered_levels_for_grid`).
    bounds: Optional[Tuple[str, str]] = None
    #: Levels this route offers by default, taken from its own built-in declaration.
    #:
    #: Empty means "not declared", and :func:`offered_levels` keeps the three-level default. A
    #: route whose contract the planner re-decided declares its own set here, so ``max`` is
    #: offered exactly where the route proves it takes it — never offered and then clamped away.
    #: The operator's ``effort_maps`` still wins: an entry for the route replaces the whole policy,
    #: offered set included.
    offered: Tuple[str, ...] = ()


#: Explicit reasoning policies for the opt-in OpenCode routes. Support on one provider is never
#: inferred from another: the Console, Go and Ollama:cloud wires are different contracts, so the
#: Console model still omits the field while the Go model carries the probe-confirmed top-level
#: ``reasoning_effort``.
OPENCODE_CONSOLE_REASONING_POLICIES: Dict[str, ModelReasoningPolicy] = {
    "deepseek-v4.1-flash": ModelReasoningPolicy(
        provider="opencode-console",
        model_id="deepseek-v4.1-flash",
        wire_parameter=None,
        accepted_levels=(),
        evidence=(
            "Unconfirmed on opencode-console generation endpoint. Support on opencode-go (/zen/go/v1) "
            "or ollama-cloud cannot be inferred."
        ),
        omitted=True,
        decision=(
            "Explicit omission: Documentation and empirical probe evidence do not confirm reasoning_effort parameter "
            "support on the opencode-console generation endpoint for deepseek-v4.1-flash. The parameter is omitted on "
            "the wire to prevent HTTP 400 rejection. The Jev logical effort choice is recorded as effort_requested in "
            "audit trails, but wire effort remains None without claiming downstream model execution."
        ),
    ),
}

#: Explicit reasoning policies for the ``opencode-go`` generation model.
#:
#: Probed on 2026-10-20 against ``POST https://opencode.ai/zen/go/v1/chat/completions`` (the
#: ``x-opencode-session`` header is mandatory) with ``deepseek-v4.1-flash``: the top-level
#: ``reasoning_effort`` is the knob this route accepts, and its vocabulary is the DeepSeek V4 one
#: the core already drives on this same wire (``low``/``medium``/``high``/``max``, ``xhigh``→``max``).
OPENCODE_GO_REASONING_POLICIES: Dict[str, ModelReasoningPolicy] = {
    "deepseek-v4.1-flash": ModelReasoningPolicy(
        provider="opencode-go",
        model_id="deepseek-v4.1-flash",
        wire_parameter="reasoning_effort",
        accepted_levels=("low", "medium", "high", "max"),
        evidence=(
            "Probe-confirmed on 2026-10-20 against POST https://opencode.ai/zen/go/v1/chat/completions "
            "(mandatory x-opencode-session header) for deepseek-v4.1-flash: reasoning_effort=low, "
            "reasoning_effort=medium, reasoning_effort=high, reasoning_effort=xhigh and "
            "reasoning_effort=max all returned HTTP 200, and the accepted responses carried reasoning_content "
            "plus usage.completion_tokens_details.reasoning_tokens. thinking=high returned HTTP 400 because "
            "this route requires thinking to be an object, so the parameter used is the top-level "
            "reasoning_effort. The vocabulary mirrors the core's DeepSeek V4 contract, which "
            "core/plugins/model-providers/opencode-zen already applies to deepseek-v4* ids on this same "
            "wire: DEEPSEEK_V4_EFFORTS = (low, medium, high, max) with DEEPSEEK_V4_OVERRIDES = {xhigh: max}. "
            "An out-of-vocabulary value also returns HTTP 200, so the route does not validate the value: "
            "the accepted set is the contract, not a 400 boundary."
        ),
        omitted=False,
        decision=(
            "The level chosen by Jev is written to the wire as reasoning_effort: low/medium/high verbatim, "
            "max verbatim, and xhigh mapped to max by the declared override — the same mapping the core "
            "applies on this route. Anything else (minimal, none, an unknown string) is omitted, never "
            "invented and never clamped. The probe proves the route accepts the parameter (HTTP 200, for an "
            "invalid level too), not that the level is graded or calibrated to a measured reasoning depth."
        ),
        overrides={"xhigh": "max"},
        offered=("low", "medium", "high", "max"),
    ),
}

#: Explicit reasoning policies for the opt-in ``commandcode`` generation model.
#:
#: The wire carries **two** reasoning components, and they now have two different authors. The
#: ``extra_body.thinking`` toggle belongs to the provider layer: CommandCode's
#: ``build_api_kwargs_extras`` delegates ``deepseek/*`` ids to the DeepSeek profile, which calls
#: ``thinking_toggle_extras(..., always_emit_toggle=True)`` and so emits the toggle from the
#: params-level ``reasoning_config``. The **top-level ``reasoning_effort`` is the plugin's**, with
#: the level Jev decided on this turn.
#:
#: That split is what the transport's order allows, traced in ``agent/turn_api_request.py``
#: (``build_api_request``): ``api_kwargs`` is built from the provider profile first, the
#: ``llm_request`` middleware runs on that payload, and its result **replaces** the payload the
#: call is made with. The routed payload is built from the pre-middleware ``original_request``
#: (which already carries the profile's ``extra_body.thinking``) and only rewrites ``model`` and
#: ``EFFORT_WIRE_KEY``, so the plugin's level lands **after** the profile's derivation: it
#: overrides the effort the profile would have sent from the session's ``reasoning_config`` and
#: leaves the toggle untouched. The wire therefore carries ``thinking: {"type": "enabled"}``
#: **plus** ``reasoning_effort: <level chosen by Jev>``, not one at the expense of the other.
#:
#: Probed on 2026-10-20 against ``POST https://api.commandcode.ai/provider/v1/chat/completions``
#: with ``deepseek/deepseek-v4.1-flash``: ``reasoning_effort=low``, ``=medium``, ``=high``,
#: ``=xhigh`` and ``=max`` all returned HTTP 200. The vocabulary is the DeepSeek V4 one the core
#: already declares (``DEEPSEEK_V4_EFFORTS``/``DEEPSEEK_V4_OVERRIDES``), so ``xhigh`` maps onto
#: ``max`` by the same declared override rather than by a guess.
COMMANDCODE_REASONING_POLICIES: Dict[str, ModelReasoningPolicy] = {
    model_id: ModelReasoningPolicy(
        provider="commandcode",
        model_id=model_id,
        wire_parameter="reasoning_effort",
        accepted_levels=("low", "medium", "high", "max"),
        evidence=(
            "Probe-confirmed on 2026-10-20 against POST https://api.commandcode.ai/provider/v1/chat/"
            "completions for deepseek/deepseek-v4.1-flash: reasoning_effort=low, reasoning_effort=medium, "
            "reasoning_effort=high, reasoning_effort=xhigh and reasoning_effort=max all returned HTTP 200. "
            "The route's reasoning contract has two components with two authors, traced in "
            "agent/turn_api_request.py::build_api_request: api_kwargs is built from the provider profile "
            "first (CommandCode delegates deepseek/* to the DeepSeek profile, whose "
            "thinking_toggle_extras(..., always_emit_toggle=True) emits extra_body.thinking and its own "
            "top-level reasoning_effort from the session reasoning_config), and the llm_request middleware "
            "payload replaces that payload afterwards. Building the routed request from the pre-middleware "
            "original_request therefore keeps extra_body.thinking intact while the plugin's top-level "
            "reasoning_effort wins over the profile's derivation. The accepted set is the core's DeepSeek V4 "
            "contract (DEEPSEEK_V4_EFFORTS = low, medium, high, max with DEEPSEEK_V4_OVERRIDES = {xhigh: "
            "max}), which is the vocabulary this endpoint accepts. A 200 proves the field is accepted, not "
            "that the level is graded or calibrated to a measured reasoning depth."
        ),
        omitted=False,
        decision=(
            f"The level chosen by Jev is written to the wire as the top-level reasoning_effort for "
            f"{model_id}: low/medium/high/max verbatim and xhigh mapped to max by the declared override, "
            "the same mapping the core applies to DeepSeek V4 ids. Anything else (minimal, none, an unknown "
            "string) is omitted, never invented and never clamped. extra_body.thinking and reasoning_config "
            "are left exactly as the provider profile built them, so the toggle and the level coexist on the "
            "wire instead of one replacing the other. The audit records the level as sent (effort) and keeps "
            "effort_requested as the Jev choice; the probe shows the endpoint accepts the parameter, not that "
            "it honours a depth."
        ),
        overrides={"xhigh": "max"},
        offered=("low", "medium", "high", "max"),
    )
    for model_id in ("deepseek/deepseek-v4.1-flash",)
}


#: GPT-6 Luna's ladder on the Codex Responses route, and GPT-6.1 Sol's. Both are the core's own
#: declarations, not this plugin's guesses: ``CODEX_GPT56_EFFORTS`` (the GPT-6 tier, which the
#: ``-900k`` picker aliases inherit through the same slug prefix) carries the full ``none..max``
#: set, while ``CODEX_ASTRA_EFFORTS`` — reached for the ``gpt-6.1-sol`` family through
#: ``NO_DISABLE_TIER_PREFIXES`` — has **no** ``none``: that route rejects a disable with a 400,
#: so a level it does not declare is omitted rather than sent.
CODEX_LUNA_EFFORTS: Tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
CODEX_SOL_EFFORTS: Tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: ``(model id, accepted levels)`` for the ids the Codex route serves. A model's family
#: decides its vocabulary: the Luna ids share one set, ``gpt-6.1-sol`` the other.
#:
#: ``gpt-6.1-sol-900k`` is deliberately absent — the backend does not serve that id (HTTP 400
#: "model entitlement") and the core does not treat it as a valid ``-900k`` variant, so a policy
#: for it would document a route that cannot happen. A decision naming it is refused by the
#: catalog before any effort is resolved.
CODEX_MODEL_LEVELS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("gpt-6-luna", CODEX_LUNA_EFFORTS),
    ("gpt-6-luna-900k", CODEX_LUNA_EFFORTS),
    ("gpt-6.1-sol", CODEX_SOL_EFFORTS),
)

#: Explicit reasoning policies for the ``openai-codex`` route (ChatGPT OAuth, Responses API).
#:
#: ``wire_parameter`` names the field the level rides in. On this route that is the ``reasoning``
#: object, not a top-level kwarg: the transport decodes the params-level ``reasoning_config`` into
#: ``reasoning: {"effort": …}`` while it *builds* the payload
#: (``agent/transports/codex.py::_reasoning_fields``), and the ``llm_request`` middleware runs
#: after that build. A top-level ``reasoning_effort`` in a Responses body is not a Responses field
#: at all — the SDK call raises ``TypeError: unexpected keyword argument`` before a byte is sent —
#: which is why the router writes ``reasoning.effort`` for this mode and nothing else.
#:
#: Captured live on 2026-10-20 (``HERMES_DUMP_REQUESTS=1``, ``--provider openai-codex -m
#: gpt-6-luna``): the request body carried ``"reasoning": {"effort": "high", "summary": "auto"}``
#: and ``"include": ["reasoning.encrypted_content"]``, with no ``reasoning_effort`` key anywhere.
#: Turn-level validation on the three ids this account serves returned HTTP 200 with no model
#: fallback; ``gpt-6.1-sol-900k`` is **not** served (HTTP 400 "model entitlement").
CODEX_REASONING_POLICIES: Dict[str, ModelReasoningPolicy] = {
    model_id: ModelReasoningPolicy(
        provider="openai-codex",
        model_id=model_id,
        wire_parameter="reasoning",
        accepted_levels=levels,
        evidence=(
            f"Declared by the Hermes core for the Responses route: "
            f"{'CODEX_GPT56_EFFORTS (none, low, medium, high, xhigh, max)' if levels == CODEX_LUNA_EFFORTS else 'CODEX_ASTRA_EFFORTS (low, medium, high, xhigh, max — no none)'} "
            f"in agent/reasoning_effort.py, selected per model by codex_supported_efforts (the "
            f"-900k picker aliases share their family prefix). The wire contract was captured live "
            f"on 2026-10-20 with HERMES_DUMP_REQUESTS=1 for --provider openai-codex: the body "
            f"carries reasoning.effort, reasoning.summary and include=[reasoning.encrypted_content], "
            f"and no top-level reasoning_effort."
        ),
        omitted=False,
        decision=(
            f"The level chosen by Jev is written as reasoning.effort on the Responses body for "
            f"{model_id}: {'none/low/medium/high/xhigh/max verbatim' if levels == CODEX_LUNA_EFFORTS else 'low/medium/high/xhigh/max verbatim, while none is omitted (that route has no disable level and 400s on it)'}. "
            "Anything else (minimal, an unknown string) is omitted, never invented and never "
            "clamped onto another rung. The reasoning object itself is the transport's: its "
            "summary/include fields and a route the transport judged as taking no reasoning field "
            "are left exactly as built, so the plugin can add a level but never a field."
        ),
        # Every Codex id accepts low/medium/high/max (Luna also accepts none and xhigh, Sol
        # accepts xhigh), so the question offers the four levels this plugin's vocabulary has —
        # the same set the operator's ``effort_maps`` used to declare for this route.
        offered=("low", "medium", "high", "max"),
    )
    for model_id, levels in CODEX_MODEL_LEVELS
}


#: ``(model id, accepted levels)`` for the ids the opt-in ``anthropic`` route serves, and the
#: range this plugin offers each family. The ids are the ones the provider's model cache lists
#: (``grid.ANTHROPIC_GRID``) — one per family, newest version.
#:
#: **Caps decided for this route** (not a wire limit): Opus and Sonnet stop at ``high``, because a
#: ``max``/``xhigh`` turn on those families costs several times a ``high`` one and this deployment
#: has no measured need for it; an operator who wants it declares it in ``effort_maps``. Haiku is
#: the high-volume family, so ``max`` is offered for it — the wire accepts it
#: (``ADAPTIVE_EFFORT_MAP`` maps ``max``→``max``) and the cap is what keeps the other two down.
#: ``default_effort`` still excludes ``max``, so a degraded turn is never the expensive one.
ANTHROPIC_MODEL_LEVELS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("claude-opus-5-5", ("low", "medium", "high")),
    ("claude-sonnet-5-5", ("low", "medium", "high")),
    ("claude-haiku-5-5", ("low", "medium", "high", "max")),
)

#: Explicit reasoning policies for the ``anthropic`` route (Anthropic Messages API).
#:
#: ``wire_parameter`` names the field the level rides in. On this route that is **not** a
#: top-level kwarg and not the Responses ``reasoning`` object either: the transport decodes the
#: params-level ``reasoning_config`` into ``thinking: {type: adaptive, display: summarized}`` plus
#: ``output_config: {effort: …}`` while it *builds* the Messages body
#: (``core/agent/anthropic_adapter.py::_thinking_kwargs``, reached from
#: ``agent/transports/anthropic.py::build_kwargs``), and the ``llm_request`` middleware runs after
#: that build. The level therefore lands in ``output_config.effort``, and only there.
#:
#: Two consequences the policy encodes:
#:
#: * the transport decides whether the route takes the field at all. A Claude the core classifies
#:   as pre-adaptive-thinking (``_is_pre_thinking_haiku``: Haiku through 4.5) gets no ``thinking``
#:   and no ``output_config``, and a session with reasoning disabled gets ``thinking: disabled``;
#:   in both cases the router omits the level instead of inventing an ``output_config``.
#: * the vocabulary is the core's: ``ADAPTIVE_EFFORT_MAP`` carries
#:   ``max/xhigh/high/medium/low`` (``ultra``→``max``, ``minimal``→``low``). This plugin declares
#:   the subset the caps allow and sends it verbatim; anything else is omitted, never clamped.
#:
#: Contract evidence, 2026-11-10 (``docs/EVIDENCIAS_OPERACIONAIS.md`` §11): three real routed
#: turns, one per id, ran against ``--provider anthropic`` and returned HTTP 200 with the decided
#: level recorded in the audit. The Messages body itself was **reproduced**, not live-dumped:
#: ``agent.anthropic_adapter.build_anthropic_kwargs`` for the same model and level yields
#: ``"thinking": {"type": "adaptive", "display": "summarized"}`` and
#: ``"output_config": {"effort": …}``, with no top-level ``reasoning_effort`` and no
#: ``reasoning_config``. ``HERMES_DUMP_REQUESTS=1`` does not reach the managed worker that
#: performs the call on this host, so no live request dump exists for this route.
ANTHROPIC_REASONING_POLICIES: Dict[str, ModelReasoningPolicy] = {
    model_id: ModelReasoningPolicy(
        provider="anthropic",
        model_id=model_id,
        wire_parameter="output_config.effort",
        accepted_levels=levels,
        evidence=(
            "Declared by the Hermes core for the Anthropic Messages route: "
            "agent/anthropic_adapter.py::_thinking_kwargs maps reasoning_config.effort onto "
            "output_config.effort for adaptive-thinking families, using ADAPTIVE_EFFORT_MAP "
            "(ultra→max, max→max, xhigh→xhigh, high→high, medium→medium, low→low, minimal→low); "
            "families the core classifies as pre-adaptive-thinking get no thinking request and no "
            "output_config at all. The wire contract is the one the core builds: "
            "thinking.type=adaptive, thinking.display=summarized and output_config.effort, with no "
            "top-level reasoning_effort and no reasoning_config. On 2026-11-10 three real routed "
            "turns, one per id and one per family, ran against --provider anthropic and returned "
            "HTTP 200 with the decided level in the audit; the Messages body was reproduced from "
            "agent.anthropic_adapter.build_anthropic_kwargs for each id, not captured live, "
            "because HERMES_DUMP_REQUESTS=1 does not reach the managed worker on this host "
            "(docs/EVIDENCIAS_OPERACIONAIS.md §11). The caps in this table are this "
            "plugin's decision, not a wire limit: Opus/Sonnet high is the ceiling this deployment "
            "pays for, Haiku keeps max."
        ),
        omitted=False,
        decision=(
            f"The level chosen by Jev is written as output_config.effort on the Messages body for "
            f"{model_id}: low/medium/high verbatim, plus max verbatim where this row declares it. "
            "Anything else (minimal, ultra, an xhigh nobody declared, an unknown string) is "
            "omitted, never invented and never clamped onto another rung — the operator's "
            "effort_maps is the supported way to widen a family. The output_config object and the "
            "whole thinking block are the transport's: the plugin rewrites the effort value inside "
            "an output_config the transport already built, and when the transport judged the route "
            "as taking no such field (pre-adaptive-thinking Claude, reasoning disabled) not even "
            "that is added, so the plugin can adjust a level but never a field."
        ),
        offered=levels,
    )
    for model_id, levels in ANTHROPIC_MODEL_LEVELS
}


def model_reasoning_policy(
    model_id: Optional[str],
    *,
    provider: str = "",
    effort_maps: Optional[Mapping[str, Any]] = None,
) -> ModelReasoningPolicy:
    """Return the reasoning policy for a model and provider.

    Order: an operator ``effort_maps`` entry for this model **replaces** the built-in policy
    wholesale (accepted levels, overrides and the omission decision), so a map can restrict or
    even disable a route; a provider map that does not mention this model leaves the built-in
    fallback in place. An entry that matched but declares nothing is a deliberate shadow: it
    omits, and never falls back.
    """
    provider_key = (provider or "").strip().lower()

    declared_by, provider_map = _effort_map_entry(effort_maps, provider_key)
    if declared_by is not None:
        found, spec = select_model_spec(provider_map, model_id)
        if found:
            contract = parse_effort_spec(spec)
            if contract.valid:
                bounds_text = list(contract.bounds) if contract.bounds else "none"
                return ModelReasoningPolicy(
                    provider=provider_key,
                    model_id=model_id or "",
                    wire_parameter=_custom_wire_parameter(declared_by),
                    accepted_levels=contract.accepted,
                    evidence=(
                        "Explicitly configured by the operator in settings.effort_maps "
                        "(a declaration of the route's vocabulary, not an empirical probe)."
                    ),
                    omitted=False,
                    decision=(
                        f"Operator declared the effort map for {declared_by}/{model_id}: accepted "
                        f"levels {contract.accepted}, overrides {dict(contract.overrides)}, bounds "
                        f"{bounds_text}. A declared level goes out "
                        "verbatim; a declared override whose target is accepted is used; anything "
                        "else is omitted."
                        + (
                            " Because the entry declares bounds, a request outside them is clamped to "
                            "the nearest bound instead of being omitted, so the ceiling always reaches "
                            "the wire."
                            if contract.bounds
                            else ""
                        )
                    ),
                    overrides=contract.overrides,
                    bounds=contract.bounds,
                )
            return ModelReasoningPolicy(
                provider=provider_key,
                model_id=model_id or "",
                wire_parameter=None,
                accepted_levels=(),
                evidence="Empty or invalid operator effort map specification",
                omitted=True,
                decision=(
                    f"Operator configured an empty or invalid effort map for "
                    f"{declared_by}/{model_id}; the entry shadows the built-in contract and the "
                    "field is omitted."
                ),
            )

    # opencode-console built-in policy
    if provider_key in ("opencode-console", "opencode_console"):
        found, policy = select_model_spec(OPENCODE_CONSOLE_REASONING_POLICIES, model_id)
        if found:
            return policy
        return ModelReasoningPolicy(
            provider=provider_key,
            model_id=model_id or "",
            wire_parameter=None,
            accepted_levels=(),
            evidence="Model not in opencode-console allowlist; unconfirmed reasoning support.",
            omitted=True,
            decision=f"Model {model_id} has no confirmed reasoning contract on {provider_key}; omitted on wire.",
        )

    # opencode-go built-in policy: the generation route of the opt-in Console decider.
    if provider_key in ("opencode-go", "opencode_go"):
        found, policy = select_model_spec(OPENCODE_GO_REASONING_POLICIES, model_id)
        if found:
            return policy
        return ModelReasoningPolicy(
            provider=provider_key,
            model_id=model_id or "",
            wire_parameter=None,
            accepted_levels=(),
            evidence="Model not in the opencode-go catalog; unconfirmed reasoning support.",
            omitted=True,
            decision=f"Model {model_id} has no confirmed reasoning contract on {provider_key}; omitted on wire.",
        )

    # commandcode built-in policy: namespaced ids, native DeepSeek thinking controls.
    # This route keeps its explicit full-id/slug equivalence instead of prefix matching: the
    # allowlist is one namespaced id, and a prefix rule would invent a family it never declared.
    if provider_key in ("commandcode", "commandcode-chat", "commandcode_chat"):
        full_id = normalise_level(model_id) or ""
        slug = full_id.rsplit("/", 1)[-1]
        for key, policy in COMMANDCODE_REASONING_POLICIES.items():
            if key == full_id or key.rsplit("/", 1)[-1] == slug:
                return policy
        return ModelReasoningPolicy(
            provider=provider_key,
            model_id=model_id or "",
            wire_parameter=None,
            accepted_levels=(),
            evidence="Model not in the commandcode allowlist; unconfirmed reasoning support.",
            omitted=True,
            decision=f"Model {model_id} has no confirmed reasoning contract on {provider_key}; omitted on wire.",
        )

    # openai-codex built-in policy: bare slugs, Responses wire, two effort vocabularies.
    if provider_key in ("openai-codex", "codex", "openai_codex"):
        found, policy = select_model_spec(CODEX_REASONING_POLICIES, model_id)
        if found:
            return policy
        return ModelReasoningPolicy(
            provider=provider_key,
            model_id=model_id or "",
            wire_parameter=None,
            accepted_levels=(),
            evidence="Model not in the openai-codex catalog; unconfirmed reasoning support.",
            omitted=True,
            decision=f"Model {model_id} has no confirmed reasoning contract on {provider_key}; omitted on wire.",
        )

    # anthropic built-in policy: bare family ids, Messages wire, level inside output_config.
    if provider_key in ("anthropic", "claude", "claude-code"):
        found, policy = select_model_spec(ANTHROPIC_REASONING_POLICIES, model_id)
        if found:
            return policy
        return ModelReasoningPolicy(
            provider=provider_key,
            model_id=model_id or "",
            wire_parameter=None,
            accepted_levels=(),
            evidence="Model not in the anthropic catalog; unconfirmed reasoning support.",
            omitted=True,
            decision=f"Model {model_id} has no confirmed reasoning contract on {provider_key}; omitted on wire.",
        )

    # ollama-cloud built-in policy: the legacy family table, and the only remaining clamp.
    if not provider_key or provider_key in ("ollama-cloud", "ollama_cloud"):
        fam = family_for(model_id, provider=provider_key)
        return ModelReasoningPolicy(
            provider="ollama-cloud",
            model_id=model_id or "",
            wire_parameter="reasoning_effort",
            accepted_levels=fam.accepted,
            evidence="Empirically verified against Ollama:cloud chat completions contract.",
            omitted=False,
            decision=f"Mapped to family '{fam.name}' with accepted levels {fam.accepted}.",
            overrides=fam.overrides,
            legacy_clamp=True,
        )

    # Generic unmapped provider
    return ModelReasoningPolicy(
        provider=provider_key,
        model_id=model_id or "",
        wire_parameter=None,
        accepted_levels=(),
        evidence="Unmapped provider without confirmed contract or operator effort_maps.",
        omitted=True,
        decision=f"Provider {provider_key} is unmapped and lacks verified reasoning contract; omitted on wire.",
    )


def _declared_contract(
    provider: Optional[str],
    model: Optional[str],
    effort_maps: Optional[Mapping[str, Any]],
) -> Optional[EffortContract]:
    """The contract this route declares for ``model``, or ``None`` when it declares none.

    ``None`` means "no map entry applies here" (no map at all, no entry for the provider, or an
    entry that does not name this model) — the case where the built-in policy still governs.
    A *matched* entry always returns a contract, even an empty one: main treats that as a
    deliberate shadow, so it must not be mistaken for an absence.
    """
    _name, provider_map = _effort_map_entry(effort_maps, (provider or "").strip().lower())
    if provider_map is None:
        return None
    found, spec = select_model_spec(provider_map, model)
    if not found:
        return None
    return parse_effort_spec(spec)


def declared_bounds(
    provider: Optional[str],
    model: Optional[str],
    effort_maps: Optional[Mapping[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    """The ``(min, max)`` an operator entry declared for this model, or ``None``.

    Bounds are an operator-only declaration: the built-in policies carry a vocabulary, and
    ``None`` here means "this route keeps the declarative omit rule". Used by the question (a
    bounded grid offers the union) and by the audit (``effort_bounds`` / ``effort_adjusted``).
    """
    contract = _declared_contract(provider, model, effort_maps)
    return contract.bounds if contract is not None else None


def offered_levels(
    provider: Optional[str],
    model: Optional[str],
    effort_maps: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, ...]:
    """The effort levels the question may offer a route, in ``EFFORT_LEVELS`` order.

    The operator's declaration decides first. A route is only asked for levels its own
    ``effort_maps`` entry declares: offering ``max`` where the declaration does not have it would
    spend the decision on an answer the router would then omit. An entry that declares nothing
    usable leaves the default.

    With no map entry for the route (or none for this model), the route's built-in policy answers
    from its own offered set (``ModelReasoningPolicy.offered``), which is where the wire contract
    lives now: ``opencode-go``, ``commandcode`` and ``openai-codex`` offer
    ``low``/``medium``/``high``/``max`` — each model's accepted set includes all four — and
    ``anthropic`` offers its capped family range (Haiku up to ``max``, Sonnet/Opus up to ``high``).
    Every other built-in policy leaves the field empty and keeps the three-level default, so a
    route with nothing declared never gains ``max`` by accident.

    A declaration that shares no level with the askable vocabulary (``accepted: [minimal]``, or
    an entry that deliberately declares nothing) is unusable *for this question*: the default
    applies, and ``resolve_effort`` then omits whatever the answer names — it never clamps. A
    bounded declaration is the documented exception: it accepts the levels between its bounds and
    clamps the rest, so the default applies only to a contract that is genuinely unusable.

    Imported lazily: ``config`` imports this module for the level normalisation, so a
    module-level import here would be a cycle.
    """
    from .config import EFFORT_LEVELS

    contract = _declared_contract(provider, model, effort_maps)
    if contract is not None:
        if not contract.valid:
            return DEFAULT_OFFERED_LEVELS
        declared = set(contract.accepted)
        offered = tuple(level for level in EFFORT_LEVELS if level in declared)
        return offered or DEFAULT_OFFERED_LEVELS

    if model:
        policy = model_reasoning_policy(model, provider=provider or "", effort_maps=effort_maps)
        if policy.offered:
            declared = set(policy.offered)
            offered = tuple(level for level in EFFORT_LEVELS if level in declared)
            return offered or DEFAULT_OFFERED_LEVELS
    return DEFAULT_OFFERED_LEVELS


def offered_levels_for_grid(
    grid: Sequence[Any],
    provider: Optional[str],
    effort_maps: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, ...]:
    """The levels the effort question may offer for a whole grid.

    Two rules, decided by whether the grid declares **bounds** per model:

    * **Bounded grid** — any model with ``min``/``max`` in its operator entry: the question
      offers the **union** of the models' levels. The question is asked once, before the model
      is chosen, and a bounded model clamps whatever it cannot take, so a level only one family
      really supports (Haiku's ``max``) is still a safe answer for the others.
    * **Unbounded grid** — the historical **intersection**: an unoffered answer would be omitted
      on the wire, so a level is only offered when every model in the grid can take it verbatim.

    A grid the operator left without maps keeps the three-level default, and an empty grid has
    nothing to narrow.
    """
    from .config import EFFORT_LEVELS

    models = [entry.model_id for entry in grid]
    if not models:
        return DEFAULT_OFFERED_LEVELS
    offered_sets = [set(offered_levels(provider, model_id, effort_maps)) for model_id in models]
    if any(declared_bounds(provider, model_id, effort_maps) for model_id in models):
        combined = set().union(*offered_sets)
    else:
        combined = set.intersection(*offered_sets)
    offered = tuple(level for level in EFFORT_LEVELS if level in combined)
    return offered or DEFAULT_OFFERED_LEVELS


def resolve_effort(
    model_id: Optional[str],
    effort: Optional[str],
    *,
    provider: str = "",
    effort_maps: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """The exact value to put on the wire for ``model_id``, or ``None`` to omit the field.

    One algorithm per contract shape, wherever the declaration came from: the selected policy's
    own accepted levels and overrides, applied by :func:`resolve_declared_effort` — accepted level
    verbatim, else a declared override with an accepted target, else omit. An operator entry that
    declares ``min``/``max`` is resolved by :func:`resolve_bounded_effort` instead: the same
    first two steps, then a clamp to the nearest bound, because an omitted level would fall back
    to the transport's default and the ceiling would not hold. The sole remaining exception is a
    legacy ``ollama-cloud`` route **with no map entry**, whose family table keeps its
    nearest-weaker clamp for compatibility.
    """
    if normalise_level(effort) is None:
        return None

    policy = model_reasoning_policy(model_id, provider=provider, effort_maps=effort_maps)
    if policy.omitted or not policy.wire_parameter or not policy.accepted_levels:
        return None

    if policy.legacy_clamp:
        return clamp(effort, policy.accepted_levels, dict(policy.overrides or {}))
    if policy.bounds is not None:
        return resolve_bounded_effort(
            effort, policy.accepted_levels, policy.overrides, policy.bounds
        )
    return resolve_declared_effort(effort, policy.accepted_levels, policy.overrides)


def is_omitted(
    model_id: Optional[str],
    effort: Optional[str],
    *,
    provider: str = "",
    effort_maps: Optional[Mapping[str, Any]] = None,
) -> bool:
    """True when the request would go out without a reasoning-effort field."""
    return resolve_effort(model_id, effort, provider=provider, effort_maps=effort_maps) is None
