"""The routing engine: decide, apply, record — and fail open at every step.

Called from the ``llm_request`` middleware with the request kwargs Hermes is about to send.
The contract is narrow on purpose:

* the payload is returned **unchanged** unless a confident decision was obtained;
* nothing here raises — the host already isolates middleware exceptions, but the router also
  enforces its own budget and swallows its own errors so a slow Jev cannot stall a turn;
* one decision per user turn, replayed for the rest of that turn's tool loop.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from .audit import AuditLog
from .catalog import ModelCatalog
from .client import (
    REASON_DISABLED,
    REASON_EXCEPTION,
    REASON_LOW_CONFIDENCE,
    REASON_NO_API_KEY,
    REASON_SKIPPED_API_MODE,
    REASON_SKIPPED_MODEL,
    REASON_SKIPPED_NO_MESSAGE,
    REASON_SKIPPED_PROVIDER,
    REASON_SKIPPED_UNKNOWN_MODEL,
    REASON_TIMEOUT,
    REASON_USER_DATA,
    Decision,
    JevClient,
)
from .config import (
    NATIVE_ANTHROPIC_PROVIDERS,
    ROUTED_API_MODES,
    ROUTED_PROVIDER,
    ROUTED_PROVIDER_ALIASES,
    WIRE_STYLE_OUTPUT_CONFIG,
    WIRE_STYLE_RESPONSES,
    CommandCodeFallback,
    EffectiveSettings,
    Settings,
    api_key,
    route_catalog,
    same_provider_route,
    wire_style,
)
from .effort import offered_levels_for_grid, resolve_effort
from .memo import Memo, TurnMemo, failure_marker
from .selection import SELECTION_SOURCE_SESSION_ROW, Selection, SelectionReader
from .selection import profile_home as selection_profile_home
from .state import last_user_message

logger = logging.getLogger(__name__)

#: Where the routing decision puts the effort, per payload style (``config.wire_style``). The style
#: is declared by the route, so no branch below tests a provider id:
#:
#: ``top_level`` (``chat_completions``) takes the level as the top-level ``reasoning_effort``. The
#: Codex Responses route (``codex_responses`` → ``responses``) takes it nested under ``reasoning``:
#: the transport decodes the params-level ``reasoning_config`` into ``reasoning: {"effort": ...}``
#: while it *builds* the body (``agent/transports/codex.py::_reasoning_fields``), and the middleware
#: runs after that build, so the payload here is already the Responses body. A top-level
#: ``reasoning_effort`` in it is not a Responses field at all — the SDK call raises ``TypeError:
#: unexpected keyword argument 'reasoning_effort'`` before a request is sent.
EFFORT_WIRE_KEY = "reasoning_effort"
RESPONSES_API_MODE = "codex_responses"
RESPONSES_REASONING_KEY = "reasoning"

#: The Anthropic Messages payload shape (``api_mode="anthropic_messages"`` → ``output_config``
#: style). Same story as the Responses route, one level deeper: the transport decodes the
#: params-level ``reasoning_config`` into ``thinking: {type: adaptive, display: summarized}`` plus
#: ``output_config: {effort: ...}`` while it *builds* the body
#: (``agent/anthropic_adapter.py::_thinking_kwargs``, reached from
#: ``agent/transports/anthropic.py::build_kwargs``), and this middleware runs after that build.
#: A top-level ``reasoning_effort`` is not a Messages field; ``output_config`` is (``format`` and,
#: for adaptive thinking, ``effort``).
ANTHROPIC_API_MODE = "anthropic_messages"
ANTHROPIC_OUTPUT_CONFIG_KEY = "output_config"
ANTHROPIC_EFFORT_KEY = "effort"

EFFORT_KEY = "effort"


#: Failures a turn may remember. Both are slow and repeatable: a decider that answered below the
#: confidence threshold will answer the same way to the same question, and a timeout inside one
#: turn's tool loop used to cost a full ``timeout_s`` on every request of that loop. Everything
#: else (a missing key, an unroutable provider, a malformed body) is either a configuration fact
#: resolved by the guards or a one-off worth retrying, so it is not memoised.
MEMOISABLE_FAILURES = frozenset({REASON_LOW_CONFIDENCE, REASON_TIMEOUT})


def conversation_messages(payload: Any) -> List[Dict[str, Any]]:
    """The conversation a payload carries, in the chat shape the state builder reads.

    ``chat_completions`` hands the middleware ``messages``. A Responses payload has no
    ``messages`` at all: the conversation is ``input``, a list of items where a turn is
    ``{"type": "message", "role": ..., "content": [...]}`` and everything else
    (``function_call``, ``function_call_output``, ``reasoning``) is a tool or reasoning item with
    no role. Only role-bearing items are conversation — the same set ``recent_context`` keeps on
    the chat shape, so tool payloads stay out of the routing state on both wires.
    """
    if not isinstance(payload, dict):
        return []
    messages = payload.get("messages")
    if isinstance(messages, list):
        return messages
    items = payload.get("input")
    if not isinstance(items, list):
        return []
    conversation: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if not role:
            continue
        conversation.append({"role": role, "content": item.get("content")})
    return conversation


def wire_model_for(provider: str, effective: EffectiveSettings, model_id: str) -> str:
    """The id to put in the provider's ``model`` field for the routed choice.

    Read from the route's **built-in catalog** first. A picker alias (the Codex ``-900k``
    variants) is stripped by the transport while it *builds* the body — before the middleware
    this router runs in — so writing the alias back would send an id the backend does not serve.
    The operator's ``grids.<provider>`` is the *choice* set, not the record of what each id means
    on the wire: a grid parsed from config carries descriptions only, so it cannot answer this
    question and must not be trusted to.
    """
    for entry in route_catalog(provider) or ():
        if entry.model_id == model_id:
            return entry.wire_model
    resolved = effective.entry_for(model_id)
    return resolved.wire_model if resolved is not None else model_id


def wire_effort(payload: Any, api_mode: str = "") -> Optional[str]:
    """The reasoning level a payload carries right now, wherever its route puts it.

    The read side of the same table :meth:`Router._apply` writes through: the level sits at the
    top of a ``chat_completions`` body, in ``reasoning`` on the Responses body and in
    ``output_config`` on the Messages body. Reading it here — instead of in each caller — is what
    lets a divergence comparison and an audit record agree with the wire.
    """
    if not isinstance(payload, dict):
        return None
    style = wire_style(api_mode)
    level: Any
    if style == WIRE_STYLE_RESPONSES:
        container = payload.get(RESPONSES_REASONING_KEY)
        level = container.get(EFFORT_KEY) if isinstance(container, dict) else None
    elif style == WIRE_STYLE_OUTPUT_CONFIG:
        container = payload.get(ANTHROPIC_OUTPUT_CONFIG_KEY)
        level = container.get(ANTHROPIC_EFFORT_KEY) if isinstance(container, dict) else None
    else:
        level = payload.get(EFFORT_WIRE_KEY)
    return level if isinstance(level, str) and level else None


class Router:
    """Stateful routing engine: one instance per loaded plugin."""

    def __init__(self, get_settings, get_state=None, *, client_factory=None, catalog=None) -> None:
        self._get_settings = get_settings
        self._get_state = get_state
        self._client_factory = client_factory or (lambda settings: JevClient(settings))
        self._memo = TurnMemo()
        self._audit: Optional[AuditLog] = None
        self._audit_key: Optional[tuple] = None
        self._client = None
        self._client_key: Optional[tuple] = None
        self._catalog = catalog if catalog is not None else ModelCatalog()
        #: The session-row reader, with its per-turn cache (see ``selection.SelectionReader``).
        self._selections = SelectionReader()
        #: Turn ids whose session-selection events were already recorded. The row is stable inside
        #: a turn, so recording the divergence on every provider request of a tool loop would
        #: repeat one fact once per request; bounded, because turns are unbounded.
        self._turn_events: Dict[str, None] = {}

    # -- wiring ------------------------------------------------------------------

    def _data_dir(self) -> Optional[str]:
        """The plugin data directory, when the host exposed one."""
        try:
            state = self._get_state() if callable(self._get_state) else None
            data_dir = getattr(state, "data_dir", None)
            return str(data_dir) if data_dir else None
        except Exception:  # noqa: BLE001 - auditing is optional, never load-bearing
            return None

    def audit(self, settings: Settings) -> AuditLog:
        key = (self._data_dir(), bool(settings.audit_enabled))
        if self._audit is None or self._audit_key != key:
            self._audit = AuditLog(key[0], enabled=key[1])
            self._audit_key = key
        return self._audit

    def client(self, settings: Settings):
        key = (
            settings.decider,
            settings.endpoint,
            settings.base_url,
            settings.jev_model,
            settings.decider_model,
            settings.timeout_s,
        )
        if self._client is None or self._client_key != key:
            self._client = self._client_factory(settings)
            self._client_key = key
        return self._client

    # -- the middleware body -----------------------------------------------------

    def on_llm_request(
        self,
        request: Any = None,
        original_request: Any = None,
        *,
        turn_id: str = "",
        session_id: str = "",
        task_id: str = "",
        platform: str = "",
        model: str = "",
        provider: str = "",
        base_url: str = "",
        api_mode: str = "",
        api_call_count: Any = 0,
        api_request_id: str = "",
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """``llm_request`` middleware entry point.

        Returns ``None`` to leave the request untouched, or ``{"request": {...}}`` with the
        rewritten provider kwargs.
        """
        try:
            settings = self._get_settings()
        except Exception as exc:  # noqa: BLE001
            logger.debug("jev-effort-router: settings unavailable (%s); leaving request untouched", exc)
            return None

        where = _Where(
            turn_id=turn_id,
            session_id=session_id,
            api_request_id=api_request_id,
            platform=platform,
            provider=provider,
            model=model,
        )

        if not settings.enabled:
            self._record_skip(settings, REASON_DISABLED, where)
            return None

        # -- skip gates: anything the router does not own goes out untouched ---------
        provider_key = (provider or "").strip().lower()
        if not settings.is_provider_routable(provider_key):
            # Out of the race the agent can already be on the native Anthropic route, which this
            # deployment may be spending through Claude Code's OAuth rather than a per-token API
            # key. Nothing here can move it: the provider is not routable, so the request leaves
            # exactly as built, and the only honest answer is the warning the session row makes
            # possible. (Once ``anthropic`` is routable this gate does not run for it and the route
            # is routed like any other; billing follows the credential the core resolves.)
            self._note_native_selection(settings, where, provider_key)
            self._record_skip(settings, REASON_SKIPPED_PROVIDER, where)
            return None
        if api_mode and api_mode not in ROUTED_API_MODES:
            self._record_skip(settings, REASON_SKIPPED_API_MODE, where, extra={"api_mode": api_mode})
            return None

        # -- the route's frozen snapshot: resolved once per request -------------------
        effective = settings.effective(provider=provider)

        # -- the session's own selection ----------------------------------------------
        # Read only after every gate above has run, and only when the mode and the platform opt
        # in: with ``session_selection.mode: off`` the row is never touched and this call is a
        # no-op (``_read_selection`` returns before opening the database). A selection that is not
        # applicable to *this* request is reported once per turn and then treated as absent.
        selection = self._eligible_selection(
            settings, effective, self._read_selection(settings, where), provider=provider, where=where
        )
        if selection is not None:
            self._note_divergence(
                settings,
                selection,
                original_request if isinstance(original_request, dict) else request,
                provider=provider,
                api_mode=api_mode,
                where=where,
            )

        # A surface's grid is a structural cap: a configured model outside it degrades to a
        # skip that keeps that configured model on the wire, rather than being re-pinned.
        if effective.entry_for(model) is None:
            return self._fail_open(
                settings,
                REASON_SKIPPED_MODEL,
                where,
                effective=effective,
                provider=provider,
                api_mode=api_mode,
                original_request=original_request,
                selection=selection,
            )
        if not isinstance(request, dict) or not isinstance(original_request, dict):
            self._record_skip(settings, REASON_USER_DATA, where)
            return None

        try:
            memo, replayed = self._lookup(settings, turn_id, session_id)
            # The opt-in SystemOne trio is part of this turn's configuration identity, both for a
            # replayed memo and for a fresh decision, so it is resolved once here.
            secondary = settings.active_commandcode_fallback()

            decision: Optional[Decision] = None
            if memo is not None:
                mismatch = self._memo_mismatch(
                    settings, memo, provider, session_id, effective, secondary
                )
                if mismatch is not None:
                    reason, detail = mismatch
                    marker = bool(getattr(memo, "failure_reason", None))
                    if marker and reason != REASON_NO_API_KEY:
                        # A negative memo is not a decision: it only records that this turn
                        # already asked the decider *for this provenance* and failed. Different
                        # provenance means a different question, so the marker is dropped and
                        # this request decides. Failing open instead would let a failure on one
                        # provider silence the decider for a request to another provider in the
                        # same turn — a turn that would have been routed before markers existed.
                        logger.info(
                            "jev-effort-router: negative memo belongs to another request (%s); asking the decider",
                            detail,
                        )
                        memo, replayed = None, False
                    else:
                        logger.info("jev-effort-router: %s; failing open", detail)
                        # A memo that belongs to another provenance is a fail-open like any other,
                        # whatever reason code it carries: the request *is* routable (the gates
                        # above passed), so the session's own selection is the right fallback. The
                        # `provider_not_routed` this branch may report is not the gate of the same
                        # name at the top of the method — that one returns untouched, always.
                        return self._fail_open(
                            settings,
                            reason,
                            where,
                            effective=effective,
                            provider=provider,
                            api_mode=api_mode,
                            original_request=original_request,
                            selection=selection,
                        )

            if memo is not None:
                # Negative memo: this turn's decision already failed. Replay the skip instead of
                # asking the same question again — the answer that was too weak (or too slow) is
                # the answer the rest of the tool loop would get. Checked before the confidence
                # guard, whose zeroed confidences would otherwise relabel a remembered timeout as
                # ``low_confidence`` and hide what actually happened. The record says ``replayed``
                # so a reader can tell the first failure from its replays.
                failure_reason = getattr(memo, "failure_reason", None)
                if failure_reason:
                    logger.info(
                        "jev-effort-router: turn already failed (%s); replaying the skip without asking the decider",
                        failure_reason,
                    )
                    return self._fail_open(
                        settings,
                        failure_reason,
                        where,
                        effective=effective,
                        provider=provider,
                        api_mode=api_mode,
                        original_request=original_request,
                        selection=selection,
                        extra={"replayed": True},
                    )

                # Policy confidence threshold guard: replayed confidence must meet active threshold
                has_effort_conf = getattr(memo, "has_effort_confidence", None)
                if has_effort_conf is None:
                    has_effort_conf = bool(memo.effort_confidence > 0.0 or memo.effort_requested is not None)

                effort_failed = has_effort_conf and (memo.effort_confidence < effective.confidence_threshold)
                if (
                    memo.model_confidence < effective.confidence_threshold
                    or effort_failed
                ) and REASON_LOW_CONFIDENCE not in memo.fallback_reasons:
                    logger.info(
                        "jev-effort-router: memo confidence (model=%.2f, effort=%.2f) is below active threshold %.2f; failing open",
                        memo.model_confidence,
                        memo.effort_confidence,
                        effective.confidence_threshold,
                    )
                    return self._fail_open(
                        settings,
                        REASON_LOW_CONFIDENCE,
                        where,
                        effective=effective,
                        provider=provider,
                        api_mode=api_mode,
                        original_request=original_request,
                        selection=selection,
                    )

                # Grid guard: the replayed model must exist in the active provider's grid, compared
                # by the same id the decision stored.
                if effective.entry_for(memo.model) is None:
                    logger.info(
                        "jev-effort-router: memo model %r is not in grid for provider %r; failing open",
                        memo.model,
                        provider,
                    )
                    return self._fail_open(
                        settings,
                        REASON_SKIPPED_MODEL,
                        where,
                        effective=effective,
                        provider=provider,
                        api_mode=api_mode,
                        original_request=original_request,
                        selection=selection,
                    )

                # Effort map guard: re-resolve effort against the active provider's map.
                req_effort = memo.effort_requested or memo.effort
                applied_effort = req_effort
                # The replayed level must also be one this route still offers. The map may have
                # been narrowed since the decision was written, and ``resolve_effort`` would then
                # consult the route's built-in policy, which declares ``max`` for ``opencode-go``,
                # ``commandcode`` and ``codex``: the turn would run a level its question never
                # offered. Omitting the field keeps the memoised model and leaves effort to the
                # host instead of to a level nobody asked for.
                if applied_effort is not None and applied_effort not in offered_levels_for_grid(
                    effective.grid, provider, settings.effort_maps
                ):
                    logger.info(
                        "jev-effort-router: memo effort %r is not offered by %r anymore; omitting it",
                        applied_effort,
                        provider,
                    )
                    applied_effort = None

                re_effort = resolve_effort(
                    memo.model,
                    applied_effort,
                    provider=provider,
                    effort_maps=settings.effort_maps,
                )
                # ``effort_requested`` keeps what the decision asked for: a cap applied to the
                # replay is visible in ``effort`` (and in ``effort_requested != effort``), not by
                # rewriting the request into the value that was actually applied.
                decision = _decision_from_memo(memo, effort=re_effort)
            else:
                fetched, skip_reason = self._decide(settings, effective, request, where)
                if fetched is None:
                    return self._fail_open(
                        settings,
                        skip_reason or REASON_EXCEPTION,
                        where,
                        effective=effective,
                        provider=provider,
                        api_mode=api_mode,
                        original_request=original_request,
                        selection=selection,
                    )
                decision = fetched
                # A decision naming a model the provider does not have is worse than no decision:
                # asking for it turns a degraded turn into a dead one (HTTP 404, no response).
                # Leave the request exactly as the operator configured it.
                #
                # Deliberately NOT memoised as a negative memo, unlike ``low_confidence`` and
                # ``timeout``: the decider answered, and what was refused is the *answer*, so the
                # next request of the turn may still get a decision this router can use. Only the
                # decider's own slow, repeatable failures become markers (see MEMOISABLE_FAILURES).
                if not self._provider_has(decision.model, provider=provider, decider=settings.decider):
                    logger.warning(
                        "jev-effort-router: Jev chose %r, which is not in the provider's catalog; "
                        "leaving the turn on the configured model",
                        decision.model,
                    )
                    return self._fail_open(
                        settings,
                        REASON_SKIPPED_UNKNOWN_MODEL,
                        where,
                        effective=effective,
                        provider=provider,
                        api_mode=api_mode,
                        original_request=original_request,
                        selection=selection,
                        extra={"chosen_model": decision.model},
                    )
                memo = Memo(
                    model=decision.model,
                    effort=decision.effort,
                    effort_requested=decision.effort_requested,
                    model_confidence=decision.model_confidence,
                    effort_confidence=decision.effort_confidence,
                    choice=decision.model_choice,
                    fallback_reasons=decision.fallback_reasons,
                    decider=decision.decider,
                    provider=provider,
                    session_id=session_id,
                    decider_endpoint=settings.base_url_for(decision.decider),
                    decider_model=settings.decider_model_for(decision.decider),
                    decider_model_used=decision.decider_model,
                    decider_endpoint_used=decision.decider_endpoint,
                    decider_commandcode_endpoint=secondary.url,
                    decider_commandcode_model=secondary.model,
                    decider_commandcode_key_env=secondary.key_env,
                    decider_fallback_reason=decision.decider_fallback_reason,
                    confidence_threshold=effective.confidence_threshold,
                    decider_fallback_model=(settings.console_jev_fallback_model or "").strip() if decision.decider == "jev_console" else "",
                    decider_key_env=settings.api_key_env_for(decision.decider),
                    grid_signature=tuple((e.model_id, e.description) for e in effective.grid),
                    has_effort_confidence=getattr(decision, "has_effort_confidence", False),
                )
                # Storing on "no memo found" rather than on an api_call_count test is deliberate:
                # Hermes passes the *incremented* counter, so the first provider call of a turn
                # arrives with api_call_count == 1 (see agent/turn_iteration_prep.py). Keying the
                # store off that value silently never stored anything, and every call inside a
                # turn's tool loop re-decided. Absence of a memo is the reliable "first call of
                # this turn" signal.
                if settings.route_per_turn:
                    self._memo.put_turn(turn_id, memo)
                else:
                    self._memo.put_session(session_id, memo)

            assert decision is not None
            routed = self._apply(
                original_request,
                decision,
                api_mode=api_mode,
                wire_model=wire_model_for(provider, effective, decision.model),
            )
            decision = self._applied_decision(decision, routed, api_mode=api_mode)
            self._record_route(
                settings,
                decision,
                where,
                api_call_count=api_call_count,
                replayed=replayed,
                wire_model=routed.get("model") or decision.model,
            )
            return {"request": routed, "source": "jev-effort-router", "reason": decision.model}
        except Exception as exc:  # noqa: BLE001 - a router must never break a turn
            logger.warning(
                "jev-effort-router: routing failed (%s: %s); leaving request untouched",
                type(exc).__name__,
                exc,
            )
            return self._fail_open(
                settings,
                REASON_EXCEPTION,
                where,
                effective=effective,
                provider=provider,
                api_mode=api_mode,
                original_request=original_request,
                selection=selection,
            )

    # -- decision plumbing -------------------------------------------------------

    def _provider_has(self, model_id: str, provider: str = "", decider: str = "") -> bool:
        """Whether the chosen model is safe to put on the wire, according to the provider catalog.

        Answers ``True`` whenever the catalog has nothing to say — an absent cache, an unreadable
        file, a non-Ollama provider, or a host that exposes none of it all mean "no evidence",
        and the router must not start refusing decisions on missing evidence. Only a catalog that
        lists models and does not contain this one is a No. An opt-in OpenCode route pins its
        own catalog, so ``decider`` is what makes an opt-in route's cap apply.
        """
        try:
            known = self._catalog.is_known(model_id, provider=provider, decider=decider)
        except Exception as exc:  # noqa: BLE001 - a catalog probe never blocks a turn
            logger.debug("jev-effort-router: catalog check failed (%s: %s)", type(exc).__name__, exc)
            return True
        return known is not False

    def _memo_mismatch(
        self,
        settings: Settings,
        memo: Memo,
        provider: str,
        session_id: str,
        effective: EffectiveSettings,
        secondary: "CommandCodeFallback",
    ) -> Optional[Tuple[str, str]]:
        """Why this memo does not belong to this request, as ``(skip_reason, detail)``.

        One place for the provenance comparison, because both memo kinds must answer it the same
        way: a decision taken for another provider, session, decider or decider configuration must
        not be replayed here, and neither may a negative memo recorded under that provenance.
        ``None`` means the memo belongs to this request.

        What differs is the caller's response. A *decision* from another provenance fails the
        request open: leaving the operator's model alone is safer than pinning a model chosen for a
        different wire. A *marker* is simply dropped — it holds no decision to lose, and failing
        open would deny this request a decision the decider can still make.
        """
        memo_prov = getattr(memo, "provider", "")
        if memo_prov and provider and memo_prov != provider:
            return REASON_SKIPPED_PROVIDER, (
                f"memo decided for provider {memo_prov!r} is incompatible with active provider {provider!r}"
            )

        memo_session = getattr(memo, "session_id", "")
        if memo_session and session_id and memo_session != session_id:
            return REASON_SKIPPED_PROVIDER, (
                f"memo decided for session {memo_session!r} is incompatible with active session {session_id!r}"
            )

        memo_dec = getattr(memo, "decider", "")
        if memo_dec and memo_dec != settings.decider:
            return REASON_SKIPPED_PROVIDER, (
                f"memo decided with decider {memo_dec!r} is incompatible with active decider {settings.decider!r}"
            )

        memo_endpoint = getattr(memo, "decider_endpoint", "")
        curr_endpoint = settings.base_url_for(settings.decider)
        if memo_endpoint and curr_endpoint and memo_endpoint != curr_endpoint:
            return REASON_SKIPPED_PROVIDER, f"memo decider endpoint {memo_endpoint!r} != active {curr_endpoint!r}"

        memo_model = getattr(memo, "decider_model", "")
        curr_model = settings.decider_model_for(settings.decider)
        if memo_model and curr_model and memo_model != curr_model:
            return REASON_SKIPPED_PROVIDER, f"memo decider model {memo_model!r} != active {curr_model!r}"

        memo_fallback = getattr(memo, "decider_fallback_model", "")
        curr_fallback = (
            (settings.console_jev_fallback_model or "").strip() if settings.decider == "jev_console" else ""
        )
        if memo_fallback != curr_fallback:
            return REASON_SKIPPED_PROVIDER, f"memo fallback model {memo_fallback!r} != active {curr_fallback!r}"

        memo_key_env = getattr(memo, "decider_key_env", "")
        curr_key_env = settings.api_key_env_for(settings.decider)
        if memo_key_env and curr_key_env and memo_key_env != curr_key_env:
            return REASON_SKIPPED_PROVIDER, f"memo key env {memo_key_env!r} != active {curr_key_env!r}"

        # Secondary-transport guard: the opt-in SystemOne trio is configuration identity too.
        # Changing its endpoint, model or key variable — or turning it on/off — invalidates the
        # replay rather than reproducing a decision the active configuration would no longer
        # make. An inactive trio is ("", "", ""), which is what every legacy memo carries.
        memo_secondary = (
            getattr(memo, "decider_commandcode_endpoint", ""),
            getattr(memo, "decider_commandcode_model", ""),
            getattr(memo, "decider_commandcode_key_env", ""),
        )
        if memo_secondary != (secondary.url, secondary.model, secondary.key_env):
            return REASON_SKIPPED_PROVIDER, (
                f"memo commandcode fallback {memo_secondary!r} != active "
                f"{(secondary.url, secondary.model, secondary.key_env)!r}"
            )

        if not api_key(curr_key_env) and not _secondary_key_available(secondary):
            return REASON_NO_API_KEY, f"active decider key env {curr_key_env!r} has no key"

        # Criteria and options must match the active grid: a decision or marker recorded against
        # a question that has since changed says nothing about this one.
        curr_grid_sig = tuple((e.model_id, e.description) for e in effective.grid)
        memo_grid_sig = getattr(memo, "grid_signature", ())
        if memo_grid_sig and memo_grid_sig != curr_grid_sig:
            return REASON_SKIPPED_MODEL, "memo grid criteria signature does not match active grid"

        return None

    def _lookup(
        self, settings: Settings, turn_id: str, session_id: str
    ) -> Tuple[Optional[Memo], bool]:
        """The memoized decision for this request, and whether it is a replay.

        ``route_per_turn: true`` (the default) memoizes per turn, so the first request of a
        turn decides and the rest of the turn's tool loop replays it. ``false`` memoizes per
        session instead, which routes once per conversation.
        """
        if settings.route_per_turn:
            memo = self._memo.get_turn(turn_id, session_id=session_id)
            return memo, memo is not None
        memo = self._memo.get_session(session_id)
        if memo is not None:
            return memo, True
        # No session decision yet, but this turn may already have decided one: don't re-decide
        # inside the same turn's tool loop.
        memo = self._memo.get_turn(turn_id, session_id=session_id)
        return memo, memo is not None

    def _decide(
        self, settings: Settings, effective: EffectiveSettings, request: Dict[str, Any], where: "_Where"
    ) -> Tuple[Optional[Decision], Optional[str]]:
        """Ask the decider for one decision, and say why when it could not be had.

        Returns ``(decision, None)`` on success and ``(None, reason)`` otherwise. The reason is
        *returned* rather than recorded here because the caller owns the fail-open response: whether
        a failed decision leaves the request untouched or reconciles it with the session's own
        selection is decided in one place (:meth:`_fail_open`), and a skip recorded here could not
        carry what was actually sent.
        """
        messages = conversation_messages(request)
        if not messages or not last_user_message(messages).strip():
            return None, REASON_SKIPPED_NO_MESSAGE

        try:
            decision, reason = self.client(settings).decide(
                messages,
                effective,
                platform=where.platform,
                provider=where.provider,
            )
        except Exception as exc:  # noqa: BLE001 - belt and braces around the client
            logger.warning("jev-effort-router: decision call failed (%s); leaving request untouched", exc)
            return None, REASON_EXCEPTION

        if decision is None:
            if reason == REASON_NO_API_KEY:
                key_env = settings.api_key_env_for(settings.decider)
                logger.info("jev-effort-router: %s is not set; turns keep the configured model", key_env)
            self._remember_failure(settings, effective, where, reason or REASON_EXCEPTION)
            return None, reason or REASON_EXCEPTION
        return decision, None

    def _remember_failure(
        self, settings: Settings, effective: EffectiveSettings, where: "_Where", reason: str
    ) -> None:
        """Memoise a failed decision for the rest of the turn (a *negative* memo).

        Without this, ``low_confidence`` and ``timeout`` were re-asked on every provider request
        of the turn's tool loop: same state, same question, same unusable answer — measured at
        ~9 decider calls per failing turn. The marker carries no model or effort; it only records
        that this turn already asked and failed, with the provenance the replay guards compare.

        Per-turn only: ``route_per_turn: false`` memoises per session, and suppressing a session
        for 24h because one request timed out would be a far larger behaviour change than the
        waste it fixes. The store is a best-effort optimisation — a failure here never changes
        the outcome of the turn.
        """
        if not settings.route_per_turn or reason not in MEMOISABLE_FAILURES or not where.turn_id:
            return
        secondary = settings.active_commandcode_fallback()
        try:
            self._memo.put_turn(
                where.turn_id,
                failure_marker(
                    reason,
                    provider=where.provider,
                    session_id=where.session_id,
                    decider=settings.decider,
                    decider_endpoint=settings.base_url_for(settings.decider),
                    decider_model=settings.decider_model_for(settings.decider),
                    decider_fallback_model=(
                        (settings.console_jev_fallback_model or "").strip()
                        if settings.decider == "jev_console"
                        else ""
                    ),
                    decider_key_env=settings.api_key_env_for(settings.decider),
                    # The SystemOne trio is provenance too; leaving it empty makes the marker
                    # foreign on every deployment with the fallback enabled.
                    decider_commandcode_endpoint=secondary.url,
                    decider_commandcode_model=secondary.model,
                    decider_commandcode_key_env=secondary.key_env,
                    grid_signature=tuple((e.model_id, e.description) for e in effective.grid),
                    confidence_threshold=effective.confidence_threshold,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - remembering a failure is optional
            logger.debug("jev-effort-router: could not memoise the turn failure (%s)", exc)

    def _apply(
        self,
        original_request: Dict[str, Any],
        decision: Decision,
        *,
        api_mode: str = "",
        wire_model: str = "",
    ) -> Dict[str, Any]:
        """Build the rewritten request from the pre-middleware payload.

        Working from ``original_request`` keeps this router idempotent when more than one
        middleware rewrites the same call, and makes "the request is unchanged" literally true
        on the no-route path. The payload branch is the one :meth:`_rewrite` documents.
        """
        return self._rewrite(
            original_request,
            model=wire_model or decision.model,
            effort=decision.effort,
            api_mode=api_mode,
        )

    def _rewrite(
        self,
        original_request: Dict[str, Any],
        *,
        model: str,
        effort: Optional[str],
        api_mode: str = "",
    ) -> Dict[str, Any]:
        """Put ``model`` and ``effort`` on the pre-middleware payload, in the payload's own shape.

        The branch is chosen by :func:`config.wire_style`, which answers from the ``api_mode`` the
        route speaks: the router never tests a provider id here. Both callers — a decision
        (:meth:`_apply`) and a reconciled fail-open turn (:meth:`_reconciled`) — go through this one
        place, so the session's selection cannot reach the wire in a shape the decider's own choice
        would not.

        On the top-level style only ``reasoning_effort`` is written. ``reasoning_config`` is a
        *params-level* input that the provider profile consumes to derive that top-level field
        (``agent/transports/chat_completions.py::_build_kwargs_from_profile``, then
        ``plugins/model-providers/ollama-cloud/__init__.py`` which returns only
        ``reasoning_effort``). It is never a wire kwarg, and leaving it in the final payload makes
        Ollama reject the entire call with "Completions.create() got an unexpected keyword argument
        'reasoning_config'" — a hard turn failure, not a degraded route.

        On the ``responses`` style the field is nested instead — see
        :meth:`_apply_responses_reasoning`; on ``output_config`` inside the Messages block — see
        :meth:`_apply_anthropic_effort`.
        """
        routed = dict(original_request)
        routed["model"] = model
        style = wire_style(api_mode)
        if style == WIRE_STYLE_RESPONSES:
            return self._apply_responses_reasoning(routed, effort)
        if style == WIRE_STYLE_OUTPUT_CONFIG:
            return self._apply_anthropic_effort(routed, effort)
        if effort:
            routed[EFFORT_WIRE_KEY] = effort
        else:
            routed.pop(EFFORT_WIRE_KEY, None)
        return routed

    @staticmethod
    def _apply_responses_reasoning(routed: Dict[str, Any], effort: Optional[str]) -> Dict[str, Any]:
        """Rewrite ``reasoning.effort`` on an already-built Responses body.

        The transport decides whether this route takes a reasoning field at all, and emits the
        object from the session's ``reasoning_config``; a route it judged field-less carries no
        ``reasoning`` key, and adding one is exactly the 400 class that guard exists to avoid. So
        the level lands inside an existing object only, and an omitted level removes just the
        ``effort`` — ``summary``/``include`` are the transport's, not this router's.
        """
        reasoning = routed.get(RESPONSES_REASONING_KEY)
        if not isinstance(reasoning, dict):
            return routed
        updated = dict(reasoning)
        if effort:
            updated["effort"] = effort
        else:
            updated.pop("effort", None)
        routed[RESPONSES_REASONING_KEY] = updated
        return routed

    @staticmethod
    def _apply_anthropic_effort(routed: Dict[str, Any], effort: Optional[str]) -> Dict[str, Any]:
        """Rewrite ``output_config.effort`` on an already-built Anthropic Messages body.

        The transport is the author of that object: it emits
        ``thinking: {type: adaptive, display: summarized}`` plus ``output_config: {effort: ...}``
        for adaptive-thinking models, ``thinking: {type: disabled}`` (and no ``output_config``) when
        the session turned reasoning off, and nothing at all for a family it classifies as
        pre-adaptive-thinking. The plugin therefore writes **inside** an ``output_config`` that
        already carries an ``effort``, and adds neither the object nor the key: an invented
        ``output_config`` on a route the transport judged field-less is exactly the 400 class the
        ``reasoning`` guard avoids on the Responses route.

        ``thinking`` and any other ``output_config`` key (``format``, for structured output) are
        the transport's and are copied through untouched. An omitted level removes the ``effort``
        key — a Messages body is valid without it (the model then uses its default) — and an
        ``output_config`` left empty by that removal is dropped rather than sent as ``{}``.
        """
        output_config = routed.get(ANTHROPIC_OUTPUT_CONFIG_KEY)
        if not isinstance(output_config, dict) or ANTHROPIC_EFFORT_KEY not in output_config:
            return routed
        updated = dict(output_config)
        if effort:
            updated[ANTHROPIC_EFFORT_KEY] = effort
        else:
            updated.pop(ANTHROPIC_EFFORT_KEY, None)
        if updated:
            routed[ANTHROPIC_OUTPUT_CONFIG_KEY] = updated
        else:
            routed.pop(ANTHROPIC_OUTPUT_CONFIG_KEY, None)
        return routed

    @staticmethod
    def _applied_decision(
        decision: Decision,
        routed: Dict[str, Any],
        *,
        api_mode: str = "",
    ) -> Decision:
        """The decision as it actually reached the payload.

        A nested route the transport judged as taking no effort field carries no container to
        write into (no ``reasoning`` object on the Responses body, no ``output_config`` on the
        Messages body), and this router will not invent one. The audit then reports the level as
        *not* sent (``effort: null``) while ``effort_requested`` keeps Jev's choice — a null effort
        is a statement about the wire, never about what the model executed. A ``reasoning`` object
        the transport disabled is the same case: nothing was written, so nothing is claimed.
        """
        if not decision.effort:
            return decision
        style = wire_style(api_mode)
        if style == WIRE_STYLE_RESPONSES:
            container, effort_key = routed.get(RESPONSES_REASONING_KEY), EFFORT_KEY
        elif style == WIRE_STYLE_OUTPUT_CONFIG:
            container, effort_key = routed.get(ANTHROPIC_OUTPUT_CONFIG_KEY), ANTHROPIC_EFFORT_KEY
        else:
            return decision
        if isinstance(container, dict) and container.get(effort_key) == decision.effort:
            return decision
        return replace(decision, effort=None)

    # -- the session's own selection ---------------------------------------------

    def _read_selection(self, settings: Settings, where: "_Where") -> Optional[Selection]:
        """The session row's selection, when this deployment and surface ask for it.

        The mode is checked first — before the profile home is even resolved — so ``mode: off``
        never opens the database (``sqlite3.connect`` is unreachable) and every observable of the
        request path stays what it was before this feature existed.
        """
        config = settings.session_selection
        if not config.enabled or not where.session_id:
            return None
        if not config.applies_to(where.platform):
            return None
        home = selection_profile_home(self._data_dir())
        if home is None:
            logger.debug("jev-effort-router: no profile home; no session selection")
            return None
        return self._selections.read(home, where.session_id, where.turn_id)

    def _eligible_selection(
        self,
        settings: Settings,
        effective: EffectiveSettings,
        selection: Optional[Selection],
        *,
        provider: str,
        where: "_Where",
    ) -> Optional[Selection]:
        """The selection this request may use, or ``None`` with the reason in the audit.

        Eligible means: the selection's provider is **this** request's route (identity, aliases
        included) **or** the ``session_selection.provider_map`` declares this request's provider as
        another route the same choice is valid on; and its model is in the route's catalog (where
        the route pins one) and in the surface's grid. Anything else is ``selection_unmappable`` and
        no action — a selection naming a model the route would reject turns a degraded turn into a
        dead one, which is the outcome this plugin exists to avoid.

        Identity is unconditional on purpose: the map is additive ("also valid in"), not a
        restriction, and by default it is empty. Reading it as "only valid in" made a native
        ``anthropic`` pick unusable on a native request and left the turn on the profile's default.
        """
        if selection is None:
            return None
        provider_key = (provider or "").strip().lower()
        mapped = settings.session_selection.provider_for(selection.provider)
        same_route = same_provider_route(selection.provider, provider_key)
        catalog = tuple(entry.model_id for entry in (route_catalog(provider_key) or ()))
        selection_model = selection.model
        reason = ""
        if not (same_route or mapped == provider_key):
            reason = "provider_mismatch"
        elif catalog and selection_model not in catalog:
            reason = "model_outside_catalog"
        elif effective.entry_for(selection_model) is None:
            reason = "model_outside_grid"
        if not reason:
            return selection

        if self._first_for_turn(where.turn_id, "unmappable"):
            self._record_session_event(
                settings,
                "selection_unmappable",
                where,
                {
                    "reason": reason,
                    "session_model": selection.model,
                    "session_provider": selection.provider,
                    "session_effort": selection.effort,
                    "mapped_provider": mapped,
                    "request_model": where.model,
                },
            )
        logger.debug(
            "jev-effort-router: session selection %r does not apply to %r (%s)",
            selection.model,
            provider_key,
            reason,
        )
        return None

    def _note_divergence(
        self,
        settings: Settings,
        selection: Selection,
        payload: Any,
        *,
        provider: str,
        api_mode: str,
        where: "_Where",
    ) -> None:
        """Record — once per turn — that the session's row and this request disagree.

        ``warn`` and ``apply`` both raise a ``WARNING``: the divergence is the symptom of the build
        race, and a deployment that only wants to watch it still needs it in the log. The audit
        record names both sides and what the plugin did about them, so a later reader can tell a
        reconciled turn from one that was only watched.
        """
        request_effort = wire_effort(payload, api_mode)
        model_differs = bool(selection.model) and selection.model != where.model
        effort_differs = (
            selection.enabled is not False
            and selection.effort is not None
            and selection.effort != request_effort
        )
        if not (model_differs or effort_differs):
            return
        if not self._first_for_turn(where.turn_id, "divergence"):
            return

        config = settings.session_selection
        action = "reconcile_on_fail_open" if config.applies else "watch"
        self._record_session_event(
            settings,
            "selection_divergence",
            where,
            {
                "session_model": selection.model,
                "session_provider": selection.provider,
                "session_effort": selection.effort,
                "mapped_provider": config.provider_for(selection.provider),
                "request_model": where.model,
                "request_effort": request_effort,
                "action": action,
            },
        )
        if config.warns:
            logger.warning(
                "jev-effort-router: the session chose %s/%s (effort %s) but this request is %s/%s "
                "(effort %s); %s",
                selection.model,
                selection.provider,
                selection.effort,
                where.model,
                provider,
                request_effort,
                "a fail-open turn is reconciled with it"
                if config.applies
                else "leaving the request untouched",
            )

    def _note_native_selection(
        self, settings: Settings, where: "_Where", provider_key: str
    ) -> None:
        """Say — once per turn — that the live agent sits on the native Anthropic route.

        An unroutable provider is left untouched by design, and no middleware can move an agent to
        another route: out of the build race this plugin has no lever, so reporting it is the whole
        of its responsibility there. What that turn costs is not this plugin's to know: the core
        resolves the credential, and Claude Code's OAuth spends the subscription quota while an
        ``sk-ant-api*`` key bills per token. The warning therefore names the route, not a price.
        """
        config = settings.session_selection
        if not config.enabled or provider_key not in NATIVE_ANTHROPIC_PROVIDERS:
            return
        selection = self._read_selection(settings, where)
        if selection is None:
            return
        if not self._first_for_turn(where.turn_id, "native"):
            return
        self._record_session_event(
            settings,
            "selection_native_provider",
            where,
            {
                "session_model": selection.model,
                "session_provider": selection.provider,
                "session_effort": selection.effort,
                "mapped_provider": config.provider_for(selection.provider),
                "action": "none",
            },
        )
        if config.warns:
            logger.warning(
                "jev-effort-router: the agent is on the native %s route; the session chose %s/%s "
                "and this turn is billed against whatever credential the core resolved for that "
                "route (Claude Code OAuth: the subscription; an API key: per token)",
                provider_key,
                selection.model,
                selection.provider,
            )

    def _fail_open(
        self,
        settings: Settings,
        reason: str,
        where: "_Where",
        *,
        effective: EffectiveSettings,
        provider: str = "",
        api_mode: str = "",
        original_request: Any = None,
        selection: Optional[Selection] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """The single exit for every **post-gate** fail-open of a turn.

        Records the skip exactly as the router always did, and — only in ``mode: apply``, only with
        an eligible selection, and only from here — returns the request rewritten to what the
        session's own row says. ``disabled``, ``provider_not_routed``, ``api_mode_not_routed``
        and ``user_data`` never reach this method: those are decisions this
        router may not override, not failures to be patched over. Neither does a successful
        decision: when the decider answers, its answer is what the turn runs on.

        The answer is the middleware's ``{"request": ...}`` shape when a rewrite happened, and
        ``None`` otherwise — byte for byte what the router returned before this feature existed.
        """
        routed: Optional[Dict[str, Any]] = None
        if selection is not None:
            try:
                routed = self._reconciled(
                    settings,
                    selection,
                    effective,
                    original_request,
                    provider=provider,
                    api_mode=api_mode,
                )
            except Exception as exc:  # noqa: BLE001 - a failed reconciliation is just a skip
                logger.debug(
                    "jev-effort-router: could not reconcile the turn (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
                routed = None

        record_extra = dict(extra or {})
        keep_none: Tuple[str, ...] = ()
        if routed is not None:
            record_extra["reconciled_model"] = routed.get("model")
            record_extra["reconciled_effort"] = wire_effort(routed, api_mode)
            record_extra["selection_source"] = SELECTION_SOURCE_SESSION_ROW
            # ``null`` here is a fact — the level was omitted (route cap, or a session that asked
            # for no level) — so the key is kept rather than filtered away with the other empties.
            keep_none = ("reconciled_effort",)
            logger.info(
                "jev-effort-router: %s; reconciled with the session's selection (%s, effort %s)",
                reason,
                record_extra["reconciled_model"],
                record_extra["reconciled_effort"],
            )

        self._record_skip(settings, reason, where, extra=record_extra, keep_none=keep_none)
        if routed is None:
            return None
        # Same shape a routed turn returns: ``reason`` names the model that went on the wire, so a
        # host that logs it cannot mistake a reconciled turn for an unrouted one.
        return {
            "request": routed,
            "source": "jev-effort-router",
            "reason": str(routed.get("model") or ""),
        }

    def _reconciled(
        self,
        settings: Settings,
        selection: Selection,
        effective: EffectiveSettings,
        original_request: Any,
        *,
        provider: str = "",
        api_mode: str = "",
    ) -> Optional[Dict[str, Any]]:
        """The request rewritten to the session's own model and effort, or ``None``.

        ``None`` unless the mode is ``apply`` — that is the whole difference between ``warn`` and
        ``apply``. The model goes through :func:`wire_model_for` (the transport strips picker
        aliases before this middleware runs) and the level through :func:`resolve_effort`, the same
        pair the decision path uses, so a level the route's family cap does not accept is *omitted*
        from the wire rather than clamped onto a neighbour.
        """
        if not settings.session_selection.applies or selection is None:
            return None
        if not isinstance(original_request, dict):
            return None
        return self._rewrite(
            original_request,
            model=wire_model_for(provider, effective, selection.model),
            effort=self._selection_effort(
                settings, selection, original_request, provider=provider, api_mode=api_mode
            ),
            api_mode=api_mode,
        )

    @staticmethod
    def _selection_effort(
        settings: Settings,
        selection: Selection,
        original_request: Dict[str, Any],
        *,
        provider: str = "",
        api_mode: str = "",
    ) -> Optional[str]:
        """The level a reconciled wire carries; the payload's own level when the row says nothing.

        Two absences must not become a level: a row that disabled reasoning (``enabled: false``) and
        a row with no ``reasoning_config`` at all. In both cases the payload's own value is what
        goes out — this router does not own the ``enabled`` switch, and inventing a level for a
        session that never named one would be exactly the kind of guess an audit cannot explain.
        """
        if selection.enabled is False or selection.effort is None:
            return wire_effort(original_request, api_mode)
        return resolve_effort(
            selection.model, selection.effort, provider=provider, effort_maps=settings.effort_maps
        )

    def _record_session_event(
        self, settings: Settings, event: str, where: "_Where", extra: Dict[str, Any]
    ) -> None:
        """Append one session-selection record to the audit for this turn.

        Unlike a skip record, ``None`` fields are kept: for these events a missing level is the
        fact being reported (``request_effort: null``), not a value to be dropped.
        """
        record: Dict[str, Any] = {"event": event}
        record.update(extra or {})
        record.update(where.fields())
        self.audit(settings).append(record)

    def _first_for_turn(self, turn_id: str, kind: str) -> bool:
        """Whether this turn has not produced a ``kind`` record yet, and remember that it has.

        The session row is read once per turn, so its events describe a *turn*: emitting them on
        every provider request of the tool loop would repeat one fact N times. A turn without an id
        cannot be deduplicated and always answers ``True``.
        """
        key = f"{kind}:{turn_id}" if turn_id else ""
        if not key:
            return True
        if key in self._turn_events:
            return False
        if len(self._turn_events) >= 512:
            self._turn_events.clear()
        self._turn_events[key] = None
        return True

    # -- audit -------------------------------------------------------------------

    def _record_route(
        self,
        settings: Settings,
        decision: Decision,
        where: "_Where",
        *,
        api_call_count: Any = 0,
        replayed: bool = False,
        wire_model: str = "",
    ) -> None:
        # An audit record records what was measured and says ``null`` for what was not. On a
        # replayed turn the distributions, alternatives and effort-choice string were not stored,
        # so the record must present them as absent (`None`) — never as a value copied from a
        # neighbouring field. The confidences stay: they were measured as a pair and re-emitted
        # as that pair.
        #
        # ``model`` is the id the *decision* named and ``wire_model`` is the id that went in the
        # provider's ``model`` field. They differ on the Codex route's ``-900k`` picker aliases,
        # which the transport strips before the wire: a record that only carried ``model`` would
        # claim a route that did not happen.
        record: Dict[str, Any] = {
            "event": "route",
            "model": decision.model,
            "wire_model": wire_model or decision.model,
            "effort": decision.effort,
            "effort_requested": decision.effort_requested,
            "model_choice": decision.model_choice,
            # The route the decision was applied to. Skip records already name it; a route record
            # that omitted it could not be told apart from one written for another provider
            # (``opencode-go`` and ``commandcode`` now both send ``reasoning_effort``).
            "provider": where.provider,
            # ``effort_choice`` is a string on a fresh decision; a replay carries "" meaning
            # "not measured at replay", which the audit surfaces as null.
            "effort_choice": decision.effort_choice if decision.effort_choice else None,
            "model_confidence": round(decision.model_confidence, 4),
            "effort_confidence": round(decision.effort_confidence, 4),
            "model_probabilities": decision.model_probabilities if decision.model_probabilities else None,
            "effort_probabilities": decision.effort_probabilities if decision.effort_probabilities else None,
            "alternatives": list(decision.alternatives) if decision.alternatives else None,
            "latency_ms": decision.latency_ms,
            "fallback_reasons": list(decision.fallback_reasons),
            # The decider model that actually produced this decision. Reading it from the decision
            # (not from ``settings.jev_model``, the legacy OpenRouter id) is what keeps a
            # ``decider: jev_console`` record from naming a model that was never consulted;
            # ``decider_fallback_reason`` names why the Console fallback answered when it did.
            "jev_model": decision.decider_model or settings.decider_model_for(decision.decider),
            "decider_fallback_reason": decision.decider_fallback_reason,
            "decider": getattr(decision, "decider", settings.decider),
            "replayed": bool(replayed),
        }
        secondary = settings.active_commandcode_fallback()
        if secondary.enabled:
            # Provenance of the call that produced the decision. Only a deployment that opted into
            # the secondary transport gains these two fields — on every other one the record keeps
            # exactly the shape 0.4.0 wrote, so nothing downstream has to learn a new column.
            record["decider_model"] = decision.decider_model or settings.decider_model_for(decision.decider)
            record["decider_endpoint"] = decision.decider_endpoint or settings.base_url_for(decision.decider)
        record.update(where.fields())
        record.update({key: value for key, value in {"api_call_count": api_call_count}.items() if value not in (None, "")})
        self.audit(settings).append(record)
        if decision.degraded:
            logger.info(
                "jev-effort-router: routed %s (effort %s) with degradation %s",
                decision.model,
                decision.effort,
                ",".join(decision.fallback_reasons),
            )
        else:
            logger.debug(
                "jev-effort-router: routed %s (effort %s) in %d ms",
                decision.model,
                decision.effort,
                decision.latency_ms,
            )

    def _record_skip(
        self,
        settings: Settings,
        reason: str,
        where: "_Where",
        *,
        extra: Optional[Dict[str, Any]] = None,
        keep_none: Tuple[str, ...] = (),
    ) -> None:
        """Write one ``skip`` record.

        ``extra`` drops empty values, because a skip's optional detail is absent when it has
        nothing to say. ``keep_none`` names the keys whose ``None`` is itself the finding — a
        reconciled level that was omitted from the wire — so that fact survives the filter.
        """
        if not settings.log_skips:
            return
        record: Dict[str, Any] = {
            "event": "skip",
            "reason": reason,
            "provider": where.provider,
            "configured_model": where.model,
        }
        record.update({
            key: value
            for key, value in (extra or {}).items()
            if value not in (None, "") or key in keep_none
        })
        record.update(where.fields())
        self.audit(settings).append(record)

    # -- read-only surfaces used by the tools and commands -----------------------

    def tail(self, settings: Settings, limit: int = 10):
        return self.audit(settings).tail(limit)

    def count(self, settings: Settings) -> int:
        return self.audit(settings).count()

    def grid_report(self, settings: Settings, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        """The grid annotated with what the provider catalog says about each entry.

        Empty when there is no catalog evidence, so a caller can tell "nothing to report" from
        "every entry is fine".
        """
        target_provider = (
            provider
            if provider is not None
            else (settings.routable_providers[0] if settings.routable_providers else "")
        )
        report: List[Dict[str, Any]] = []
        for entry in settings.grid_for(target_provider):
            try:
                known = self._catalog.is_known(
                    entry.model_id, provider=target_provider, decider=settings.decider
                )
            except Exception:  # noqa: BLE001 - a status view never raises
                known = None
            if known is None:
                return []
            report.append({"model": entry.model_id, "available": bool(known)})
        return report

    def forget(self) -> None:
        self._memo.clear()


def _secondary_key_available(fallback: CommandCodeFallback) -> bool:
    """Whether the opt-in secondary transport can actually be called: configured *and* keyed.

    Only used to keep a memo replayable on a deployment whose primary credential is absent: a memo
    whose decision came from the secondary transport is still this configuration's decision.
    """
    return bool(fallback.enabled and api_key(fallback.key_env))


class _Where:
    """The identifiers a routing record is stamped with."""

    __slots__ = ("turn_id", "session_id", "api_request_id", "platform", "provider", "model")

    def __init__(self, *, turn_id, session_id, api_request_id, platform, provider, model) -> None:
        self.turn_id = turn_id
        self.session_id = session_id
        self.api_request_id = api_request_id
        self.platform = platform
        self.provider = provider
        self.model = model

    def fields(self) -> Dict[str, Any]:
        return {
            key: value
            for key, value in {
                "turn_id": self.turn_id,
                "session_id": self.session_id,
                "api_request_id": self.api_request_id,
                "platform": self.platform,
            }.items()
            if value
        }


_UNSET = object()


def _decision_from_memo(memo: Memo, effort: Any = _UNSET) -> Decision:
    """Present a replayed memo through the same shape as a fresh decision.

    Only the fields that were actually measured and are still load-bearing at replay are
    re-emitted. The confidences were measured as a pair, so they are re-emitted as that pair
    (never collapsed, never copied one onto the other). Everything that was *not* stored —
    the per-criterion distributions, the full alternative list, the effort-choice string —
    is left empty/``None`` rather than reconstructed from a sibling field: an audit record
    must never report a value that was not measured at replay.
    """
    actual_effort = memo.effort if effort is _UNSET else effort
    return Decision(
        model=memo.model,
        effort=actual_effort,
        effort_requested=memo.effort_requested,
        model_confidence=memo.model_confidence,
        effort_confidence=memo.effort_confidence,
        model_choice=memo.choice,
        effort_choice="",
        alternatives=(),
        latency_ms=0,
        fallback_reasons=memo.fallback_reasons,
        decider=getattr(memo, "decider", "jev") or "jev",
        # Provenance of the replayed decision: the model that answered when the memo was written,
        # never the currently configured primary (which this replay did not consult).
        decider_model=memo.decider_model_used or memo.decider_model,
        # The endpoint that answered when the memo was written — the secondary transport's, on a
        # turn it rescued — falling back to the configured endpoint of a legacy memo.
        decider_endpoint=getattr(memo, "decider_endpoint_used", "") or memo.decider_endpoint,
        decider_fallback_reason=memo.decider_fallback_reason,
        has_effort_confidence=bool(getattr(memo, "has_effort_confidence", False)),
    )
