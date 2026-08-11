"""Durable progress, cancellation and checkpoints for close-market bulk pulls."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.data_platform.db import _conn


CONTRACT = "alis_market_bulk_checkpoint_v1"


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="milliseconds")


def begin_execution(
    trade_date: str, provider: str, scope: str, total_batches: int, *, evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    now = _iso()
    with _conn() as conn:
        conn.execute(
            """DELETE FROM dc_market_bulk_batch_checkpoint
               WHERE trade_date=? AND provider=? AND scope=? AND batch_index>=?""",
            (trade_date, provider, scope, int(total_batches)),
        )
        existing = conn.execute(
            """SELECT started_at,status FROM dc_market_bulk_execution
               WHERE trade_date=? AND provider=? AND scope=?""",
            (trade_date, provider, scope),
        ).fetchone()
        started_at = str((dict(existing) if existing else {}).get("started_at") or now)
        conn.execute(
            """INSERT INTO dc_market_bulk_execution
               (trade_date,provider,scope,status,total_batches,completed_batches,rows_received,
                started_at,last_progress_at,finished_at,elapsed_ms,eta_ms,cancel_requested,evidence_json)
               VALUES(?,?,?,?,?,0,0,?,?,NULL,0,0,0,?)
               ON CONFLICT(trade_date,provider,scope) DO UPDATE SET
                status='running',total_batches=excluded.total_batches,last_progress_at=excluded.last_progress_at,
                finished_at=NULL,cancel_requested=0,evidence_json=excluded.evidence_json""",
            (trade_date, provider, scope, "running", int(total_batches), started_at, now,
             json.dumps({"contract": CONTRACT, **(evidence or {})}, ensure_ascii=False, separators=(",", ":"))),
        )
    return execution_status(trade_date, provider, scope)


def completed_batches(trade_date: str, provider: str, scope: str) -> dict[int, dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT batch_index,codes_hash,payload_json FROM dc_market_bulk_batch_checkpoint
               WHERE trade_date=? AND provider=? AND scope=? AND status='completed' ORDER BY batch_index""",
            (trade_date, provider, scope),
        ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        result[int(item["batch_index"])] = {"codes_hash": str(item["codes_hash"]), "payload": payload}
    return result


def record_batch(
    trade_date: str, provider: str, scope: str, batch_index: int, codes_hash: str,
    code_count: int, payload: dict[str, Any], elapsed_ms: int,
) -> dict[str, Any]:
    now = _iso()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO dc_market_bulk_batch_checkpoint
               (trade_date,provider,scope,batch_index,status,code_count,rows_received,codes_hash,payload_json,completed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date,provider,scope,batch_index) DO UPDATE SET
                status=excluded.status,code_count=excluded.code_count,rows_received=excluded.rows_received,
                codes_hash=excluded.codes_hash,payload_json=excluded.payload_json,completed_at=excluded.completed_at""",
            (trade_date, provider, scope, int(batch_index), "completed", int(code_count), len(payload), codes_hash,
             json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False), now),
        )
        totals = conn.execute(
            """SELECT COUNT(*) AS batches,COALESCE(SUM(rows_received),0) AS rows
               FROM dc_market_bulk_batch_checkpoint
               WHERE trade_date=? AND provider=? AND scope=? AND status='completed'""",
            (trade_date, provider, scope),
        ).fetchone()
        item = dict(totals) if totals else {}
        done = int(item.get("batches") or 0)
        rows = int(item.get("rows") or 0)
        total = conn.execute(
            """SELECT total_batches FROM dc_market_bulk_execution
               WHERE trade_date=? AND provider=? AND scope=?""",
            (trade_date, provider, scope),
        ).fetchone()
        total_batches = int((dict(total) if total else {}).get("total_batches") or 0)
        eta_ms = int(elapsed_ms / done * max(0, total_batches - done)) if done else 0
        conn.execute(
            """UPDATE dc_market_bulk_execution SET completed_batches=?,rows_received=?,
               last_progress_at=?,elapsed_ms=?,eta_ms=? WHERE trade_date=? AND provider=? AND scope=?""",
            (done, rows, now, max(0, int(elapsed_ms)), max(0, eta_ms), trade_date, provider, scope),
        )
    return execution_status(trade_date, provider, scope)


def request_cancel(trade_date: str, provider: str, scope: str, *, reason: str) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE dc_market_bulk_execution SET cancel_requested=1,status='cancelling',
               evidence_json=? WHERE trade_date=? AND provider=? AND scope=? AND status='running'""",
            (json.dumps({"contract": CONTRACT, "cancel_reason": reason}, ensure_ascii=False),
             trade_date, provider, scope),
        )


def cancel_requested(trade_date: str, provider: str, scope: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            """SELECT cancel_requested FROM dc_market_bulk_execution
               WHERE trade_date=? AND provider=? AND scope=?""", (trade_date, provider, scope),
        ).fetchone()
    return bool(int((dict(row) if row else {}).get("cancel_requested") or 0))


def finish_execution(
    trade_date: str, provider: str, scope: str, *, status: str, elapsed_ms: int, evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    with _conn() as conn:
        conn.execute(
            """UPDATE dc_market_bulk_execution SET status=?,finished_at=?,last_progress_at=?,elapsed_ms=?,eta_ms=0,
               completed_batches=CASE WHEN ?='validated' THEN total_batches ELSE completed_batches END,
               rows_received=CASE WHEN ?>0 THEN ? ELSE rows_received END,
               evidence_json=? WHERE trade_date=? AND provider=? AND scope=?""",
            (status, _iso(), _iso(), max(0, int(elapsed_ms)), status,
             int((evidence or {}).get("rows_received") or 0), int((evidence or {}).get("rows_received") or 0),
             json.dumps({"contract": CONTRACT, **(evidence or {})}, ensure_ascii=False, separators=(",", ":")),
             trade_date, provider, scope),
        )
    return execution_status(trade_date, provider, scope)


def execution_status(trade_date: str, provider: str, scope: str) -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM dc_market_bulk_execution WHERE trade_date=? AND provider=? AND scope=?""",
            (trade_date, provider, scope),
        ).fetchone()
    return dict(row) if row else {}


def recent_execution_status(trade_date: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT trade_date,provider,scope,status,total_batches,completed_batches,rows_received,
                      started_at,last_progress_at,finished_at,elapsed_ms,eta_ms,cancel_requested,evidence_json
               FROM dc_market_bulk_execution WHERE trade_date=? ORDER BY started_at,provider,scope""",
            (trade_date,),
        ).fetchall()
    return [dict(row) for row in rows]
