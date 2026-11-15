#!/usr/bin/env python3
"""Measure the Jev decider from the router audit trail. Read-only.

Answers "how often is the decider consulted, how often does it fail the confidence
gate, and what effort actually reaches the wire" for a time window, so the effect of
a rubric or memo change can be compared against a baseline.

Sources (never written to):

* ``routes.jsonl`` — one record per router invocation (``event`` = ``route``|``skip``),
  written by :mod:`audit`. A route record with ``replayed: true`` came from the memo,
  so it did not reach the decider.
* ``state.db`` (``sessions`` + ``session_model_usage``) — token volume per session,
  used for reasoning-per-turn.

Unit of analysis is the turn (``turn_id``) of a *routable* provider, because the router
only decides for providers listed in ``routable_providers``; every other provider's
call is a ``provider_not_routed`` skip and says nothing about the decider.

Usage::

    python3 scripts/measure_routes.py --since 2026-10-30 --until 2026-11-06
    python3 scripts/measure_routes.py --since 2026-10-30 --sessions-db ~/.hermes/.../state.db
    python3 scripts/measure_routes.py --since 2026-10-30 --format json > baseline.json

Defaults resolve ``--routes`` and ``--sessions-db`` from the profile named by
``--profile`` (``sysadmin``) under ``$HERMES_HOME/profiles``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Providers the router is expected to decide for. Overridden by
#: ``--routable-provider`` or by the profile config's ``routable_providers`` block.
DEFAULT_ROUTABLE = ("opencode-go", "commandcode", "openai-codex")

#: Skip reasons that mean the decider was actually consulted. The rest are pre-gates
#: (provider not routed, surface disabled, model not in grid, no message, no key): the
#: decider never ran, so counting them as decider calls would inflate the cost metric.
DECIDER_SKIP_REASONS = frozenset(
    {
        "low_confidence",
        "timeout",
        "upstream_error",
        "malformed_answer",
        "unknown_choice",
        "exception",
        "model_not_in_provider_catalog",
    }
)

#: Pre-decision skips kept as separate heads because they explain a turn that never
#: reached the decider at all.
PREDECISION_SKIP_REASONS = frozenset(
    {
        "provider_not_routed",
        "api_mode_not_routed",
        "disabled",
        "no_api_key",
        "no_user_message",
        "model_not_in_grid",
        "user_data",
    }
)

EFFORT_ORDER = ("low", "medium", "high", "max")
#: What the host runs when the router leaves the turn untouched. ``skip`` means the
#: provider call keeps its configured reasoning level, which the operator pins at high.
DEFAULT_HOST_EFFORT = "high"

#: Phase-1 acceptance targets from the Jev parecer (t_7805500b, section 5).
TARGETS = {
    "decider_calls_per_turn": ("<=", 1.2),
    "low_confidence_turn_rate": ("<=", 0.25),
    "high_share_of_calls": ("<", 0.60),
}

_ROUTES_GLOB = "agent-plugin-jev-effort-router-*/routes.jsonl"


class UsageError(Exception):
    """A problem with the input that the operator can fix (bad window, missing file)."""


# --------------------------------------------------------------------------- window


def _parse_instant(text: str, *, end_of_day: bool = False) -> datetime:
    """Parse an ISO date or datetime into an aware UTC instant.

    A bare date means "start of that day" (or "end of that day" for ``--until``), and a
    naive datetime is taken as UTC — the audit log writes UTC only.
    """
    raw = (text or "").strip()
    if not raw:
        raise UsageError("empty date")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise UsageError(f"cannot parse date {text!r}: use YYYY-MM-DD or ISO 8601") from exc
    if value.tzinfo is None:
        if len(raw) <= 10 and end_of_day:
            value = value.replace(hour=23, minute=59, second=59, microsecond=999999)
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ts(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# --------------------------------------------------------------------------- sources


def resolve_profile_dir(profile: str) -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        candidate = Path(home) / "profiles" / profile
        if candidate.is_dir():
            return candidate
    return Path.home() / "hermes" / "home" / "profiles" / profile


def discover_routes(profile_dir: Path) -> Optional[Path]:
    matches = sorted(profile_dir.glob(f"plugin-data/{_ROUTES_GLOB}"))
    return matches[-1] if matches else None


def read_routable_providers(config_path: Path) -> List[str]:
    """Best-effort read of ``routable_providers`` from a profile's YAML config.

    Not a YAML parser: it collects the ``- item`` lines that follow the block key, which
    is all this script needs and keeps it stdlib-only. Any surprise returns ``[]`` so the
    caller falls back to the built-in default.
    """
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    collected: List[str] = []
    inside = False
    key_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not inside:
            match = re.match(r"^(\s*)routable_providers\s*:\s*(\[.*\])?\s*$", line)
            if match:
                key_indent = len(match.group(1))
                inline = match.group(2)
                if inline:
                    return [item.strip().strip("'\"") for item in inline.strip("[]").split(",") if item.strip()]
                inside = True
            continue
        indent = len(line) - len(line.lstrip())
        item = re.match(r"^\s*-\s*(.+?)\s*$", line)
        if item and indent >= key_indent:
            collected.append(item.group(1).strip().strip("'\""))
            continue
        break
    return collected


def load_records(routes_path: Path) -> Tuple[List[Dict[str, Any]], int]:
    records: List[Dict[str, Any]] = []
    bad = 0
    with routes_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                bad += 1
    return records, bad


# ------------------------------------------------------------------------- aggregate


def _percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return (numerator / denominator) if denominator else None


class TurnAgg:
    __slots__ = ("turn_id", "session_id", "surface", "provider", "calls", "decider_calls",
                 "skip_reasons", "efforts", "route_efforts", "latencies", "confidences", "ts")

    def __init__(self, record: Dict[str, Any]) -> None:
        self.turn_id = record.get("turn_id") or ""
        self.session_id = record.get("session_id") or ""
        self.surface = record.get("platform") or "unknown"
        self.provider = record.get("provider") or "unknown"
        self.calls = 0
        self.decider_calls = 0
        self.skip_reasons: Counter = Counter()
        self.efforts: Counter = Counter()
        self.route_efforts: List[str] = []
        self.latencies: List[int] = []
        self.confidences: List[float] = []
        self.ts: Optional[datetime] = None

    def add(self, record: Dict[str, Any], host_effort: str) -> None:
        self.calls += 1
        stamp = _parse_ts(record.get("ts"))
        if stamp is not None and (self.ts is None or stamp < self.ts):
            self.ts = stamp
        event = record.get("event")
        if event == "route":
            if record.get("replayed"):
                self.efforts[record.get("effort") or "unset"] += 1
                return
            self.decider_calls += 1
            effort = record.get("effort") or "unset"
            self.efforts[effort] += 1
            self.route_efforts.append(effort)
            latency = record.get("latency_ms")
            if isinstance(latency, (int, float)):
                self.latencies.append(int(latency))
            confidence = record.get("effort_confidence")
            if isinstance(confidence, (int, float)):
                self.confidences.append(float(confidence))
            return
        reason = record.get("reason") or "unknown"
        self.skip_reasons[reason] += 1
        if reason in DECIDER_SKIP_REASONS:
            self.decider_calls += 1
        # A skipped call keeps the host's configured effort on the wire.
        self.efforts[host_effort] += 1

    def effective_effort(self) -> str:
        """The level the turn actually ran at: its accepted route, else the host default."""
        if self.route_efforts:
            return self.route_efforts[-1]
        if self.efforts:
            return self.efforts.most_common(1)[0][0]
        return "unset"


def aggregate(
    records: Iterable[Dict[str, Any]],
    *,
    since: datetime,
    until: datetime,
    routable: Sequence[str],
    host_effort: str,
    confidence_threshold: float,
) -> Dict[str, Any]:
    routable_keys = {p.strip().lower() for p in routable if p.strip()}
    turns: Dict[str, TurnAgg] = {}
    total_records = 0
    routed_records = 0
    out_of_window = 0
    reasons_all: Counter = Counter()
    routable_call_reasons: Counter = Counter()
    replay_count = 0
    predecision_only_turns = 0

    for record in records:
        stamp = _parse_ts(record.get("ts"))
        if stamp is None:
            out_of_window += 1
            continue
        if stamp < since or stamp > until:
            out_of_window += 1
            continue
        total_records += 1
        provider_key = (record.get("provider") or "").strip().lower()
        if record.get("event") == "skip":
            reasons_all[record.get("reason") or "unknown"] += 1
        if provider_key not in routable_keys:
            continue
        routed_records += 1
        turn_id = record.get("turn_id") or ""
        turn = turns.get(turn_id)
        if turn is None:
            turn = turns[turn_id] = TurnAgg(record)
        turn.add(record, host_effort)
        if record.get("event") == "route":
            if record.get("replayed"):
                replay_count += 1
            else:
                routable_call_reasons["route"] += 1
        else:
            routable_call_reasons[record.get("reason") or "unknown"] += 1

    routable_turns = {tid: t for tid, t in turns.items() if t.decider_calls or t.skip_reasons}
    # A turn whose every call was pre-gated never reached the decider; keeping it in the
    # denominator would make the decider look cheaper than it is.
    decidable_turns = {tid: t for tid, t in routable_turns.items() if t.decider_calls}
    predecision_only_turns = len(routable_turns) - len(decidable_turns)

    decider_calls = sum(t.decider_calls for t in routable_turns.values())
    low_conf_skips = sum(t.skip_reasons.get("low_confidence", 0) for t in routable_turns.values())
    timeout_skips = sum(t.skip_reasons.get("timeout", 0) for t in routable_turns.values())
    upstream_skips = sum(t.skip_reasons.get("upstream_error", 0) for t in routable_turns.values())
    total_skips = sum(sum(t.skip_reasons.values()) for t in routable_turns.values())

    skip_turns = sum(1 for t in routable_turns.values() if t.skip_reasons)
    low_conf_turns = sum(1 for t in routable_turns.values() if t.skip_reasons.get("low_confidence"))
    timeout_turns = sum(1 for t in routable_turns.values() if t.skip_reasons.get("timeout"))

    call_efforts: Counter = Counter()
    turn_efforts: Counter = Counter()
    latencies: List[int] = []
    confidences: List[float] = []
    for turn in routable_turns.values():
        call_efforts.update(turn.efforts)
        turn_efforts[turn.effective_effort()] += 1
        latencies.extend(turn.latencies)
        confidences.extend(turn.confidences)

    denominador_turnos = len(decidable_turns) or len(routable_turns)
    high_calls = call_efforts.get("high", 0) + call_efforts.get("max", 0)
    total_calls = sum(call_efforts.values())
    total_turn_efforts = sum(turn_efforts.values())
    high_turns = turn_efforts.get("high", 0) + turn_efforts.get("max", 0)

    buckets = _confidence_buckets(confidences, confidence_threshold)

    surfaces: Dict[str, Dict[str, Any]] = {}
    for turn in routable_turns.values():
        entry = surfaces.setdefault(
            turn.surface,
            {"turns": 0, "decider_calls": 0, "low_conf_turns": 0, "skip_turns": 0,
             "timeouts": 0, "efforts": Counter(), "session_ids": set()},
        )
        entry["turns"] += 1
        entry["decider_calls"] += turn.decider_calls
        entry["low_conf_turns"] += 1 if turn.skip_reasons.get("low_confidence") else 0
        entry["skip_turns"] += 1 if turn.skip_reasons else 0
        entry["timeouts"] += turn.skip_reasons.get("timeout", 0)
        entry["efforts"][turn.effective_effort()] += 1
        if turn.session_id:
            entry["session_ids"].add(turn.session_id)
    surface_rows = []
    for name, entry in sorted(surfaces.items(), key=lambda kv: kv[1]["turns"], reverse=True):
        efforts = entry.pop("efforts")
        sessions = entry.pop("session_ids")
        hi = efforts.get("high", 0) + efforts.get("max", 0)
        surface_rows.append(
            {
                "surface": name,
                "turns": entry["turns"],
                "sessions": len(sessions),
                "decider_calls_per_turn": _ratio(entry["decider_calls"], entry["turns"]),
                "low_conf_turn_rate": _ratio(entry["low_conf_turns"], entry["turns"]),
                "skip_turn_rate": _ratio(entry["skip_turns"], entry["turns"]),
                "timeouts": entry["timeouts"],
                "high_share": _ratio(hi, entry["turns"]),
                "efforts": dict(efforts),
            }
        )

    return {
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "days": round((until - since).total_seconds() / 86400, 2),
        },
        "scope": {
            "routable_providers": sorted(routable_keys),
            "host_effort_on_skip": host_effort,
            "confidence_threshold": confidence_threshold,
            "records_in_file_window": total_records,
            "records_outside_window_or_bad_ts": out_of_window,
            "routable_records": routed_records,
        },
        "denominators": {
            "routable_turns": len(routable_turns),
            "routable_turns_that_reached_decider": len(decidable_turns),
            "routable_turns_pre_gated_only": predecision_only_turns,
        },
        "per_turn": {
            "decider_calls": decider_calls,
            "decider_calls_per_turn": _ratio(decider_calls, denominador_turnos),
            "skip_turns": skip_turns,
            "skip_turn_rate": _ratio(skip_turns, len(routable_turns)),
            "low_confidence_turns": low_conf_turns,
            "low_confidence_turn_rate": _ratio(low_conf_turns, len(routable_turns)),
            "timeout_turns": timeout_turns,
            "timeouts": timeout_skips,
            "timeouts_per_turn": _ratio(timeout_skips, denominador_turnos),
            "upstream_error_skips": upstream_skips,
            "upstream_errors_per_turn": _ratio(upstream_skips, denominador_turnos),
        },
        "per_call": {
            "routable_calls": routed_records,
            "decider_calls": decider_calls,
            "memo_replays": replay_count,
            "replay_rate": _ratio(replay_count, routed_records),
            "skip_calls": total_skips,
            "skip_call_rate": _ratio(total_skips, routed_records),
            "low_confidence_calls": low_conf_skips,
            "low_confidence_call_rate": _ratio(low_conf_skips, routed_records),
            "timeout_calls": timeout_skips,
            "timeout_call_rate": _ratio(timeout_skips, routed_records),
            "reasons": dict(routable_call_reasons),
        },
        "effort": {
            "calls": dict(call_efforts),
            "calls_total": total_calls,
            "turns": dict(turn_efforts),
            "turns_total": total_turn_efforts,
            "high_share_of_calls": _ratio(high_calls, total_calls),
            "high_share_of_turns": _ratio(high_turns, total_turn_efforts),
        },
        "confidence": {
            "n": len(confidences),
            "min": min(confidences) if confidences else None,
            "p50": _percentile(confidences, 0.5),
            "p90": _percentile(confidences, 0.9),
            "max": max(confidences) if confidences else None,
            "mean": (sum(confidences) / len(confidences)) if confidences else None,
            "buckets": buckets,
        },
        "latency_ms": {
            "n": len(latencies),
            "p50": _percentile(latencies, 0.5),
            "p90": _percentile(latencies, 0.9),
            "max": max(latencies) if latencies else None,
        },
        "surfaces": surface_rows,
        "skip_reasons_all_providers": dict(reasons_all),
    }


def _confidence_buckets(values: Sequence[float], threshold: float) -> List[Dict[str, Any]]:
    """Histogram of ``effort_confidence`` around the gate that decides skip vs route."""
    edges = [0.0, threshold, 0.6, 0.7, 0.8, 0.9, 1.0001]
    edges = sorted(set(round(edge, 4) for edge in edges))
    buckets: List[Dict[str, Any]] = []
    for low, high in zip(edges, edges[1:]):
        count = sum(1 for value in values if low <= value < high)
        buckets.append({"range": f"{low:.2f}-{min(high, 1.0):.2f}", "n": count})
    return buckets


# --------------------------------------------------------------------------- sessions


def session_metrics(
    db_path: Path,
    *,
    since: datetime,
    until: datetime,
    routable: Sequence[str],
    turns_by_session: Dict[str, int],
) -> Dict[str, Any]:
    """Token volume for the sessions that had routable turns in the window."""
    if not db_path.is_file():
        return {"available": False, "reason": f"not found: {db_path}"}
    uri = f"file:{db_path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"cannot open {db_path}: {exc}"}
    con.row_factory = sqlite3.Row
    try:
        since_ts = since.timestamp()
        until_ts = until.timestamp()
        rows = con.execute(
            """
            SELECT id, source, model, billing_provider, started_at, ended_at,
                   message_count, api_call_count, output_tokens, reasoning_tokens,
                   input_tokens, estimated_cost_usd
            FROM sessions
            WHERE started_at >= ? AND started_at <= ?
            """,
            (since_ts, until_ts),
        ).fetchall()
        usage_rows = con.execute(
            """
            SELECT session_id, model, billing_provider, task, api_call_count,
                   output_tokens, reasoning_tokens
            FROM session_model_usage
            WHERE first_seen >= ? AND first_seen <= ?
            """,
            (since_ts, until_ts),
        ).fetchall()
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"query failed: {exc}"}
    finally:
        con.close()

    routable_keys = {p.strip().lower() for p in routable if p.strip()}
    usage_by_session: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"output_tokens": 0, "reasoning_tokens": 0, "api_calls": 0, "models": Counter(), "routable_output": 0, "routable_reasoning": 0}
    )
    for row in usage_rows:
        entry = usage_by_session[row["session_id"]]
        entry["output_tokens"] += row["output_tokens"] or 0
        entry["reasoning_tokens"] += row["reasoning_tokens"] or 0
        entry["api_calls"] += row["api_call_count"] or 0
        entry["models"][row["model"] or "?"] += row["output_tokens"] or 0
        if (row["billing_provider"] or "").strip().lower() in routable_keys:
            entry["routable_output"] += row["output_tokens"] or 0
            entry["routable_reasoning"] += row["reasoning_tokens"] or 0

    cohort = []
    for row in rows:
        session_id = row["id"]
        usage = usage_by_session.get(session_id)
        turns = turns_by_session.get(session_id, 0)
        output_tokens = row["output_tokens"] or 0
        reasoning_tokens = row["reasoning_tokens"] or 0
        cohort.append(
            {
                "session_id": session_id,
                "source": row["source"],
                "model": row["model"],
                "billing_provider": row["billing_provider"],
                "routable_turns": turns,
                "api_call_count": row["api_call_count"] or 0,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "reasoning_share": _ratio(reasoning_tokens, output_tokens),
                "reasoning_per_turn": _ratio(reasoning_tokens, turns),
                "output_per_turn": _ratio(output_tokens, turns),
                "has_usage_rows": usage is not None,
            }
        )

    with_turns = [item for item in cohort if item["routable_turns"] > 0]
    total_turns = sum(item["routable_turns"] for item in with_turns)
    total_output = sum(item["output_tokens"] for item in with_turns)
    total_reasoning = sum(item["reasoning_tokens"] for item in with_turns)
    total_calls = sum(item["api_call_count"] for item in with_turns)

    by_source: Dict[str, Dict[str, Any]] = {}
    for item in with_turns:
        entry = by_source.setdefault(
            item["source"] or "unknown",
            {"sessions": 0, "turns": 0, "calls": 0, "output_tokens": 0, "reasoning_tokens": 0},
        )
        entry["sessions"] += 1
        entry["turns"] += item["routable_turns"]
        entry["calls"] += item["api_call_count"]
        entry["output_tokens"] += item["output_tokens"]
        entry["reasoning_tokens"] += item["reasoning_tokens"]

    return {
        "available": True,
        "db": str(db_path),
        "sessions_in_window": len(rows),
        "sessions_with_routable_turns": len(with_turns),
        "sessions_without_routable_turns": len(cohort) - len(with_turns),
        "routable_turns": total_turns,
        "api_calls": total_calls,
        "output_tokens": total_output,
        "reasoning_tokens": total_reasoning,
        "reasoning_share_of_output": _ratio(total_reasoning, total_output),
        "reasoning_per_turn": _ratio(total_reasoning, total_turns),
        "output_per_turn": _ratio(total_output, total_turns),
        "reasoning_per_api_call": _ratio(total_reasoning, total_calls),
        "output_per_api_call": _ratio(total_output, total_calls),
        "by_source": [
            {
                "source": name,
                "sessions": entry["sessions"],
                "turns": entry["turns"],
                "calls": entry["calls"],
                "output_tokens": entry["output_tokens"],
                "reasoning_tokens": entry["reasoning_tokens"],
                "reasoning_share": _ratio(entry["reasoning_tokens"], entry["output_tokens"]),
                "reasoning_per_turn": _ratio(entry["reasoning_tokens"], entry["turns"]),
                "reasoning_per_api_call": _ratio(entry["reasoning_tokens"], entry["calls"]),
            }
            for name, entry in sorted(by_source.items(), key=lambda kv: kv[1]["turns"], reverse=True)
        ],
        "largest_sessions": sorted(with_turns, key=lambda item: item["output_tokens"], reverse=True)[:10],
    }


# --------------------------------------------------------------------------- render


def _fmt_int(value: Any) -> str:
    return "—" if value is None else f"{int(value):,}".replace(",", ".")


def _fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return _fmt_int(value)
    return f"{value:.{digits}f}".replace(".", ",")


def _fmt_pct(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.1f}%".replace(".", ",")


def _verdict(value: Optional[float], target: Tuple[str, float]) -> str:
    if value is None:
        return "—"
    op, limit = target
    ok = value <= limit if op == "<=" else value < limit
    return "OK" if ok else "ATENCAO"


def render_markdown(metrics: Dict[str, Any], *, title: str, routes_path: Path, sessions: Dict[str, Any]) -> str:
    window = metrics["window"]
    scope = metrics["scope"]
    den = metrics["denominators"]
    per_turn = metrics["per_turn"]
    per_call = metrics["per_call"]
    effort = metrics["effort"]
    confidence = metrics["confidence"]
    latency = metrics["latency_ms"]

    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        f"Janela: **{window['since']}** → **{window['until']}** "
        f"({_fmt_num(window['days'], 2)} dias) · fonte: `{routes_path}`"
    )
    lines.append("")
    lines.append("## Resumo executivo")
    lines.append("")
    headline = (
        f"{_fmt_num(per_turn['decider_calls_per_turn'], 2)} chamadas ao decider por turno, "
        f"{_fmt_pct(per_turn['low_confidence_turn_rate'])} dos turnos roteáveis em skip low_confidence, "
        f"{_fmt_pct(effort['high_share_of_calls'])} das chamadas efetivas em high/max "
        f"({_fmt_int(den['routable_turns'])} turnos roteáveis, {_fmt_int(den['routable_turns_that_reached_decider'])} que chegaram ao decider)."
    )
    lines.append(headline)
    lines.append("")
    lines.append("| métrica | valor | meta fase 1 | situação |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| chamadas ao decider por turno | {_fmt_num(per_turn['decider_calls_per_turn'], 2)} | ≤ 1,2 | "
        f"{_verdict(per_turn['decider_calls_per_turn'], TARGETS['decider_calls_per_turn'])} |"
    )
    lines.append(
        f"| turnos em skip low_confidence | {_fmt_pct(per_turn['low_confidence_turn_rate'])} | ≤ 25% | "
        f"{_verdict(per_turn['low_confidence_turn_rate'], TARGETS['low_confidence_turn_rate'])} |"
    )
    lines.append(
        f"| chamadas efetivas em high + max | {_fmt_pct(effort['high_share_of_calls'])} | < 60% | "
        f"{_verdict(effort['high_share_of_calls'], TARGETS['high_share_of_calls'])} |"
    )
    lines.append(
        f"| timeouts por turno | {_fmt_num(per_turn['timeouts_per_turn'], 2)} | vigiar | "
        f"{'ATENCAO' if (per_turn['timeouts_per_turn'] or 0) >= 0.5 else 'OK'} |"
    )
    lines.append("")

    lines.append("## Denominadores")
    lines.append("")
    lines.append("| item | valor |")
    lines.append("|---|---|")
    lines.append(f"| registros na janela (todos os providers) | {_fmt_int(scope['records_in_file_window'])} |")
    lines.append(f"| registros de providers roteáveis | {_fmt_int(scope['routable_records'])} |")
    lines.append(f"| providers roteáveis considerados | {', '.join(scope['routable_providers'])} |")
    lines.append(f"| turnos roteáveis | {_fmt_int(den['routable_turns'])} |")
    lines.append(f"| turnos roteáveis que chegaram ao decider | {_fmt_int(den['routable_turns_that_reached_decider'])} |")
    lines.append(f"| turnos roteáveis barrados antes do decider | {_fmt_int(den['routable_turns_pre_gated_only'])} |")
    lines.append(f"| effort do host quando o router não roteia | `{scope['host_effort_on_skip']}` |")
    lines.append("")

    lines.append("## Por turno")
    lines.append("")
    lines.append("| métrica | valor | denominador |")
    lines.append("|---|---|---|")
    lines.append(f"| chamadas ao decider | {_fmt_int(per_turn['decider_calls'])} | turnos que chegaram ao decider |")
    lines.append(f"| chamadas ao decider por turno | {_fmt_num(per_turn['decider_calls_per_turn'], 2)} | idem |")
    lines.append(f"| turnos com pelo menos um skip | {_fmt_int(per_turn['skip_turns'])} ({_fmt_pct(per_turn['skip_turn_rate'])}) | turnos roteáveis |")
    lines.append(f"| turnos com skip low_confidence | {_fmt_int(per_turn['low_confidence_turns'])} ({_fmt_pct(per_turn['low_confidence_turn_rate'])}) | turnos roteáveis |")
    lines.append(f"| turnos com timeout | {_fmt_int(per_turn['timeout_turns'])} | turnos roteáveis |")
    lines.append(f"| timeouts (chamadas) | {_fmt_int(per_turn['timeouts'])} ({_fmt_num(per_turn['timeouts_per_turn'], 2)}/turno) | turnos que chegaram ao decider |")
    lines.append(f"| skips por upstream_error | {_fmt_int(per_turn['upstream_error_skips'])} ({_fmt_num(per_turn['upstream_errors_per_turn'], 2)}/turno) | turnos que chegaram ao decider |")
    lines.append("")

    lines.append("## Por chamada")
    lines.append("")
    lines.append("| métrica | valor | denominador |")
    lines.append("|---|---|---|")
    lines.append(f"| chamadas roteáveis | {_fmt_int(per_call['routable_calls'])} | registros com provider roteável |")
    lines.append(f"| chamadas ao decider | {_fmt_int(per_call['decider_calls'])} | idem |")
    lines.append(f"| replays de memo | {_fmt_int(per_call['memo_replays'])} ({_fmt_pct(per_call['replay_rate'])}) | idem |")
    lines.append(f"| skips | {_fmt_int(per_call['skip_calls'])} ({_fmt_pct(per_call['skip_call_rate'])}) | idem |")
    lines.append(f"| skip low_confidence | {_fmt_int(per_call['low_confidence_calls'])} ({_fmt_pct(per_call['low_confidence_call_rate'])}) | idem |")
    lines.append(f"| skip timeout | {_fmt_int(per_call['timeout_calls'])} ({_fmt_pct(per_call['timeout_call_rate'])}) | idem |")
    lines.append("")

    lines.append("## Effort efetivo (skip ⇒ effort do host)")
    lines.append("")
    lines.append("| nível | chamadas | % chamadas | turnos | % turnos |")
    lines.append("|---|---|---|---|---|")
    call_total = effort["calls_total"] or 0
    turn_total = effort["turns_total"] or 0
    for level in list(EFFORT_ORDER) + sorted(set(effort["calls"]) - set(EFFORT_ORDER)):
        calls = effort["calls"].get(level, 0)
        turns = effort["turns"].get(level, 0)
        lines.append(
            f"| {level} | {_fmt_int(calls)} | {_fmt_pct(_ratio(calls, call_total))} | "
            f"{_fmt_int(turns)} | {_fmt_pct(_ratio(turns, turn_total))} |"
        )
    lines.append(f"| **total** | **{_fmt_int(call_total)}** | 100% | **{_fmt_int(turn_total)}** | 100% |")
    lines.append("")

    lines.append("## effort_confidence (decisões aceitas)")
    lines.append("")
    lines.append("| estatística | valor |")
    lines.append("|---|---|")
    lines.append(f"| n | {_fmt_int(confidence['n'])} |")
    lines.append(f"| mínimo | {_fmt_num(confidence['min'], 3)} |")
    lines.append(f"| mediana | {_fmt_num(confidence['p50'], 3)} |")
    lines.append(f"| p90 | {_fmt_num(confidence['p90'], 3)} |")
    lines.append(f"| máximo | {_fmt_num(confidence['max'], 3)} |")
    lines.append(f"| média | {_fmt_num(confidence['mean'], 3)} |")
    lines.append(f"| limiar de skip | {_fmt_num(scope['confidence_threshold'], 3)} |")
    lines.append("")
    lines.append("| faixa | decisões |")
    lines.append("|---|---|")
    for bucket in confidence["buckets"]:
        lines.append(f"| {bucket['range'].replace('.', ',')} | {_fmt_int(bucket['n'])} |")
    lines.append("")

    lines.append("## Latência do decider")
    lines.append("")
    lines.append("| estatística | ms |")
    lines.append("|---|---|")
    lines.append(f"| n | {_fmt_int(latency['n'])} |")
    lines.append(f"| p50 | {_fmt_int(latency['p50'])} |")
    lines.append(f"| p90 | {_fmt_int(latency['p90'])} |")
    lines.append(f"| máximo | {_fmt_int(latency['max'])} |")
    lines.append("")

    lines.append("## Recorte por surface")
    lines.append("")
    lines.append("| surface | turnos | chamadas/turno | skip low_confidence | skip (qualquer) | timeouts | high+max |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in metrics["surfaces"]:
        lines.append(
            f"| {row['surface']} | {_fmt_int(row['turns'])} | {_fmt_num(row['decider_calls_per_turn'], 2)} | "
            f"{_fmt_pct(row['low_conf_turn_rate'])} | {_fmt_pct(row['skip_turn_rate'])} | "
            f"{_fmt_int(row['timeouts'])} | {_fmt_pct(row['high_share'])} |"
        )
    lines.append("")

    lines.append("## Sessions (tokens por turno roteável)")
    lines.append("")
    if not sessions.get("available"):
        lines.append(f"Sessões não medidas: {sessions.get('reason', 'indisponível')}.")
        lines.append("")
    else:
        lines.append(f"Fonte: `{sessions['db']}` (somente leitura).")
        lines.append("")
        lines.append("| métrica | valor |")
        lines.append("|---|---|")
        lines.append(f"| sessões na janela | {_fmt_int(sessions['sessions_in_window'])} |")
        lines.append(f"| sessões com turnos roteáveis | {_fmt_int(sessions['sessions_with_routable_turns'])} |")
        lines.append(f"| turnos roteáveis cobertos | {_fmt_int(sessions['routable_turns'])} |")
        lines.append(f"| output_tokens | {_fmt_int(sessions['output_tokens'])} |")
        lines.append(f"| reasoning_tokens | {_fmt_int(sessions['reasoning_tokens'])} |")
        lines.append(f"| chamadas de API (sessions.api_call_count) | {_fmt_int(sessions['api_calls'])} |")
        lines.append(f"| reasoning / output | {_fmt_pct(sessions['reasoning_share_of_output'])} |")
        lines.append(f"| reasoning por turno | {_fmt_int(sessions['reasoning_per_turn'])} |")
        lines.append(f"| output por turno | {_fmt_int(sessions['output_per_turn'])} |")
        lines.append(f"| reasoning por chamada de API | {_fmt_int(sessions['reasoning_per_api_call'])} |")
        lines.append(f"| output por chamada de API | {_fmt_int(sessions['output_per_api_call'])} |")
        lines.append("")
        lines.append(
            "Turno aqui é o `turn_id` do router: uma sessão de cron ou de worker kanban pode ter "
            "1 turno com dezenas de chamadas, então `reasoning/turno` não é comparável entre "
            "surfaces — use `reasoning/chamada` para comparar."
        )
        lines.append("")
        lines.append("| source | sessões | turnos | chamadas | output_tokens | reasoning_tokens | reasoning/output | reasoning/turno | reasoning/chamada |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in sessions["by_source"]:
            lines.append(
                f"| {row['source']} | {_fmt_int(row['sessions'])} | {_fmt_int(row['turns'])} | "
                f"{_fmt_int(row['calls'])} | {_fmt_int(row['output_tokens'])} | {_fmt_int(row['reasoning_tokens'])} | "
                f"{_fmt_pct(row['reasoning_share'])} | {_fmt_int(row['reasoning_per_turn'])} | "
                f"{_fmt_int(row['reasoning_per_api_call'])} |"
            )
        lines.append("")

    lines.append("## Composição dos skips (todos os providers, janela)")
    lines.append("")
    lines.append("| motivo | registros |")
    lines.append("|---|---|")
    for reason, count in sorted(metrics["skip_reasons_all_providers"].items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"| {reason} | {_fmt_int(count)} |")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the Jev decider from routes.jsonl (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--routes", type=Path, help="routes.jsonl to read (default: profile plugin-data)")
    parser.add_argument("--since", help="window start: YYYY-MM-DD or ISO 8601 (UTC)")
    parser.add_argument("--until", help="window end: YYYY-MM-DD (inclusive) or ISO 8601 (UTC)")
    parser.add_argument("--sessions-db", type=Path, help="profile state.db (default: profile state.db, when present)")
    parser.add_argument("--profile", default="sysadmin", help="Hermes profile whose plugin-data/state.db to read")
    parser.add_argument("--profile-dir", type=Path, help="override the profile directory")
    parser.add_argument("--profile-config", type=Path, help="config.yaml to read routable_providers from")
    parser.add_argument("--routable-provider", action="append", default=[], metavar="PROVIDER",
                        help="provider the router decides for (repeatable; default: config or %s)" % ", ".join(DEFAULT_ROUTABLE))
    parser.add_argument("--host-effort", default=DEFAULT_HOST_EFFORT,
                        help=f"effort the host runs when the router skips (default: {DEFAULT_HOST_EFFORT})")
    parser.add_argument("--confidence-threshold", type=float, default=0.5,
                        help="confidence gate used by the router (default: 0.5)")
    parser.add_argument("--days", type=float, default=7.0, help="window length when --since is omitted (default: 7)")
    parser.add_argument("--no-sessions", action="store_true", help="skip the state.db section entirely")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--title", default="Jev decider — medição", help="report title (markdown only)")
    return parser


def run(args: argparse.Namespace) -> int:
    profile_dir = args.profile_dir or resolve_profile_dir(args.profile)
    routes_path = args.routes or discover_routes(profile_dir)
    if routes_path is None:
        raise UsageError(f"routes.jsonl not found under {profile_dir}/plugin-data")
    if not routes_path.is_file():
        raise UsageError(f"routes.jsonl not found: {routes_path}")

    now = datetime.now(timezone.utc)
    until = _parse_instant(args.until, end_of_day=True) if args.until else now
    since = (
        _parse_instant(args.since)
        if args.since
        else (until - timedelta(days=args.days)).replace(microsecond=0)
    )
    if since >= until:
        raise UsageError(f"window is empty: since {since.isoformat()} >= until {until.isoformat()}")

    routable: List[str] = list(args.routable_provider)
    if not routable:
        config_path = args.profile_config or (profile_dir / "config.yaml")
        routable = read_routable_providers(config_path)
    if not routable:
        routable = list(DEFAULT_ROUTABLE)

    records, bad_lines = load_records(routes_path)
    metrics = aggregate(
        records,
        since=since,
        until=until,
        routable=routable,
        host_effort=args.host_effort,
        confidence_threshold=args.confidence_threshold,
    )
    metrics["sources"] = {
        "routes": str(routes_path),
        "records": len(records),
        "bad_lines": bad_lines,
    }

    sessions: Dict[str, Any] = {"available": False, "reason": "desativado por --no-sessions"}
    if not args.no_sessions:
        db_path = args.sessions_db or (profile_dir / "state.db")
        routable_keys = {p.strip().lower() for p in routable}
        turns_by_session: Dict[str, set] = defaultdict(set)
        for record in records:
            stamp = _parse_ts(record.get("ts"))
            if stamp is None or stamp < since or stamp > until:
                continue
            if (record.get("provider") or "").strip().lower() not in routable_keys:
                continue
            session_id = record.get("session_id")
            turn_id = record.get("turn_id")
            if session_id and turn_id:
                turns_by_session[session_id].add(turn_id)
        sessions = session_metrics(
            db_path,
            since=since,
            until=until,
            routable=routable,
            turns_by_session={session: len(turns) for session, turns in turns_by_session.items()},
        )
    metrics["sessions"] = sessions

    if args.format == "json":
        print(json.dumps(metrics, indent=2, ensure_ascii=False, default=str))
    else:
        print(render_markdown(metrics, title=args.title, routes_path=routes_path, sessions=sessions))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except UsageError as exc:
        print(f"measure_routes: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
