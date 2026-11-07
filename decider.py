"""Pluggable decider backends for jev-effort-router.

Supported deciders:
- 'jev': OpenRouter Decisions API (typesafe/jev-1.13 by default).
- 'openai_compatible': OpenAI-compatible chat completions (e.g. OpenCode zen/go/v1).

Every method returns a :class:`Decision` or ``None``. It never raises: a timeout, an HTTP error,
a malformed body, an unknown choice or a low-confidence answer all fail-open to ``None`` plus a
reason string, so the caller's request goes out byte-identical to a plugin-disabled run.
"""

from __future__ import annotations

import json
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import (
    DEFAULT_CONSOLE_BASE_URL,
    DEFAULT_CONSOLE_BREAKER_THRESHOLD,
    DEFAULT_CONSOLE_BREAKER_TTL_S,
    DEFAULT_CONSOLE_JEV_FALLBACK_MODEL,
    DEFAULT_CONSOLE_JEV_MODEL,
    DEFAULT_CONSOLE_KEY_ENV,
    EFFORT_LEVELS,
    CommandCodeFallback,
    EffectiveSettings,
    Settings,
    _as_float,
    _as_optional_float,
    api_key,
    redact,
    route_allowlist,
)
from .effort import offered_levels_for_grid, resolve_effort
from .grid import Entry, criteria, resolve
from .state import (
    EFFORT_QUESTION_ID,
    MODEL_QUESTION_ID,
    SINGLE_MODEL_CHOICE,
    build_payload,
    rubric_criteria,
    rubric_instructions,
    last_user_message,
    normalise_effort_choice,
    previous_turn_signal,
    recent_context,
)

logger = logging.getLogger(__name__)

#: Reasons a turn was left unrouted. Stable strings — they are what the audit file records.
REASON_DISABLED = "disabled"
REASON_NO_API_KEY = "no_api_key"
REASON_SKIPPED_PROVIDER = "provider_not_routed"
REASON_SKIPPED_API_MODE = "api_mode_not_routed"
REASON_SKIPPED_MODEL = "model_not_in_grid"
REASON_SKIPPED_UNKNOWN_MODEL = "model_not_in_provider_catalog"
REASON_SKIPPED_NO_MESSAGE = "no_user_message"
REASON_SKIPPED_REPLAYED = "turn_already_routed"
REASON_UPSTREAM_ERROR = "upstream_error"
#: The console quota breaker is open: the turn was not routed and **not one HTTP request** was
#: sent to SystemOne. Distinct from ``upstream_error`` on purpose — a 429 there is a deterministic
#: quota answer, and an audit reader has to be able to tell "we stopped asking" from "we asked and
#: the host refused" (the latter is still what the call that opened the window records).
REASON_DECIDER_UNAVAILABLE = "decider_unavailable_quota"
REASON_TIMEOUT = "timeout"
REASON_MALFORMED = "malformed_answer"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_UNKNOWN_CHOICE = "unknown_choice"
REASON_USER_DATA = "user_data"
REASON_EXCEPTION = "exception"

#: Exception class names that mean "the host did not answer in time" rather than "the host
#: answered with a failure". Shared by every decider so the timeout branch cannot drift apart.
TIMEOUT_EXCEPTION_TOKENS = (
    "Timeout",
    "ReadTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "TimeoutException",
)

#: Header names that must never reach the opt-in SystemOne fallback, matched case-insensitively.
#: The generation provider's ``extra_headers`` are never inherited by the decision transport, so
#: this is the backstop that keeps a chat-only marker (``x-cmd-zdr``) off a decision request even
#: if a future caller starts assembling headers by merging.
PROHIBITED_DECIDER_HEADERS = frozenset({"x-cmd-zdr"})

#: Wire failures that justify asking a second host the same question. A *semantic* failure — a
#: valid answer whose choice is not routable, or one below the confidence threshold — says the
#: console is healthy and the router simply did not like the answer, so re-asking elsewhere would
#: only add latency and cost.
TECHNICAL_FAILURE_REASONS = frozenset({REASON_MALFORMED, REASON_TIMEOUT, REASON_UPSTREAM_ERROR, REASON_NO_API_KEY})


def _is_timeout(exc: BaseException) -> bool:
    name = type(exc).__name__
    return any(token in name for token in TIMEOUT_EXCEPTION_TOKENS)


def _secondary_headers(key: str) -> Dict[str, str]:
    """Headers for the opt-in SystemOne fallback, built from nothing else.

    Never merged with the generation provider's header set, so nothing the operator configured for
    chats can travel with a decision request. ``User-Agent`` is deliberately absent: the HTTP
    client's own effective value is preserved rather than overridden, exactly as on the legacy
    console path.
    """
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "hermes-jev-effort-router",
    }
    return {
        name: value
        for name, value in headers.items()
        if name.strip().lower() not in PROHIBITED_DECIDER_HEADERS
    }


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
    decider: str = "jev"
    #: The decider model that actually produced this decision: the configured primary, or the
    #: backend's fallback model when the primary failed over. Empty means the backend reported no
    #: provenance, and a caller must resolve it from the active decider's settings rather than
    #: assume a model id that was never consulted.
    decider_model: str = ""
    #: The endpoint that actually answered the call, when the backend has more than one (the
    #: ``jev_console`` chain, or its opt-in SystemOne fallback). Empty means the backend did not
    #: report one, and a caller resolves it from the active decider's settings. Never carries a
    #: credential: the secondary URL is validated without userinfo before it is stored.
    decider_endpoint: str = ""
    #: Why the decider's *fallback model* answered, when it did (``None`` when the primary
    #: answered, or the backend has no fallback). Kept apart from ``fallback_reasons``, which
    #: explains a degraded *answer*, not a degraded *backend call*.
    decider_fallback_reason: Optional[str] = None
    has_effort_confidence: bool = False

    @property
    def degraded(self) -> bool:
        return bool(self.fallback_reasons)


def _offered_levels(
    effective: EffectiveSettings, provider: str, settings: Settings
) -> Tuple[str, ...]:
    """The levels the effort question may offer this route, and the answer may name.

    Derived from the route's effort map (``effort.offered_levels_for_grid``), so the question,
    the accepted answer keys and the applied value all come from one source: ``max`` is only
    offered where the map declares it, and never offered and clamped away afterwards.
    """
    return offered_levels_for_grid(effective.grid, provider, settings.effort_maps)


def _effort_question_kwargs(levels: Sequence[str]) -> Dict[str, Any]:
    """``state.build_questions``'s offered levels, bound once per request."""
    return {"levels": tuple(levels)}


def _offered_wire_effort(applied: Optional[str], levels: Sequence[str]) -> Optional[str]:
    """The level to resolve onto the wire, or ``None`` when the question never offered it.

    ``max`` joined ``EFFORT_LEVELS``, but a route only *offers* the levels its ``effort_maps``
    entry declares — while ``resolve_effort`` still consults the *built-in* policy of an unmapped
    route, and ``opencode-go``, ``commandcode`` and ``codex`` declare ``max`` there. Validating the
    answer against the vocabulary alone is therefore not enough: a well-formed but unoffered level
    would reach the wire, and the turn would run the priciest rung on a question that never named
    it. ``effort_requested`` keeps what Jev answered; only the wire drops it.
    """
    if applied is None or applied in levels:
        return applied
    return None


def _fallback_model(effective: EffectiveSettings) -> str:
    """Resolve the fallback model when the decider's answer was absent, off-grid or low-confidence."""
    chosen = effective.entry_for(effective.default_model)
    if chosen is not None:
        return chosen.model_id
    return effective.default_model


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
    if answer_type(answer) != "choice":
        return ""
    return str(answer.get("choice") or "")


def answer_type(answer: Dict[str, Any]) -> str:
    return str(answer.get("type") or "choice").strip().lower() or "choice"


def _criteria_map(model_answer: Dict[str, Any]) -> Dict[str, str]:
    echoed = model_answer.get("criteria")
    if isinstance(echoed, dict):
        return {str(key): str(value) for key, value in echoed.items()}
    return {}


class _SimpleResponse:
    """Minimal response wrapper for stdlib urllib calls."""

    def __init__(self, status_code: int, text: str, data: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._data = data

    def json(self) -> Any:
        if self._data is not None:
            return self._data
        return json.loads(self.text)


@dataclass
class QuotaBreaker:
    """Consecutive-429 circuit breaker for the ``jev_console`` decider.

    SystemOne answers a spent free quota with HTTP 429 and the body ``FreeUsageLimitError`` —
    deterministic, not transient. Asking it again on the next turn buys the same refusal and
    another log line, which is how four profiles ended up with 671 of them in a single day.

    After ``threshold`` consecutive 429 answers the breaker **opens** and every call inside
    ``ttl_s`` returns without touching the network. Any non-429 outcome — a valid answer, another
    status, a timeout — clears the counter, so a lone 429 followed by a success never opens it.
    When the TTL expires the breaker closes and the next call tries the endpoint again; if that
    call is a 429 again the window starts over from one.

    State is per process and per decider instance: it is a rate-of-retries guard, not a shared
    distributed lock, and a restart simply starts from closed.
    """

    threshold: int = DEFAULT_CONSOLE_BREAKER_THRESHOLD
    ttl_s: float = DEFAULT_CONSOLE_BREAKER_TTL_S
    consecutive_429: int = 0
    #: ``time.monotonic()`` when the window opened; ``None`` while closed.
    opened_at: Optional[float] = None
    #: Wall-clock ISO timestamp of the opening, for the audit and the status view.
    opened_at_wall: Optional[str] = None
    #: Monotonic serial of the windows opened so far. The identity the audit dedupes on: two
    #: windows can open inside the same wall-clock second, so a timestamp is not an identity.
    window_id: int = 0
    #: Clock seam: tests advance it instead of sleeping.
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def _now_wall(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def is_open(self) -> bool:
        """Whether the breaker currently blocks calls. An expired window closes itself."""
        if self.opened_at is None:
            return False
        if self.clock() - self.opened_at < max(0.0, float(self.ttl_s)):
            return True
        self.reset()
        logger.info(
            "jev-effort-router: Jev Console quota breaker closed after its %.0fs window; the next call asks again",
            float(self.ttl_s),
        )
        return False

    def record_429(self) -> bool:
        """Count one 429. Returns True when *this* answer opened the window."""
        self.consecutive_429 += 1
        if self.opened_at is None and self.consecutive_429 >= max(1, int(self.threshold)):
            self.opened_at = self.clock()
            self.opened_at_wall = self._now_wall()
            self.window_id += 1
            return True
        return False

    def record_other(self) -> None:
        """Any non-429 outcome: the consecutive streak is over, so the counter restarts."""
        self.reset()

    def reset(self) -> None:
        self.consecutive_429 = 0
        self.opened_at = None
        self.opened_at_wall = None

    def snapshot(self) -> Dict[str, Any]:
        """The state exposed to the status tool, the audit record and the tests."""
        is_open = self.is_open()
        return {
            "state": "open" if is_open else "closed",
            "reason": "quota" if is_open else None,
            "consecutive_429": self.consecutive_429,
            "threshold": int(self.threshold),
            "ttl_s": float(self.ttl_s),
            "opened_at": self.opened_at_wall if is_open else None,
            "window_id": self.window_id if is_open else None,
        }


class BaseDecider(ABC):
    """Abstract base decider."""

    def __init__(self, settings: Settings, transport: Any = None) -> None:
        self._settings = settings
        self._transport = transport

    def breaker_state(self) -> Optional[Dict[str, Any]]:
        """The quota breaker's state, or ``None`` for deciders that have none."""
        return None

    @abstractmethod
    def decide(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        platform: str = "",
        provider: str = "",
    ) -> Tuple[Optional[Decision], Optional[str]]:
        pass


class JevDecider(BaseDecider):
    """Decider backend for TypeSafe Jev via OpenRouter Decisions API."""

    def _post(self, payload: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
        key_env = self._settings.api_key_env_for("jev")
        headers = {
            "Authorization": f"Bearer {api_key(key_env)}",
            "Content-Type": "application/json",
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

        import httpx

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

    def decide(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        platform: str = "",
        provider: str = "",
    ) -> Tuple[Optional[Decision], Optional[str]]:
        key_env = self._settings.api_key_env_for("jev")
        if not api_key(key_env):
            return None, REASON_NO_API_KEY

        levels = _offered_levels(effective, provider, self._settings)
        payload = build_payload(
            messages,
            effective.grid,
            jev_model=self._settings.jev_model,
            context_turns=self._settings.context_turns,
            platform=platform,
            provider=provider,
            # The legacy Decisions API contract always carries both questions; keeping it
            # explicit is what leaves that payload byte-identical to a one-model grid or not.
            ask_model=True,
            **_effort_question_kwargs(levels),
        )

        started = time.monotonic()
        try:
            response, _ = self._post(payload)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            name = type(exc).__name__
            if _is_timeout(exc):
                logger.warning("jev-effort-router: Jev timed out after %d ms (%s)", latency_ms, name)
                return None, REASON_TIMEOUT
            logger.warning("jev-effort-router: Jev call failed: %s: %s", name, redact(exc, 300, env_name=key_env))
            return None, REASON_UPSTREAM_ERROR
        latency_ms = int((time.monotonic() - started) * 1000)

        status = getattr(response, "status_code", None)
        if status is None or not (200 <= int(status) < 300):
            body = redact(getattr(response, "text", ""), 300, env_name=key_env)
            logger.warning("jev-effort-router: Jev returned HTTP %s: %s", status, body)
            return None, REASON_UPSTREAM_ERROR

        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            logger.warning(
                "jev-effort-router: Jev returned a non-JSON body: %s",
                redact(getattr(response, "text", ""), 300, env_name=key_env),
            )
            return None, REASON_MALFORMED

        return self._interpret(
            data, effective, latency_ms=latency_ms, provider=provider, levels=levels
        )

    def _interpret(
        self,
        data: Any,
        effective: EffectiveSettings,
        *,
        latency_ms: int,
        provider: str = "",
        levels: Sequence[str],
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

        model = chosen.model_id if chosen is not None else _fallback_model(effective)

        # -- effort ----------------------------------------------------------------
        # The answer is validated against the vocabulary and then dropped from the wire if this
        # route never offered it (``_offered_wire_effort``): ``max`` is well-formed everywhere but
        # an *option* only where the map declares it, while the built-in policy of an unmapped
        # route still accepts it.
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
            # A weak or unknown answer still routes: fall back to the configured default.
            requested = effective.default_effort

        # ``requested`` keeps what was asked for (Jev's answer, or the configured default) and
        # only a level the question offered goes further to the wire.
        applied = _offered_wire_effort(requested, levels)

        effort = resolve_effort(
            model,
            applied,
            provider=provider,
            effort_maps=self._settings.effort_maps,
        )

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
                fallback_reasons=tuple(reasons),
                decider="jev",
                decider_model=self._settings.jev_model,
                decider_endpoint=self._settings.endpoint,
                has_effort_confidence=answered,
            ),
            None,
        )


class OpenAICompatibleDecider(BaseDecider):
    """Decider backend for OpenAI-compatible chat completions (e.g. OpenCode)."""

    def _post(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Tuple[Optional[Any], Optional[str]]:
        if self._transport is not None:
            response = self._transport.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._settings.timeout_s,
            )
            return response, None

        import socket
        import urllib.error
        import urllib.request

        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._settings.timeout_s) as resp:
                status_code = resp.status
                body_bytes = resp.read()
                text = body_bytes.decode("utf-8", errors="replace")
                try:
                    json_data = json.loads(text)
                except Exception:
                    json_data = None
                return _SimpleResponse(status_code, text, json_data), None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            return _SimpleResponse(exc.code, body, None), None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
                raise TimeoutError(str(exc)) from exc
            raise exc

    def decide(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        platform: str = "",
        provider: str = "",
    ) -> Tuple[Optional[Decision], Optional[str]]:
        key_env = self._settings.api_key_env_for("openai_compatible")
        key = api_key(key_env)
        if not key:
            return None, REASON_NO_API_KEY

        base_url = self._settings.base_url_for()
        if not base_url or not isinstance(base_url, str) or not base_url.strip():
            return None, REASON_UPSTREAM_ERROR

        url = f"{base_url.rstrip('/')}/chat/completions"
        decider_model = self._settings.decider_model_for()

        user_msg = last_user_message(messages)
        ctx = recent_context(messages, self._settings.context_turns)

        single_model = len(effective.grid) == 1
        levels = _offered_levels(effective, provider, self._settings)
        effort_lines = [
            f"- {lvl}: {desc}"
            for lvl, desc in rubric_criteria(levels).items()
        ]
        # The schema and the rule name exactly the levels this route was offered: a level the
        # wire would clamp away must never appear in the prompt, or the model spends the answer
        # on a value the router cannot apply.
        effort_enum = "|".join(levels)
        if len(levels) == 1:
            effort_rule = f"- The effort MUST be {levels[0]}."
        else:
            effort_rule = f"- The effort MUST be {', '.join(levels[:-1])}, or {levels[-1]}."
        effort_scope = rubric_instructions()

        if single_model:
            grid_lines: List[str] = []
            # One model is not a choice: the model block, its rule and its JSON field are all
            # withheld, so the answer is only the effort. Asking "pick the model" over a single
            # option buys a certainty at the cost of tokens and latency.
            system_prompt = (
                "You are an expert reasoning-effort router for an AI coding assistant.\n"
                "Given the user request and recent context, select the appropriate reasoning effort level.\n"
                "Return valid JSON ONLY matching exactly this schema:\n"
                "{\n"
                f'  "effort": "<{effort_enum}>",\n'
                '  "confidence": <float between 0.0 and 1.0>\n'
                "}\n"
                "Rules:\n"
                f"{effort_rule}\n"
                f"{effort_scope}\n"
                "- Return JSON only, with no commentary, markdown or extra text."
            )
        else:
            grid_lines = [f"- {entry.model_id}: {entry.description}" for entry in effective.grid]
            system_prompt = (
                "You are an expert model and effort router for an AI coding assistant.\n"
                "Given the user request, recent context, available models and reasoning effort levels, "
                "select the single best model from the available grid and the appropriate reasoning effort level.\n"
                "Return valid JSON ONLY matching exactly this schema:\n"
                "{\n"
                '  "model": "<model_id>",\n'
                f'  "effort": "<{effort_enum}>",\n'
                '  "confidence": <float between 0.0 and 1.0>\n'
                "}\n"
                "Rules:\n"
                "- The model MUST be one of the IDs from the Available Models list.\n"
                f"{effort_rule}\n"
                f"{effort_scope}\n"
                "- Return JSON only, with no commentary, markdown or extra text."
            )

        user_prompt_parts = []
        if user_msg:
            user_prompt_parts.append(f"User Request:\n{user_msg}")
        if ctx:
            user_prompt_parts.append(f"Recent Context:\n{ctx}")
        signal = previous_turn_signal(messages)
        if signal:
            user_prompt_parts.append("Previous Turn:\n" + json.dumps(signal, ensure_ascii=False))
        if platform:
            user_prompt_parts.append(f"Platform: {platform}")
        if provider:
            user_prompt_parts.append(f"Provider: {provider}")
        if not single_model:
            user_prompt_parts.append("Available Models:\n" + "\n".join(grid_lines))
        user_prompt_parts.append("Reasoning Effort Levels:\n" + "\n".join(effort_lines))
        user_prompt_parts.append(
            "Pick the effort. Output JSON only." if single_model else "Pick the model and effort. Output JSON only."
        )
        user_prompt = "\n\n".join(user_prompt_parts)

        payload = {
            "model": decider_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-Agent/0.21.4 (jev-effort-router)",
        }
        if "opencode" in url.lower():
            headers["x-opencode-session"] = f"jev-{int(time.time())}"

        started = time.monotonic()
        try:
            response, _ = self._post(url, payload, headers)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            name = type(exc).__name__
            if _is_timeout(exc):
                logger.warning(
                    "jev-effort-router: openai_compatible decider timed out after %d ms (%s)",
                    latency_ms,
                    name,
                )
                return None, REASON_TIMEOUT
            logger.warning(
                "jev-effort-router: openai_compatible decider call failed: %s: %s",
                name,
                redact(exc, 300, env_name=key_env),
            )
            return None, REASON_UPSTREAM_ERROR
        latency_ms = int((time.monotonic() - started) * 1000)

        status = getattr(response, "status_code", None)
        if status is None or not (200 <= int(status) < 300):
            body = redact(getattr(response, "text", ""), 300, env_name=key_env)
            logger.warning("jev-effort-router: openai_compatible decider returned HTTP %s: %s", status, body)
            return None, REASON_UPSTREAM_ERROR

        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            logger.warning(
                "jev-effort-router: openai_compatible decider returned non-JSON body: %s",
                redact(getattr(response, "text", ""), 300, env_name=key_env),
            )
            return None, REASON_MALFORMED

        if not isinstance(data, dict):
            return None, REASON_MALFORMED

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None, REASON_MALFORMED
        msg_obj = choices[0].get("message")
        if not isinstance(msg_obj, dict):
            return None, REASON_MALFORMED
        raw_content = msg_obj.get("content")
        if isinstance(raw_content, dict):
            parsed = raw_content
        elif isinstance(raw_content, str):
            content_text = raw_content.strip()
            if content_text.startswith("```"):
                lines = content_text.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                content_text = "\n".join(lines).strip()
            try:
                parsed = json.loads(content_text)
            except Exception:
                logger.warning(
                    "jev-effort-router: openai_compatible decider content not valid JSON: %s",
                    redact(content_text, 300, env_name=key_env),
                )
                return None, REASON_MALFORMED
        else:
            return None, REASON_MALFORMED

        if not isinstance(parsed, dict):
            return None, REASON_MALFORMED

        return self._interpret(
            parsed,
            effective,
            latency_ms=latency_ms,
            provider=provider,
            endpoint=url,
            levels=levels,
        )

    def _interpret(
        self,
        parsed: Dict[str, Any],
        effective: EffectiveSettings,
        *,
        latency_ms: int,
        provider: str = "",
        endpoint: str = "",
        levels: Sequence[str],
    ) -> Tuple[Optional[Decision], Optional[str]]:
        if not isinstance(parsed, dict):
            return None, REASON_MALFORMED

        # Model: with one option the question is not part of the prompt, so the only entry is
        # the answer by construction (confidence 1.0). A model field the model volunteers
        # anyway is ignored — it was not asked for.
        single_model = len(effective.grid) == 1
        if single_model:
            chosen = effective.grid[0]
            model_choice_text = SINGLE_MODEL_CHOICE
        else:
            model_raw = parsed.get("model") or parsed.get("model_id") or parsed.get("choice")
            if model_raw is None or not isinstance(model_raw, str):
                return None, REASON_MALFORMED
            model_choice_text = model_raw.strip()
            if not model_choice_text:
                return None, REASON_MALFORMED

            grid_models = {entry.model_id: entry for entry in effective.grid}
            chosen = grid_models.get(model_choice_text)
            if chosen is None:
                return None, REASON_UNKNOWN_CHOICE

        # Confidence validation: must be finite float in [0.0, 1.0]
        conf_raw = parsed.get("confidence")
        if conf_raw is None or isinstance(conf_raw, bool) or not isinstance(conf_raw, (int, float)):
            return None, REASON_MALFORMED
        try:
            conf_float = float(conf_raw)
        except (TypeError, ValueError):
            return None, REASON_MALFORMED
        if not math.isfinite(conf_float) or conf_float < 0.0 or conf_float > 1.0:
            return None, REASON_MALFORMED

        if conf_float < effective.confidence_threshold:
            return None, REASON_LOW_CONFIDENCE

        # Effort validation: the answer must be a vocabulary level, and it only reaches the wire
        # if the prompt offered it. ``max`` is well-formed everywhere but an *option* only on a
        # route whose map declares it, and an unmapped route's built-in policy accepts it.
        effort_raw = parsed.get("effort") or parsed.get("reasoning_effort")
        if effort_raw is None or not isinstance(effort_raw, str):
            return None, REASON_MALFORMED
        effort_choice_text = effort_raw.strip()
        candidate = normalise_effort_choice(effort_choice_text)
        if candidate is None:
            return None, REASON_UNKNOWN_CHOICE

        # What was asked for stays visible; only a level the prompt actually offered survives to
        # the wire.
        requested: Optional[str] = candidate
        applied = _offered_wire_effort(requested, levels)

        effort = resolve_effort(
            chosen.model_id,
            applied,
            provider=provider,
            effort_maps=self._settings.effort_maps,
        )

        alternatives = tuple(
            entry.model_id for entry in effective.grid if entry.model_id != chosen.model_id
        )

        return (
            Decision(
                model=chosen.model_id,
                effort=effort,
                effort_requested=requested,
                model_confidence=1.0 if single_model else conf_float,
                effort_confidence=conf_float,
                model_choice=model_choice_text,
                effort_choice=effort_choice_text,
                model_probabilities={} if single_model else {chosen.model_id: conf_float},
                effort_probabilities={effort_choice_text: conf_float},
                alternatives=alternatives,
                latency_ms=latency_ms,
                fallback_reasons=(),
                decider="openai_compatible",
                decider_model=self._settings.decider_model_for(),
                decider_endpoint=endpoint,
                has_effort_confidence=True,
            ),
            None,
        )


class JevConsoleDecider(BaseDecider):
    """Decider backend for Jev on OpenCode Console SystemOne."""

    def __init__(self, settings: Settings, transport: Any = None) -> None:
        super().__init__(settings, transport)
        #: Quota breaker: N consecutive 429s from SystemOne stop the HTTP calls for the TTL.
        self._breaker = QuotaBreaker(
            threshold=settings.console_jev_breaker_threshold,
            ttl_s=settings.console_jev_breaker_ttl_s,
        )

    def breaker_state(self) -> Optional[Dict[str, Any]]:
        return self._breaker.snapshot()

    def _post_one(
        self, url: str, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[Optional[Any], Optional[str]]:
        if self._transport is not None:
            response = self._transport.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._settings.timeout_s,
            )
            return response, None

        import httpx

        with httpx.Client(timeout=self._settings.timeout_s) as client:
            return (
                client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self._settings.timeout_s,
                ),
                None,
            )

    def decide(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        platform: str = "",
        provider: str = "",
    ) -> Tuple[Optional[Decision], Optional[str]]:
        """Ask the console chain, then — opt-in only — the SystemOne fallback.

        The order is fixed: the primary (free) console model, its paid counterpart when configured,
        then the opt-in secondary transport at most once. The console chain runs exactly as it did
        before the secondary transport existed; the fallback only inherits its *last* reason, and
        only a technical failure there justifies asking a second host the same question.
        """
        fallback = self._settings.active_commandcode_fallback()
        key_env = self._settings.api_key_env_for("jev_console")
        started = time.monotonic()

        if not api_key(key_env):
            # The legacy early return, kept verbatim: without a console key not one console request
            # is sent. A fully configured secondary transport is the only thing left that can answer.
            decision, reason, technical = None, REASON_NO_API_KEY, True
        else:
            decision, reason, technical = self._decide_on_console(
                messages,
                effective,
                platform=platform,
                provider=provider,
                started=started,
            )
        if decision is not None:
            return decision, None

        if not technical or not fallback.enabled:
            # Either the console answered validly and its answer was not routable (low_confidence,
            # unknown_choice), or no secondary transport is configured. The console's own reason is
            # the final one in both cases — asking another host is only justified by a host failure.
            return None, reason

        return self._decide_on_commandcode(
            messages,
            effective,
            fallback=fallback,
            reason_before=reason,
            platform=platform,
            provider=provider,
            started=started,
        )

    def _decide_on_console(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        platform: str,
        provider: str,
        started: float,
    ) -> Tuple[Optional[Decision], str, bool]:
        """The free-then-paid console chain, plus whether it failed technically.

        Returns ``(decision, reason, technical)``. ``technical`` is what the caller needs to decide
        whether a second host may be asked: ``False`` means the console produced a valid answer the
        router simply could not apply, which is not a host failure.
        """
        if self._breaker.is_open():
            # No HTTP while the window is open: a 429 here is a spent quota, so a call would only
            # reproduce the same answer (and another warning line) until the TTL expires.
            logger.debug(
                "jev-effort-router: Jev Console quota breaker is open (%d consecutive HTTP 429); "
                "not calling the endpoint",
                self._breaker.consecutive_429,
            )
            return None, REASON_DECIDER_UNAVAILABLE, True

        key_env = self._settings.api_key_env_for("jev_console")
        key = api_key(key_env)
        url = self._settings.base_url_for("jev_console")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "hermes-jev-effort-router",
        }

        # Models to try: primary (free) first, fallback (paid) second
        primary_model = self._settings.decider_model_for("jev_console")
        fallback_model = (self._settings.console_jev_fallback_model or "").strip()
        models_to_try = [primary_model]
        if fallback_model and fallback_model != primary_model:
            models_to_try.append(fallback_model)

        last_reason = REASON_UPSTREAM_ERROR
        # Whether the *last* console attempt failed technically — the only thing that justifies
        # asking a second host. This is tracked apart from ``last_reason`` on purpose: a missing
        # response carries no reason of its own, and the legacy contract keeps the previous reason
        # in the returned value (which is what a failed primary alone has always reported). Only
        # the second-host decision needs the distinction.
        last_technical = True
        # ``started`` is the chain's clock, handed in by ``decide``: the recorded latency covers
        # every attempt, so a new clock here would restart it mid-chain.
        levels = _offered_levels(effective, provider, self._settings)

        for model_name in models_to_try:
            payload = build_payload(
                messages,
                effective.grid,
                jev_model=model_name,
                context_turns=self._settings.context_turns,
                platform=platform,
                provider=provider,
                # One model is not a choice: asking the model question over a single option
                # spends the answer on a certainty, and Jev cannot express anything else.
                ask_model=len(effective.grid) > 1,
                **_effort_question_kwargs(levels),
            )
            try:
                response, _ = self._post_one(url, payload, headers)
            except Exception as exc:  # noqa: BLE001
                latency_ms = int((time.monotonic() - started) * 1000)
                name = type(exc).__name__
                if _is_timeout(exc):
                    logger.warning("jev-effort-router: Jev Console timed out on %s after %d ms (%s)", model_name, latency_ms, name)
                    last_reason = REASON_TIMEOUT
                else:
                    logger.warning(
                        "jev-effort-router: Jev Console call on %s failed: %s: %s",
                        model_name,
                        name,
                        redact(exc, 300, env_name=key_env),
                    )
                    last_reason = REASON_UPSTREAM_ERROR
                last_technical = True
                self._breaker.record_other()
                continue

            latency_ms = int((time.monotonic() - started) * 1000)
            if response is None:
                # No response object at all is a technical failure of this host, even though the
                # returned reason keeps the previous one for legacy compatibility.
                last_technical = True
                self._breaker.record_other()
                continue
            status = getattr(response, "status_code", None)
            try:
                status_code = int(status) if status is not None else None
            except (TypeError, ValueError):
                status_code = None
            if status_code == 429:
                # A 429 from SystemOne is a quota answer, not a transient hiccup: count it and, on
                # the Nth consecutive one, open the breaker for the TTL.
                if self._breaker.record_429():
                    logger.warning(
                        "jev-effort-router: Jev Console quota exhausted (%d consecutive HTTP 429); "
                        "quota breaker open for %.0f s",
                        self._breaker.consecutive_429,
                        float(self._breaker.ttl_s),
                    )
            else:
                self._breaker.record_other()
            if status_code is None or not (200 <= status_code < 300):
                body = redact(getattr(response, "text", ""), 300, env_name=key_env)
                logger.warning("jev-effort-router: Jev Console returned HTTP %s on %s: %s", status, model_name, body)
                last_reason = REASON_UPSTREAM_ERROR
                last_technical = True
                continue

            try:
                data = response.json()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "jev-effort-router: Jev Console returned a non-JSON body on %s: %s",
                    model_name,
                    redact(getattr(response, "text", ""), 300, env_name=key_env),
                )
                last_reason = REASON_MALFORMED
                last_technical = True
                continue

            fallback_answered = bool(fallback_model) and model_name == fallback_model and model_name != primary_model
            dec, reason = self._interpret(
                data,
                effective,
                latency_ms=latency_ms,
                provider=provider,
                decider_model=model_name,
                fallback_reason=last_reason if fallback_answered else None,
                decider_endpoint=url,
            )
            if dec is not None:
                return dec, "", False
            if reason:
                last_reason = reason
                # A semantic answer (below threshold, option outside the grid) is the console
                # host doing its job; only a technical reason may hand the turn to another host.
                last_technical = reason in TECHNICAL_FAILURE_REASONS
                continue

        return None, last_reason, last_technical

    def _decide_on_commandcode(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        fallback: CommandCodeFallback,
        reason_before: str,
        platform: str,
        provider: str,
        started: float,
    ) -> Tuple[Optional[Decision], Optional[str]]:
        """One attempt against the opt-in SystemOne fallback, with its own reason classification.

        No retries and no recursion: one request, the same per-call ``timeout_s`` as every other
        decider call. ``latency_ms`` is measured from the start of the chain, because the cumulative
        delay is what the turn actually pays.
        """
        key_env = fallback.key_env
        key = api_key(key_env)
        if not key:
            # No credential for the secondary host, so not one byte goes there. The console chain's
            # reason stays final: replacing a 429 or a timeout with "no key" would blame a fallback
            # configuration problem for a host failure the operator has to act on.
            logger.info(
                "jev-effort-router: the opt-in CommandCode fallback needs %s, which is unset; "
                "falling open with the console reason %r",
                key_env,
                reason_before,
            )
            return None, reason_before

        payload = build_payload(
            messages,
            effective.grid,
            jev_model=fallback.model,
            context_turns=self._settings.context_turns,
            platform=platform,
            provider=provider,
        )
        headers = _secondary_headers(key)

        try:
            response, _ = self._post_one(fallback.url, payload, headers)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            name = type(exc).__name__
            if _is_timeout(exc):
                logger.warning(
                    "jev-effort-router: CommandCode fallback timed out after %d ms (%s)",
                    latency_ms,
                    name,
                )
                return None, REASON_TIMEOUT
            logger.warning(
                "jev-effort-router: CommandCode fallback call failed: %s: %s",
                name,
                redact(exc, 300, env_name=key_env),
            )
            return None, REASON_UPSTREAM_ERROR

        latency_ms = int((time.monotonic() - started) * 1000)
        if response is None:
            logger.warning("jev-effort-router: CommandCode fallback returned no response")
            return None, REASON_UPSTREAM_ERROR
        status = getattr(response, "status_code", None)
        if status is None or not (200 <= int(status) < 300):
            # 400 (credit), 401/403 (credential), 404/422 and 429/5xx are the same class here: the
            # host refused the call, so the turn falls open with ``upstream_error``.
            body = redact(getattr(response, "text", ""), 300, env_name=key_env)
            logger.warning("jev-effort-router: CommandCode fallback returned HTTP %s: %s", status, body)
            return None, REASON_UPSTREAM_ERROR

        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            logger.warning(
                "jev-effort-router: CommandCode fallback returned a non-JSON body: %s",
                redact(getattr(response, "text", ""), 300, env_name=key_env),
            )
            return None, REASON_MALFORMED

        decision, reason = self._interpret(
            data,
            effective,
            latency_ms=latency_ms,
            provider=provider,
            decider_model=fallback.model,
            fallback_reason=reason_before,
            decider_endpoint=fallback.url,
        )
        if decision is not None:
            return decision, None
        # A valid answer that is not routable keeps its semantic reason (low_confidence,
        # unknown_choice); an invalid wire keeps ``malformed_answer``.
        return None, reason

    def _interpret(
        self,
        data: Any,
        effective: EffectiveSettings,
        *,
        latency_ms: int,
        provider: str = "",
        decider_model: str = "",
        fallback_reason: Optional[str] = None,
        decider_endpoint: str = "",
    ) -> Tuple[Optional[Decision], Optional[str]]:
        if not isinstance(data, dict):
            return None, REASON_MALFORMED
        answers = data.get("answers")
        if not isinstance(answers, dict):
            return None, REASON_MALFORMED

        # -- model -----------------------------------------------------------------
        # A one-model grid is not a choice: ``state.build_questions`` withholds the model
        # question, the model is fixed to the only option, and its confidence is 1.0 by
        # construction. A model answer the endpoint volunteers anyway is ignored — it was not
        # asked, and honouring it would reintroduce the question the payload just dropped.
        single_model = len(effective.grid) == 1
        if single_model:
            model = effective.grid[0].model_id
            model_confidence = 1.0
            model_choice_text = SINGLE_MODEL_CHOICE
            model_probabilities: Dict[str, float] = {}
        else:
            if MODEL_QUESTION_ID not in answers:
                return None, REASON_MALFORMED
            model_answer = answers[MODEL_QUESTION_ID]
            if not isinstance(model_answer, dict):
                return None, REASON_MALFORMED

            # Specific wire validation: type must be explicitly present, string, and 'choice'
            if "type" not in model_answer or not isinstance(model_answer["type"], str):
                return None, REASON_MALFORMED
            if model_answer["type"].strip().lower() != "choice":
                return None, REASON_MALFORMED

            conf_raw = model_answer.get("confidence")
            if conf_raw is None or isinstance(conf_raw, bool) or not isinstance(conf_raw, (int, float)):
                return None, REASON_MALFORMED
            try:
                model_confidence = float(conf_raw)
            except (TypeError, ValueError):
                return None, REASON_MALFORMED
            if not math.isfinite(model_confidence) or model_confidence < 0.0 or model_confidence > 1.0:
                return None, REASON_MALFORMED

            model_probabilities = {}
            if "probabilities" not in model_answer or model_answer["probabilities"] is None:
                return None, REASON_MALFORMED
            probs_raw = model_answer["probabilities"]
            if not isinstance(probs_raw, dict) or not probs_raw:
                return None, REASON_MALFORMED
            grid_positions = {str(i) for i in range(1, len(effective.grid) + 1)}
            grid_model_ids = {entry.model_id for entry in effective.grid}
            allowed_prob_keys = grid_positions | grid_model_ids
            prob_sum = 0.0
            for k, v in probs_raw.items():
                k_str = str(k).strip()
                if k_str not in allowed_prob_keys:
                    return None, REASON_MALFORMED
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return None, REASON_MALFORMED
                vf = float(v)
                if not math.isfinite(vf) or vf < 0.0 or vf > 1.0:
                    return None, REASON_MALFORMED
                model_probabilities[k_str] = vf
                prob_sum += vf
            if abs(prob_sum - 1.0) > 0.02:
                return None, REASON_MALFORMED

            if "choice" not in model_answer or model_answer["choice"] is None:
                return None, REASON_MALFORMED
            model_choice_text = str(model_answer["choice"]).strip()
            if not model_choice_text:
                return None, REASON_MALFORMED

            # Resolve choice strictly against sent criteria and options (rejecting invented/remapped criteria)
            sent_criteria = criteria(effective.grid)
            echoed_criteria = model_answer.get("criteria")
            if echoed_criteria is not None:
                if not isinstance(echoed_criteria, dict):
                    return None, REASON_MALFORMED
                for k, v in echoed_criteria.items():
                    k_str = str(k).strip()
                    v_str = str(v).strip()
                    if k_str not in sent_criteria or v_str != sent_criteria[k_str]:
                        return None, REASON_UNKNOWN_CHOICE

            chosen = resolve(sent_criteria, model_choice_text, effective.grid)
            if chosen is None:
                return None, REASON_UNKNOWN_CHOICE
            if model_confidence < effective.confidence_threshold:
                return None, REASON_LOW_CONFIDENCE

            model = chosen.model_id

        allowlist = route_allowlist(provider, self._settings.decider)
        if allowlist is not None and model not in allowlist:
            return None, REASON_UNKNOWN_CHOICE

        # -- effort ----------------------------------------------------------------
        # The criteria and the accepted keys are derived from the levels this route was offered,
        # which is the same set the payload put in the question — an echoed criteria or a
        # probability key outside it is a malformed answer, not a new option. The chosen level is
        # recorded as asked and dropped from the wire when the route never offered it.
        levels = _offered_levels(effective, provider, self._settings)
        sent_rubric_criteria = rubric_criteria(levels)
        requested: Optional[str] = None
        effort_confidence = 0.0
        effort_choice_text = ""
        effort_probabilities: Dict[str, float] = {}

        if EFFORT_QUESTION_ID in answers:
            effort_answer = answers[EFFORT_QUESTION_ID]
            if not isinstance(effort_answer, dict):
                return None, REASON_MALFORMED
            if "type" not in effort_answer or not isinstance(effort_answer["type"], str):
                return None, REASON_MALFORMED
            if effort_answer["type"].strip().lower() != "choice":
                return None, REASON_MALFORMED

            e_conf_raw = effort_answer.get("confidence")
            if e_conf_raw is None or isinstance(e_conf_raw, bool) or not isinstance(e_conf_raw, (int, float)):
                return None, REASON_MALFORMED
            try:
                effort_confidence = float(e_conf_raw)
            except (TypeError, ValueError):
                return None, REASON_MALFORMED
            if not math.isfinite(effort_confidence) or effort_confidence < 0.0 or effort_confidence > 1.0:
                return None, REASON_MALFORMED

            if "probabilities" not in effort_answer or effort_answer["probabilities"] is None:
                return None, REASON_MALFORMED
            e_probs_raw = effort_answer["probabilities"]
            if not isinstance(e_probs_raw, dict) or not e_probs_raw:
                return None, REASON_MALFORMED
            # Probability keys name the criteria that were sent *and* the vocabulary: the answer
            # is validated as a level and then resolved against the route's declaration, so
            # a level the route would omit is still a well-formed answer here (main's rule).
            # A well-formed level the *question* did not offer is dropped from the wire below.
            allowed_effort_keys = set(EFFORT_LEVELS) | {
                str(position) for position in range(1, len(levels) + 1)
            }
            e_prob_sum = 0.0
            for k, v in e_probs_raw.items():
                k_str = str(k).strip().lower()
                if k_str not in allowed_effort_keys:
                    return None, REASON_MALFORMED
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return None, REASON_MALFORMED
                vf = float(v)
                if not math.isfinite(vf) or vf < 0.0 or vf > 1.0:
                    return None, REASON_MALFORMED
                effort_probabilities[k_str] = vf
                e_prob_sum += vf
            if abs(e_prob_sum - 1.0) > 0.02:
                return None, REASON_MALFORMED

            e_criteria = effort_answer.get("criteria")
            if e_criteria is not None:
                if not isinstance(e_criteria, dict):
                    return None, REASON_MALFORMED
                for k, v in e_criteria.items():
                    k_str = str(k).strip().lower()
                    if k_str not in sent_rubric_criteria or str(v).strip() != sent_rubric_criteria[k_str]:
                        return None, REASON_UNKNOWN_CHOICE

            if "choice" not in effort_answer or effort_answer["choice"] is None:
                return None, REASON_MALFORMED
            effort_choice_text = str(effort_answer["choice"]).strip()
            candidate = normalise_effort_choice(effort_choice_text)
            if candidate is None:
                return None, REASON_UNKNOWN_CHOICE
            if effort_confidence < effective.confidence_threshold:
                return None, REASON_LOW_CONFIDENCE
            requested = candidate

        # ``requested`` is what Jev asked for; the decision carries it beside the applied level so
        # the audit can tell an omitted turn from an applied one. A level this route never offered
        # stops here: it is recorded, never resolved onto the wire.
        applied = _offered_wire_effort(requested, levels)

        effort = resolve_effort(
            model,
            applied,
            provider=provider,
            effort_maps=self._settings.effort_maps,
        )

        alternatives = tuple(
            entry.model_id for entry in effective.grid if entry.model_id != model
        )
        has_effort = EFFORT_QUESTION_ID in answers
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
                fallback_reasons=(),
                decider="jev_console",
                decider_model=decider_model,
                decider_endpoint=decider_endpoint,
                decider_fallback_reason=fallback_reason,
                has_effort_confidence=has_effort,
            ),
            None,
        )


def get_decider(settings: Settings, transport: Any = None) -> BaseDecider:
    """Factory for decider backends."""
    if settings.decider == "openai_compatible":
        return OpenAICompatibleDecider(settings, transport=transport)
    if settings.decider == "jev_console":
        return JevConsoleDecider(settings, transport=transport)
    return JevDecider(settings, transport=transport)
