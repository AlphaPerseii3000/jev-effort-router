"""Durable audit trail.

One JSONL record per routing attempt (routed or skipped) under the plugin's data directory,
so routing quality can be reviewed over time without re-running benchmarks. The API key and
the user message never appear; the message is only included when the operator explicitly turns
that on for local debugging.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

AUDIT_FILENAME = "routes.jsonl"

#: Keep the file bounded; the tail is what matters and old decisions lose value quickly.
MAX_RECORDS = 20000
_ROTATE_READ_BYTES = 16 * 1024 * 1024

_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    """Append-only JSONL writer for one data directory."""

    def __init__(self, data_dir: Optional[Path], enabled: bool = True) -> None:
        self._data_dir = Path(data_dir) if data_dir else None
        self._enabled = bool(enabled) and self._data_dir is not None

    @property
    def path(self) -> Optional[Path]:
        return self._data_dir / AUDIT_FILENAME if self._data_dir else None

    def append(self, record: Dict[str, Any]) -> bool:
        """Append one record. Never raises; returns True when it landed on disk."""
        if not self._enabled or self.path is None:
            return False
        entry = {"ts": now_iso(), **record}
        try:
            payload = json.dumps(entry, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - an unserialisable record is dropped, not fatal
            logger.debug("jev-router: dropping unserialisable audit record")
            return False
        try:
            with _LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(payload + "\n")
            return True
        except Exception as exc:  # noqa: BLE001 - auditing must never break a turn
            logger.debug("jev-router: audit write failed: %s", exc)
            return False

    def _rotate_if_needed(self) -> None:
        path = self.path
        if path is None or not path.exists():
            return
        try:
            if path.stat().st_size <= _ROTATE_READ_BYTES:
                return
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            # Rotate when the file is *at* capacity, because the record being appended lands
            # right after this. Leaving MAX_RECORDS lines here would persist MAX_RECORDS + 1.
            if len(lines) < MAX_RECORDS:
                return
            keep_count = max(0, MAX_RECORDS - 1)
            keep = lines[-keep_count:] if keep_count else []
            temp = path.with_suffix(".tmp")
            temp.write_text("\n".join(keep) + "\n" if keep else "", encoding="utf-8")
            os.replace(temp, path)
        except Exception:  # noqa: BLE001
            logger.debug("jev-router: audit rotation skipped")

    def tail(self, limit: int = 10) -> List[Dict[str, Any]]:
        """The most recent ``limit`` records, newest last. Never raises."""
        path = self.path
        if path is None or not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:  # noqa: BLE001
            return []
        records: List[Dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def count(self) -> int:
        path = self.path
        if path is None or not path.exists():
            return 0
        try:
            return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
        except Exception:  # noqa: BLE001
            return 0
