"""Per-turn and per-session decision memory.

Hermes identifies a user turn by ``turn_id``, which stays constant for every provider request
inside that turn's tool loop. Memoizing on it is what makes "route the user input" mean *one*
decision per message rather than one per API call: the model chosen at the first request keeps
every follow-up request of the same turn on the same model and effort, so nothing about the
system prompt or the prompt cache changes mid-turn.

Entries are bounded and time-limited; a stale id (a resumed session days later) must not
resurrect an old decision.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

#: How long a turn's decision stays replayable. A turn cannot outlive this in practice;
#: the window exists so a long-lived process does not grow without bound.
TURN_TTL_S = 30 * 60

#: Sessions are remembered so `route_per_turn: false` can route once per session.
SESSION_TTL_S = 24 * 60 * 60

MAX_TURN_ENTRIES = 4096
MAX_SESSION_ENTRIES = 512


@dataclass(frozen=True)
class Memo:
    model: str
    effort: Optional[str]
    effort_requested: Optional[str]
    confidence: float
    choice: str
    fallback_reasons: tuple = ()


class TurnMemo:
    """Thread-safe, bounded memo of decisions keyed by turn or session."""

    def __init__(self, max_turns: int = MAX_TURN_ENTRIES, max_sessions: int = MAX_SESSION_ENTRIES) -> None:
        self._turns: "OrderedDict[str, tuple[float, Memo]]" = OrderedDict()
        self._sessions: "OrderedDict[str, tuple[float, Memo]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_turns = max_turns
        self._max_sessions = max_sessions

    def get_turn(self, turn_id: Optional[str]) -> Optional[Memo]:
        return self._get(self._turns, turn_id, TURN_TTL_S)

    def put_turn(self, turn_id: Optional[str], memo: Memo) -> None:
        self._put(self._turns, turn_id, memo, self._max_turns)

    def get_session(self, session_id: Optional[str]) -> Optional[Memo]:
        return self._get(self._sessions, session_id, SESSION_TTL_S)

    def put_session(self, session_id: Optional[str], memo: Memo) -> None:
        self._put(self._sessions, session_id, memo, self._max_sessions)

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()
            self._sessions.clear()

    # -- internals ---------------------------------------------------------------

    def _get(self, store: "OrderedDict[str, tuple[float, Memo]]", key: Optional[str], ttl: float) -> Optional[Memo]:
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            entry = store.get(key)
            if entry is None:
                return None
            stamp, memo = entry
            if now - stamp > ttl:
                del store[key]
                return None
            store.move_to_end(key)
            return memo

    def _put(self, store: "OrderedDict[str, tuple[float, Memo]]", key: Optional[str], memo: Memo, limit: int) -> None:
        if not key:
            return
        with self._lock:
            store[key] = (time.monotonic(), memo)
            store.move_to_end(key)
            while len(store) > limit:
                store.popitem(last=False)
