"""Bounded first-response discovery for low-frequency research data."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
import json
import time
from typing import Any, Callable

import pandas as pd

from app.data_platform.db import _conn
from app.data_platform.tushare_client import (
    configured_tushare_probe_endpoints,
    create_tushare_pro_for_endpoint,
    load_tushare_token_from_env,
)


Validator = Callable[[pd.DataFrame], bool]


def _ensure_table() -> None:
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS dc_research_provider_probe (
            dataset TEXT NOT NULL,
            target_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            probed_at TEXT NOT NULL,
            next_probe_at TEXT NOT NULL,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(dataset,target_key,provider)
        )""")


def _probe(
    provider: str,
    endpoint: str,
    api_name: str,
    params: dict[str, Any],
    validator: Validator,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        pro = create_tushare_pro_for_endpoint(
            load_tushare_token_from_env(), endpoint, timeout_seconds=timeout_seconds,
        )
        method = getattr(pro, api_name, None)
        frame = method(**params) if callable(method) else pro.query(api_name, **params)
        frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        available = bool(validator(frame))
        error = ""
    except Exception as exc:  # noqa: BLE001 - each provider is independent
        frame = pd.DataFrame()
        available = False
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    return {
        "provider": provider, "endpoint": endpoint,
        "status": "available" if available else "not_published" if not error else "probe_failed",
        "available": available, "rows": int(len(frame.index)), "frame": frame,
        "latency_ms": max(0, int((time.monotonic() - started) * 1000)), "error": error,
    }


def _record(dataset: str, target_key: str, result: dict[str, Any], *, cooldown_seconds: int) -> None:
    observed = datetime.now().astimezone()
    delay = 86_400 if result.get("available") else max(60, int(cooldown_seconds))
    evidence = {
        key: result.get(key) for key in ("endpoint", "rows", "error")
    }
    evidence["contract"] = "alis_research_first_source_probe_v1"
    with _conn() as conn:
        conn.execute(
            """INSERT INTO dc_research_provider_probe(dataset,target_key,provider,status,probed_at,next_probe_at,latency_ms,evidence_json)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(dataset,target_key,provider) DO UPDATE SET
               status=excluded.status,probed_at=excluded.probed_at,next_probe_at=excluded.next_probe_at,
               latency_ms=excluded.latency_ms,evidence_json=excluded.evidence_json""",
            (
                dataset, target_key, str(result.get("provider") or ""), str(result.get("status") or "probe_failed"),
                observed.isoformat(timespec="seconds"),
                (observed + timedelta(seconds=delay)).isoformat(timespec="seconds"),
                int(result.get("latency_ms") or 0), json.dumps(evidence, ensure_ascii=False, default=str),
            ),
        )


def _eligible_endpoints(dataset: str, target_key: str) -> list[tuple[str, str]]:
    _ensure_table()
    endpoints = list(configured_tushare_probe_endpoints())
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT provider,next_probe_at,status FROM dc_research_provider_probe WHERE dataset=? AND target_key=?",
            (dataset, target_key),
        ).fetchall()
    state = {str(dict(row).get("provider") or ""): dict(row) for row in rows}
    return [
        item for item in endpoints
        if item[0] not in state or str(state[item[0]].get("next_probe_at") or "") <= now
    ]


def race_tushare_sources(
    *,
    dataset: str,
    target_key: str,
    api_name: str,
    params: dict[str, Any],
    validator: Validator,
    timeout_seconds: float = 12.0,
    cooldown_seconds: int = 600,
) -> dict[str, Any]:
    """Return the first valid frame; every provider has its own hard timeout."""
    endpoints = _eligible_endpoints(dataset, target_key)
    if not endpoints:
        return {
            "ok": False, "status": "probe_cooldown", "dataset": dataset,
            "target_key": target_key, "completed": [],
        }
    executor = ThreadPoolExecutor(max_workers=len(endpoints), thread_name_prefix=f"research-probe-{dataset}")
    futures = {
        executor.submit(_probe, provider, endpoint, api_name, params, validator, timeout_seconds): provider
        for provider, endpoint in endpoints
    }
    pending = set(futures)
    completed: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    try:
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            batch = [future.result() for future in done]
            for result in batch:
                _record(dataset, target_key, result, cooldown_seconds=cooldown_seconds)
                completed.append({key: value for key, value in result.items() if key != "frame"})
            available = sorted(
                (item for item in batch if item.get("available")),
                key=lambda item: (int(item.get("latency_ms") or 0), str(item.get("provider") or "")),
            )
            if available:
                winner = available[0]
                break
    finally:
        executor.shutdown(wait=winner is None, cancel_futures=True)
    return {
        "ok": winner is not None,
        "status": "available" if winner is not None else "not_published",
        "dataset": dataset, "target_key": target_key,
        "winner": str((winner or {}).get("provider") or ""),
        "endpoint": str((winner or {}).get("endpoint") or ""),
        "frame": (winner or {}).get("frame") if winner is not None else pd.DataFrame(),
        "completed": completed,
        "pending_providers": [futures[future] for future in pending],
        "probe_contract": "alis_research_first_source_probe_v1",
    }
