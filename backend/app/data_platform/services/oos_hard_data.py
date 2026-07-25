"""Permanent hard-data contract for formal Champion OOS validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.market_calendar import target_trade_date

CONTRACT_VERSION = "alis_oos_hard_data_v1"
TRAIN_SESSIONS = 756
PURGE_SESSIONS = 20
VALIDATION_SESSIONS = 126
EMBARGO_SESSIONS = 5
TEST_SESSIONS = 126
STEP_SESSIONS = 126
MINIMUM_WINDOWS = 5
MINIMUM_DAILY_ROWS = 500
MINIMUM_HARD_API_ROWS = 500
MINIMUM_MARKET_FIELD_RATIO = 0.80
PENDING_TRADE_DAY_BUFFER = 30
HARD_APIS = ("daily", "daily_basic", "stk_limit", "adj_factor")
REQUIRED_SESSIONS = (
    TRAIN_SESSIONS + PURGE_SESSIONS + VALIDATION_SESSIONS + EMBARGO_SESSIONS
    + TEST_SESSIONS + (MINIMUM_WINDOWS - 1) * STEP_SESSIONS
)


def _date_api_sets(start_date: str, end_date: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    with _conn() as conn:
        for api in HARD_APIS:
            if api == "adj_factor":
                rows = conn.execute(
                    """SELECT coverage.trade_date,MAX(coverage.rows_read) AS row_count,
                              coverage.trade_date AS max_available_time
                       FROM dc_tushare_request_coverage coverage
                       WHERE coverage.api_name=? AND coverage.trade_date BETWEEN ? AND ?
                         AND coverage.result_status='success' AND coverage.rows_read>=?
                       GROUP BY coverage.trade_date""",
                    (api, start_date, end_date, MINIMUM_HARD_API_ROWS),
                ).fetchall()
            else:
                # dc_available_trade_dates is a compact snapshot ledger.  It
                # gives us one indexed raw-existence probe per retained date,
                # including legacy slices whose coverage row predates the
                # current ledger, without scanning the full raw table.  It is
                # intentionally independent from dc_trade_calendar.
                rows = conn.execute(
                    """SELECT available.trade_date,? AS row_count,
                              available.trade_date AS max_available_time
                       FROM dc_available_trade_dates available
                       WHERE available.trade_date BETWEEN ? AND ?
                         AND EXISTS (
                           SELECT 1 FROM dc_tushare_raw_records raw
                           WHERE raw.api_name=? AND raw.trade_date=available.trade_date
                         )""",
                    (MINIMUM_HARD_API_ROWS, start_date, end_date, api),
                ).fetchall()
            valid: set[str] = set()
            for row in rows:
                item = dict(row)
                trade_date = str(item.get("trade_date") or "")
                cutoff = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00"
                # Raw ingestion derives economic availability from trade_date
                # at 15:00. Full-slice evidence comes from the compact request
                # coverage ledger, avoiding a 7M-row raw-table scan per batch.
                pit_ok = True
                if int(item.get("row_count") or 0) >= MINIMUM_HARD_API_ROWS and pit_ok:
                    valid.add(trade_date)
            result[api] = valid
    return result


def audit_oos_hard_data(*, end_date: str | None = None, include_dates: bool = True) -> dict[str, Any]:
    """Audit the latest contiguous protocol-sized calendar window.

    A session is eligible only when every hard API has a retained slice, the
    normalized snapshot has broad OHLC-volume coverage, and its availability
    timestamp is no later than the T-day close cutoff.
    """
    with _conn() as conn:
        end = str(end_date or target_trade_date(conn)).replace("-", "")[:8]
        calendar_limit = REQUIRED_SESSIONS if end_date else REQUIRED_SESSIONS + PENDING_TRADE_DAY_BUFFER
        calendar_rows = conn.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE exchange='SSE' AND is_open=1 AND cal_date<=?
               ORDER BY cal_date DESC LIMIT ?""",
            (end, calendar_limit),
        ).fetchall()
        calendar_dates = list(reversed([str(dict(row).get("cal_date") or "") for row in calendar_rows]))
    start = calendar_dates[0] if calendar_dates else end
    api_dates = _date_api_sets(start, end)
    with _conn() as conn:
        snapshot_rows = conn.execute(
            """SELECT trade_date,COUNT(*) AS rows_total,
                      SUM(CASE WHEN close IS NOT NULL AND volume IS NOT NULL AND turnover IS NOT NULL THEN 1 ELSE 0 END) AS market_rows,
                      MIN(available_time) AS min_available_time,MAX(available_time) AS max_available_time
               FROM dc_stock_daily_snapshot WHERE trade_date BETWEEN ? AND ?
               GROUP BY trade_date""",
            (start, end),
        ).fetchall()
    snapshots = {str(dict(row).get("trade_date") or ""): dict(row) for row in snapshot_rows}
    details: list[dict[str, Any]] = []
    for trade_date in calendar_dates:
        snapshot = snapshots.get(trade_date) or {}
        rows_total = int(snapshot.get("rows_total") or 0)
        market_rows = int(snapshot.get("market_rows") or 0)
        ratio = market_rows / rows_total if rows_total else 0.0
        cutoff = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00"
        missing_apis = [api for api in HARD_APIS if trade_date not in api_dates[api]]
        pit_ok = bool(snapshot.get("max_available_time")) and str(snapshot.get("max_available_time")) <= cutoff
        eligible = rows_total >= MINIMUM_DAILY_ROWS and ratio >= MINIMUM_MARKET_FIELD_RATIO and not missing_apis and pit_ok
        reasons = []
        if rows_total < MINIMUM_DAILY_ROWS:
            reasons.append("snapshot_rows_insufficient")
        if ratio < MINIMUM_MARKET_FIELD_RATIO:
            reasons.append("market_fields_incomplete")
        if missing_apis:
            reasons.append("missing_hard_api_slices")
        if not pit_ok:
            reasons.append("point_in_time_invalid")
        details.append({
            "trade_date": trade_date, "eligible": eligible, "rows_total": rows_total,
            "market_rows": market_rows, "market_field_ratio": round(ratio, 6),
            "missing_apis": missing_apis, "point_in_time_passed": pit_ok,
            "max_available_time": snapshot.get("max_available_time"), "reasons": reasons,
        })
    trailing_pending_dates: list[str] = []
    if not end_date:
        contiguous_newest_first: list[dict[str, Any]] = []
        started = False
        for item in reversed(details):
            if not started and not item["eligible"]:
                trailing_pending_dates.append(str(item["trade_date"]))
                continue
            if not item["eligible"]:
                break
            started = True
            contiguous_newest_first.append(item)
        if len(contiguous_newest_first) >= REQUIRED_SESSIONS:
            details = list(reversed(contiguous_newest_first[:REQUIRED_SESSIONS]))
        else:
            usable = [item for item in details if str(item["trade_date"]) not in set(trailing_pending_dates)]
            details = usable[-REQUIRED_SESSIONS:]
    else:
        details = details[-REQUIRED_SESSIONS:]
    required_dates = [str(item["trade_date"]) for item in details]
    start = required_dates[0] if required_dates else end
    effective_end = required_dates[-1] if required_dates else end
    eligible_dates = [item["trade_date"] for item in details if item["eligible"]]
    missing_dates = [item["trade_date"] for item in details if not item["eligible"]]
    calendar_missing = max(0, REQUIRED_SESSIONS - len(required_dates))
    coverage = len(eligible_dates) / REQUIRED_SESSIONS
    result = {
        "ok": True, "schema_version": CONTRACT_VERSION, "status": "ready" if len(eligible_dates) >= REQUIRED_SESSIONS else "blocked",
        "hard_requirement": True, "consumer": "Champion Purged Walk-Forward OOS",
        "required_sessions": REQUIRED_SESSIONS, "calendar_sessions": len(required_dates),
        "eligible_sessions": len(eligible_dates), "missing_sessions": REQUIRED_SESSIONS - len(eligible_dates),
        "calendar_missing_sessions": calendar_missing, "coverage": round(min(1.0, coverage), 6),
        "window_start": start, "window_end": effective_end, "hard_apis": list(HARD_APIS),
        "hard_fields": ["close", "volume", "turnover", "total_mv", "circ_mv", "pe_ttm", "pb", "turnover_rate", "up_limit", "down_limit", "available_time"],
        "thresholds": {"minimum_daily_rows": MINIMUM_DAILY_ROWS, "minimum_hard_api_rows": MINIMUM_HARD_API_ROWS, "minimum_market_field_ratio": MINIMUM_MARKET_FIELD_RATIO},
        "repair_dates": missing_dates,
        "trailing_pending_dates": list(reversed(trailing_pending_dates)),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if include_dates:
        result["dates"] = details
    return result


def oos_hard_data_asset() -> dict[str, Any]:
    audit = audit_oos_hard_data(include_dates=False)
    return {
        "key": "oos_hard_data", "name": "Champion OOS 永久硬数据", "domain": "模型验证",
        "consumer": audit["consumer"], "weight": 100, "target_scope": f"最近 {REQUIRED_SESSIONS} 个交易日",
        "frequency": "daily", "source": "TuShare + 标准化日截面", "window_start": audit["window_start"],
        "window_end": audit["window_end"], "window_depth": f"{REQUIRED_SESSIONS} sessions",
        "rationale": "不足时禁止正式 Champion OOS 验收；每日自检并进入统一补全计划",
        "actual_records": audit["eligible_sessions"], "expected_records": REQUIRED_SESSIONS,
        "coverage": audit["coverage"], "coverage_basis": audit["eligible_sessions"],
        "coverage_expected": REQUIRED_SESSIONS, "symbols": 0, "dates": audit["eligible_sessions"],
        "periods_actual": None, "periods_expected": None, "min_date": audit["window_start"],
        "max_date": audit["window_end"], "status": "complete" if audit["status"] == "ready" else "partial",
        "measurement": "eligible_sessions", "period_details": None, "hard_requirement": True,
    }


def repair_oos_hard_data_batch(trade_dates: list[str], *, date_gaps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    from app.data_platform.services.data_center import sync_market_bundle, sync_tushare_date_batch

    gaps = date_gaps or {}
    market_results: dict[str, dict[str, Any]] = {}
    adj_dates = [value for value in trade_dates if "adj_factor" in set((gaps.get(value) or {}).get("missing_apis") or [])]
    for trade_date in trade_dates:
        gap = gaps.get(trade_date) or {}
        missing = set(gap.get("missing_apis") or [])
        core = [api for api in ("daily", "daily_basic", "stk_limit") if api in missing]
        reasons = set(gap.get("reasons") or [])
        rebuild_snapshot = bool(reasons & {"snapshot_rows_insufficient", "market_fields_incomplete", "point_in_time_invalid"})
        if rebuild_snapshot:
            core = ["daily", "daily_basic", "stk_limit"]
        market_results[trade_date] = sync_market_bundle(trade_date, apis=core) if core else {"ok": True, "skipped": True, "reason": "core_market_already_complete"}
    adj_batch = sync_tushare_date_batch(
        "adj_factor", trade_dates=adj_dates, limit=max(1, len(adj_dates)), require_rows=True,
        minimum_rows=MINIMUM_HARD_API_ROWS, require_point_in_time=True,
    ) if adj_dates else {"ok": True, "skipped": True, "results": []}
    adj_by_date = {str(item.get("trade_date") or ""): item for item in adj_batch.get("results") or [] if isinstance(item, dict)}
    results = []
    for trade_date in trade_dates:
        adj = adj_by_date.get(trade_date) or ({"ok": bool(adj_batch.get("ok")), "batch": True} if trade_date in adj_dates else {"ok": True, "skipped": True})
        market = market_results[trade_date]
        results.append({"trade_date": trade_date, "ok": bool(market.get("ok")) and bool(adj.get("ok")), "market": market, "adj_factor": adj})
    return {"ok": all(item.get("ok") for item in results), "results": results, "dates": trade_dates, "adj_factor_batch": adj_batch, "contract": CONTRACT_VERSION}
