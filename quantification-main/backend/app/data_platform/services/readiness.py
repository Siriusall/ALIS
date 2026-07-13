from __future__ import annotations

from datetime import date, datetime, timedelta
import threading
import time
from typing import Any

from app.data_platform.contracts import CONTRACTS
from app.data_platform.db import _conn
from app.data_platform.redis_store import get_json, set_json

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "value": None}
_CACHE_TTL = 30.0


def _value(row: Any, key: str, default: Any = 0) -> Any:
    return dict(row).get(key, default) if row is not None else default


def _metrics() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    today = date.today().strftime("%Y%m%d")
    window_start = (date.today() - timedelta(days=730)).strftime("%Y%m%d")
    calendar_end = (date.today() + timedelta(days=180)).strftime("%Y%m%d")
    calendar_expected = (date.today() + timedelta(days=180) - (date.today() - timedelta(days=730))).days + 1
    with _conn() as c:
        latest = str(_value(c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone(), "value", ""))
        stock_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_stock_master").fetchone(), "n"))
        snap = c.execute(
            """SELECT COUNT(*) n,
            SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) daily_n,
            SUM(CASE WHEN total_mv IS NOT NULL THEN 1 ELSE 0 END) valuation_n,
            SUM(CASE WHEN up_limit IS NOT NULL AND down_limit IS NOT NULL THEN 1 ELSE 0 END) limit_n
            FROM dc_stock_daily_snapshot WHERE trade_date=?""", (latest,),
        ).fetchone()
        symbol_apis = ("income", "balancesheet", "cashflow", "fina_indicator", "suspend_d", "namechange")
        raw = {}
        for api in symbol_apis:
            raw[api] = dict(c.execute(
                "SELECT COUNT(DISTINCT ts_code) symbols FROM dc_tushare_raw_records WHERE api_name=?", (api,)
            ).fetchone())
        raw_window = {}
        for api in ("daily", "adj_factor", "suspend_d", "stk_limit"):
            raw_window[api] = dict(c.execute(
                "SELECT COUNT(DISTINCT trade_date) dates FROM dc_tushare_raw_records WHERE api_name=? AND trade_date BETWEEN ? AND ?",
                (api, window_start, today),
            ).fetchone())
        open_dates = int(_value(c.execute(
            "SELECT COUNT(*) n FROM dc_trade_calendar WHERE is_open=1 AND cal_date BETWEEN ? AND ?", (window_start, today)
        ).fetchone(), "n"))
        calendar_rows = int(_value(c.execute(
            "SELECT COUNT(*) n FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date BETWEEN ? AND ?", (window_start, calendar_end)
        ).fetchone(), "n"))
        history_dates = int(_value(c.execute(
            "SELECT COUNT(DISTINCT trade_date) n FROM dc_stock_daily_snapshot WHERE trade_date BETWEEN ? AND ?", (window_start, today)
        ).fetchone(), "n"))
        announcement_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_announcement_raw").fetchone(), "n"))
        latest_ann = str(_value(c.execute("SELECT MAX(publish_time) value FROM dc_announcement_raw").fetchone(), "value", ""))

    n = int(_value(snap, "n"))
    ratio = lambda count, total: min(1.0, count / total) if total else 0.0
    financial_symbols = len(set().union(*[
        set() if api not in raw else {int(raw[api].get("symbols") or 0)}
        for api in ("income", "balancesheet", "cashflow", "fina_indicator")
    ]))
    # Above set is only used to avoid claiming row counts as symbol coverage; use the maximum observed symbol count.
    financial_symbols = max([int(raw.get(api, {}).get("symbols") or 0) for api in ("income", "balancesheet", "cashflow", "fina_indicator")] or [0])
    ann_fresh = False
    try:
        ann_fresh = bool(latest_ann) and (datetime.now() - datetime.fromisoformat(latest_ann)).days <= 7
    except ValueError:
        pass
    values = {
        "trade_calendar": (ratio(calendar_rows, calendar_expected), calendar_rows, calendar_expected, f"自然日日历 {calendar_rows}/{calendar_expected}"),
        "latest_daily": (ratio(int(_value(snap, "daily_n")), stock_count), int(_value(snap, "daily_n")), stock_count, f"最新日期 {latest}"),
        "latest_valuation": (ratio(int(_value(snap, "valuation_n")), stock_count), int(_value(snap, "valuation_n")), stock_count, "总市值字段覆盖"),
        "limit_price": (ratio(int(_value(snap, "limit_n")), stock_count), int(_value(snap, "limit_n")), stock_count, "涨跌停上下限同时存在"),
        "suspension": (ratio(int(raw.get("suspend_d", {}).get("symbols") or 0), stock_count), int(raw.get("suspend_d", {}).get("symbols") or 0), stock_count, "独立停复牌历史接口"),
        "stock_master": (ratio(stock_count, max(stock_count, n)), stock_count, max(stock_count, n), "上市股票主数据"),
        "history_calendar": (ratio(history_dates, open_dates), history_dates, open_dates, "当前数据库历史区间"),
        "history_daily": (ratio(int(raw_window.get("daily", {}).get("dates") or 0), open_dates), int(raw_window.get("daily", {}).get("dates") or 0), open_dates, "两年窗口日线日期 / 已知交易日"),
        "adj_factor": (ratio(int(raw_window.get("adj_factor", {}).get("dates") or 0), open_dates), int(raw_window.get("adj_factor", {}).get("dates") or 0), open_dates, "两年窗口复权因子日期覆盖"),
        "history_suspension": (ratio(int(raw_window.get("suspend_d", {}).get("dates") or 0), open_dates), int(raw_window.get("suspend_d", {}).get("dates") or 0), open_dates, "两年窗口停复牌日期覆盖"),
        "history_limit": (ratio(int(raw_window.get("stk_limit", {}).get("dates") or 0), open_dates), int(raw_window.get("stk_limit", {}).get("dates") or 0), open_dates, "两年窗口涨跌停日期覆盖"),
        "stock_history": (ratio(int(raw.get("namechange", {}).get("symbols") or 0), stock_count), int(raw.get("namechange", {}).get("symbols") or 0), stock_count, "名称/ST 历史状态覆盖"),
        "financial": (ratio(financial_symbols, stock_count), financial_symbols, stock_count, "财务报表股票覆盖"),
        "financial_indicator": (ratio(int(raw.get("fina_indicator", {}).get("symbols") or 0), stock_count), int(raw.get("fina_indicator", {}).get("symbols") or 0), stock_count, "财务指标股票覆盖"),
        "announcements": (1.0 if ann_fresh and announcement_count else (0.5 if announcement_count else 0.0), announcement_count, announcement_count, f"最新公告 {latest_ann or '无'}"),
        "point_in_time": (0.0, 0, 1, "尚未完成 available_time 历史时点审计"),
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
