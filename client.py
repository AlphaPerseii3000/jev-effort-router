"""The Jev client: one HTTP round-trip, no parsing of prose, total failure containment.

Every method returns a :class:`Decision` or ``None``. It never raises: a timeout, a 500, a
402, a malformed body, an unknown choice or a low-confidence answer all end as ``None`` plus
a reason string, so the caller's request goes out byte-identical to a plugin-disabled run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import EFFORT_LEVELS, EffectiveSettings, Settings, api_key, redact
from .effort import resolve_effort
from .grid import Entry, resolve
from .state import (
    EFFORT_QUESTION_ID,
    MODEL_QUESTION_ID,
    build_payload,
    normalise_effort_choice,
)

logger = logging.getLogger(__name__)

#: Reasons a turn was left unrouted. Stable strings — they are what the audit file records.
REASON_DISABLED = "disabled"
REASON_SURFACE_DISABLED = "surface_disabled"
REASON_NO_API_KEY = "no_api_key"
REASON_SKIPPED_PROVIDER = "provider_not_routed"
REASON_SKIPPED_API_MODE = "api_mode_not_routed"
REASON_SKIPPED_MODEL = "model_not_in_grid"
REASON_SKIPPED_UNKNOWN_MODEL = "model_not_in_provider_catalog"
REASON_SKIPPED_NO_MESSAGE = "no_user_message"
REASON_SKIPPED_REPLAYED = "turn_already_routed"
REASON_UPSTREAM_ERROR = "upstream_error"
REASON_TIMEOUT = "timeout"
REASON_MALFORMED = "malformed_answer"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_UNKNOWN_CHOICE = "unknown_choice"
REASON_USER_DATA = "user_data"
REASON_EXCEPTION = "exception"


@dataclass(frozen=True)
class Decision:
    """A routed turn: the model and the effort that will be applied, plus the audit trail."""

    model: str
    effort: Optional[str]
    effort_requested: Optional[str]
    model_confidence: float
    effort_confidence: float
    model_choice: str
    effort_choice: str
    model_probabilities: Dict[str, float] = field(default_factory=dict)
    effort_probabilities: Dict[str, float] = field(default_factory=dict)
    alternatives: Tuple[str, ...] = ()
    latency_ms: int = 0
    fallback_reasons: Tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.fallback_reasons)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


#: Narrower than :data:`effort.LADDER`: the policy vocabulary is Jev's three rungs, so a cap
#: never has to reason about ``none``/``max`` (which the plugin never routes to).
_EFFORT_ORDER = {level: index for index, level in enumerate(EFFORT_LEVELS)}


def _cap_effort(level: Optional[str], cap: Optional[str]) -> Optional[str]:
    """Lower ``level`` to ``cap`` when it is strictly stronger; never raise it.

    The ceiling only ever lowers a level and an absent ceiling changes nothing. A level or cap
    outside the three-rung vocabulary is passed through untouched rather than guessed at.
    """
    if not level or not cap:
        return level
    if level not in _EFFORT_ORDER or cap not in _EFFORT_ORDER:
        return level
    return level if _EFFORT_ORDER[level] <= _EFFORT_ORDER[cap] else cap


def _as_probabilities(raw: Any) -> Dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): _as_float(value) for key, value in raw.items()}


def _answer(answers: Any, question_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(answers, dict):
        return None
    value = answers.get(question_id)
    return value if isinstance(value, dict) else None


def _choice_text(answer: Dict[str, Any]) -> str:
    """The chosen option, or ``""`` when the answer is not a Choice answer.

    The endpoint echoes the question type back. Anything other than ``choice`` (``noul``,
    ``score``) carries no ``choice`` field, and reading one anyway would put a probability or
    an empty string on the wire as if it were a model name. ``""`` falls into the off-grid
    path, which degrades to the configured defaults.
    """
    if answer_type(answer) != "choice":
        return ""
    return str(answer.get("choice") or "")


def answer_type(answer: Dict[str, Any]) -> str:
    """The question type the endpoint echoed on this answer, lower-cased.

    A missing ``type`` is treated as ``choice``: older/proxied responses may omit it, and a
    Choice answer is the only shape these two questions ever ask for.
    """
    return str(answer.get("type") or "choice").strip().lower() or "choice"


class JevClient:
    """Decision-endpoint client.

    ``transport`` is an injection seam for tests: anything exposing
    ``post(url, *, json, headers, timeout)`` returning an object with ``status_code``,
    ``json()`` and ``text``. When it is ``None``, ``httpx`` is imported lazily inside the
    call — never at import or registration time, because ``hermes plugins doctor`` blocks
    socket access while ``register(ctx)`` runs.
    """

    def __init__(self, settings: Settings, transport: Any = None) -> None:
        self._settings = settings
        self._transport = transport

    # -- transport ---------------------------------------------------------------

    def _post(self, payload: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
        headers = {
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            # Ranking headers are optional; they make the traffic identifiable on OpenRouter.
            "X-Title": "hermes-jev-effort-router",
        }
        if self._transport is not None:
            response = self._transport.post(
                self._settings.endpoint,
                json=payload,
                headers=headers,
                timeout=self._settings.timeout_s,
            )
            return response, None

        import httpx  # lazy: no socket machinery while the plugin registers

        with httpx.Client(timeout=self._settings.timeout_s) as client:
            return (
                client.post(
                    self._settings.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self._settings.timeout_s,
                ),
                None,
            )

    # -- routing -----------------------------------------------------------------

    def decide(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        platform: str = "",
        provider: str = "",
    ) -> Tuple[Optional[Decision], Optional[str]]:
        """Ask Jev for a model and an effort level.

        ``effective`` is the frozen per-request snapshot the surface's policy produced: the grid
        offered to Jev, the confidence threshold, and the fallback model/effort all come from it,
        so a policy surface's decision path never reads the global grid or threshold. The global
        ``Settings`` stays on the client for ``endpoint``/``timeout_s``/``jev_model``/
        ``context_turns`` — the genuinely global, per-instance-cached values.

        Returns ``(decision, None)`` on a usable answer and ``(None, reason)`` otherwise. A
        partially usable answer still yields a decision: a confident model with an unconfident
        effort degrades only the effort, and vice versa.

        Note the absence of a ``current_model`` argument. It used to be forwarded into the
        request `state`, and measurement against the live endpoint showed it anchors Jev on the
        model the operator already has configured — a code-debugging task switched from
        ``kimi-k3`` (4/4 calls without it) to ``deepseek-v4.1-flash`` (4/4 calls with it).
        Telling Jev what is configured defeats the purpose of asking it.
        """
        settings = self._settings
        if not api_key():
            return None, REASON_NO_API_KEY

        payload = build_payload(
            messages,
            effective.grid,
            jev_model=settings.jev_model,
            context_turns=settings.context_turns,
            platform=platform,
            provider=provider,
        )

        started = time.monotonic()
        try:
            response, _ = self._post(payload)
        except Exception as exc:  # noqa: BLE001 - a router must never break a turn
            latency_ms = int((time.monotonic() - started) * 1000)
            name = type(exc).__name__
            timeout_types = ("Timeout", "ReadTimeout", "ConnectTimeout", "PoolTimeout", "TimeoutException")
            if any(token in name for token in timeout_types):
                logger.warning("jev-effort-router: Jev timed out after %d ms (%s)", latency_ms, name)
                return None, REASON_TIMEOUT
            logger.warning("jev-effort-router: Jev call failed: %s: %s", name, redact(exc, 300))
            return None, REASON_UPSTREAM_ERROR
        latency_ms = int((time.monotonic() - started) * 1000)

        status = getattr(response, "status_code", None)
        if status is None or not (200 <= int(status) < 300):
            body = redact(getattr(response, "text", ""), 300)
            logger.warning("jev-effort-router: Jev returned HTTP %s: %s", status, body)
            return None, REASON_UPSTREAM_ERROR

        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is just another failure
            logger.warning("jev-effort-router: Jev returned a non-JSON body: %s", redact(getattr(response, "text", ""), 300))
            return None, REASON_MALFORMED

        return self._interpret(data, effective, latency_ms=latency_ms)

    def _interpret(
        self, data: Any, effective: EffectiveSettings, *, latency_ms: int
    ) -> Tuple[Optional[Decision], Optional[str]]:
        if not isinstance(data, dict):
            return None, REASON_MALFORMED
        answers = data.get("answers")
        model_answer = _answer(answers, MODEL_QUESTION_ID)
        effort_answer = _answer(answers, EFFORT_QUESTION_ID)
        if model_answer is None and effort_answer is None:
            return None, REASON_MALFORMED

        reasons: List[str] = []

        # -- model -----------------------------------------------------------------
        chosen: Optional[Entry] = None
        model_confidence = 0.0
        model_choice_text = ""
        model_probabilities: Dict[str, float] = {}
        if model_answer is None:
            reasons.append(REASON_MALFORMED)
        else:
            model_choice_text = _choice_text(model_answer)
            model_confidence = _as_float(model_answer.get("confidence"))
            model_probabilities = _as_probabilities(model_answer.get("probabilities"))
            chosen = resolve(_criteria_map(model_answer), model_choice_text, effective.grid)
            if chosen is None:
                reasons.append(REASON_UNKNOWN_CHOICE)
            elif model_confidence < effective.confidence_threshold:
                reasons.append(REASON_LOW_CONFIDENCE)
                chosen = None

        if chosen is None:
            chosen = effective.entry_for(effective.default_model)
            if chosen is not None:
                model = chosen.model_id
            elif effective.grid_from_policy and effective.grid:
                # A policy grid is a structural cap, and its fallback resolves inside it. When
                # the operator's configured default is outside the policy grid, re-pin to the
                # grid's first entry rather than emitting a model the surface forbids.
                model = effective.grid[0].model_id
            else:
                # No policy grid: today's behaviour — the configured default verbatim, even
                # when the global grid does not contain it.
                model = effective.default_model
        else:
            model = chosen.model_id

        # -- effort ----------------------------------------------------------------
        # A usable-but-distrusted effort answer (low confidence, off-vocabulary choice)
        # degrades to the operator's configured default. An effort answer that is simply
        # absent carries nothing to degrade *to*: inventing a level for a model whose ladder
        # we have no evidence for is exactly what `effort.py` exists to avoid, so the field
        # is omitted and the model's own default applies. The route still happens either way.
        requested: Optional[str] = None
        effort_confidence = 0.0
        effort_choice_text = ""
        effort_probabilities: Dict[str, float] = {}
        answered = False
        if effort_answer is None:
            reasons.append(REASON_MALFORMED)
        elif answer_type(effort_answer) != "choice":
            reasons.append(REASON_MALFORMED)
        else:
            answered = True
            effort_choice_text = _choice_text(effort_answer)
            effort_confidence = _as_float(effort_answer.get("confidence"))
            effort_probabilities = _as_probabilities(effort_answer.get("probabilities"))
            candidate = normalise_effort_choice(effort_choice_text)
            if candidate is None:
                reasons.append(REASON_UNKNOWN_CHOICE)
            elif effort_confidence < effective.confidence_threshold:
                reasons.append(REASON_LOW_CONFIDENCE)
            else:
                requested = candidate

        if requested is None and answered:
            requested = effective.default_effort

        # The ceiling bounds the level the router emits — Jev's answer or `default_effort` —
        # compared before the per-family table resolves it. A family's declared override may
        # still raise the resolved wire value; that is intended (the ceiling is a cap on what
        # the router asks for, not on the family's translation of it).
        if requested is None and effective.max_effort:
            # No usable effort answer at all, but the surface has a ceiling: emit it rather
            # than omit the field, so the restricted surface is a policy and not a hint.
            requested = effective.max_effort
        requested = _cap_effort(requested, effective.max_effort)

        effort = resolve_effort(model, requested)

        alternatives = tuple(
            entry.model_id for entry in effective.grid if entry.model_id != model
        )
        return (
            Decision(
                model=model,
                effort=effort,
                effort_requested=requested,
                model_confidence=model_confidence,
                effort_confidence=effort_confidence,
                model_choice=model_choice_text,
                effort_choice=effort_choice_text,
                model_probabilities=model_probabilities,
                effort_probabilities=effort_probabilities,
                alternatives=alternatives,
                latency_ms=latency_ms,
                # Deliberately not de-duplicated: the model and the effort degrade
                # independently, so "low_confidence" twice means both were degraded.
                fallback_reasons=tuple(reasons),
            ),
            None,
        )


def _criteria_map(model_answer: Dict[str, Any]) -> Dict[str, str]:
    """The criteria the endpoint was given, when it echoes them back with the answer."""
    echoed = model_answer.get("criteria")
    if isinstance(echoed, dict):
        return {str(key): str(value) for key, value in echoed.items()}
    return {}
