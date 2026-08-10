"""Lightweight publication discovery for close-data providers.

Discovery and bulk transfer are deliberately separate.  A probe requests one
stable instrument only.  The first provider returning the exact target date is
selected for the bulk slice; unavailable providers are cooled down in a
durable ledger so the minute scheduler does not hammer upstream APIs.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.eastmoney_index_daily_fallback import (
    fetch_eastmoney_hs300_bar,
    fetch_sina_hs300_bar,
)
from app.data_platform.services.tencent_market_snapshot import _fetch_batch
from app.data_platform.tushare_client import get_tushare_client


STOCK_PROBE_CODE = "600000.SH"
INDEX_PROBE_CODE = "000300.SH"
DATASET_PROVIDERS: dict[str, tuple[str, ...]] = {
    "daily": ("tencent_market_snapshot", "tushare"),
    "daily_basic": ("tencent_market_snapshot", "tushare"),
    "stk_limit": ("tencent_market_snapshot", "tushare"),
    "index_daily": ("eastmoney_index_daily_fallback", "sina_index_daily_fallback", "tushare"),
    "adj_factor": ("tushare",),
    "defensive_fund_daily": ("tencent_etf_quote", "tushare"),
}
_tencent_probe_lock = threading.Lock()
_tencent_probe_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _probe_interval_seconds(now: datetime) -> int:
    minutes = now.hour * 60 + now.minute
    if 15 * 60 <= minutes < 15 * 60 + 20:
        return 60
    if 15 * 60 + 20 <= minutes < 16 * 60:
        return 180
    return 600


def _tushare_probe(dataset: str, trade_date: str) -> dict[str, Any]:
    client = get_tushare_client()
    api_name = "fund_daily" if dataset == "defensive_fund_daily" else dataset
    method = getattr(client.pro, api_name)
    if dataset == "index_daily":
        frame = method(ts_code=INDEX_PROBE_CODE, start_date=trade_date, end_date=trade_date)
    elif dataset == "defensive_fund_daily":
        frame = method(ts_code="510300.SH", start_date=trade_date, end_date=trade_date)
    else:
        frame = method(ts_code=STOCK_PROBE_CODE, trade_date=trade_date)
    available = bool(frame is not None and not frame.empty)
    returned_date = ""
    if available and "trade_date" in frame.columns:
        returned_date = str(frame.iloc[0].get("trade_date") or "").replace("-", "")[:8]
        available = returned_date == trade_date
    return {"available": available, "returned_date": returned_date, "rows": int(len(frame.index)) if frame is not None else 0}


def _tencent_probe(dataset: str, trade_date: str) -> dict[str, Any]:
    with _tencent_probe_lock:
        cached = _tencent_probe_cache.get(trade_date)
        if cached and time.monotonic() - cached[0] <= 30.0:
            quotes = cached[1]
        else:
            quotes = _fetch_batch([STOCK_PROBE_CODE], trade_date)
            _tencent_probe_cache[trade_date] = (time.monotonic(), quotes)
    quote = quotes.get(STOCK_PROBE_CODE)
    if not quote:
        return {"available": False, "rows": 0}
    required = {
        "daily": ("open", "high", "low", "close", "vol", "amount"),
        "daily_basic": ("turnover_rate", "volume_ratio", "total_mv", "circ_mv"),
        "stk_limit": ("up_limit", "down_limit"),
    }[dataset]
    return {
        "available": all(quote.get(field) is not None for field in required),
        "rows": 1,
        "returned_date": str(quote.get("trade_date") or ""),
        "quote_time": str(quote.get("quote_time") or ""),
    }


def _index_probe(provider: str, trade_date: str) -> dict[str, Any]:
    if provider == "eastmoney_index_daily_fallback":
        bar = fetch_eastmoney_hs300_bar(trade_date)
    elif provider == "sina_index_daily_fallback":
        bar = fetch_sina_hs300_bar(trade_date)
    else:
        return _tushare_probe("index_daily", trade_date)
    return {
        "available": bool(bar and str(bar.get("trade_date") or "") == trade_date),
        "rows": 1 if bar else 0,
        "returned_date": str((bar or {}).get("trade_date") or ""),
        "close": (bar or {}).get("close"),
    }


def _tencent_etf_probe(trade_date: str) -> dict[str, Any]:
    from app.data_platform.services.tencent_etf_daily_fallback import fetch_tencent_etf_quotes

    quotes = fetch_tencent_etf_quotes(["510300.SH"], trade_date)
    quote = quotes.get("510300.SH")
    return {
        "available": bool(quote),
        "rows": 1 if quote else 0,
        "returned_date": str((quote or {}).get("trade_date") or ""),
        "quote_time": str((quote or {}).get("quote_time") or ""),
    }


def _run_probe(dataset: str, provider: str, trade_date: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        if provider == "tencent_market_snapshot":
            result = _tencent_probe(dataset, trade_date)
        elif provider == "tencent_etf_quote":
            result = _tencent_etf_probe(trade_date)
        elif dataset == "index_daily":
            result = _index_probe(provider, trade_date)
        else:
            result = _tushare_probe(dataset, trade_date)
        status = "available" if result.get("available") else "not_published"
        error = ""
    except Exception as exc:  # noqa: BLE001
        result = {"available": False}
        status = "probe_failed"
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    observed = _now()
    evidence = {**result, "error": error, "probe_contract": "alis_market_publication_probe_v1"}
    with _conn() as conn:
        conn.execute(
            """INSERT INTO dc_market_provider_probe
               (dataset,trade_date,provider,status,probed_at,next_probe_at,latency_ms,evidence_json)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(dataset,trade_date,provider) DO UPDATE SET
               status=excluded.status,probed_at=excluded.probed_at,next_probe_at=excluded.next_probe_at,
               latency_ms=excluded.latency_ms,evidence_json=excluded.evidence_json""",
            (
                dataset,
                trade_date,
                provider,
                status,
                _iso(observed),
                _iso(observed + timedelta(seconds=_probe_interval_seconds(observed))),
                latency_ms,
                json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )
    return {"dataset": dataset, "provider": provider, "status": status, "latency_ms": latency_ms, **evidence}


def _cached_available(dataset: str, trade_date: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT provider,status,probed_at,next_probe_at,latency_ms,evidence_json
               FROM dc_market_provider_probe
               WHERE dataset=? AND trade_date=? AND status='available'
               ORDER BY probed_at ASC,latency_ms ASC LIMIT 1""",
            (dataset, trade_date),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        evidence = json.loads(item.get("evidence_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {}
    return {"dataset": dataset, "provider": item["provider"], "status": "available", "cached": True, **evidence}


def _eligible_providers(dataset: str, trade_date: str) -> list[str]:
    providers = list(DATASET_PROVIDERS.get(dataset) or ())
    now = _iso(_now())
    with _conn() as conn:
        rows = conn.execute(
            """SELECT provider,next_probe_at,status FROM dc_market_provider_probe
               WHERE dataset=? AND trade_date=?""",
            (dataset, trade_date),
        ).fetchall()
    state = {str(dict(row).get("provider") or ""): dict(row) for row in rows}
    return [
        provider for provider in providers
        if provider not in state
        or str(state[provider].get("status") or "") == "available"
        or str(state[provider].get("next_probe_at") or "") <= now
    ]


def discover_first_published(dataset: str, trade_date: str) -> dict[str, Any]:
    """Probe all due providers concurrently and return the first available."""
    target = str(trade_date or "").replace("-", "")[:8]
    if dataset not in DATASET_PROVIDERS or len(target) != 8:
        return {"ok": False, "status": "invalid_scope", "dataset": dataset, "trade_date": target}
    cached = _cached_available(dataset, target)
    if cached:
        return {"ok": True, "winner": cached["provider"], "result": cached, "trade_date": target}
    providers = _eligible_providers(dataset, target)
    if not providers:
        return {"ok": False, "status": "probe_cooldown", "dataset": dataset, "trade_date": target}
    executor = ThreadPoolExecutor(max_workers=len(providers), thread_name_prefix=f"probe-{dataset}")
    futures = {executor.submit(_run_probe, dataset, provider, target): provider for provider in providers}
    pending = set(futures)
    results: list[dict[str, Any]] = []
    winner = ""
    try:
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            completed = []
            for future in done:
                result = future.result()
                results.append(result)
                completed.append(result)
            ready = sorted(
                (item for item in completed if item.get("status") == "available"),
                key=lambda item: (int(item.get("latency_ms") or 0), str(item.get("provider") or "")),
            )
            if ready:
                winner = str(ready[0]["provider"])
                break
    finally:
        executor.shutdown(wait=not winner)
    return {
        "ok": bool(winner),
        "status": "available" if winner else "not_published",
        "dataset": dataset,
        "trade_date": target,
        "winner": winner,
        "completed": results,
        "pending_probes": [futures[item] for item in pending],
        "probe_contract": "alis_market_publication_probe_v1",
    }


def discover_market_core(
    trade_date: str,
    datasets: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    selected = tuple(dataset for dataset in (datasets or ("daily", "daily_basic", "stk_limit"))
                     if dataset in {"daily", "daily_basic", "stk_limit"})
    if not selected:
        return {"ok": True, "trade_date": str(trade_date), "datasets": {}, "winners": {},
                "probe_contract": "alis_market_publication_probe_v1"}
    executor = ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="market-discovery")
    try:
        futures = {dataset: executor.submit(discover_first_published, dataset, trade_date) for dataset in selected}
        results = {dataset: future.result() for dataset, future in futures.items()}
    finally:
        executor.shutdown(wait=True)
    return {
        "ok": all(result.get("ok") for result in results.values()),
        "trade_date": str(trade_date),
        "datasets": results,
        "winners": {dataset: str(result.get("winner") or "") for dataset, result in results.items()},
        "probe_contract": "alis_market_publication_probe_v1",
    }


def mark_provider_bulk_result(
    dataset: str,
    trade_date: str,
    provider: str,
    *,
    ok: bool,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Close the probe→bulk loop so a bad winner can be bypassed next tick."""
    observed = _now()
    status = "bulk_validated" if ok else "bulk_failed"
    with _conn() as conn:
        conn.execute(
            """UPDATE dc_market_provider_probe
               SET status=?,probed_at=?,next_probe_at=?,evidence_json=?
               WHERE dataset=? AND trade_date=? AND provider=?""",
            (
                status,
                _iso(observed),
                _iso(observed + timedelta(seconds=600 if ok else 60)),
                json.dumps({
                    "bulk_ok": bool(ok),
                    "bulk_evidence": evidence or {},
                    "bulk_contract": "alis_market_first_publisher_bulk_v1",
                }, ensure_ascii=False, separators=(",", ":"), default=str),
                dataset,
                str(trade_date),
                provider,
            ),
        )
