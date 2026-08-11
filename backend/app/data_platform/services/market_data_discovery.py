"""Lightweight publication discovery for close-data providers.

Discovery and bulk transfer are deliberately separate.  A probe requests one
stable instrument only.  The first provider returning the exact target date is
selected for the bulk slice; unavailable providers are cooled down in a
durable ledger so the minute scheduler does not hammer upstream APIs.
"""

from __future__ import annotations

import json
import math
import os
import statistics
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


def _adaptive_probe_interval_seconds(dataset: str, provider: str, now: datetime) -> int:
    """Tighten polling near this provider's recent publication time."""
    base = _probe_interval_seconds(now)
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT probed_at FROM dc_market_provider_probe
                   WHERE dataset=? AND provider=? AND status IN ('available','bulk_validated')
                   ORDER BY trade_date DESC LIMIT 20""",
                (dataset, provider),
            ).fetchall()
        minutes = []
        for row in rows:
            value = datetime.fromisoformat(str(dict(row).get("probed_at") or ""))
            minutes.append(value.hour * 60 + value.minute + value.second / 60)
        if len(minutes) < 5:
            return base
        expected = statistics.median(minutes)
        current = now.hour * 60 + now.minute + now.second / 60
        if expected - 5 <= current <= expected + 10:
            return min(base, 60)
        if current < expected - 15:
            return max(base, 300)
    except Exception:  # noqa: BLE001 - scheduling must remain fail-safe
        return base
    return base


def provider_performance(dataset: str, provider: str) -> dict[str, Any]:
    """Return a bounded historical ETA/reliability score for bulk hedging."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT bulk_latency_ms,coverage,success,failure_kind
               FROM dc_market_provider_performance WHERE dataset=? AND provider=?
               ORDER BY sample_date DESC LIMIT 20""",
            (dataset, provider),
        ).fetchall()
    samples = [dict(row) for row in rows]
    successes = [row for row in samples if int(row.get("success") or 0) == 1]
    latencies = [int(row.get("bulk_latency_ms") or 0) for row in successes if int(row.get("bulk_latency_ms") or 0) > 0]
    reliability = sum(int(row.get("success") or 0) for row in samples) / len(samples) if samples else .75
    coverage = statistics.mean(float(row.get("coverage") or 0) for row in successes) if successes else .8
    default_eta = int(os.getenv("MARKET_BULK_DEFAULT_ETA_MS", "12000"))
    eta_ms = int(statistics.median(latencies)) if len(latencies) >= 5 else default_eta
    p75_ms = int(sorted(latencies)[max(0, math.ceil(len(latencies) * .75) - 1)]) if len(latencies) >= 5 else default_eta
    consecutive_failures = 0
    for row in samples:
        if int(row.get("success") or 0):
            break
        consecutive_failures += 1
    with _conn() as conn:
        quality_row = conn.execute(
            """SELECT COUNT(DISTINCT trade_date) AS sessions,
                      COALESCE(SUM(mismatch_count),0) AS mismatches,
                      COALESCE(SUM(compared_count),0) AS compared
               FROM (SELECT * FROM dc_market_provider_field_audit
                     WHERE dataset=? AND fallback_provider=? ORDER BY trade_date DESC LIMIT 200) recent""",
            (dataset, provider),
        ).fetchone()
    quality = dict(quality_row) if quality_row else {}
    quality_sessions = int(quality.get("sessions") or 0)
    compared = int(quality.get("compared") or 0)
    mismatch_rate = int(quality.get("mismatches") or 0) / compared if compared else 0.0
    quality_factor = max(.5, 1.0 - mismatch_rate) if quality_sessions >= 5 else 1.0
    circuit_open = consecutive_failures >= int(os.getenv("MARKET_PROVIDER_CIRCUIT_FAILURES", "3"))
    score = p75_ms / max(.1, reliability * max(.5, coverage) * quality_factor)
    return {
        "samples": len(samples), "calibration_ready": len(successes) >= 5,
        "eta_ms": eta_ms, "p75_ms": p75_ms, "reliability": round(reliability, 4),
        "coverage": round(coverage, 4), "consecutive_failures": consecutive_failures,
        "quality_sessions": quality_sessions, "field_mismatch_rate": round(mismatch_rate, 6),
        "quality_factor": round(quality_factor, 4), "circuit_open": circuit_open,
        "completion_score": round(score, 2),
    }


def available_bulk_candidates(dataset: str, trade_date: str) -> list[dict[str, Any]]:
    """Rank all already-published providers by expected complete-slice time."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT provider,status,latency_ms,probed_at FROM dc_market_provider_probe
               WHERE dataset=? AND trade_date=? AND status IN ('available','bulk_validated')""",
            (dataset, trade_date),
        ).fetchall()
    ranked = []
    for raw in rows:
        row = dict(raw)
        perf = provider_performance(dataset, str(row["provider"]))
        if perf["circuit_open"] and str(row.get("status") or "") != "bulk_validated":
            continue
        ranked.append({**row, "performance": perf})
    return sorted(ranked, key=lambda item: (
        float(item["performance"]["completion_score"]), int(item.get("latency_ms") or 0), str(item["provider"])
    ))


def hedge_delay_seconds(dataset: str, provider: str) -> float:
    perf = provider_performance(dataset, provider)
    floor = float(os.getenv("MARKET_BULK_HEDGE_MIN_SECONDS", "3"))
    ceiling = float(os.getenv("MARKET_BULK_HEDGE_MAX_SECONDS", "20"))
    return max(floor, min(ceiling, perf["p75_ms"] / 1000 * .75))


def hedge_decision(
    dataset: str, primary_provider: str, candidate_provider: str, *, elapsed_ms: int,
    primary_progress: dict[str, Any] | None = None, force: bool = False,
) -> dict[str, Any]:
    """Compare remaining completion time instead of relying on a fixed timer alone."""
    primary = provider_performance(dataset, primary_provider)
    candidate = provider_performance(dataset, candidate_provider)
    progress = primary_progress or {}
    completed = int(progress.get("completed_batches") or 0)
    total = int(progress.get("total_batches") or 0)
    reported_eta = int(progress.get("eta_ms") or 0)
    primary_remaining = reported_eta if completed > 0 and total > completed and reported_eta > 0 else max(
        0, int(primary["p75_ms"]) - max(0, int(elapsed_ms))
    )
    candidate_eta = int(candidate["p75_ms"])
    advantage_ms = max(500, int(os.getenv("MARKET_BULK_HEDGE_ADVANTAGE_MS", "1500")))
    minimum_elapsed_ms = int(float(os.getenv("MARKET_BULK_HEDGE_MIN_SECONDS", "3")) * 1000)
    stalled = completed == 0 and elapsed_ms >= int(primary["p75_ms"] * .75)
    faster = candidate_eta + advantage_ms < primary_remaining
    start = bool(force or (elapsed_ms >= minimum_elapsed_ms and (stalled or faster)))
    return {
        "start": start, "reason": "primary_failed" if force else "primary_stalled" if stalled else
        "candidate_eta_advantage" if faster else "primary_progress_preferred",
        "primary_remaining_ms": primary_remaining, "candidate_eta_ms": candidate_eta,
        "required_advantage_ms": advantage_ms, "primary_progress": progress,
        "primary_performance": primary, "candidate_performance": candidate,
    }


def provider_calibration_status() -> list[dict[str, Any]]:
    return [
        {"dataset": dataset, "provider": provider, **provider_performance(dataset, provider)}
        for dataset, providers in DATASET_PROVIDERS.items() for provider in providers
    ]


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
                _iso(observed + timedelta(seconds=_adaptive_probe_interval_seconds(dataset, provider, observed))),
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
    eligible = [
        provider for provider in providers
        if provider not in state
        or str(state[provider].get("status") or "") == "available"
        or str(state[provider].get("next_probe_at") or "") <= now
    ]
    healthy = [provider for provider in eligible if not provider_performance(dataset, provider)["circuit_open"]]
    return healthy or eligible


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
    latency_ms: int = 0,
    coverage: float = 0,
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
        conn.execute(
            """INSERT INTO dc_market_provider_performance
               (dataset,provider,sample_date,probe_latency_ms,bulk_latency_ms,coverage,success,
                failure_kind,completed_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dataset,provider,sample_date) DO UPDATE SET
               bulk_latency_ms=excluded.bulk_latency_ms,coverage=excluded.coverage,
               success=excluded.success,failure_kind=excluded.failure_kind,
               completed_at=excluded.completed_at,evidence_json=excluded.evidence_json""",
            (
                dataset, provider, str(trade_date), 0, max(0, int(latency_ms)), max(0.0, min(1.0, float(coverage))),
                1 if ok else 0, "" if ok else str((evidence or {}).get("status") or "bulk_failed"),
                _iso(observed), json.dumps(evidence or {}, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )
