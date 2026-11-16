"""The Jev client: pluggable decision endpoints with total failure containment.

Every method returns a :class:`Decision` or ``None``. It never raises: a timeout, a 500, a
402, a malformed body, an unknown choice or a low-confidence answer all end as ``None`` plus
a reason string, so the caller's request goes out byte-identical to a plugin-disabled run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence, Tuple

from .config import EffectiveSettings, Settings
from .decider import (
    REASON_DECIDER_UNAVAILABLE,
    REASON_DISABLED,
    REASON_EXCEPTION,
    REASON_LOW_CONFIDENCE,
    REASON_MALFORMED,
    REASON_NO_API_KEY,
    REASON_SKIPPED_API_MODE,
    REASON_SKIPPED_MODEL,
    REASON_SKIPPED_NO_MESSAGE,
    REASON_SKIPPED_PROVIDER,
    REASON_SKIPPED_REPLAYED,
    REASON_SKIPPED_UNKNOWN_MODEL,
    REASON_TIMEOUT,
    REASON_UNKNOWN_CHOICE,
    REASON_UPSTREAM_ERROR,
    REASON_USER_DATA,
    Decision,
    JevConsoleDecider,
    JevDecider,
    OpenAICompatibleDecider,
    _answer,
    _as_probabilities,
    _choice_text,
    _criteria_map,
    _fallback_model,
    answer_type,
    get_decider,
)

logger = logging.getLogger(__name__)


class JevClient:
    """Decision-endpoint client.

    Supports pluggable decision backends ('jev' via OpenRouter Decisions API,
    or 'openai_compatible' via OpenAI chat completions).
    ``transport`` is an injection seam for tests.
    """

    def __init__(self, settings: Settings, transport: Any = None) -> None:
        self._settings = settings
        self._transport = transport
        self._decider = get_decider(settings, transport=transport)

    def decide(
        self,
        messages: Sequence[Dict[str, Any]],
        effective: EffectiveSettings,
        *,
        platform: str = "",
        provider: str = "",
    ) -> Tuple[Optional[Decision], Optional[str]]:
        """Ask the decider for a model and an effort level."""
        return self._decider.decide(
            messages,
            effective,
            platform=platform,
            provider=provider,
        )

    def breaker_state(self) -> Optional[Dict[str, Any]]:
        """The decider's quota breaker (``None`` for backends that have none)."""
        getter = getattr(self._decider, "breaker_state", None)
        if not callable(getter):
            return None
        try:
            state = getter()
        except Exception:  # noqa: BLE001 - a status view never raises
            return None
        return state if isinstance(state, dict) else None
