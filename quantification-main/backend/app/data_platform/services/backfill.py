"""Historical backfill plan: turn audit-window gaps into an actionable checklist."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.assets import (
    DAILY_LOOKBACK_DAYS,
    FINANCIAL_ANNUAL_YEARS,
    FINANCIAL_QUARTERS,
    FALLBACK_OPEN_DAYS,
    _annual_periods,
    _report_periods,
    get_data_assets,
    invalidate_data_assets_cache,
)
from app.data_platform.services.data_center import (
    run_data_quality_checks,
    sync_cninfo_announcements,
    sync_financial_batch,
    sync_market_bundle,
    sync_pmi_industry_proxies,
    sync_trade_calendar,
    sync_tushare_api_raw,
    sync_tushare_date_batch,
    sync_tushare_stock_batch,
    sync_index_bundle,
    set_tushare_activity_hook,
    backfill_capital_structure_history,
    backfill_etf_constituents_history,
    backfill_etf_price_history,
    backfill_major_news_history,
    DEFER_EMPTY_AFTER_ATTEMPTS,
    get_vip_backfill_status,
)
from app.data_platform.services.market_calendar import target_trade_day
from app.data_platform.redis_store import ack_task, enqueue_task, get_json, set_json

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "phase": "idle", "message": "尚未生成计划"}
# Recoverable process/rate-limit failures must be finite. Permanent upstream
# failures are surfaced to the operator after this many automatic resumes.
AUTO_RESUME_MAX_ATTEMPTS = 5
AUTO_RESUME_DELAYS_SECONDS = (15, 30, 60, 120, 180)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _open_dates(start: str, end: str) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT cal_date FROM dc_trade_calendar
            WHERE is_open = 1 AND cal_date BETWEEN ? AND ?
            ORDER BY cal_date ASC
            """,
            (start, end),
        ).fetchall()
    return [str(dict(r)["cal_date"]) for r in rows]


def _calendar_row_count(start: str, end: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date BETWEEN ? AND ?",
            (start, end),
        ).fetchone()
    return int(dict(row).get("n") or 0)


def _raw_dates(api_name: str, start: str, end: str) -> set[str]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT DISTINCT trade_date FROM dc_tushare_raw_records
            WHERE api_name = ? AND trade_date <> '' AND trade_date BETWEEN ? AND ?
            """,
            (api_name, start, end),
        ).fetchall()
    return {str(dict(r)["trade_date"]) for r in rows}


def _financial_symbols(api_name: str, period: str) -> int:
    with _conn() as c:
        row = c.execute(
            """
            SELECT COUNT(DISTINCT ts_code) n FROM dc_tushare_raw_records
            WHERE api_name = ? AND period = ?
            """,
            (api_name, period),
        ).fetchone()
    return int(dict(row).get("n") or 0)


def _request_coverage_count(api_name: str, period: str = "") -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT ts_code) n FROM dc_tushare_request_coverage WHERE api_name=? AND period=? AND ts_code<>'' AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')",
            (api_name, period),
        ).fetchone()
    return int(dict(row).get("n") or 0)


def _financial_completion_count(api_name: str, period: str) -> int:
    """Count symbols completed by either raw rows or terminal request coverage."""
    with _conn() as c:
        row = c.execute(
            """
            SELECT COUNT(DISTINCT ts_code) n
            FROM (
                SELECT ts_code
                FROM dc_tushare_raw_records
                WHERE api_name=? AND period=? AND ts_code<>''
                UNION
                SELECT ts_code
                FROM dc_tushare_request_coverage
                WHERE api_name=? AND period=? AND ts_code<>''
                  AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')
            ) done
            """,
            (api_name, period, api_name, period),
        ).fetchone()
    return int(dict(row).get("n") or 0)


def _period_batch_checked(api_name: str, period: str) -> bool:
    """Whether a full-market period request completed successfully."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM dc_tushare_request_coverage WHERE api_name=? AND period=? AND ts_code='' AND result_status='success' LIMIT 1",
            (api_name, period),
        ).fetchone()
    return row is not None


def _request_date_coverage(api_name: str, start: str, end: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT trade_date) n FROM dc_tushare_request_coverage WHERE api_name=? AND trade_date BETWEEN ? AND ? AND result_status IN ('success','not_applicable','valid_empty','deferred_empty')",
            (api_name, start, end),
        ).fetchone()
    return int(dict(row).get("n") or 0)


def build_backfill_plan(*, market_batch_days: int = 30, financial_batch_size: int = 100) -> dict[str, Any]:
    """Build a prioritized historical completion plan from current gaps."""
    today = date.today()
    with _conn() as c:
        end = target_trade_day(c)
    start = end - timedelta(days=DAILY_LOOKBACK_DAYS)
    start_s, end_s = _ymd(start), _ymd(end)
    # TuShare currently publishes the exchange calendar through year-end.  Do
    # not manufacture a permanent gap by demanding next-year dates before the
    # upstream calendar has published them.
    cal_end = min(today + timedelta(days=180), date(today.year, 12, 31))
    quarters = _report_periods(today)
    annuals = _annual_periods(today)
    assets = get_data_assets()
    asset_status = {str(item.get("key") or ""): str(item.get("status") or "") for item in assets.get("items") or []}
    stocks = int(assets["scope"]["stocks"] or 0)
    open_days = _open_dates(start_s, end_s)
    # Align with assets.py: calendar is usable once window has >=200 open days;
    # FALLBACK_OPEN_DAYS (~500) is only the expected count when calendar is still thin.
    calendar_end_s = _ymd(cal_end)
    calendar_rows = _calendar_row_count(start_s, calendar_end_s)
    calendar_expected_rows = (cal_end - start).days + 1
    calendar_incomplete = calendar_rows < calendar_expected_rows
    open_days_expected = len(open_days) if not calendar_incomplete else FALLBACK_OPEN_DAYS

    have_daily = _raw_dates("daily", start_s, end_s)
    have_basic = _raw_dates("daily_basic", start_s, end_s)
    have_limit = _raw_dates("stk_limit", start_s, end_s)
    have_adj = _raw_dates("adj_factor", start_s, end_s)

    # Dates that exist in calendar but lack market bundles.
    missing_market = [d for d in open_days if d not in have_daily or d not in have_basic or d not in have_limit]
    missing_market_apis = {
        d: [api for api, dates in (("daily", have_daily), ("daily_basic", have_basic), ("stk_limit", have_limit)) if d not in dates]
        for d in missing_market
    }
    # If calendar itself is thin, plan still schedules calendar sync first;
    # market steps will expand after calendar is filled.
    estimated_market_days = FALLBACK_OPEN_DAYS - len(have_daily) if calendar_incomplete else len(missing_market)

    steps: list[dict[str, Any]] = []
    step_id = 0

    def add(step: dict[str, Any]) -> None:
        nonlocal step_id
        step_id += 1
        step["id"] = f"S{step_id:02d}"
        steps.append(step)

    # 1. Calendar — prerequisite for everything date-based.
    if calendar_incomplete:
        calendar_why = (
            f"两年历史至当前可用未来日历边界应有 {calendar_expected_rows} 个自然日日历记录，当前只有 {calendar_rows} 个；"
            f"缺少的日期可能导致调度和行情缺口判断失真"
        )
    else:
        calendar_why = (
            f"两年历史至当前可用未来日历边界 {calendar_expected_rows} 个自然日均有开闭市记录；"
            f"其中历史窗口有 {len(open_days)} 个开市日，可作为行情补全基准"
        )
    add(
        {
            "priority": 1,
            "key": "trade_calendar",
            "title": "补全交易日历",
            "why": calendar_why,
            "action": "sync_trade_calendar",
            "params": {"start_date": start_s, "end_date": calendar_end_s, "exchange": "SSE"},
            "estimated_calls": 1,
            "estimated_minutes": 0.5,
            "status": "needed" if calendar_incomplete else "done",
            "gap": {
                "have": calendar_rows,
                "need": calendar_expected_rows,
                "missing": max(0, calendar_expected_rows - calendar_rows),
            },
        }
    )

    # 2. Market daily bundles — day by day in batches.
    market_batches: list[list[str]] = []
    if missing_market:
        for i in range(0, len(missing_market), max(1, market_batch_days)):
            market_batches.append(missing_market[i : i + market_batch_days])
    elif calendar_incomplete:
        # Placeholder batch describing work after calendar expands.
        market_batches = []
        add(
            {
                "priority": 2,
                "key": "market_after_calendar",
                "title": "补全两年日频行情（待日历就绪后展开）",
                "why": "日历不足时无法枚举全部缺失交易日；先跑 S01，再重新生成计划",
                "action": "noop",
                "params": {},
                "estimated_calls": estimated_market_days * 3,
                "estimated_minutes": round(estimated_market_days * 0.2, 1),
                "status": "needed",
                "gap": {"have": len(have_daily), "need": FALLBACK_OPEN_DAYS, "missing": estimated_market_days},
            }
        )

    for idx, batch in enumerate(market_batches, 1):
        add(
            {
                "priority": 2,
                "key": f"market_batch_{idx}",
                "title": f"补全行情批次 {idx}/{len(market_batches)}",
                "why": f"补 daily + daily_basic + stk_limit，共 {len(batch)} 个交易日",
                "action": "sync_market_batch",
                "params": {"trade_dates": batch, "include_suspend": False, "missing_apis": {d: missing_market_apis[d] for d in batch}},
                "estimated_calls": len(batch) * 3,
                "estimated_minutes": round(len(batch) * 0.25, 1),
                "status": "needed",
                "gap": {"dates": batch, "count": len(batch)},
            }
        )

    # 3. Adj factor — TuShare officially supports one full-market request per
    # trade date.  Backfill by date, never by 5,000+ individual stocks.
    if open_days and len(have_adj) < len(open_days):
        missing_adj_dates = [trade_date for trade_date in open_days if trade_date not in have_adj]
        add(
            {
                "priority": 2,
                "key": "adj_factor",
                "title": "补全复权因子",
                "why": f"审计窗口内复权因子交易日覆盖 {len(have_adj)}/{len(open_days)}",
                "action": "sync_date_api_batch",
                "params": {"api_name": "adj_factor", "trade_dates": missing_adj_dates, "limit": market_batch_days, "require_rows": True},
                "estimated_calls": min(len(missing_adj_dates), market_batch_days),
                "estimated_minutes": round(min(len(missing_adj_dates), market_batch_days) * 0.2, 1),
                "status": "done" if open_days and len(have_adj) >= len(open_days) else "needed",
                "gap": {"have_dates": len(have_adj), "need_dates": len(open_days), "missing_dates": missing_adj_dates},
            }
        )

    # Strategy-specific VIP datasets are first-class assets. They must appear
    # in the same plan used by the UI, not in a hidden side workflow.
    vip = get_vip_backfill_status(end_date=end_s)
    vip_steps = (
        (
            "capital_structure_vip", "补全北向、两融与龙虎榜历史", "sync_capital_history_batch",
            vip["capital"],
            {"start_date": vip["scope"]["capital_start"], "end_date": end_s, "cursor": vip["capital"].get("resume_cursor", ""), "limit_dates": min(60, market_batch_days)},
        ),
        (
            "etf_price_nav_vip", "补全ETF日线与净值历史", "sync_etf_history_batch",
            vip["etf_price_nav"],
            {"start_date": vip["scope"]["capital_start"], "end_date": end_s, "cursor": vip["etf_price_nav"].get("resume_cursor", ""), "limit_funds": min(100, financial_batch_size)},
        ),
        (
            "etf_constituents_vip", "补全沪深ETF每日持仓组合", "sync_etf_constituents_batch",
            vip["etf_constituents"],
            {"start_date": vip["scope"]["capital_start"], "end_date": end_s, "cursor": vip["etf_constituents"].get("resume_cursor", ""), "limit_dates": min(60, market_batch_days)},
        ),
        (
            "major_news_vip", "补全九来源长篇新闻历史", "sync_major_news_history_batch",
            vip["major_news"],
            {"start_date": vip["scope"]["news_start"], "end_date": end_s, "cursor": vip["major_news"].get("resume_cursor", ""), "limit_days": min(31, market_batch_days)},
        ),
    )
    for key, title, action, contract, params in vip_steps:
        missing = int(contract.get("missing_slices") or 0)
        unsupported = key == "etf_constituents_vip" and not bool(
            vip.get("capabilities", {}).get("etf_constituents_supported", True)
        )
        add({
            "priority": 2,
            "key": key,
            "title": title,
            "why": (
                "当前VIP网关未提供etf_sh_cons/etf_sz_cons；已完成官方接口接入，待网关透传后可直接续跑"
                if unsupported else
                f"精确终态切片覆盖 {int(contract.get('completed_slices') or 0)}/{int(contract.get('expected_slices') or 0)}，缺失 {missing}"
            ),
            "action": action,
            "params": params,
            "estimated_calls": missing,
            "estimated_minutes": round(missing * 0.03, 1),
            "status": "blocked" if unsupported else ("done" if contract.get("complete") else "needed"),
            "gap": {
                "have": int(contract.get("completed_slices") or 0),
                "need": int(contract.get("expected_slices") or 0),
                "missing": missing,
                "coverage": float(contract.get("coverage") or 0),
            },
        })

    # 4. Core financial statements — VIP APIs fetch the whole report period.
    # Annual-only extras are deliberately separate: a successful VIP return
    # must not make audit opinions/main-business composition look complete.
    for period in quarters:
        # A successful empty response is still a completed audit for that
        # stock/period.  Use request coverage as well as returned rows so newly
        # listed/non-reporting stocks do not keep the plan partial forever.
        have = _financial_completion_count("income", period)
        if stocks > 0 and have < stocks:
            add(
                {
                    "priority": 3,
                    "key": f"financial_{period}",
                    "title": f"补全财务 {period}",
                    "why": f"核心四表股票覆盖 {have}/{stocks}；优先按报告期 VIP 整批获取，单接口失败才逐股补偿",
                    "action": "sync_financial_batch",
                    "params": {"period": period, "limit": financial_batch_size, "include_yearly": False, "completion_api": "core"},
                    "estimated_calls": 4,
                    "estimated_minutes": 1.0,
                    "status": "needed",
                    "gap": {"have_symbols": have, "need_symbols": stocks, "batch": financial_batch_size},
                }
            )

    for period in annuals:
        have = _financial_completion_count("fina_audit", period)
        if stocks > 0 and have < stocks:
            add(
                {
                    "priority": 3,
                    "key": f"annual_{period}",
                    "title": f"补全审计意见 {period}",
                    "why": "审计意见没有可用 VIP，按股票断点补全；主营构成由独立 VIP 批次处理",
                    "action": "sync_financial_batch",
                    "params": {"period": period, "limit": financial_batch_size, "include_yearly": True, "completion_api": "fina_audit"},
                    "estimated_calls": financial_batch_size,
                    "estimated_minutes": round(financial_batch_size * 0.1, 1),
                    "status": "needed",
                    "gap": {"have_symbols": have, "batch": financial_batch_size},
                    "estimated_calls": max(0, stocks - have),
                    "estimated_minutes": round(max(0, stocks - have) * 0.1, 1),
                }
            )

    # 4b. Additional TuShare APIs verified by real small-sample calls.
    latest_period = quarters[0] if quarters else ""
    stock_routes = [
        ("namechange", "补全名称/ST历史", ""),
        ("forecast", "补全业绩预告", latest_period),
        ("top10_holders", "补全十大股东", latest_period),
        ("top10_floatholders", "补全十大流通股东", latest_period),
        ("stk_holdernumber", "补全股东人数", latest_period),
        ("pledge_stat", "补全质押统计", ""),
        ("pledge_detail", "补全质押明细", ""),
        ("express", "补全业绩快报", latest_period),
    ]
    for period in annuals:
        stock_routes.append(("fina_mainbz", f"补全主营构成 {period}", period))
    for api_name, title, period in stock_routes:
        checked = _request_coverage_count(api_name, period)
        batch_checked = bool(period) and _period_batch_checked(api_name, period)
        remaining_symbols = max(0, stocks - checked)
        is_period_vip = api_name in {"forecast", "express", "fina_mainbz", "top10_holders", "top10_floatholders", "stk_holdernumber"} and bool(period)
        estimated_calls = 0 if batch_checked else (1 if is_period_vip else remaining_symbols)
        estimated_minutes = 0.0 if batch_checked else (1.0 if is_period_vip else round(remaining_symbols * 0.15, 1))
        add({
            "priority": 4,
            "key": f"stock_api_{api_name}_{period}" if api_name == "fina_mainbz" else f"stock_api_{api_name}",
            "title": title,
            "why": f"{api_name} 优先按报告期/日期窗批量获取，失败才逐股断点补偿；空结果也记录以避免重复请求",
            "action": "sync_stock_api_batch",
            "params": {"api_name": api_name, "period": period, "limit": financial_batch_size},
            "estimated_calls": estimated_calls, "estimated_minutes": estimated_minutes,
            "status": "done" if batch_checked or (stocks > 0 and checked >= stocks) else "needed",
            "gap": {"checked_symbols": checked, "need_symbols": stocks, "batch": financial_batch_size, "period_batch_checked": batch_checked},
        })
    for api_name, title in (("suspend_d", "补全停复牌检查"), ("moneyflow", "补全资金流")):
        checked_dates = _request_date_coverage(api_name, start_s, end_s)
        remaining_dates = max(0, len(open_days) - checked_dates)
        add({
            "priority": 4, "key": f"date_api_{api_name}", "title": title,
            "why": f"{api_name} 已通过真实接口验证；按交易日分批，成功空结果也记为已检查",
            "action": "sync_date_api_batch",
            "params": {"api_name": api_name, "trade_dates": open_days, "limit": market_batch_days},
            # Date requests are executed by the shared 16-worker pool. Keep
            # the call count exact, but estimate wall time by concurrent waves;
            # any TuShare cooldown is added dynamically by the worker.
            "estimated_calls": remaining_dates,
            "estimated_minutes": round((remaining_dates / 16) * 0.2, 1),
            "status": "done" if open_days and checked_dates >= len(open_days) else "needed",
            "gap": {"checked_dates": checked_dates, "need_dates": len(open_days), "batch": market_batch_days},
        })
    add({
        "priority": 4, "key": "index_core", "title": "补全沪深300指数与权重",
        "why": "index_daily 与 index_weight 已通过真实写入验证",
        "action": "sync_index_core", "params": {"index_code": "000300.SH", "start_date": start_s, "end_date": end_s},
        "estimated_calls": 3, "estimated_minutes": 1,
        "status": "done" if asset_status.get("industry_index") == "complete" and asset_status.get("index_weight") == "available" else "needed",
        "gap": {},
    })
    macro_apis = ("cn_gdp", "cn_cpi", "cn_pmi", "shibor", "fx_daily")
    with _conn() as c:
        macro_ready = sum(1 for api in macro_apis if c.execute(
            "SELECT 1 FROM dc_tushare_raw_records WHERE api_name=? LIMIT 1", (api,)
        ).fetchone())
    add({
        "priority": 4, "key": "macro_core", "title": "补全核心宏观序列",
        "why": "GDP、CPI、PMI、Shibor、美元离岸人民币已通过真实接口验证",
        "action": "sync_macro_core", "params": {"start_date": start_s, "end_date": end_s},
        "estimated_calls": 5, "estimated_minutes": 1,
        "status": "done" if macro_ready == len(macro_apis) else "needed",
        "gap": {"ready_series": macro_ready, "need_series": len(macro_apis)},
    })

    # 5. Announcements — recent window first, then expand in monthly chunks.
    announcement_start = _ymd(today - timedelta(days=30))
    with _conn() as c:
        announcement_count = int(dict(c.execute(
            "SELECT COUNT(*) n FROM dc_announcement_raw WHERE substr(publish_time, 1, 10) BETWEEN ? AND ?",
            ((today - timedelta(days=30)).isoformat(), today.isoformat()),
        ).fetchone()).get("n") or 0)
    add(
        {
            "priority": 4,
            "key": "announcements_recent",
            "title": "补全近 30 日公告",
            "why": "事件数据先保证近期可用，再向两年窗口延伸",
            "action": "sync_announcements",
            "params": {
                "start_date": announcement_start,
                "end_date": end_s,
                "page_size": 30,
                "max_pages": 10,
            },
            "estimated_calls": 10,
            "estimated_minutes": 3,
            "status": "done" if announcement_count > 0 else "needed",
            "gap": {"records": announcement_count},
        }
    )

    # 6. Quality check at the end.
    with _conn() as c:
        quality_row = c.execute(
            "SELECT MAX(checked_at) AS checked_at FROM dc_data_quality_checks"
        ).fetchone()
        coverage_row = c.execute(
            "SELECT MAX(checked_at) AS checked_at FROM dc_tushare_request_coverage"
        ).fetchone()
    latest_quality_at = str(dict(quality_row).get("checked_at") or "") if quality_row else ""
    latest_coverage_at = str(dict(coverage_row).get("checked_at") or "") if coverage_row else ""
    # A quality pass remains valid while no ingestion attempt has happened
    # after it.  Tying this only to `prior_work_needed` made quality appear
    # perpetually pending whenever one upstream asset was deliberately
    # deferred with a residual gap.
    quality_is_current = bool(
        latest_quality_at
        and latest_quality_at[:10] >= today.isoformat()
        and (not latest_coverage_at or latest_quality_at >= latest_coverage_at)
    )
    add(
        {
            "priority": 5,
            "key": "quality",
            "title": "运行质量检查",
            "why": "补数后复核快照与主数据覆盖",
            "action": "run_quality",
            "params": {"trade_date": sorted(have_daily)[-1] if have_daily else end_s},
            "estimated_calls": 0,
            "estimated_minutes": 0.2,
            "status": "done" if quality_is_current else "needed",
            "gap": {},
        }
    )

    asset_gaps = [
        {
            "key": item["key"], "name": item["name"], "domain": item["domain"],
            "status": item["status"], "coverage": item["coverage"],
            "window_start": item.get("window_start"), "window_end": item.get("window_end"),
            "window_depth": item.get("window_depth"), "coverage_basis": item.get("coverage_basis"),
            "coverage_expected": item.get("coverage_expected"),
        }
        for item in assets["items"]
        if item["status"] in {"partial", "missing"} and item.get("weight", 0) > 0
    ]

    # Surface every weighted asset gap, including those without a safe
    # automatic implementation. Hidden gaps are more dangerous than blocked
    # steps because they make the repair workflow look complete.
    covered_asset_keys = {
        "trade_calendar", "daily", "valuation", "limit_price", "snapshot", "adj_factor",
        "namechange", "suspension", "moneyflow", "top10_holders", "top10_floatholders",
        "stk_holdernumber", "pledge_stat", "express", "industry_index", "index_weight",
        "income", "balancesheet", "cashflow", "financial_indicator", "fina_audit",
        "fina_mainbz", "announcements", "macro",
        "capital_structure_vip", "etf_price_nav_vip", "major_news_vip",
        "vip_moneyflow_hsgt", "vip_hk_hold", "vip_margin", "vip_margin_detail",
        "vip_margin_secs", "vip_top_list", "vip_top_inst", "vip_fund_basic",
        "vip_fund_daily", "vip_fund_nav", "vip_etf_sh_cons", "vip_etf_sz_cons",
    }
    fallback_supplements = {"income", "balancesheet", "cashflow", "financial_indicator", "valuation"}
    for item in asset_gaps:
        key = str(item.get("key") or "")
        if key in covered_asset_keys or any(s.get("key") == f"uncovered_{key}" for s in steps):
            continue
        source = str(next((x.get("source") for x in assets["items"] if x.get("key") == key), "") or "")
        fallback = "网页/F10 可补部分字段，但不能替代标准结构化表" if key in fallback_supplements else "当前网页/OCR备用源未覆盖此标准数据集"
        add({
            "priority": 4,
            "key": f"uncovered_{key}",
            "title": f"待接入补全路线：{item.get('name') or key}",
            "why": f"自检发现 {item.get('status')}，主来源为 {source or '未配置'}；{fallback}",
            "action": "manual_or_fallback_required",
            "params": {"asset_key": key, "source": source, "fallback_status": "supplement_only" if key in fallback_supplements else "not_connected"},
            "estimated_calls": 0,
            "estimated_minutes": 0,
            "status": "blocked",
            "gap": {"coverage": item.get("coverage"), "window_start": item.get("window_start"), "window_end": item.get("window_end")},
        })

    needed = [s for s in steps if s["status"] == "needed"]
    blocked = [s for s in steps if s["status"] == "blocked"]
    done = [s for s in steps if s["status"] == "done"]
    total_minutes = round(sum(float(s.get("estimated_minutes") or 0) for s in needed), 1)

    return {
        "ok": True,
        "generated_at": _now(),
        "scope": {
            "market": "沪深A股",
            "start_date": start_s,
            "end_date": end_s,
            "stocks": stocks,
            "open_days_in_calendar": len(open_days),
            "daily_dates_have": len(have_daily),
            "daily_basic_dates_have": len(have_basic),
            "stk_limit_dates_have": len(have_limit),
            "adj_factor_dates_have": len(have_adj),
            "financial_quarters": quarters,
            "financial_annuals": annuals,
            "policy": {
                "daily_lookback_days": DAILY_LOOKBACK_DAYS,
                "financial_quarters": FINANCIAL_QUARTERS,
                "financial_annual_years": FINANCIAL_ANNUAL_YEARS,
                "market_batch_days": market_batch_days,
                "financial_batch_size": financial_batch_size,
            },
        },
        "summary": {
            "steps_total": len(steps),
            "steps_needed": len(needed),
            "steps_blocked": len(blocked),
            "steps_done": len(done),
            "estimated_minutes": total_minutes,
            "estimated_hours": round(total_minutes / 60, 2),
            "asset_gaps": len(asset_gaps),
            "next_action": needed[0]["title"] if needed else (blocked[0]["title"] if blocked else "无需补全"),
        },
        "asset_gaps": asset_gaps,
        "steps": steps,
        "execution": {
            "supported_actions": [
                "sync_trade_calendar",
                "sync_market_batch",
                "sync_financial_batch",
                "sync_announcements",
                "run_quality",
                "sync_stock_api_batch",
                "sync_date_api_batch",
                "sync_index_core",
                "sync_macro_core",
                "sync_capital_history_batch",
                "sync_etf_history_batch",
                "sync_etf_constituents_batch",
                "sync_major_news_history_batch",
                "noop",
            ],
            "note": "可一键执行全部自动步骤；复权因子按交易日整批获取，核心财务按报告期 VIP 整批获取，失败接口再降级补偿。",
        },
    }


def _step_progress_units(step: dict[str, Any]) -> tuple[float, float]:
    """Return (done, total) units for a plan step.

    Progress is based on real data coverage rather than the current execution
    queue index. This keeps the UI from jumping 99% -> 60% when the plan is
    rebuilt after a batch or auto-resume.
    """
    status = str(step.get("status") or "")
    action = str(step.get("action") or "")
    if status == "blocked" or action in {"manual_or_fallback_required", "noop"}:
        return 0.0, 0.0
    gap = step.get("gap") if isinstance(step.get("gap"), dict) else {}

    total = 0.0
    done = 0.0
    for have_key, need_key in (
        ("have_symbols", "need_symbols"),
        ("checked_symbols", "need_symbols"),
        ("have_dates", "need_dates"),
        ("checked_dates", "need_dates"),
        ("have", "need"),
        ("ready_series", "need_series"),
    ):
        if need_key in gap:
            total = float(gap.get(need_key) or 0)
            done = float(gap.get(have_key) or 0)
            break
    if total <= 0 and "count" in gap:
        total = float(gap.get("count") or 0)
        done = total if status == "done" else 0.0
    if total <= 0:
        total = max(1.0, float(step.get("estimated_calls") or 1))
        done = total if status == "done" else 0.0
    if status == "done":
        done = total
    return max(0.0, min(done, total)), max(0.0, total)


def _plan_progress_percent(plan: dict[str, Any]) -> int:
    ratios: list[float] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_done, step_total = _step_progress_units(step)
        ratios.append(step_done / step_total if step_total > 0 else 1.0)
    if not ratios:
        return 100
    # Each declared plan step has equal weight. Weighting by raw request slices
    # let a completed 9,855-slice news job hide an ETF job at only 66% and made
    # the UI claim 99% far too early.
    return max(0, min(100, int(round(sum(ratios) / len(ratios) * 100))))


def get_backfill_status() -> dict[str, Any]:
    cached = get_json("alis:data-platform:backfill:state")
    if isinstance(cached, dict):
        _ensure_auto_resume_alive(cached)
        if not cached.get("running") and str(cached.get("phase") or "") in {"completed", "completed_with_gaps"}:
            # A completed state already persists the last plan summary.  The
            # global status bar polls every few seconds; rebuilding the whole
            # historical gap plan here turns a cheap status request into a
            # multi-million-row scan and makes the dashboard time out.
            summary = cached.get("plan_summary")
            if isinstance(summary, dict):
                return {"ok": True, **cached}
            try:
                plan = build_backfill_plan()
                summary = plan.get("summary") if isinstance(plan, dict) else None
                if isinstance(summary, dict) and int(summary.get("steps_needed") or 0) <= 0:
                    normalized = {
                        **cached,
                        "running": False,
                        "phase": "completed",
                        "message": "历史补全已完成，当前无剩余缺口",
                        "progress": 100,
                        "plan_summary": summary,
                        "current_step": None,
                        "activity": None,
                        "auto_resume": False,
                        "can_resume": False,
                        "resume_action": "",
                    }
                    if normalized != cached:
                        with _lock:
                            _state.clear()
                            _state.update(normalized)
                        set_json("alis:data-platform:backfill:state", normalized, ttl=86400)
                    return {"ok": True, **normalized}
            except Exception:
                # Status must stay cheap and available; fall back to the last
                # persisted state if plan reconstruction has a transient issue.
                pass
        return {"ok": True, **cached}
    with _lock:
        return {"ok": True, **_state}


def _ensure_auto_resume_alive(snapshot: dict[str, Any]) -> None:
    """Self-heal stale Redis auto-resume states whose in-process timer was lost."""
    if snapshot.get("running") or not snapshot.get("auto_resume"):
        return
    resume_at = str(snapshot.get("auto_resume_at") or "")
    due = True
    if resume_at:
        try:
            due = datetime.fromisoformat(resume_at) <= datetime.now()
        except ValueError:
            due = True
    if not due:
        return

    def _resume_now() -> None:
        start_backfill(
            max_steps=int(snapshot.get("max_steps") or 0),
            market_batch_days=30,
            financial_batch_size=100,
            enqueue_stream=True,
            auto_resume_attempt=max(1, int(snapshot.get("auto_resume_attempt") or 1)),
        )

    threading.Thread(target=_resume_now, daemon=True, name="backfill-auto-resume-heal").start()


def recover_interrupted_backfill() -> dict[str, Any]:
    """Turn a Redis/DB `running` snapshot left by a process restart into a resumable state."""
    cached = get_json("alis:data-platform:backfill:state")
    run_id = int((cached or {}).get("run_id") or 0) if isinstance(cached, dict) else 0
    was_running = bool((cached or {}).get("running")) if isinstance(cached, dict) else False
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM data_ingestion_runs WHERE job_name='data_platform.backfill' AND status='running'"
        ).fetchall()
        stale_ids = [int(dict(row).get("id") or 0) for row in rows]
        if stale_ids:
            now = _now()
            for stale_id in stale_ids:
                c.execute(
                    "UPDATE data_ingestion_runs SET status='interrupted',finished_at=?,error=? WHERE id=?",
                    (now, "数据中台进程重启，任务已中断；已写入数据保留，可从缺口继续执行", stale_id),
                )
    if was_running or stale_ids:
        snapshot = {
            **(cached or {}), "running": False, "phase": "interrupted",
            "message": "服务重启导致任务中断；已写入数据保留，可点击继续补全",
            "finished_at": _now(), "error": "process_restarted",
        }
        with _lock:
            _state.clear()
            _state.update(snapshot)
        set_json("alis:data-platform:backfill:state", snapshot, ttl=86400)
        _schedule_auto_resume(
            error="process_restarted",
            attempt=1,
            max_steps=int(snapshot.get("max_steps") or 0),
            market_batch_days=30,
            financial_batch_size=100,
        )
        return {"recovered": True, "run_id": run_id, "stale_runs": stale_ids}
    if isinstance(cached, dict) and cached.get("phase") == "interrupted":
        _schedule_auto_resume(
            error=str(cached.get("error") or "process_restarted"),
            attempt=int(cached.get("auto_resume_attempt") or 0) + 1,
            max_steps=int(cached.get("max_steps") or 0),
            market_batch_days=30,
            financial_batch_size=100,
        )
        return {"recovered": True, "run_id": run_id, "stale_runs": [], "auto_resume": True}
    if isinstance(cached, dict) and cached.get("auto_resume") and not cached.get("running"):
        _schedule_auto_resume(
            error=str(cached.get("error") or "process_restarted"),
            attempt=max(1, int(cached.get("auto_resume_attempt") or 1)),
            max_steps=int(cached.get("max_steps") or 0),
            market_batch_days=30,
            financial_batch_size=100,
        )
        return {"recovered": True, "run_id": run_id, "stale_runs": [], "auto_resume": True}
    return {"recovered": False, "stale_runs": []}


def _set(**values: Any) -> None:
    with _lock:
        _state.update(values)
        snapshot = dict(_state)
    set_json("alis:data-platform:backfill:state", snapshot, ttl=86400)


def _is_auto_resumable_error(error: str) -> bool:
    text = (error or "").lower()
    if not text:
        return True
    fatal_markers = (
        "unsupported action",
        "invalid token",
        "token",
        "unauthorized",
        "forbidden",
        "401",
        "403",
        "permission",
        "权限",
        "未授权",
        "积分",
        "接口权限",
        "unsupported",
    )
    return not any(marker in text for marker in fatal_markers)


def _auto_resume_delay(attempt: int) -> int:
    idx = max(0, min(len(AUTO_RESUME_DELAYS_SECONDS) - 1, attempt - 1))
    return AUTO_RESUME_DELAYS_SECONDS[idx]


def _schedule_auto_resume(
    *,
    error: str,
    attempt: int,
    max_steps: int,
    market_batch_days: int,
    financial_batch_size: int,
    delay_override_seconds: int | None = None,
) -> None:
    if not _is_auto_resumable_error(error):
        _set(
            auto_resume=False,
            auto_resume_reason="fatal_error",
            message=f"历史补全失败：{error}；该错误不适合自动续跑，请手动处理",
        )
        return
    if AUTO_RESUME_MAX_ATTEMPTS > 0 and attempt > AUTO_RESUME_MAX_ATTEMPTS:
        _set(
            auto_resume=False,
            auto_resume_reason="max_attempts_reached",
            message=f"历史补全失败：{error}；已自动续跑 {AUTO_RESUME_MAX_ATTEMPTS} 次，暂停等待人工处理",
        )
        return
    delay = max(1, int(delay_override_seconds or _auto_resume_delay(attempt)))
    resume_at = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
    _set(
        auto_resume=True,
        auto_resume_attempt=attempt,
        auto_resume_max_attempts=AUTO_RESUME_MAX_ATTEMPTS or None,
        auto_resume_delay_seconds=delay,
        auto_resume_at=resume_at,
        message=f"历史补全失败：{error}；{delay} 秒后自动继续（第 {attempt}/{AUTO_RESUME_MAX_ATTEMPTS} 次）",
    )

    def _resume() -> None:
        should_start = False
        with _lock:
            if not _state.get("running"):
                phase = str(_state.get("phase") or "")
                should_start = phase in {"failed", "interrupted"} or bool(_state.get("auto_resume"))
        if not should_start:
            return
        start_backfill(
            max_steps=max_steps,
            market_batch_days=market_batch_days,
            financial_batch_size=financial_batch_size,
            enqueue_stream=True,
            auto_resume_attempt=attempt,
        )

    timer = threading.Timer(delay, _resume)
    timer.daemon = True
    timer.name = f"backfill-auto-resume-{attempt}"
    timer.start()


def _activity_text(item: dict[str, Any]) -> str:
    api = str(item.get("api") or "TuShare")
    params = item.get("params") or {}
    focus = []
    for key, label in (("trade_date", "交易日"), ("period", "报告期"), ("ts_code", "股票"), ("start_date", "开始"), ("end_date", "结束")):
        if params.get(key):
            focus.append(f"{label} {params[key]}")
    target = " · ".join(focus) or "默认范围"
    event = item.get("event")
    if event == "request":
        return f"请求 {api} · {target} · 第 {item.get('attempt', 1)} 次"
    if event == "retry":
        return f"重试 {api} · {target} · {item.get('category') or '异常'}"
    if event == "cooldown":
        return f"冷却 {api} · {target} · 剩余 {item.get('seconds', 0)} 秒"
    if event == "failed":
        return f"失败 {api} · {target} · {item.get('error') or '未知错误'}"
    if event == "response":
        return f"收到 {api} · {target} · {item.get('rows', 0)} 行"
    if event == "saved":
        return f"已保存 {api} · {target} · 写入 {item.get('written', 0)} 行"
    return f"{api} · {target}"


def _on_tushare_activity(item: dict[str, Any]) -> None:
    _set(activity={**item, "text": _activity_text(item)})


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Keep polling/status payload small while preserving operator evidence."""
    summary = {k: result.get(k) for k in (
        "ok", "api", "trade_date", "period", "total", "success", "failed",
        "rows_read", "rows_written", "error", "soft_failed", "cooldown", "cooldown_seconds",
    ) if k in result}
    failed_items = result.get("failed_items") or []
    if failed_items:
        summary["failed_items"] = failed_items[:8]
    children = result.get("results") or []
    if children:
        summary["subtasks"] = len(children)
        summary["subtasks_failed"] = sum(1 for x in children if isinstance(x, dict) and not x.get("ok"))
    return summary


def _run_step(step: dict[str, Any]) -> dict[str, Any]:
    action = step.get("action")
    params = step.get("params") or {}
    if action == "sync_trade_calendar":
        return sync_trade_calendar(
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            exchange=params.get("exchange") or "SSE",
        )
    if action == "sync_market_batch":
        results = []
        trade_dates = list(params.get("trade_dates") or [])
        for date_index, trade_date in enumerate(trade_dates, start=1):
            missing_apis = (params.get("missing_apis") or {}).get(trade_date) or ["daily", "daily_basic", "stk_limit"]
            _set(
                message=(
                    f"正在同步行情 {trade_date} "
                    f"（本批第 {date_index}/{len(trade_dates)} 天）"
                ),
                activity={
                    "event": "market_batch_progress",
                    "trade_date": trade_date,
                    "date_index": date_index,
                    "date_total": len(trade_dates),
                    "apis": missing_apis,
                    "text": f"{trade_date}: {', '.join(missing_apis)}",
                },
            )
            results.append(sync_market_bundle(trade_date, include_suspend=bool(params.get("include_suspend")), apis=missing_apis))
        ok = all(bool(r.get("ok")) for r in results) if results else True
        return {"ok": ok, "results": results, "dates": trade_dates}
    if action == "sync_financial_batch":
        result = sync_financial_batch(
            str(params.get("period") or ""),
            limit=int(params.get("limit") or 50),
            include_yearly=bool(params.get("include_yearly")),
            allow_partial=True,
            completion_api=str(params.get("completion_api") or "core"),
        )
        # Soft-continue historical plan: one flaky stock must not abort the whole run.
        if not result.get("ok") and not result.get("cooldown") and int(result.get("success") or 0) > 0:
            result = {**result, "ok": True, "soft_failed": True}
        return result
    if action == "sync_stock_api_batch":
        return sync_tushare_stock_batch(
            str(params.get("api_name") or ""), period=str(params.get("period") or ""),
            start_date=str(params.get("start_date") or ""), end_date=str(params.get("end_date") or ""),
            limit=int(params.get("limit") or 20),
        )
    if action == "sync_date_api_batch":
        return sync_tushare_date_batch(
            str(params.get("api_name") or ""), trade_dates=list(params.get("trade_dates") or []),
            limit=int(params.get("limit") or 10),
            require_rows=bool(params.get("require_rows")),
        )
    if action == "sync_capital_history_batch":
        return backfill_capital_structure_history(
            start_date=str(params.get("start_date") or "20200101"),
            end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""),
            limit_dates=int(params.get("limit_dates") or 10),
        )
    if action == "sync_etf_history_batch":
        return backfill_etf_price_history(
            start_date=str(params.get("start_date") or "20200101"),
            end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""),
            limit_funds=int(params.get("limit_funds") or 20),
        )
    if action == "sync_etf_constituents_batch":
        return backfill_etf_constituents_history(
            start_date=str(params.get("start_date") or "20200101"),
            end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""),
            limit_dates=int(params.get("limit_dates") or 20),
        )
    if action == "sync_major_news_history_batch":
        return backfill_major_news_history(
            start_date=str(params.get("start_date") or ""),
            end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""),
            limit_days=int(params.get("limit_days") or 7),
        )
    if action == "sync_index_core":
        return sync_index_bundle(
            index_code=str(params.get("index_code") or "000300.SH"),
            start_date=str(params.get("start_date") or ""), end_date=str(params.get("end_date") or ""),
        )
    if action == "sync_macro_core":
        start_date, end_date = str(params.get("start_date") or ""), str(params.get("end_date") or "")
        calls = (
            ("cn_gdp", {}), ("cn_cpi", {}), ("cn_pmi", {}),
            ("shibor", {"start_date": start_date, "end_date": end_date}),
            ("fx_daily", {"ts_code": "USDCNH.FXCM", "start_date": start_date, "end_date": end_date}),
        )
        results = [sync_tushare_api_raw(api, call_params) for api, call_params in calls]
        pmi_proxy = sync_pmi_industry_proxies() if next((r for r in results if r.get("api") == "cn_pmi" and r.get("ok")), None) else None
        return {"ok": all(r.get("ok") for r in results), "results": results, "pmi_industry_proxy": pmi_proxy}
    if action == "sync_announcements":
        return sync_cninfo_announcements(
            start_date=str(params.get("start_date") or ""),
            end_date=str(params.get("end_date") or ""),
            page_size=int(params.get("page_size") or 30),
            max_pages=int(params.get("max_pages") or 5),
        )
    if action == "run_quality":
        return run_data_quality_checks(trade_date=params.get("trade_date"))
    if action in {"noop", "sync_adj_factor_hint"}:
        return {"ok": True, "skipped": True, "reason": action}
    return {"ok": False, "error": f"unsupported action: {action}"}


def _worker(plan: dict[str, Any], *, max_steps: int, stream_id: str | None = None, auto_resume_attempt: int = 0) -> None:
    run_id = int(_state.get("run_id") or 0)
    executed: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    deferred_keys: set[str] = set()
    error = ""
    should_auto_resume = False
    auto_resume_delay_override: int | None = None
    set_tushare_activity_hook(_on_tushare_activity)
    try:
        runnable = [
            s for s in plan.get("steps") or []
            if s.get("status") == "needed" and s.get("action") in {
                "sync_trade_calendar", "sync_market_batch", "sync_financial_batch",
                "sync_announcements", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core",
                "sync_capital_history_batch", "sync_etf_history_batch", "sync_etf_constituents_batch", "sync_major_news_history_batch",
            }
        ]
        if not runnable:
            quality_step = next(
                (
                    s for s in plan.get("steps") or []
                    if s.get("status") == "needed" and s.get("action") == "run_quality"
                ),
                None,
            )
            if quality_step is not None:
                runnable = [quality_step]
        # max_steps <= 0 means run the full plan.
        if max_steps > 0:
            runnable = runnable[:max_steps]
        elif runnable:
            # Run one logical asset at a time. Each step is a quota-safe batch;
            # after it persists coverage we rebuild and continue that asset
            # before moving to the next one.
            runnable = runnable[:1]
        total = len(runnable)
        index = 0
        previous_gap_signatures: dict[str, str] = {}
        stagnant_batches: dict[str, int] = {}
        while index < len(runnable):
            step = runnable[index]
            index += 1
            step_key = str(step.get("key") or step.get("id") or "")
            previous_gap_signatures.setdefault(
                step_key,
                json.dumps(step.get("gap") or {}, ensure_ascii=False, sort_keys=True, default=str),
            )
            _set(
                phase=str(step.get("key") or step.get("id")),
                message=f"正在执行：{step.get('title')}",
                progress=_plan_progress_percent(build_backfill_plan(
                    market_batch_days=int(plan.get("scope", {}).get("policy", {}).get("market_batch_days") or 10),
                    financial_batch_size=int(plan.get("scope", {}).get("policy", {}).get("financial_batch_size") or 20),
                )),
                current_step=step,
            )
            result = _run_step(step)
            executed.append({"step": {k: step.get(k) for k in ("id", "key", "title", "action")}, "result": _result_summary(result)})
            if result.get("ok") or result.get("skipped"):
                _set(failure_detail=None)
            if not result.get("ok") and not result.get("skipped"):
                if result.get("cooldown"):
                    auto_resume_delay_override = max(1, int(result.get("cooldown_seconds") or 180))
                detail = str(result.get("error") or "").strip()
                if not detail and result.get("cooldown"):
                    detail = f"Tushare 触发限流，冷却 {auto_resume_delay_override or 180} 秒后自动继续"
                if not detail and result.get("failed"):
                    detail = f"失败 {result.get('failed')}/{result.get('total')} 只"
                if not detail:
                    # Market batch: dig first failed sub-result.
                    for sub in result.get("results") or []:
                        if isinstance(sub, dict) and not sub.get("ok"):
                            detail = str(sub.get("error") or sub.get("api") or "子步骤失败")[:300]
                            break
                # A global rate-limit cooldown pauses the worker. Other batch
                # failures remain eligible for the bounded re-plan loop below:
                # successful siblings may have advanced coverage, and the
                # no-progress guard will defer the step after three unchanged
                # rounds. Deferring immediately made the UI claim completion
                # while the same executable gaps were still present.
                if not result.get("cooldown"):
                    if step.get("action") == "run_quality":
                        step_key = str(step.get("key") or step.get("id") or "")
                        deferred_keys.add(step_key)
                        deferred.append({
                            "step": {k: step.get(k) for k in ("id", "key", "title", "action")},
                            "reason": (detail or "质量检查失败")[:1000],
                            "summary": _result_summary(result),
                        })
                    _set(
                        message=f"本批未完成：{step.get('title')}；完成其他步骤后按剩余缺口重试",
                        deferred=deferred,
                        deferred_count=len(deferred),
                        failure_detail={
                            "step": {k: step.get(k) for k in ("id", "key", "title", "action")},
                            "summary": _result_summary(result),
                            "resume_hint": f"系统会按剩余缺口有限重试；连续{DEFER_EMPTY_AFTER_ATTEMPTS}轮覆盖不增长后自动停止。",
                        },
                    )
                else:
                    error = detail or "step failed"
                    error = error[:1000]
                    _set(
                        failure_detail={
                            "step": {k: step.get(k) for k in ("id", "key", "title", "action")},
                            "summary": _result_summary(result),
                            "resume_hint": "已写入的数据会保留；点击继续补全会重新生成缺口计划，从未完成/失败覆盖项继续。",
                        },
                        can_resume=True,
                        resume_action="start_backfill",
                    )
                    break
            # Calendar is a prerequisite. Rebuild the remaining plan after it
            # succeeds so market gaps discovered from the new calendar are
            # executed in this same run instead of requiring another click.
            if step.get("action") == "sync_trade_calendar":
                refreshed = build_backfill_plan(
                    market_batch_days=int(plan.get("scope", {}).get("policy", {}).get("market_batch_days") or 10),
                    financial_batch_size=int(plan.get("scope", {}).get("policy", {}).get("financial_batch_size") or 20),
                )
                already = {str(x["step"].get("key")) for x in executed}
                remaining = [
                    s for s in refreshed.get("steps") or []
                    if s.get("status") == "needed"
                    and s.get("action") in {"sync_market_batch", "sync_financial_batch", "sync_announcements", "run_quality", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core", "sync_capital_history_batch", "sync_etf_history_batch", "sync_etf_constituents_batch", "sync_major_news_history_batch"}
                    and str(s.get("key")) not in already
                    and str(s.get("key") or s.get("id") or "") not in deferred_keys
                ]
                runnable = runnable[:index] + remaining
                if max_steps > 0:
                    runnable = runnable[:max_steps]
                total = len(runnable)
            # A plan step is intentionally a quota-safe batch.  When the queue
            # is exhausted, rebuild it from persisted coverage and keep taking
            # the next batch until no executable gap remains.  This makes one
            # full run converge instead of leaving every asset permanently
            # partial and requiring repeated clicks/restarts.
            if index >= len(runnable) and max_steps <= 0:
                refreshed = build_backfill_plan(
                    market_batch_days=int(plan.get("scope", {}).get("policy", {}).get("market_batch_days") or 10),
                    financial_batch_size=int(plan.get("scope", {}).get("policy", {}).get("financial_batch_size") or 20),
                )
                remaining = [
                    s for s in refreshed.get("steps") or []
                    if s.get("status") == "needed"
                    # Quality is a terminal verification step. Re-adding it
                    # together with every quota-safe data batch caused
                    # "补全 -> 质量 -> 下一轮" to repeat indefinitely.
                    and s.get("action") in {"sync_trade_calendar", "sync_market_batch", "sync_financial_batch", "sync_announcements", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core", "sync_capital_history_batch", "sync_etf_history_batch", "sync_etf_constituents_batch", "sync_major_news_history_batch"}
                    and str(s.get("key") or s.get("id") or "") not in deferred_keys
                ]
                if remaining:
                    current_key = step_key
                    current_remaining = next(
                        (s for s in remaining if str(s.get("key") or s.get("id") or "") == current_key),
                        None,
                    )
                    gap_signature = json.dumps(
                        (current_remaining or {}).get("gap") or {},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    if current_remaining is not None:
                        previous = previous_gap_signatures.get(current_key)
                        stagnant_batches[current_key] = stagnant_batches.get(current_key, 0) + 1 if previous == gap_signature else 0
                        previous_gap_signatures[current_key] = gap_signature
                    if current_remaining is not None and stagnant_batches.get(current_key, 0) >= DEFER_EMPTY_AFTER_ATTEMPTS:
                        deferred_keys.add(current_key)
                        deferred.append({
                            "step": {k: current_remaining.get(k) for k in ("id", "key", "title", "action")},
                            "reason": f"连续 {DEFER_EMPTY_AFTER_ATTEMPTS} 个批次执行后缺口没有变化，已停止该项自动补全",
                            "summary": {"gap": current_remaining.get("gap") or {}},
                        })
                        _set(
                            message=f"{current_remaining.get('title')} 连续 {DEFER_EMPTY_AFTER_ATTEMPTS} 个批次无进展，已转为待处理",
                            deferred=deferred,
                            deferred_count=len(deferred),
                        )
                        remaining = [
                            pending for pending in remaining
                            if str(pending.get("key") or pending.get("id") or "") not in deferred_keys
                        ]
                    # Prefer continuing the same asset until it is complete;
                    # otherwise move to the next remaining asset.
                    next_step = next(
                        (pending for pending in remaining if str(pending.get("key") or pending.get("id") or "") == current_key),
                        remaining[0] if remaining else None,
                    )
                    if next_step is not None:
                        runnable.append(next_step)
                    total = len(runnable)
                    if next_step is not None:
                        runtime_summary = dict(refreshed.get("summary") or {})
                        runtime_summary["steps_needed"] = len(remaining) + 1  # terminal quality check
                        runtime_summary["steps_blocked"] = len(deferred_keys)
                        runtime_summary["next_action"] = str(next_step.get("title") or "继续补全")
                        runtime_plan = {
                            **refreshed,
                            "summary": runtime_summary,
                            "steps": [
                                {
                                    **pending,
                                    "status": "blocked",
                                    "why": f"本次运行连续{DEFER_EMPTY_AFTER_ATTEMPTS}个批次无进展，已停止自动请求",
                                }
                                if str(pending.get("key") or pending.get("id") or "") in deferred_keys
                                else pending
                                for pending in refreshed.get("steps") or []
                            ],
                        }
                        _set(
                            message=f"继续补全：{next_step.get('title')}",
                            progress=_plan_progress_percent(refreshed),
                            current_step=next_step,
                            plan_summary=runtime_summary,
                            plan=runtime_plan,
                        )
                    # `remaining` may have contained only the current asset
                    # before the no-progress guard deferred it.  In that case
                    # the list becomes empty inside this branch, so the outer
                    # `elif not remaining` path is never reached.  Always add
                    # the terminal quality check once the last executable data
                    # step has disappeared.
                    elif step.get("action") != "run_quality":
                        quality_step = next(
                            (
                                pending for pending in refreshed.get("steps") or []
                                if pending.get("status") == "needed" and pending.get("action") == "run_quality"
                            ),
                            None,
                        )
                        if quality_step is not None:
                            runnable.append(quality_step)
                            total = len(runnable)
                elif step.get("action") != "run_quality":
                    quality_step = next(
                        (
                            s for s in refreshed.get("steps") or []
                            if s.get("status") == "needed" and s.get("action") == "run_quality"
                        ),
                        None,
                    )
                    if quality_step is not None:
                        runnable.append(quality_step)
                        total = len(runnable)
        invalidate_data_assets_cache()
        final_plan = build_backfill_plan(
            market_batch_days=int(plan.get("scope", {}).get("policy", {}).get("market_batch_days") or 10),
            financial_batch_size=int(plan.get("scope", {}).get("policy", {}).get("financial_batch_size") or 20),
        )
        if deferred_keys:
            final_plan = {
                **final_plan,
                "steps": [
                    {
                        **pending,
                        "status": "blocked",
                        "why": f"本次运行连续{DEFER_EMPTY_AFTER_ATTEMPTS}个批次无进展，已停止自动请求；可检查接口错误后手动重试",
                    }
                    if str(pending.get("key") or pending.get("id") or "") in deferred_keys
                    else pending
                    for pending in final_plan.get("steps") or []
                ],
            }
        final_summary = final_plan.get("summary") if isinstance(final_plan, dict) else {}
        if deferred_keys and isinstance(final_summary, dict):
            final_summary = {**final_summary, "steps_blocked": len(deferred_keys)}
            final_plan = {**final_plan, "summary": final_summary}
        remaining_steps = int((final_summary or {}).get("steps_needed") or 0)
        blocked_steps = int((final_summary or {}).get("steps_blocked") or 0)
        fully_done = not error and not deferred and remaining_steps <= 0 and blocked_steps <= 0
        final = "failed" if error else ("completed" if fully_done else "completed_with_gaps")
        _set(
            running=False,
            phase=final,
            message=("历史补全失败：" + error) if error else (
                f"自动补全已停止；{len(deferred)} 个步骤无进展或失败，仍有缺口待处理" if deferred
                else "历史补全已完成，当前无剩余缺口" if fully_done
                else f"可自动步骤已完成；仍有 {blocked_steps} 个外部能力阻塞项"
                if blocked_steps > 0 and remaining_steps <= 0
                else f"本次已执行 {len(executed)} 个步骤，仍有 {remaining_steps} 个待补步骤"
            ),
            progress=100 if fully_done else _plan_progress_percent(final_plan),
            plan_summary=final_summary,
            plan=final_plan,
            current_step=None,
            activity=None,
            finished_at=_now(),
            executed=executed,
            deferred=deferred,
            deferred_count=len(deferred),
            error=error,
            can_resume=bool(error or remaining_steps > 0),
            resume_action="start_backfill" if error or remaining_steps > 0 else "",
            auto_resume=False,
            auto_resume_attempt=auto_resume_attempt,
            auto_resume_max_attempts=AUTO_RESUME_MAX_ATTEMPTS,
        )
        should_auto_resume = bool(error)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:1000]
        invalidate_data_assets_cache()
        _set(
            running=False,
            phase="failed",
            message=f"历史补全失败：{error}",
            finished_at=_now(),
            executed=executed,
            error=error,
            failure_detail={
                "step": dict(_state.get("current_step") or {}),
                "summary": {"error": error},
                "resume_hint": "异常已记录；点击继续补全会重新生成缺口计划并从剩余缺口继续。",
            },
            can_resume=True,
            resume_action="start_backfill",
            auto_resume=False,
            auto_resume_attempt=auto_resume_attempt,
            auto_resume_max_attempts=AUTO_RESUME_MAX_ATTEMPTS,
        )
        should_auto_resume = True
    finally:
        set_tushare_activity_hook(None)
        if stream_id:
            # The stream message is acknowledged only after the resumable
            # worker has persisted its terminal state in SQLite/Redis.
            ack_task(stream_id)
    with _conn() as c:
        c.execute(
            """
            UPDATE data_ingestion_runs
            SET status=?, finished_at=?, rows_read=?, rows_written=?, error=?, meta_json=?
            WHERE id=?
            """,
            (
                str(_state.get("phase") or ("failed" if error else "completed")),
                _now(),
                len(executed),
                len(executed),
                error,
                json.dumps({"executed": executed}, ensure_ascii=False, default=str)[:100000],
                run_id,
            ),
        )
    if should_auto_resume:
        policy = plan.get("scope", {}).get("policy", {}) if isinstance(plan.get("scope"), dict) else {}
        _schedule_auto_resume(
            error=error,
            attempt=auto_resume_attempt + 1,
            max_steps=max_steps,
            market_batch_days=int(policy.get("market_batch_days") or 10),
            financial_batch_size=int(policy.get("financial_batch_size") or 20),
            delay_override_seconds=auto_resume_delay_override,
        )


def start_backfill(*, max_steps: int = 0, market_batch_days: int = 30, financial_batch_size: int = 100, enqueue_stream: bool = True, auto_resume_attempt: int = 0) -> dict[str, Any]:
    """Execute auto-runnable steps of a freshly built plan.

    max_steps <= 0 runs the entire plan; otherwise only the first N runnable steps.
    """
    with _lock:
        if _state.get("running"):
            return {"ok": False, "reason": "already_running", **_state}
        plan = build_backfill_plan(market_batch_days=market_batch_days, financial_batch_size=financial_batch_size)
        summary = plan.get("summary") if isinstance(plan, dict) else {}
        steps_needed = int((summary or {}).get("steps_needed") or 0)
        steps_blocked = int((summary or {}).get("steps_blocked") or 0)
        if steps_needed <= 0:
            snapshot = {
                "running": False,
                "run_id": int(_state.get("run_id") or 0),
                "phase": "completed_with_gaps" if steps_blocked > 0 else "completed",
                "message": (
                    f"可自动步骤已完成；仍有 {steps_blocked} 个外部能力阻塞项"
                    if steps_blocked > 0 else "历史补全已完成，当前无剩余缺口"
                ),
                "progress": 100,
                "started_at": _state.get("started_at") or _now(),
                "finished_at": _now(),
                "plan_summary": summary,
                "plan": plan,
                "current_step": None,
                "activity": None,
                "max_steps": max_steps,
                "auto_resume": False,
                "auto_resume_attempt": auto_resume_attempt,
                "auto_resume_max_attempts": AUTO_RESUME_MAX_ATTEMPTS,
                "can_resume": False,
                "resume_action": "",
            }
            _state.clear()
            _state.update(snapshot)
            set_json("alis:data-platform:backfill:state", dict(_state), ttl=86400)
            return {
                "ok": True,
                "reason": "blocked_only" if steps_blocked > 0 else "no_gaps",
                **snapshot,
                "plan": plan,
            }
        with _conn() as c:
            c.execute(
                """
                INSERT INTO data_ingestion_runs
                (job_name, source_code, status, started_at, rows_read, rows_written, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("data_platform.backfill", "system", "running", _now(), 0, 0, json.dumps({"max_steps": max_steps}, ensure_ascii=False)),
            )
            run_id = c.last_insert_id()
        _state.clear()
        _state.update(
            {
                "running": True,
                "run_id": run_id,
                "phase": "starting",
                "message": "正在启动历史补全",
                "progress": _plan_progress_percent(plan),
                "started_at": _now(),
                "plan_summary": plan.get("summary"),
                "plan": plan,
                "max_steps": max_steps,
                "auto_resume": False,
                "auto_resume_attempt": auto_resume_attempt,
                "auto_resume_max_attempts": AUTO_RESUME_MAX_ATTEMPTS,
            }
        )
        set_json("alis:data-platform:backfill:state", dict(_state), ttl=86400)
        stream_id = None
        if enqueue_stream:
            stream_id = enqueue_task(
                "data_platform.backfill",
                {
                    "run_id": run_id,
                    "max_steps": max_steps,
                    "plan_generated_at": plan.get("generated_at"),
                    "auto_resume_attempt": auto_resume_attempt,
                },
            )
        _state["stream_id"] = stream_id
        set_json("alis:data-platform:backfill:state", dict(_state), ttl=86400)
        threading.Thread(
            target=_worker,
            args=(plan,),
            kwargs={"max_steps": max_steps, "stream_id": stream_id, "auto_resume_attempt": auto_resume_attempt},
            daemon=True,
            name=f"backfill-{run_id}",
        ).start()
        return {"ok": True, **_state, "plan": plan}


def process_stream_task(fields: dict[str, str]) -> dict[str, Any]:
    """Recover a queued backfill without enqueueing a second message."""
    if str(fields.get("task_type") or "") != "data_platform.backfill":
        return {"ok": False, "skipped": True, "reason": "unsupported_task"}
    try:
        payload = json.loads(fields.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return start_backfill(
        max_steps=int(payload.get("max_steps") or 0),
        enqueue_stream=False,
        auto_resume_attempt=int(payload.get("auto_resume_attempt") or 0),
    )
