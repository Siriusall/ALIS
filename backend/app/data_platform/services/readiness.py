from __future__ import annotations

from datetime import date, datetime, timedelta
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.contracts import CONTRACTS
from app.data_platform.db import _read_conn
from app.data_platform.redis_store import delete as redis_delete, get_json, set_json
from app.data_platform.services.market_calendar import TradeCalendarNotReady, target_trade_date
from app.data_platform.services.assets import _report_periods
from app.data_platform.services.snapshot_health import latest_usable_snapshot_date

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "value": None}
_CACHE_TTL = 30.0
_refresh_lock = threading.Lock()
_refresh_state: dict[str, Any] = {"running": False, "last_started_at": "", "last_finished_at": "", "last_error": "", "last_result": None}
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _business_today() -> date:
    """Use the ALIS business date, independent of the host OS timezone."""
    return datetime.now(_BUSINESS_TZ).date()


def invalidate_readiness_cache() -> None:
    redis_delete("alis:data-platform:readiness:v2")
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def _value(row: Any, key: str, default: Any = 0) -> Any:
    if row is None:
        return default
    value = dict(row).get(key, default)
    return default if value is None else value


def _latest_usable_snapshot_date(c: Any, stock_count: int) -> str:
    return latest_usable_snapshot_date(c, stock_count)


def _recent_report_periods(today: date) -> list[str]:
    """Use the same disclosed report-period window as the asset/backfill plan."""
    return _report_periods(today)


def _metrics() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    today_d = _business_today()
    today = today_d.strftime("%Y%m%d")
    with _read_conn() as c:
        target = target_trade_date(c)
    target_d = date(int(target[:4]), int(target[4:6]), int(target[6:8]))
    window_start = (target_d - timedelta(days=730)).strftime("%Y%m%d")
    history_end = target
    # Only published dates through today are eligible. Future calendar rows
    # must not inflate the denominator and create a false gap.
    calendar_end = today
    calendar_expected = (today_d - (today_d - timedelta(days=730))).days + 1
    with _read_conn() as c:
        stock_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_stock_master").fetchone(), "n"))
        latest = target
        usable_latest = _latest_usable_snapshot_date(c, stock_count)
        snap = c.execute(
            """SELECT COUNT(*) n,
            SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) daily_n,
            SUM(CASE WHEN total_mv IS NOT NULL THEN 1 ELSE 0 END) valuation_n,
            SUM(CASE WHEN up_limit IS NOT NULL AND down_limit IS NOT NULL THEN 1 ELSE 0 END) limit_n
            FROM dc_stock_daily_snapshot WHERE trade_date=?""", (latest,),
        ).fetchone()
        raw_window = {}
        for api in ("daily", "adj_factor", "suspend_d", "stk_limit", "moneyflow"):
            raw_window[api] = dict(c.execute(
                """SELECT COUNT(DISTINCT trade_date) dates
                   FROM dc_tushare_request_coverage
                  WHERE api_name=? AND trade_date BETWEEN ? AND ?
                    AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')""",
                (api, window_start, history_end),
            ).fetchone())
        report_periods = _recent_report_periods(today_d)
        financial_pair_coverage: dict[str, tuple[int, int]] = {}
        financial_expected = stock_count * len(report_periods)
        period_placeholders = ",".join("?" for _ in report_periods)
        for api in ("income", "balancesheet", "cashflow", "fina_indicator"):
            covered_pairs = int(_value(c.execute(
                f"""
                SELECT COUNT(DISTINCT ts_code || ':' || period) n
                  FROM dc_tushare_request_coverage
                 WHERE api_name=? AND period IN ({period_placeholders}) AND ts_code<>''
                   AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')
                """,
                (api, *report_periods),
            ).fetchone(), "n"))
            financial_pair_coverage[api] = (min(covered_pairs, financial_expected), financial_expected)
        checked_symbols = {}
        checked_dates = {}
        for api in ("suspend_d", "namechange", "adj_factor", "moneyflow"):
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
            "SELECT COUNT(*) n FROM dc_available_trade_dates WHERE trade_date BETWEEN ? AND ?", (window_start, history_end)
        ).fetchone(), "n"))
        theme_snapshot_dates = int(_value(c.execute(
            """
            SELECT COUNT(DISTINCT as_of_date) n
            FROM dc_theme_snapshot
            WHERE as_of_date IN (
                SELECT cal_date FROM dc_trade_calendar
                WHERE is_open=1 AND cal_date<=?
                ORDER BY cal_date DESC LIMIT 250
            )
            """,
            (usable_latest or history_end,),
        ).fetchone(), "n"))
        announcement_count = int(_value(c.execute("SELECT COUNT(*) n FROM dc_announcement_raw").fetchone(), "n"))
        latest_ann = str(_value(c.execute("SELECT MAX(publish_time) value FROM dc_announcement_raw").fetchone(), "value", ""))
        # A current-row ``available_time`` is not historical PIT evidence. The
        # readiness card is only green when the observation-clock migration has
        # retained at least one real version; missing migration/evidence is an
        # explicit unknown/blocked state rather than a legacy success.
        try:
            has_available_time = c.execute(
                """
                SELECT 1 n
                FROM dc_tushare_raw_record_version
                WHERE api_name IN ('income','balancesheet','cashflow','fina_indicator','daily','daily_basic','stk_limit')
                  AND observed_at <> '' AND first_observed_at <> ''
                  AND content_hash <> ''
                  AND LOWER(COALESCE(observation_provenance,'')) NOT LIKE 'legacy_%'
                LIMIT 1
                """
            ).fetchone() is not None
        except Exception:
            has_available_time = False
        pcf_latest_date = str(_value(c.execute(
            "SELECT MAX(trade_date) value FROM dc_etf_pcf_header WHERE trade_date<=?", (today,)
        ).fetchone(), "value", ""))
        pcf_latest = int(_value(c.execute(
            "SELECT COUNT(DISTINCT etf_code) n FROM dc_etf_pcf_header WHERE trade_date=?", (pcf_latest_date,)
        ).fetchone(), "n")) if pcf_latest_date else 0

    n = int(_value(snap, "n"))
    ratio = lambda count, total: min(1.0, count / total) if total else 0.0
    financial_available, financial_expected = financial_pair_coverage.get("income", (0, 0))
    indicator_available, indicator_expected = financial_pair_coverage.get("fina_indicator", (0, 0))
    ann_fresh = False
    try:
        ann_fresh = bool(latest_ann) and (datetime.now() - datetime.fromisoformat(latest_ann)).days <= 7
    except ValueError:
        pass
    from app.data_platform.services.data_center import get_vip_backfill_status
    vip = get_vip_backfill_status(end_date=history_end)
    # Collection coverage and research-field readiness are separate contracts.
    # Only active candidate/core names need the expensive field-level audit.
    from app.db.store import _conn as business_conn
    from app.data_platform.services.data_center import financial_research_completeness
    with business_conn() as bc:
        candidate_codes = [str(dict(row).get("ts_code") or "") for row in bc.execute(
            """SELECT DISTINCT ts_code FROM stock_pool_memberships
               WHERE pool_type IN ('candidate','core') AND status='active' ORDER BY ts_code"""
        ).fetchall()]
    candidate_field_states = [financial_research_completeness(code, period_limit=5) for code in candidate_codes]
    candidate_field_expected = sum(int(item.get("field_checks") or 0) for item in candidate_field_states)
    candidate_field_present = sum(int(item.get("present_field_checks") or 0) for item in candidate_field_states)
    candidate_field_complete = sum(1 for item in candidate_field_states if item.get("status") == "complete")
    from app.data_platform.services.exchange_etf_pcf import tracked_etf_codes
    from app.data_platform.services.oos_hard_data import audit_oos_hard_data
    pcf_expected = len(tracked_etf_codes())
    oos_hard = audit_oos_hard_data(end_date=history_end, include_dates=False)
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
        "moneyflow": (
            ratio(max(int(raw_window.get("moneyflow", {}).get("dates") or 0), checked_dates["moneyflow"]), open_dates),
            max(int(raw_window.get("moneyflow", {}).get("dates") or 0), checked_dates["moneyflow"]),
            open_dates,
            "两年窗口个股资金流请求日期覆盖",
        ),
        "financial": (ratio(financial_available, financial_expected), financial_available, financial_expected, "财务报表实际股票×报告期采集切片覆盖"),
        "financial_indicator": (ratio(indicator_available, indicator_expected), indicator_available, indicator_expected, "财务指标实际股票×报告期采集切片覆盖"),
        "candidate_financial_fields": (
            ratio(candidate_field_present, candidate_field_expected), candidate_field_present, candidate_field_expected,
            f"候选池与核心池研究字段 {candidate_field_present}/{candidate_field_expected}；完全通过 {candidate_field_complete}/{len(candidate_field_states)} 只",
        ),
        "announcements": (1.0 if ann_fresh and announcement_count else (0.5 if announcement_count else 0.0), announcement_count, announcement_count, f"最新公告 {latest_ann or '无'}"),
        "point_in_time": (1.0 if has_available_time else 0.0, 1 if has_available_time else 0, 1, "仅以不可变 observed_at 版本证明点时可用性；缺失证据即阻断"),
        "capital_structure": (
            float(vip["capital"]["coverage"]), int(vip["capital"]["completed_slices"]),
            int(vip["capital"]["expected_slices"]), "北向、两融和龙虎榜精确请求切片覆盖",
        ),
        "etf_history": (
            float(vip["etf_price_nav"]["coverage"]), int(vip["etf_price_nav"]["completed_slices"]),
            int(vip["etf_price_nav"]["expected_slices"]), "已上市ETF日线与NAV精确请求切片覆盖",
        ),
        "etf_pcf": (
            ratio(pcf_latest, pcf_expected) if pcf_latest_date >= history_end else 0.0,
            pcf_latest if pcf_latest_date >= history_end else 0, pcf_expected,
            f"当前已发布PCF {pcf_latest_date or '无'} 覆盖 {pcf_latest}/{pcf_expected}；行情目标日 {history_end}，盘前PCF可领先收盘行情一个交易日",
        ),
        "major_news": (
            float(vip["major_news"]["coverage"]), int(vip["major_news"]["completed_slices"]),
            int(vip["major_news"]["expected_slices"]), "九来源长篇新闻按来源×自然日切片覆盖",
        ),
        "theme_snapshots": (
            ratio(theme_snapshot_dates, 250), theme_snapshot_dates, 250,
            f"叙事状态转移模型要求最近250个可用行情交易日主题快照，当前 {theme_snapshot_dates}/250，截止 {usable_latest or history_end}",
        ),
        "oos_hard_data": (
            float(oos_hard["coverage"]), int(oos_hard["eligible_sessions"]), int(oos_hard["required_sessions"]),
            f"正式 Champion OOS 永久硬数据 {oos_hard['eligible_sessions']}/{oos_hard['required_sessions']} 个合格交易日",
        ),
    }
    return {key: {"coverage": round(v[0], 6), "available": v[1], "expected": v[2], "evidence": v[3]} for key, v in values.items()}, {
        "latest_trade_date": latest, "stock_count": stock_count, "known_open_dates": open_dates, "stored_history_dates": history_dates,
    }


def readiness_overview() -> dict[str, Any]:
    # Validate the authoritative exchange calendar before serving either a
    # Redis or process-cache snapshot.  A cached green readiness result must
    # never survive a calendar outage/bootstrap gap.
    try:
        with _read_conn() as c:
            target_trade_date(c)
    except TradeCalendarNotReady as exc:
        blocked = {
            "ok": False,
            "status": "blocked",
            "error_code": exc.code,
            "error": str(exc),
            "scope": {"target_trade_date": "", "calendar_status": "blocked"},
            "consumers": [{
                "key": "trade_calendar",
                "name": "交易日历",
                "description": "无法证明当前目标交易日，所有日期型数据消费者暂停",
                "status": "blocked",
                "blocked_count": 1,
                "degraded_count": 0,
                "requirements": [{
                    "key": "trade_calendar",
                    "label": "交易所交易日历",
                    "threshold": 1.0,
                    "status": "blocked",
                    "coverage": 0.0,
                    "available": 0,
                    "expected": 1,
                    "evidence": str(exc),
                }],
            }],
            "status_definition": {
                "ready": "满足正式运行要求",
                "degraded": "可运行但必须标注数据风险",
                "blocked": "关键数据不足，应禁止正式运行",
                "unknown": "尚无足够证据",
            },
        }
        _refresh_state.update({"last_result": blocked, "last_finished_at": datetime.now().isoformat(timespec="seconds"), "last_error": str(exc)})
        return blocked
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
    _refresh_state.update({"last_result": payload, "last_finished_at": datetime.now().isoformat(timespec="seconds"), "last_error": ""})
    return payload


def readiness_refresh_status() -> dict[str, Any]:
    cached = get_json("alis:data-platform:readiness:v2")
    return {
        "ok": True,
        "running": bool(_refresh_state.get("running")),
        "last_started_at": _refresh_state.get("last_started_at") or "",
        "last_finished_at": _refresh_state.get("last_finished_at") or "",
        "last_error": _refresh_state.get("last_error") or "",
        "has_cached_snapshot": isinstance(cached, dict) or isinstance(_refresh_state.get("last_result"), dict),
        "cached_consumers": len((cached or _refresh_state.get("last_result") or {}).get("consumers") or []) if isinstance(cached or _refresh_state.get("last_result"), dict) else 0,
    }


def start_readiness_refresh() -> dict[str, Any]:
    with _refresh_lock:
        if _refresh_state.get("running"):
            return {**readiness_refresh_status(), "started": False, "reason": "already_running"}
        _refresh_state.update({"running": True, "last_started_at": datetime.now().isoformat(timespec="seconds"), "last_error": ""})

    def worker() -> None:
        try:
            with _cache_lock:
                _cache.update({"at": 0.0, "value": None})
            result = readiness_overview()
            _refresh_state.update({"last_result": result, "last_finished_at": datetime.now().isoformat(timespec="seconds"), "last_error": ""})
        except Exception as exc:
            _refresh_state.update({"last_error": str(exc)[:500], "last_finished_at": datetime.now().isoformat(timespec="seconds")})
        finally:
            _refresh_state["running"] = False

    threading.Thread(target=worker, daemon=True, name="data-readiness-refresh").start()
    return {**readiness_refresh_status(), "started": True}
