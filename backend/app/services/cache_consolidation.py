from __future__ import annotations

"""PostgreSQL-only compatibility projections for retired market caches.

The previous all-in-one JSON cache combined independent local files and could
make an old or partial slice appear usable. The market data platform now owns
durable data, observation evidence, and readiness. This module remains only
for legacy read APIs and callers while enforcing an exact PostgreSQL session.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("quant.cache_consolidation")
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

_available_trade_dates_cache: tuple[float, List[str]] = (0.0, [])
_available_trade_dates_lock = threading.Lock()
_AVAILABLE_TRADE_DATES_TTL_SECONDS = 30.0

# Retained for the legacy read endpoint's bounded response contract.
MAX_SYMBOL_ROWS_PER_REQUEST = 50000

FACTOR_KEYS_FOR_COVERAGE = (
    "volume", "turnover", "change_pct", "momentum_5", "momentum_20", "volatility_20", "volume_ratio_5",
    "float_mv", "total_mv", "pe_ttm", "pb",
    "is_st", "is_limit_up", "is_limit_down", "is_suspended",
    "open", "high", "low", "close", "price", "turnover_rate",
)


def _now() -> datetime:
    return datetime.now(_BUSINESS_TZ)


def _normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip()
    if "." in value:
        value = value.split(".", 1)[0]
    lower = value.lower()
    for prefix in ("sh", "sz", "bj"):
        if lower.startswith(prefix):
            value = value[len(prefix):]
            break
    return value.zfill(6) if value.isdigit() and len(value) < 6 else value


def _normalize_ymd(value: str) -> str:
    normalized = str(value or "").strip().replace("-", "")
    return normalized if len(normalized) == 8 and normalized.isdigit() else ""


def compute_factor_coverage(data: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, float]]:
    """Return non-null field coverage for one or more exact-date projections."""
    result: Dict[str, Dict[str, float]] = {}
    for trade_date, per_symbol in data.items():
        if not per_symbol:
            continue
        total = len(per_symbol)
        counts: Dict[str, int] = {}
        for row in per_symbol.values():
            if not isinstance(row, dict):
                continue
            for key in FACTOR_KEYS_FOR_COVERAGE:
                if key in row and row[key] is not None:
                    counts[key] = counts.get(key, 0) + 1
        result[trade_date] = {key: counts.get(key, 0) / total for key in FACTOR_KEYS_FOR_COVERAGE}
    return result


def write_init_status(data: Dict[str, Any]) -> None:
    payload = dict(data or {})
    payload["updated_at"] = _now().isoformat(timespec="seconds")
    try:
        from app.data_platform.db import _conn

        with _conn() as conn:
            conn.execute(
                """INSERT INTO dc_app_state (state_key,payload_json,updated_at) VALUES (?,?,?)
                   ON CONFLICT(state_key) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                ("market_data_init_status", json.dumps(payload, ensure_ascii=False), payload["updated_at"]),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL init-status write failed: %s", str(exc)[:120])


def read_init_status() -> Dict[str, Any]:
    try:
        from app.data_platform.db import _conn

        with _conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM dc_app_state WHERE state_key=?", ("market_data_init_status",)
            ).fetchone()
        payload = json.loads(str(dict(row).get("payload_json") or "{}")) if row else {}
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL init-status read failed: %s", str(exc)[:120])
        return {}


def is_strict_first_load_enabled() -> bool:
    return str(os.getenv("INIT_STRICT_FIRST_LOAD", "1")).strip().lower() not in {"0", "false", "no", "off"}


def validate_first_load_quality(
    *,
    spot_count: int,
    snapshot_count: int,
    basic_info_count: int,
    merged_payload: Optional[Dict[str, Any]],
    trade_date: str,
) -> Dict[str, Any]:
    """Compatibility validator; ``merged_payload`` is an exact PG projection."""
    ymd = _normalize_ymd(trade_date)
    counts = (merged_payload or {}).get("counts") if isinstance(merged_payload, dict) else {}
    merged_rows = int((counts or {}).get("symbol_rows") or 0)
    coverage = (merged_payload or {}).get("factor_coverage") if isinstance(merged_payload, dict) else {}
    day_coverage = coverage.get(ymd) if isinstance(coverage, dict) else {}
    checks = {
        "spot_count": int(spot_count),
        "snapshot_count": int(snapshot_count),
        "basic_info_count": int(basic_info_count),
        "merged_rows": merged_rows,
    }
    errors: List[str] = []
    if spot_count <= 0:
        errors.append("spot_count=0，行情为空")
    if snapshot_count <= 0:
        errors.append("snapshot_count=0，规范快照为空")
    if basic_info_count <= 0:
        errors.append("basic_info_count=0，基础信息为空")
    if merged_rows <= 0:
        errors.append("merged_rows=0，规范截面为空")
    for key in ("close", "volume", "change_pct"):
        value = float((day_coverage or {}).get(key, 0) or 0)
        checks[f"coverage_{key}"] = round(value, 4)
        if merged_rows > 0 and value < 0.95:
            errors.append(f"{key} 覆盖率过低 {value:.3f} < 0.95")
    return {"ok": not errors, "checks": checks, "errors": errors, "trade_date": ymd}


def _read_consolidated_postgres(date_ymd: str, symbols: Optional[List[str]]) -> Optional[Dict[str, Any]]:
    """Build a legacy-shaped response from one exact normalized snapshot."""
    if not date_ymd:
        return None
    try:
        from app.data_platform.db import _conn

        with _conn() as conn:
            rows = conn.execute(
                """SELECT symbol,name,close,change_pct,volume,turnover,total_mv,circ_mv,pe_ttm,pb,
                          turnover_rate,is_suspended,up_limit,down_limit,data_quality_score
                   FROM dc_stock_daily_snapshot
                   WHERE trade_date=?
                     AND COALESCE(observed_at,'')<>''
                     AND LOWER(COALESCE(observation_provenance,'')) NOT LIKE 'legacy_%'
                   ORDER BY symbol""",
                (date_ymd,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL exact snapshot read failed: %s", str(exc)[:160])
        return None
    wanted = {_normalize_symbol(symbol) for symbol in (symbols or []) if _normalize_symbol(symbol)}
    day: Dict[str, Dict[str, Any]] = {}
    for raw in rows:
        item = dict(raw)
        symbol = _normalize_symbol(str(item.get("symbol") or ""))
        if not symbol or (wanted and symbol not in wanted):
            continue
        close = item.get("close")
        day[symbol] = {
            "trade_date": date_ymd,
            "symbol": symbol,
            "name": str(item.get("name") or ""),
            "price": float(close) if close is not None else None,
            "close": float(close) if close is not None else None,
            "change_pct": item.get("change_pct"),
            "volume": item.get("volume"),
            "turnover": item.get("turnover"),
            "float_mv": item.get("circ_mv"),
            "total_mv": item.get("total_mv"),
            "pe_ttm": item.get("pe_ttm"),
            "pb": item.get("pb"),
            "turnover_rate": item.get("turnover_rate"),
            "is_suspended": bool(item.get("is_suspended")),
            "up_limit": item.get("up_limit"),
            "down_limit": item.get("down_limit"),
            "data_quality_score": item.get("data_quality_score"),
        }
    if not day:
        return None
    data = {date_ymd: day}
    return {
        "schema_version": "postgresql-exact-session-v1",
        "generated_at": _now().isoformat(timespec="seconds"),
        "trade_date": date_ymd,
        "counts": {"dates": 1, "symbol_rows": len(day)},
        "factor_coverage": compute_factor_coverage(data),
        "data": data,
    }


def read_consolidated_cache(date: Optional[str] = None, symbols: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Read one requested session only; no latest-date or filesystem fallback."""
    return _read_consolidated_postgres(_normalize_ymd(date or ""), symbols)


def has_consolidated_for_date(date_ymd: str) -> bool:
    return _read_consolidated_postgres(_normalize_ymd(date_ymd), None) is not None


def list_available_trade_dates() -> List[str]:
    """Return only dates with a durable normalized exact-session snapshot.

    ``dc_available_trade_dates`` is only the calendar index.  A date becomes
    usable when its accepted normalized projection still has a matching,
    auditable immutable version.  The exact content-hash join prevents a late
    projection rewrite from making a date look durable before its evidence is
    available.

    This is polled by read paths, so it deliberately uses an autocommit,
    server-read-only connection.  The matching partial index is installed by
    ``data_0013_strict_snapshot_idx``; it lets PostgreSQL reject legacy
    dates from a small index instead of probing the multi-gigabyte snapshot
    table for every calendar row.
    """
    global _available_trade_dates_cache
    now = time.monotonic()
    with _available_trade_dates_lock:
        cached_at, cached_dates = _available_trade_dates_cache
        if cached_dates and now - cached_at <= _AVAILABLE_TRADE_DATES_TTL_SECONDS:
            return list(cached_dates)
    try:
        from app.data_platform.db import _read_conn

        with _read_conn() as conn:
            rows = conn.execute(
                """SELECT available.trade_date
                   FROM dc_available_trade_dates AS available
                   WHERE EXISTS (
                       SELECT 1
                         FROM dc_stock_daily_snapshot AS snapshot
                         JOIN dc_stock_daily_snapshot_version AS versioned
                           ON versioned.ts_code=snapshot.ts_code
                          AND versioned.trade_date=snapshot.trade_date
                          AND versioned.content_hash=snapshot.content_hash
                        WHERE snapshot.trade_date=available.trade_date
                          AND snapshot.observed_at<>''
                          AND snapshot.content_hash<>''
                          AND LOWER(COALESCE(snapshot.observation_provenance,'')) NOT LIKE 'legacy_%'
                          AND versioned.observed_at<>''
                          AND versioned.content_hash<>''
                          AND LOWER(COALESCE(versioned.observation_provenance,'')) NOT LIKE 'legacy_%'
                   )
                   ORDER BY available.trade_date"""
            ).fetchall()
            dates = [
                value
                for value in (str(dict(row).get("trade_date") or "") for row in rows)
                if _normalize_ymd(value)
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("PostgreSQL available-session read failed: %s", str(exc)[:120])
        return []
    with _available_trade_dates_lock:
        _available_trade_dates_cache = (time.monotonic(), list(dates))
    return dates


def trigger_backfill_merge_meta_async(reason: str = "", force: bool = False) -> Dict[str, Any]:
    del force
    return {
        "started": False,
        "running": False,
        "storage": "postgresql",
        "reason": reason,
        "message": "Retired file-cache metadata is not used by PostgreSQL market data.",
    }


# These names remain as safe no-op compatibility adapters for inactive legacy
# operators. They must never read, write, or validate a market-data file cache.
def build_consolidated_cache_bundle(trade_date: Optional[str] = None) -> Dict[str, Any]:
    payload = read_consolidated_cache(trade_date)
    return payload or {
        "schema_version": "postgresql-exact-session-v1",
        "trade_date": _normalize_ymd(trade_date or ""),
        "counts": {"dates": 0, "symbol_rows": 0},
        "data": {},
        "error": "exact_normalized_snapshot_missing",
    }


def get_full_sync_status() -> Dict[str, Any]:
    return {"status": "not_required", "storage": "postgresql", "message": "File-cache full sync is retired."}


def run_full_sync_background(skip_existing: bool = True, cleanup_sources: bool = True) -> bool:
    del skip_existing, cleanup_sources
    return False


def cleanup_cache_temp_files() -> Dict[str, Any]:
    return {"removed": 0, "failed": 0, "files": [], "storage": "postgresql"}


def cleanup_source_cache_dirs() -> Dict[str, Any]:
    return {"removed_dirs": 0, "removed_files": 0, "failed": 0, "dirs": [], "storage": "postgresql"}


def read_latest_consolidated_cache() -> Optional[Dict[str, Any]]:
    return None


def backfill_merge_meta_for_existing_all_in_one(force: bool = False) -> Dict[str, int]:
    del force
    return {"updated": 0, "skipped": 0, "failed": 0}


def list_materialized_all_in_one_dates() -> List[str]:
    return []


def list_daily_cache_trade_dates() -> List[str]:
    return []
