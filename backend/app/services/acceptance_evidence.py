from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from app.data_platform.db import _conn
from app.services.advanced_controls import evaluate_slo
from app.services.backend_acceptance import backend_acceptance_matrix


_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")


def _ensure_tables() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS dc_slo_evaluations (
                id BIGSERIAL PRIMARY KEY,
                scope VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                success_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
                freshness_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
                p95_latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL,
                created_at VARCHAR(64) NOT NULL
            )"""
        )
        c.execute(
            """CREATE INDEX IF NOT EXISTS idx_dc_slo_evaluations_scope_created
               ON dc_slo_evaluations (scope, created_at DESC)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS dc_acceptance_runs (
                id BIGSERIAL PRIMARY KEY,
                suite VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL,
                passed_count INTEGER NOT NULL DEFAULT 0,
                partial_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL,
                created_at VARCHAR(64) NOT NULL
            )"""
        )
        c.execute(
            """CREATE INDEX IF NOT EXISTS idx_dc_acceptance_runs_suite_created
               ON dc_acceptance_runs (suite, created_at DESC)"""
        )


def evaluate_and_record_slo(
    samples: list[dict[str, Any]],
    *,
    scope: str = "model-strategy",
    freshness_seconds: float = 86400.0,
    success_target: float = .99,
) -> dict[str, Any]:
    _ensure_tables()
    result = evaluate_slo(samples, freshness_seconds=freshness_seconds, success_target=success_target)
    with _conn() as c:
        c.execute(
            """INSERT INTO dc_slo_evaluations
               (scope,status,sample_count,success_rate,freshness_rate,p95_latency_ms,result_json,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                scope[:128],
                str(result.get("status") or "unknown")[:32],
                int(result.get("sample_count") or 0),
                float(result.get("success_rate") or 0),
                float(result.get("freshness_rate") or 0),
                float(result.get("p95_latency_ms") or 0),
                json.dumps(result, ensure_ascii=False, default=str),
                _now(),
            ),
        )
    return {**result, "persisted": True, "scope": scope}


def list_slo_evaluations(*, scope: str | None = None, limit: int = 100) -> dict[str, Any]:
    _ensure_tables()
    where = "WHERE scope=?" if scope else ""
    params: tuple[Any, ...] = (scope[:128], max(1, min(limit, 500))) if scope else (max(1, min(limit, 500)),)
    with _conn() as c:
        rows = c.execute(
            f"""SELECT id,scope,status,sample_count,success_rate,freshness_rate,p95_latency_ms,result_json,created_at
                FROM dc_slo_evaluations {where}
                ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
    return {"ok": True, "items": [_format_json_row(dict(row), "result_json", "result") for row in rows]}


def run_and_record_backend_acceptance(*, suite: str = "plan-book-backend") -> dict[str, Any]:
    _ensure_tables()
    result = backend_acceptance_matrix()
    counts = result.get("counts") or {}
    with _conn() as c:
        c.execute(
            """INSERT INTO dc_acceptance_runs
               (suite,status,passed_count,partial_count,failed_count,result_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                suite[:128],
                str(result.get("status") or "unknown")[:32],
                int(counts.get("passed") or 0),
                int(counts.get("partial") or 0),
                int(counts.get("failed") or 0),
                json.dumps(result, ensure_ascii=False, default=str),
                _now(),
            ),
        )
    return {**result, "persisted": True, "suite": suite}


def list_backend_acceptance_runs(*, suite: str | None = None, limit: int = 50) -> dict[str, Any]:
    _ensure_tables()
    where = "WHERE suite=?" if suite else ""
    params: tuple[Any, ...] = (suite[:128], max(1, min(limit, 200))) if suite else (max(1, min(limit, 200)),)
    with _conn() as c:
        rows = c.execute(
            f"""SELECT id,suite,status,passed_count,partial_count,failed_count,result_json,created_at
                FROM dc_acceptance_runs {where}
                ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
    return {"ok": True, "items": [_format_json_row(dict(row), "result_json", "result") for row in rows]}


def _format_json_row(row: dict[str, Any], source_key: str, target_key: str) -> dict[str, Any]:
    raw = row.pop(source_key, "{}")
    try:
        row[target_key] = json.loads(raw or "{}")
    except Exception:
        row[target_key] = {}
    return row
