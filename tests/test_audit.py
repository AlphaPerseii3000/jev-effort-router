"""Audit trail and per-turn memo."""

from __future__ import annotations

import json

from audit import AuditLog
from memo import Memo, TurnMemo


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_audit_appends_one_record_per_call(tmp_path):
    log = AuditLog(tmp_path, enabled=True)

    assert log.append({"event": "route", "model": "kimi-k3"})
    assert log.append({"event": "route", "model": "glm-5.3"})

    records = read_records(tmp_path / "routes.jsonl")
    assert [record["model"] for record in records] == ["kimi-k3", "glm-5.3"]
    assert all("ts" in record for record in records)
    assert log.count() == 2


def test_audit_is_disabled_without_a_data_dir(tmp_path):
    log = AuditLog(None, enabled=True)
    assert not log.append({"event": "route"})
    assert log.tail() == []
    assert log.count() == 0


def test_audit_respects_the_switch(tmp_path):
    log = AuditLog(tmp_path, enabled=False)
    assert not log.append({"event": "route"})
    assert log.path is not None and not log.path.exists()


def test_audit_tail_is_newest_last(tmp_path):
    log = AuditLog(tmp_path, enabled=True)
    for index in range(5):
        log.append({"event": "route", "n": index})
    assert [record["n"] for record in log.tail(3)] == [2, 3, 4]


def test_audit_survives_an_unserialisable_record(tmp_path):
    log = AuditLog(tmp_path, enabled=True)

    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("nope")

    # `default=str` cannot save an object whose repr raises; the record is dropped, not fatal.
    log.append({"event": "route", "bad": Hostile()})
    assert log.count() <= 1  # either dropped or stringified; never an exception


def test_audit_tolerates_a_corrupt_line(tmp_path):
    path = tmp_path / "routes.jsonl"
    path.write_text('{"event": "route"}\nnot json\n{"event": "skip"}\n', encoding="utf-8")

    records = AuditLog(tmp_path, enabled=True).tail(10)
    assert [record["event"] for record in records] == ["route", "skip"]


def test_audit_truncates_when_over_the_record_cap(tmp_path, monkeypatch):
    import audit as audit_module

    monkeypatch.setattr(audit_module, "MAX_RECORDS", 3)
    monkeypatch.setattr(audit_module, "_ROTATE_READ_BYTES", 10)
    log = AuditLog(tmp_path, enabled=True)
    for index in range(10):
        log.append({"n": index})
    assert log.count() == 3


def test_memo_replays_within_the_ttl(tmp_path):
    memo = TurnMemo()
    entry = Memo(model="kimi-k3", effort="high", effort_requested="high", confidence=0.9, choice="2")

    memo.put_turn("turn-1", entry)

    assert memo.get_turn("turn-1") == entry
    assert memo.get_turn("turn-2") is None
    assert memo.get_turn("") is None
    assert memo.get_turn(None) is None


def test_memo_expires(tmp_path, monkeypatch):
    import memo as memo_module

    memo = TurnMemo()
    entry = Memo(model="m", effort=None, effort_requested=None, confidence=0.9, choice="1")
    memo.put_turn("turn-1", entry)

    monkeypatch.setattr(memo_module, "TURN_TTL_S", -1.0)
    assert memo.get_turn("turn-1") is None


def test_memo_is_bounded():
    memo = TurnMemo(max_turns=2)
    entry = Memo(model="m", effort=None, effort_requested=None, confidence=0.9, choice="1")
    for index in range(5):
        memo.put_turn(f"turn-{index}", entry)

    assert memo.get_turn("turn-0") is None
    assert memo.get_turn("turn-4") == entry


def test_memo_clear():
    memo = TurnMemo()
    entry = Memo(model="m", effort=None, effort_requested=None, confidence=0.9, choice="1")
    memo.put_turn("turn-1", entry)
    memo.put_session("session-1", entry)

    memo.clear()

    assert memo.get_turn("turn-1") is None
    assert memo.get_session("session-1") is None
