"""Observational timing telemetry (implementation-handoff 9, 10).

Events append to ``run_events`` and never touch mathematical record versions,
commits or publications. Summaries are observational only and must not act as
an acceptance gate. This module never imports the legacy monolith.
"""
from __future__ import annotations

import contextlib
import json
import time

from .errors import InvalidRequest
from .ids import new_id, valid_id
from .storage import Database, now_iso

STAGES = ("reading_reasoning", "authoring", "retry", "renewal", "command", "waiting", "report", "combined")


def _validate(run_id, stage, elapsed_ms, details) -> None:
    errors = []
    if not valid_id(run_id):
        errors.append("run_id must be an identifier (letters, digits, '_' or '-', starting with a letter)")
    if stage not in STAGES:
        errors.append(f"stage must be one of {list(STAGES)}")
    if elapsed_ms is not None and (isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0):
        errors.append("elapsed_ms must be null or a non-negative integer")
    if not isinstance(details, dict):
        errors.append("details must be a JSON object")
    if errors:
        raise InvalidRequest("invalid run event", code="INVALID_EVENT", records=errors)


def record_event(db: Database, *, run_id: str, stage: str, elapsed_ms=None, details=None, started_at=None) -> dict:
    """Append one timing event. ``elapsed_ms`` may be null when the duration is unknown."""
    details = {} if details is None else details
    _validate(run_id, stage, elapsed_ms, details)
    if not db.write:
        raise InvalidRequest("recording telemetry needs a writable database")
    started_at = started_at or now_iso()
    event_id = new_id("event")
    db.begin_immediate()
    try:
        db.insert_run_event(event_id, run_id, stage, started_at, elapsed_ms, details)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"event_id": event_id, "run_id": run_id, "stage": stage, "started_at": started_at,
            "elapsed_ms": elapsed_ms}


def events(db: Database, *, run_id=None) -> list:
    sql = "SELECT id, run_id, stage, started_at, elapsed_ms, details_json FROM run_events"
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    sql += " ORDER BY started_at, id"
    return [{"event_id": row["id"], "run_id": row["run_id"], "stage": row["stage"], "started_at": row["started_at"],
             "elapsed_ms": row["elapsed_ms"], "details": json.loads(row["details_json"])}
            for row in db.conn.execute(sql, params)]


def summarize(db: Database, *, run_id=None) -> dict:
    """Per-stage and per-run totals. Observational only: never a gate for acceptance or release."""
    rows = events(db, run_id=run_id)
    stages = {stage: {"events": 0, "elapsed_ms": 0, "unknown_elapsed": 0} for stage in STAGES}
    runs: dict = {}
    for row in rows:
        run = runs.setdefault(row["run_id"], {"run_id": row["run_id"], "events": 0, "elapsed_ms": 0,
                                              "unknown_elapsed": 0, "first_started_at": row["started_at"],
                                              "last_started_at": row["started_at"], "stages": {}})
        for bucket in (stages[row["stage"]], run, run["stages"].setdefault(
                row["stage"], {"events": 0, "elapsed_ms": 0, "unknown_elapsed": 0})):
            bucket["events"] += 1
            if row["elapsed_ms"] is None:
                bucket["unknown_elapsed"] += 1
            else:
                bucket["elapsed_ms"] += row["elapsed_ms"]
        run["first_started_at"] = min(run["first_started_at"], row["started_at"])
        run["last_started_at"] = max(run["last_started_at"], row["started_at"])
    return {"run_id": run_id, "events": len(rows), "runs": [runs[k] for k in sorted(runs)], "stages": stages,
            "observational_only": True}


@contextlib.contextmanager
def timed(db: Database, *, run_id: str, stage: str, details=None):
    """Record one event for the enclosed block; the yielded dict becomes the event details."""
    info = {} if details is None else dict(details)
    started_at = now_iso()
    clock = time.monotonic()
    try:
        yield info
    except BaseException:
        info.setdefault("outcome", "error")
        raise
    finally:
        elapsed = int(round((time.monotonic() - clock) * 1000))
        info.setdefault("outcome", "ok")
        record_event(db, run_id=run_id, stage=stage, elapsed_ms=elapsed, details=info, started_at=started_at)


__all__ = ["STAGES", "events", "record_event", "summarize", "timed"]
