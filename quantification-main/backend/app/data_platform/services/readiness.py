from __future__ import annotations

from datetime import date, datetime, timedelta
import threading
import time
from typing import Any

from app.data_platform.contracts import CONTRACTS
from app.data_platform.db import _conn
from app.data_platform.redis_store import get_json, set_json
from app.data_platform.services.market_calendar import target_trade_date
from app.data_platform.services.assets import _report_periods

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "value": None}
_CACHE_TTL = 30.0


def _value(row: Any, key: str, default: Any = 0) -> Any:
    if row is None:
        return default
    value = dict(row).get(key, default)
    return default if value is None else value


def _latest_usable_snapshot_date(c: Any, stock_count: int) -> str:
    threshold = max(1, int(stock_count * 0.8)) if stock_count else 1
    row = c.execute(
        """
        SELECT trade_date value
        FROM dc_stock_daily_snapshot
        GROUP BY trade_date
        HAVING SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) >= ?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (threshold,),
    ).fetchone()
    usable = str(_value(row, "value", ""))
    if usable:
        return usable
    return str(_value(c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone(), "value", ""))


def _recent_report_periods(today: date) -> list[str]:
    """Use the same disclosed report-period window as the asset/backfill plan."""
    return _report_periods(today)


def _metrics() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    today_d = date.today()
    today = today_d.strftime("%Y%m%d")
    with _conn() as c:
        target = target_trade_date(c)
    target_d = date(int(target[:4]), int(target[4:6]), int(target[6:8]))
    window_start = (target_d - timedelta(days=730)).strftime("%Y%m%d")
    history_end = target
    # Only published dates through today are eligible. Future calendar rows
    # must not inflate the denominator and create a false gap.
    calendar_end = today
    calendar_expected = (date.today() - (date.today() - timedelta(days=730))).days + 1
    with _conn() as c:
        stock_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_stock_master").fetchone(), "n"))
        latest = target
        snap = c.execute(
            """SELECT COUNT(*) n,
            SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) daily_n,
            SUM(CASE WHEN total_mv IS NOT NULL THEN 1 ELSE 0 END) valuation_n,
            SUM(CASE WHEN up_limit IS NOT NULL AND down_limit IS NOT NULL THEN 1 ELSE 0 END) limit_n
            FROM dc_stock_daily_snapshot WHERE trade_date=?""", (latest,),
        ).fetchone()
        raw_window = {}
        for api in ("daily", "adj_factor", "suspend_d", "stk_limit"):
            raw_window[api] = dict(c.execute(
                "SELECT COUNT(DISTINCT trade_date) dates FROM dc_tushare_raw_records WHERE api_name=? AND trade_date BETWEEN ? AND ?",
                (api, window_start, history_end),
            ).fetchone())
        report_periods = _recent_report_periods(date.today())
        financial_pair_coverage: dict[str, tuple[int, int]] = {}
        financial_expected = stock_count * len(report_periods)
        for api in ("income", "balancesheet", "cashflow", "fina_indicator"):
            covered_symbols = int(_value(c.execute(
                """
                SELECT COUNT(DISTINCT ts_code) n
                FROM (
                    SELECT ts_code
                    FROM dc_tushare_raw_records
                    WHERE api_name=? AND period<>'' AND ts_code<>''
                    UNION
                    SELECT ts_code
                    FROM dc_tushare_request_coverage
                    WHERE api_name=? AND period<>'' AND ts_code<>''
                      AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')
                ) done
                """,
                (api, api),
            ).fetchone(), "n"))
            financial_pair_coverage[api] = (min(covered_symbols, stock_count) * len(report_periods), financial_expected)
        checked_symbols = {}
        checked_dates = {}
        for api in ("suspend_d", "namechange", "adj_factor"):
            checked_symbols[api] = int(_value(c.execute(
                "SELECT COUNT(DISTINCT ts_code) n FROM dc_tushare_request_coverage WHERE api_name=? AND result_status IN ('success','valid_empty','not_applicable','deferred_empty') AND ts_code<>''",
                (api,),
            ).fetchone(), "n"))
            checked_dates[api] = int(_value(c.execute(
                """SELECT COUNT(DISTINCT q.trade_date) n
                   FROM dc_tushare_request_coverage q
                   JOIN dc_trade_calendar cal ON cal.cal_date=q.trade_date
                   WHERE q.api_name=?
                     AND q.result_status IN ('success','valid_empty','not_applicable','deferred_empty')
                     AND q.trade_date BETWEEN ? AND ? AND cal.is_open=1""",
                (api, window_start, history_end),
            ).fetchone(), "n"))
        open_dates = int(_value(c.execute(
            "SELECT COUNT(*) n FROM dc_trade_calendar WHERE is_open=1 AND cal_date BETWEEN ? AND ?", (window_start, history_end)
        ).fetchone(), "n"))
        calendar_rows = int(_value(c.execute(
            "SELECT COUNT(*) n FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date BETWEEN ? AND ?", (window_start, calendar_end)
        ).fetchone(), "n"))
        history_dates = int(_value(c.execute(
            "SELECT COUNT(DISTINCT trade_date) n FROM dc_stock_daily_snapshot WHERE trade_date BETWEEN ? AND ?", (window_start, history_end)
        ).fetchone(), "n"))
        announcement_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_announcement_raw").fetchone(), "n"))
        latest_ann = str(_value(c.execute("SELECT MAX(publish_time) value FROM dc_announcement_raw").fetchone(), "value", ""))
        has_available_time = c.execute(
            """
            SELECT 1 n
            FROM dc_tushare_raw_records
            WHERE api_name IN ('income','balancesheet','cashflow','fina_indicator','daily','daily_basic','stk_limit')
              AND available_time <> ''
            LIMIT 1
            """
        ).fetchone() is not None

    n = int(_value(snap, "n"))
    ratio = lambda count, total: min(1.0, count / total) if total else 0.0
    financial_available, financial_expected = financial_pair_coverage.get("income", (0, 0))
    indicator_available, indicator_expected = financial_pair_coverage.get("fina_indicator", (0, 0))
    ann_fresh = False
    try:
        ann_fresh = bool(latest_ann) and (datetime.now() - datetime.fromisoformat(latest_ann)).days <= 7
    except ValueError:
        pass
    values = {
        "trade_calendar": (ratio(calendar_rows, calendar_expected), calendar_rows, calendar_expected, f"自然日日历 {calendar_rows}/{calendar_expected}"),
        "latest_daily": (ratio(int(_value(snap, "daily_n")), stock_count), int(_value(snap, "daily_n")), stock_count, f"目标交易日 {latest}"),
        "latest_valuation": (ratio(int(_value(snap, "valuation_n")), stock_count), int(_value(snap, "valuation_n")), stock_count, "总市值字段覆盖"),
        "limit_price": (ratio(int(_value(snap, "limit_n")), stock_count), int(_value(snap, "limit_n")), stock_count, "涨跌停上下限同时存在"),
        "suspension": (ratio(checked_dates["suspend_d"], open_dates), checked_dates["suspend_d"], open_dates, "独立停复牌历史接口请求日期覆盖（无停牌也算已检查）"),
        "stock_master": (ratio(stock_count, max(stock_count, n)), stock_count, max(stock_count, n), "上市股票主数据"),
        "history_calendar": (ratio(history_dates, open_dates), history_dates, open_dates, "当前数据库历史区间"),
        "history_daily": (ratio(int(raw_window.get("daily", {}).get("dates") or 0), open_dates), int(raw_window.get("daily", {}).get("dates") or 0), open_dates, "两年窗口日线日期 / 已知交易日"),
        "adj_factor": (ratio(max(int(raw_window.get("adj_factor", {}).get("dates") or 0), checked_dates["adj_factor"]), open_dates), max(int(raw_window.get("adj_factor", {}).get("dates") or 0), checked_dates["adj_factor"]), open_dates, "两年窗口复权因子日期覆盖"),
        "history_suspension": (ratio(checked_dates["suspend_d"], open_dates), checked_dates["suspend_d"], open_dates, "两年窗口停复牌请求日期覆盖（无停牌也算已检查）"),
        "history_limit": (ratio(int(raw_window.get("stk_limit", {}).get("dates") or 0), open_dates), int(raw_window.get("stk_limit", {}).get("dates") or 0), open_dates, "两年窗口涨跌停日期覆盖"),
        "stock_history": (ratio(checked_symbols["namechange"], stock_count), checked_symbols["namechange"], stock_count, "名称/ST 历史接口请求覆盖（无变更也算已检查）"),
        "financial": (ratio(financial_available, financial_expected), financial_available, financial_expected, "财务报表股票×报告期覆盖"),
        "financial_indicator": (ratio(indicator_available, indicator_expected), indicator_available, indicator_expected, "财务指标股票×报告期覆盖"),
        "announcements": (1.0 if ann_fresh and announcement_count else (0.5 if announcement_count else 0.0), announcement_count, announcement_count, f"最新公告 {latest_ann or '无'}"),
        "point_in_time": (1.0 if has_available_time else 0.0, 1 if has_available_time else 0, 1, "核心原始记录已保存 available_time；严格未来函数审计另列质量项"),
    }
    return {key: {"coverage": round(v[0], 6), "available": v[1], "expected": v[2], "evidence": v[3]} for key, v in values.items()}, {
        "latest_trade_date": latest, "stock_count": stock_count, "known_open_dates": open_dates, "stored_history_dates": history_dates,
    }


def readiness_overview() -> dict[str, Any]:
    redis_cached = get_json("alis:data-platform:readiness:v2")
    if isinstance(redis_cached, dict):
        return redis_cached
    now = time.monotonic()
    with _cache_lock:
        if _cache["value"] is not None and now - float(_cache["at"]) < _CACHE_TTL:
            return _cache["value"]
    metrics, scope = _metrics()
    consumers = []
    for key, contract in CONTRACTS.items():
        requirements = []
        blocked = degraded = 0
        for metric_key, label, threshold, policy in contract["requirements"]:
            metric = metrics[metric_key]
            passed = metric["coverage"] >= threshold
            state = "ready" if passed else ("blocked" if policy == "block" else "degraded")
            blocked += int(state == "blocked")
            degraded += int(state == "degraded")
            requirements.append({"key": metric_key, "label": label, "threshold": threshold, "policy": policy,
                                 "status": state, **metric})
        status = "blocked" if blocked else ("degraded" if degraded else "ready")
        consumers.append({"key": key, "name": contract["name"], "description": contract["description"],
                          "status": status, "blocked_count": blocked, "degraded_count": degraded,
                          "requirements": requirements})
    payload = {"ok": True, "scope": scope, "consumers": consumers,
               "status_definition": {"ready": "满足正式运行要求", "degraded": "可运行但必须标注数据风险", "blocked": "关键数据不足，应禁止正式运行", "unknown": "尚无足够证据"}}
    with _cache_lock:
        _cache.update({"at": time.monotonic(), "value": payload})
    set_json("alis:data-platform:readiness:v2", payload, ttl=30)
    return payload
