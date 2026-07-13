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
    sync_trade_calendar,
    sync_tushare_api_raw,
    sync_tushare_date_batch,
    sync_tushare_stock_batch,
    sync_index_bundle,
    set_tushare_activity_hook,
)
from app.data_platform.redis_store import ack_task, enqueue_task, get_json, set_json

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "phase": "idle", "message": "尚未生成计划"}


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
            "SELECT COUNT(DISTINCT ts_code) n FROM dc_tushare_request_coverage WHERE api_name=? AND period=? AND ts_code<>''",
            (api_name, period),
        ).fetchone()
    return int(dict(row).get("n") or 0)


def _request_date_coverage(api_name: str, start: str, end: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT trade_date) n FROM dc_tushare_request_coverage WHERE api_name=? AND trade_date BETWEEN ? AND ?",
            (api_name, start, end),
        ).fetchone()
    return int(dict(row).get("n") or 0)


def build_backfill_plan(*, market_batch_days: int = 20, financial_batch_size: int = 50) -> dict[str, Any]:
    """Build a prioritized historical completion plan from current gaps."""
    today = date.today()
    start = today - timedelta(days=DAILY_LOOKBACK_DAYS)
    # Do not treat the current trading day as historical data before close.
    end = today - timedelta(days=1)
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

    # 3. Adj factor — often missing entirely; sync by recent window sample then expand.
    if len(have_adj) < len(open_days) * 0.5:
        adj_checked = _request_coverage_count("adj_factor")
        add(
            {
                "priority": 2,
                "key": "adj_factor",
                "title": "补全复权因子",
                "why": f"审计窗口内复权因子交易日覆盖 {len(have_adj)}/{max(len(open_days), FALLBACK_OPEN_DAYS)}",
                "action": "sync_stock_api_batch",
                "params": {"api_name": "adj_factor", "start_date": start_s, "end_date": end_s, "limit": financial_batch_size},
                "estimated_calls": financial_batch_size,
                "estimated_minutes": round(financial_batch_size * 0.05, 1),
                "status": "done" if stocks > 0 and adj_checked >= stocks else "needed",
                "gap": {"have_dates": len(have_adj), "need_dates": max(len(open_days), FALLBACK_OPEN_DAYS)},
            }
        )

    # 4. Financial periods — one batch per report period for a staged stock limit.
    for period in quarters:
        # A successful empty response is still a completed audit for that
        # stock/period.  Use request coverage as well as returned rows so newly
        # listed/non-reporting stocks do not keep the plan partial forever.
        have = max(_financial_symbols("income", period), _request_coverage_count("income", period))
        if stocks > 0 and have < stocks:
            add(
                {
                    "priority": 3,
                    "key": f"financial_{period}",
                    "title": f"补全财务 {period}",
                    "why": f"利润表股票覆盖 {have}/{stocks}；本任务连续补至 {stocks} 只全部检查完成，每 {financial_batch_size} 只持久化一个断点",
                    "action": "sync_financial_batch",
                    "params": {"period": period, "limit": financial_batch_size, "include_yearly": period.endswith("1231"), "completion_api": "core"},
                    "estimated_calls": financial_batch_size * (6 if period.endswith("1231") else 4),
                    "estimated_minutes": round(financial_batch_size * 0.4, 1),
                    "status": "needed",
                    "gap": {"have_symbols": have, "need_symbols": stocks, "batch": financial_batch_size},
                }
            )

    for period in annuals:
        # Annual-only extras already covered when quarter ends with 1231 and include_yearly.
        if period in quarters:
            continue
        have = max(_financial_symbols("fina_audit", period), _request_coverage_count("fina_audit", period))
        if stocks > 0 and have < stocks:
            add(
                {
                    "priority": 3,
                    "key": f"annual_{period}",
                    "title": f"补全年度财务附加 {period}",
                    "why": "审计意见 / 主营构成按年补",
                    "action": "sync_financial_batch",
                    "params": {"period": period, "limit": financial_batch_size, "include_yearly": True, "completion_api": "fina_audit"},
                    "estimated_calls": financial_batch_size * 6,
                    "estimated_minutes": round(financial_batch_size * 0.5, 1),
                    "status": "needed",
                    "gap": {"have_symbols": have, "batch": financial_batch_size},
                }
            )

    # 4b. Additional TuShare APIs verified by real small-sample calls.
    latest_period = quarters[0] if quarters else ""
    stock_routes = [
        ("namechange", "补全名称/ST历史", ""),
        ("top10_holders", "补全十大股东", latest_period),
        ("top10_floatholders", "补全十大流通股东", latest_period),
        ("stk_holdernumber", "补全股东人数", latest_period),
        ("pledge_stat", "补全质押统计", ""),
        ("pledge_detail", "补全质押明细", ""),
        ("express", "补全业绩快报", latest_period),
    ]
    for api_name, title, period in stock_routes:
        checked = _request_coverage_count(api_name, period)
        add({
            "priority": 4, "key": f"stock_api_{api_name}", "title": title,
            "why": f"{api_name} 已通过真实接口写入验证；连续补至全市场检查完成，每 {financial_batch_size} 只持久化断点，空结果也记录以避免重复请求",
            "action": "sync_stock_api_batch",
            "params": {"api_name": api_name, "period": period, "limit": financial_batch_size},
            "estimated_calls": financial_batch_size, "estimated_minutes": round(financial_batch_size * 0.15, 1),
            "status": "done" if stocks > 0 and checked >= stocks else "needed",
            "gap": {"checked_symbols": checked, "need_symbols": stocks, "batch": financial_batch_size},
        })
    for api_name, title in (("suspend_d", "补全停复牌检查"), ("moneyflow", "补全资金流")):
        checked_dates = _request_date_coverage(api_name, start_s, end_s)
        add({
            "priority": 4, "key": f"date_api_{api_name}", "title": title,
            "why": f"{api_name} 已通过真实接口验证；按交易日分批，成功空结果也记为已检查",
            "action": "sync_date_api_batch",
            "params": {"api_name": api_name, "trade_dates": open_days, "limit": market_batch_days},
            "estimated_calls": min(len(open_days), market_batch_days), "estimated_minutes": round(min(len(open_days), market_batch_days) * 0.2, 1),
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
    prior_work_needed = any(
        s.get("status") == "needed"
        and s.get("action") in {"sync_trade_calendar", "sync_market_batch", "sync_financial_batch", "sync_announcements"}
        for s in steps
    )
    with _conn() as c:
        quality_checked_today = bool(c.execute(
            "SELECT 1 FROM dc_data_quality_checks WHERE checked_at >= ? LIMIT 1",
            (today.isoformat(),),
        ).fetchone())
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
            "status": "needed" if prior_work_needed or not quality_checked_today else "done",
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
                "noop",
            ],
            "note": "可一键执行可自动步骤；adj_factor 全市场按票拉取成本高，计划中仅提示。",
        },
    }


def get_backfill_status() -> dict[str, Any]:
    cached = get_json("alis:data-platform:backfill:state")
    if isinstance(cached, dict):
        return {"ok": True, **cached}
    with _lock:
        return {"ok": True, **_state}


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
        return {"recovered": True, "run_id": run_id, "stale_runs": stale_ids}
    return {"recovered": False, "stale_runs": []}


def _set(**values: Any) -> None:
    with _lock:
        _state.update(values)
        snapshot = dict(_state)
    set_json("alis:data-platform:backfill:state", snapshot, ttl=86400)


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
        "rows_read", "rows_written", "error", "soft_failed",
    ) if k in result}
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
        if not result.get("ok") and int(result.get("success") or 0) > 0:
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
        return {"ok": all(r.get("ok") for r in results), "results": results}
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


def _worker(plan: dict[str, Any], *, max_steps: int, stream_id: str | None = None) -> None:
    run_id = int(_state.get("run_id") or 0)
    executed: list[dict[str, Any]] = []
    error = ""
    set_tushare_activity_hook(_on_tushare_activity)
    try:
        runnable = [
            s for s in plan.get("steps") or []
            if s.get("status") == "needed" and s.get("action") in {
                "sync_trade_calendar", "sync_market_batch", "sync_financial_batch",
                "sync_announcements", "run_quality", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core",
            }
        ]
        # max_steps <= 0 means run the full plan.
        if max_steps > 0:
            runnable = runnable[:max_steps]
        total = len(runnable)
        index = 0
        refresh_rounds = 0
        while index < len(runnable):
            step = runnable[index]
            index += 1
            _set(
                phase=str(step.get("key") or step.get("id")),
                message=f"正在执行：{step.get('title')}",
                progress=int((index - 1) / max(total, 1) * 100),
                current_step=step,
            )
            result = _run_step(step)
            executed.append({"step": {k: step.get(k) for k in ("id", "key", "title", "action")}, "result": _result_summary(result)})
            if not result.get("ok") and not result.get("skipped"):
                detail = str(result.get("error") or "").strip()
                if not detail and result.get("failed"):
                    detail = f"失败 {result.get('failed')}/{result.get('total')} 只"
                if not detail:
                    # Market batch: dig first failed sub-result.
                    for sub in result.get("results") or []:
                        if isinstance(sub, dict) and not sub.get("ok"):
                            detail = str(sub.get("error") or sub.get("api") or "子步骤失败")[:300]
                            break
                error = detail or "step failed"
                error = error[:1000]
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
                    and s.get("action") in {"sync_market_batch", "sync_financial_batch", "sync_announcements", "run_quality", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core"}
                    and str(s.get("key")) not in already
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
                    and s.get("action") in {"sync_trade_calendar", "sync_market_batch", "sync_financial_batch", "sync_announcements", "run_quality", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core"}
                ]
                if remaining:
                    refresh_rounds += 1
                    runnable.extend(remaining)
                    total = len(runnable)
                    _set(message=f"仍有缺口，继续第 {refresh_rounds + 1} 轮补全", current_step=remaining[0])
        final = "failed" if error else "completed"
        invalidate_data_assets_cache()
        _set(
            running=False,
            phase=final,
            message=("历史补全失败：" + error) if error else f"已执行 {len(executed)} 个补全步骤",
            progress=100 if not error else int(len(executed) / max(total, 1) * 100),
            finished_at=_now(),
            executed=executed,
            error=error,
        )
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
        )
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
                "failed" if error else "completed",
                _now(),
                len(executed),
                len(executed),
                error,
                json.dumps({"executed": executed}, ensure_ascii=False, default=str)[:100000],
                run_id,
            ),
        )


def start_backfill(*, max_steps: int = 0, market_batch_days: int = 10, financial_batch_size: int = 20, enqueue_stream: bool = True) -> dict[str, Any]:
    """Execute auto-runnable steps of a freshly built plan.

    max_steps <= 0 runs the entire plan; otherwise only the first N runnable steps.
    """
    with _lock:
        if _state.get("running"):
            return {"ok": False, "reason": "already_running", **_state}
        plan = build_backfill_plan(market_batch_days=market_batch_days, financial_batch_size=financial_batch_size)
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
                "progress": 0,
                "started_at": _now(),
                "plan_summary": plan.get("summary"),
                "max_steps": max_steps,
            }
        )
        set_json("alis:data-platform:backfill:state", dict(_state), ttl=86400)
        stream_id = None
        if enqueue_stream:
            stream_id = enqueue_task(
                "data_platform.backfill",
                {"run_id": run_id, "max_steps": max_steps, "plan_generated_at": plan.get("generated_at")},
            )
        _state["stream_id"] = stream_id
        set_json("alis:data-platform:backfill:state", dict(_state), ttl=86400)
        threading.Thread(
            target=_worker,
            args=(plan,),
            kwargs={"max_steps": max_steps, "stream_id": stream_id},
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
    )
