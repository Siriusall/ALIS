"""Historical backfill plan: turn audit-window gaps into an actionable checklist."""

from __future__ import annotations

import json
import threading
from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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

PLAN_MODEL_REQUIRED_LABELED_SESSIONS = 1577
MODEL_DATA_BACKFILL_TASK = "data_platform.plan_model_data_backfill"
MODEL_DATA_BACKFILL_STATE_KEY = "alis:data-platform:plan-model-data-backfill:state"
MODEL_DATA_BACKFILL_CONTRACT = "alis_plan_model_data_backfill_v3_event_financial_disclosures"
PREVIOUS_MODEL_DATA_BACKFILL_CONTRACT = "alis_plan_model_data_backfill_v2_event_research"
from app.data_platform.services.asset_watermarks import mark_asset_terminal
from app.data_platform.services.data_center import (
    enrich_financial_research_batch,
    run_data_quality_checks,
    sync_cninfo_announcements,
    sync_financial_batch,
    sync_financial_vip_period,
    sync_market_bundle,
    sync_pmi_industry_proxies,
    sync_trade_calendar,
    sync_tushare_api_raw,
    sync_tushare_date_batch,
    sync_tushare_stock_batch,
    sync_index_bundle,
    set_tushare_activity_hook,
    backfill_capital_structure_history,
    backfill_etf_daily_incremental,
    backfill_etf_price_history,
    backfill_event_research_history,
    backfill_major_news_history,
    normalize_report_rc_record_keys,
    DEFER_EMPTY_AFTER_ATTEMPTS,
    get_vip_backfill_status,
)
from app.data_platform.services.market_calendar import target_trade_day
from app.data_platform.services.oos_hard_data import audit_oos_hard_data, repair_oos_hard_data_batch
from app.data_platform.redis_store import (
    STREAM_MAX_ATTEMPTS,
    ack_task,
    enqueue_task,
    get_json,
    set_json,
    stream_health,
    task_attempts,
)
from app.services.theme_engine import materialize_theme_snapshot_history
from app.data_platform.services.exchange_etf_pcf import backfill_exchange_etf_pcf

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "phase": "idle", "message": "尚未生成计划"}
_model_backfill_status_cache: dict[str, Any] = {"at": 0.0, "value": None}
# Recoverable process/rate-limit failures must be finite. Permanent upstream
# failures are surfaced to the operator after this many automatic resumes.
AUTO_RESUME_MAX_ATTEMPTS = 5
AUTO_RESUME_DELAYS_SECONDS = (15, 30, 60, 120, 180)
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")


def _resume_due(value: Any, *, now: datetime | None = None) -> bool:
    try:
        resume_at = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=_BUSINESS_TZ)
        current = now or datetime.now(_BUSINESS_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=_BUSINESS_TZ)
        return resume_at.astimezone(_BUSINESS_TZ) <= current.astimezone(_BUSINESS_TZ)
    except (TypeError, ValueError):
        return True


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
            SELECT cal.cal_date AS trade_date
            FROM dc_trade_calendar cal
            WHERE cal.exchange = 'SSE' AND cal.is_open = 1
              AND cal.cal_date BETWEEN ? AND ?
              AND EXISTS (
                  SELECT 1 FROM dc_tushare_raw_records raw
                  WHERE raw.api_name = ? AND raw.trade_date = cal.cal_date
              )
            """,
            (start, end, api_name),
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


def _theme_snapshot_dates(dates: list[str]) -> set[str]:
    if not dates:
        return set()
    placeholders = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT DISTINCT as_of_date FROM dc_theme_snapshot WHERE as_of_date IN ({placeholders})",
            tuple(dates),
        ).fetchall()
    return {str(dict(row).get("as_of_date") or "") for row in rows}


def _theme_snapshot_dates_for_version(dates: list[str], model_version: str) -> set[str]:
    if not dates:
        return set()
    placeholders = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT DISTINCT as_of_date FROM dc_theme_snapshot WHERE model_version=? AND as_of_date IN ({placeholders})",
            (model_version, *dates),
        ).fetchall()
    return {str(dict(row).get("as_of_date") or "") for row in rows}


def _latest_snapshot_date_on_or_before(end: str) -> str:
    with _conn() as c:
        row = c.execute(
            """
            SELECT trade_date value
            FROM dc_stock_daily_snapshot
            WHERE trade_date<=?
            GROUP BY trade_date
            HAVING SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) > 0
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            (end,),
        ).fetchone()
    return str(dict(row).get("value") or end) if row else end


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
    # Plan generation is an explicit heavy operation and needs a fresh catalog;
    # the read-only GET endpoint intentionally serves a persisted snapshot.
    assets = get_data_assets(force=True)
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

    # Permanent Champion OOS contract. This is intentionally independent of
    # the rolling two-year UI audit window and can never age out of the plan.
    oos_hard = audit_oos_hard_data(end_date=end_s, include_dates=True)
    oos_repair_dates = list(oos_hard.get("repair_dates") or [])
    oos_details = {str(item.get("trade_date") or ""): item for item in oos_hard.get("dates") or []}
    if oos_repair_dates:
        batch = oos_repair_dates[:max(1, market_batch_days)]
        add({
            "priority": 1,
            "key": "oos_hard_data",
            "title": "补全 Champion OOS 永久硬数据",
            "why": f"正式五窗口 OOS 要求 {oos_hard['required_sessions']} 个合格交易日，当前 {oos_hard['eligible_sessions']}，永久阻断直到完整",
            "action": "sync_oos_hard_data_batch",
            "params": {"trade_dates": batch, "date_gaps": {value: oos_details.get(value, {}) for value in batch}},
            "estimated_calls": len(batch) * 4,
            "estimated_minutes": round(len(batch) * 0.35, 1),
            "status": "needed",
            "gap": {"have": int(oos_hard["eligible_sessions"]), "need": int(oos_hard["required_sessions"]), "missing": int(oos_hard["missing_sessions"]), "dates": batch},
        })
    else:
        add({
            "priority": 1, "key": "oos_hard_data", "title": "Champion OOS 永久硬数据",
            "why": "正式五窗口 OOS 数据合同已满足", "action": "sync_oos_hard_data_batch",
            "params": {"trade_dates": []}, "estimated_calls": 0, "estimated_minutes": 0,
            "status": "done", "gap": {"have": int(oos_hard["eligible_sessions"]), "need": int(oos_hard["required_sessions"]), "missing": 0},
        })

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
            {"start_date": vip["scope"]["capital_start"], "end_date": end_s, "cursor": vip["etf_price_nav"].get("resume_cursor", ""), "mode": "daily", "limit_dates": min(30, market_batch_days), "nav_limit_funds": min(100, financial_batch_size)},
        ),
        (
            "major_news_vip", "补全九来源长篇新闻历史", "sync_major_news_history_batch",
            vip["major_news"],
            {"start_date": vip["scope"]["news_start"], "end_date": end_s, "cursor": vip["major_news"].get("resume_cursor", ""), "limit_days": min(31, market_batch_days)},
        ),
    )
    for key, title, action, contract, params in vip_steps:
        missing = int(contract.get("missing_slices") or 0)
        add({
            "priority": 2,
            "key": key,
            "title": title,
            "why": f"精确终态切片覆盖 {int(contract.get('completed_slices') or 0)}/{int(contract.get('expected_slices') or 0)}，缺失 {missing}",
            "action": action,
            "params": params,
            "estimated_calls": missing,
            "estimated_minutes": round(missing * 0.03, 1),
            "status": "done" if contract.get("complete") else "needed",
            "gap": {
                "have": int(contract.get("completed_slices") or 0),
                "need": int(contract.get("expected_slices") or 0),
                "missing": missing,
                "coverage": float(contract.get("coverage") or 0),
            },
        })

    # Official exchange PCF is independent from TuShare. SZSE exposes dated
    # files; SSE public data only guarantees the latest basket, so unavailable
    # old SSE dates remain explicit rather than being marked as valid-empty.
    pcf_asset = next((item for item in assets.get("items") or [] if item.get("key") == "exchange_etf_pcf"), {})
    pcf_open_dates = _open_dates(end_s, _ymd(today))
    pcf_end = max([end_s, *pcf_open_dates])
    pcf_start = max(start_s, _ymd(today - timedelta(days=365)))
    pcf_done = str(pcf_asset.get("status") or "") == "complete" and str(pcf_asset.get("max_date") or "") >= pcf_end
    # Incomplete latest coverage must retry that exact day; advancing a date
    # cursor merely because one ETF succeeded would strand the remaining ETFs.
    pcf_params_start = pcf_start if pcf_done else pcf_end
    pcf_cursor = str(pcf_asset.get("max_date") or "") if pcf_done else ""
    add({
        "priority": 2,
        "key": "exchange_etf_pcf",
        "title": "补全交易所ETF申购赎回篮子",
        "why": "深交所按公开日期回补最近一年；上交所补最新并从接入日起每日归档，不伪造不可得历史",
        "action": "sync_exchange_etf_pcf_batch",
        "params": {"start_date": pcf_params_start, "end_date": pcf_end, "cursor": pcf_cursor, "limit_dates": 1},
        "estimated_calls": 1,
        "estimated_minutes": 10,
        "status": "done" if pcf_done else "needed",
        "gap": {"have": int(pcf_asset.get("actual_records") or 0), "max_date": pcf_asset.get("max_date"), "need_date": pcf_end},
    })

    # 3b. Derived theme snapshots for the narrative lifecycle model.  This is
    # generated from already-stored market/industry data, so it belongs in the
    # data platform plan rather than hidden inside model training.
    theme_end_s = _latest_snapshot_date_on_or_before(end_s)
    theme_open_days = [day for day in open_days if day <= theme_end_s]
    target_theme_dates = list(reversed(theme_open_days[-250:])) if theme_open_days else []
    have_theme_dates = _theme_snapshot_dates(target_theme_dates)
    missing_theme_dates = [trade_date for trade_date in reversed(target_theme_dates) if trade_date not in have_theme_dates]
    add({
        "priority": 2,
        "key": "theme_snapshots_250",
        "title": "补全250日主题快照",
        "why": f"叙事状态转移模型需要最近250个可用行情交易日主题快照，当前 {len(have_theme_dates)}/{len(target_theme_dates) or 250}，截止 {theme_end_s}",
        "action": "materialize_theme_snapshots_batch",
        "params": {"end_date": theme_end_s, "target_sessions": 250, "limit_dates": min(30, market_batch_days)},
        "estimated_calls": max(0, len(missing_theme_dates)),
        "estimated_minutes": round(max(0, len(missing_theme_dates)) * 0.05, 1),
        "status": "done" if target_theme_dates and len(have_theme_dates) >= min(250, len(target_theme_dates)) else "needed",
        "gap": {
            "have_dates": len(have_theme_dates),
            "need_dates": min(250, len(target_theme_dates) or 250),
            "missing_dates": missing_theme_dates[:60],
            "batch": min(30, market_batch_days),
        },
    })

    # V3 labels are versioned separately from the ordinary 250-day readiness
    # cache. Formal five-window narrative training must never mix v1/v2 labels.
    narrative_formal_labeled_sessions = 1577
    narrative_label_horizon_sessions = 3
    narrative_required_snapshot_sessions = (
        narrative_formal_labeled_sessions + narrative_label_horizon_sessions
    )
    with _conn() as c:
        v3_calendar_rows = c.execute(
            "SELECT cal_date FROM dc_trade_calendar WHERE is_open=1 AND cal_date<=? ORDER BY cal_date DESC LIMIT ?",
            (theme_end_s, narrative_required_snapshot_sessions),
        ).fetchall()
    v3_open_days = [str(dict(row).get("cal_date") or "") for row in v3_calendar_rows if dict(row).get("cal_date")]
    v3_target_dates = list(reversed(v3_open_days)) if v3_open_days else []
    v3_model_version = "industry_price_fundamental_theme_v3"
    have_v3_dates = _theme_snapshot_dates_for_version(v3_target_dates, v3_model_version)
    missing_v3_dates = [trade_date for trade_date in reversed(v3_target_dates) if trade_date not in have_v3_dates]
    add({
        "priority": 2,
        "key": "theme_snapshots_lifecycle_v3",
        "title": "补全叙事生命周期 V3 历史标签",
        "why": (
            "正式叙事五窗口只允许 V3 标签；3日预测周期需要额外3个快照日，"
            f"当前 {len(have_v3_dates)}/{len(v3_target_dates) or narrative_required_snapshot_sessions} 个快照日，禁止混入旧版标签"
        ),
        "action": "materialize_theme_snapshots_batch",
        "params": {
            "end_date": theme_end_s,
            "target_sessions": narrative_required_snapshot_sessions,
            "limit_dates": min(20, market_batch_days),
        },
        "estimated_calls": max(0, len(missing_v3_dates)),
        "estimated_minutes": round(max(0, len(missing_v3_dates)) * 0.05, 1),
        "status": "done" if v3_target_dates and len(have_v3_dates) >= len(v3_target_dates) else "needed",
        "gap": {
            "have_dates": len(have_v3_dates),
            "need_dates": len(v3_target_dates) or narrative_required_snapshot_sessions,
            "required_labeled_dates": narrative_formal_labeled_sessions,
            "label_horizon_sessions": narrative_label_horizon_sessions,
            "missing_dates": missing_v3_dates[:60],
            "model_version": v3_model_version,
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

    # 4a. Field-level research readiness is repaired for the whole market by
    # report period. One step calls four VIP endpoints, not thousands of
    # per-stock endpoints. Rebuilding the plan verifies the persisted fields.
    financial_field_asset = next(
        (item for item in assets.get("items") or [] if item.get("key") == "financial_research_fields"),
        {},
    )
    financial_field_states = {
        str(item.get("period") or ""): item
        for item in financial_field_asset.get("period_details") or []
    }
    for period in quarters:
        field_state = financial_field_states.get(period, {})
        if field_state.get("status") == "complete":
            continue
        if field_state.get("status") in {"upstream_unavailable", "not_applicable", "permission_denied"}:
            add({
                "priority": 3, "key": f"financial_research_terminal_{period}",
                "title": f"财务字段终态缺口 {period}",
                "why": str(field_state.get("terminal_reason") or "连续补偿无进展，等待上游能力变化或人工重置"),
                "action": "upstream_residual_unavailable", "params": {"period": period},
                "source_asset_key": "financial_research_fields", "source_slice_key": period,
                "estimated_calls": 0, "estimated_minutes": 0, "status": "blocked",
                "gap": {"have": int(field_state.get("complete_checks") or 0), "need": int(field_state.get("checks") or 0)},
            })
            continue
        if field_state.get("batch_attempted"):
            incomplete_symbols = int(field_state.get("incomplete_symbols") or 0)
            add({
                "priority": 3, "key": f"financial_research_residual_{period}",
                "title": f"按报告期补偿财务字段残余 {period}",
                "why": f"数据资产验收发现该报告期 VIP 整批后仍有字段残余；普通单股接口实测可补回，仅处理 {incomplete_symbols} 只缺口股票",
                "action": "sync_financial_research_residual_batch", "params": {"period": period, "limit": financial_batch_size},
                "source_asset_key": "financial_research_fields", "source_slice_key": period,
                "estimated_calls": min(financial_batch_size, incomplete_symbols),
                "estimated_minutes": round(min(financial_batch_size, incomplete_symbols) * 0.03, 1), "status": "needed",
                "gap": {"have": int(field_state.get("complete_checks") or 0), "need": int(field_state.get("checks") or 0),
                        "incomplete_symbols": incomplete_symbols,
                        "missing_by_api": field_state.get("missing_by_api") or {}},
            })
            continue
        add({
            "priority": 3,
            "key": f"financial_research_fields_{period}",
            "title": f"按报告期补全全市场研究财务字段 {period}",
            "why": f"全市场字段契约覆盖 {int(field_state.get('complete_checks') or 0)}/{int(field_state.get('checks') or 0)}；缺口 {field_state.get('missing_by_api')}",
            "action": "sync_financial_research_period",
            "source_asset_key": "financial_research_fields", "source_slice_key": period,
            "params": {"period": period},
            "estimated_calls": 4,
            "estimated_minutes": 1.0,
            "status": "needed",
            "gap": {"have": int(field_state.get("complete_checks") or 0), "need": int(field_state.get("checks") or 0),
                    "missing": int(field_state.get("checks") or 0) - int(field_state.get("complete_checks") or 0),
                    "missing_by_api": field_state.get("missing_by_api") or {}},
        })

    # 4b. Additional TuShare APIs verified by real small-sample calls.
    latest_period = quarters[0] if quarters else ""
    stock_routes = [
        ("namechange", "补全名称/ST历史", ""),
        ("forecast", "补全业绩预告", latest_period),
        ("pledge_stat", "补全质押统计", ""),
        ("pledge_detail", "补全质押明细", ""),
        ("express", "补全业绩快报", latest_period),
    ]
    for period in quarters:
        stock_routes.extend([
            ("top10_holders", f"补全十大股东 {period}", period),
            ("top10_floatholders", f"补全十大流通股东 {period}", period),
            ("stk_holdernumber", f"补全股东人数 {period}", period),
        ])
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
            "key": f"stock_api_{api_name}_{period}" if period else f"stock_api_{api_name}",
            "title": title,
            "why": f"{api_name} 优先按报告期/日期窗批量获取，失败才逐股断点补偿；空结果也记录以避免重复请求",
            "action": "sync_stock_api_batch",
            "params": {"api_name": api_name, "period": period, "limit": financial_batch_size},
            "estimated_calls": estimated_calls, "estimated_minutes": estimated_minutes,
            "status": "done" if batch_checked or (stocks > 0 and checked >= stocks) else "needed",
            "gap": {"checked_symbols": checked, "need_symbols": stocks, "batch": financial_batch_size, "period_batch_checked": batch_checked},
        })
    all_historical_open_days = _open_dates("20100101", end_s)
    moneyflow_open_days = all_historical_open_days[-(PLAN_MODEL_REQUIRED_LABELED_SESSIONS + 30):]
    for api_name, title in (("suspend_d", "补全停复牌检查"), ("moneyflow", "补全资金流")):
        target_days = moneyflow_open_days if api_name == "moneyflow" else open_days
        target_start = target_days[0] if target_days else start_s
        checked_dates = _request_date_coverage(api_name, target_start, end_s)
        remaining_dates = max(0, len(target_days) - checked_dates)
        add({
            "priority": 2 if api_name == "moneyflow" else 4,
            "key": f"date_api_{api_name}",
            "title": title,
            "why": (
                "资金模型正式五窗要求1,577个可标注日，按交易日全市场回补并校验至少500行及收盘点时"
                if api_name == "moneyflow"
                else f"{api_name} 已通过真实接口验证；按交易日分批，成功空结果也记为已检查"
            ),
            "action": "sync_date_api_batch",
            "params": {
                "api_name": api_name,
                "trade_dates": target_days,
                "limit": market_batch_days,
                "require_rows": api_name == "moneyflow",
                "minimum_rows": 500 if api_name == "moneyflow" else 1,
                "require_point_in_time": api_name == "moneyflow",
            },
            # Date requests are executed by the shared 16-worker pool. Keep
            # the call count exact, but estimate wall time by concurrent waves;
            # any TuShare cooldown is added dynamically by the worker.
            "estimated_calls": remaining_dates,
            "estimated_minutes": round((remaining_dates / 16) * 0.2, 1),
            "status": "done" if target_days and checked_dates >= len(target_days) else "needed",
            "gap": {"checked_dates": checked_dates, "need_dates": len(target_days), "batch": market_batch_days},
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

    # Data assets are the sole inventory source for repair planning. Consumer
    # readiness remains a view over those assets, not a second hidden gap list.
    contract_gaps: list[dict[str, Any]] = []

    # Surface every weighted asset gap, including those without a safe
    # automatic implementation. Hidden gaps are more dangerous than blocked
    # steps because they make the repair workflow look complete.
    covered_asset_keys = {
        "trade_calendar", "daily", "valuation", "limit_price", "snapshot", "adj_factor",
        "namechange", "suspension", "moneyflow", "top10_holders", "top10_floatholders",
        "stk_holdernumber", "pledge_stat", "express", "industry_index", "index_weight",
        "income", "balancesheet", "cashflow", "financial_indicator", "fina_audit",
        "fina_mainbz", "financial_research_fields", "announcements", "macro",
        "capital_structure_vip", "etf_price_nav_vip", "major_news_vip",
        "vip_moneyflow_hsgt", "vip_hk_hold", "vip_margin", "vip_margin_detail",
        "vip_margin_secs", "vip_top_list", "vip_top_inst", "vip_fund_basic",
        "vip_fund_daily", "vip_fund_nav",
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
        "gap_source": "data_assets",
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
        "contract_gaps": contract_gaps,
        "steps": steps,
        "execution": {
            "supported_actions": [
                "sync_trade_calendar",
                "sync_market_batch",
                "sync_oos_hard_data_batch",
                "sync_financial_batch",
                "sync_financial_research_period",
                "sync_financial_research_residual_batch",
                "sync_announcements",
                "run_quality",
                "sync_stock_api_batch",
                "sync_date_api_batch",
                "sync_index_core",
                "sync_macro_core",
                "sync_capital_history_batch",
                "sync_etf_history_batch",
                "sync_major_news_history_batch",
                "sync_exchange_etf_pcf_batch",
                "materialize_theme_snapshots_batch",
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


def _completed_gap_message(plan: dict[str, Any], blocked_steps: int) -> str:
    blocked = [step for step in plan.get("steps") or [] if step.get("status") == "blocked"]
    residual_periods = [
        str((step.get("params") or {}).get("period") or "")
        for step in blocked
        if step.get("action") == "upstream_residual_unavailable"
    ]
    if blocked and len(residual_periods) == len(blocked):
        return f"自动补全已完成；{len(residual_periods)} 个报告期仍有上游未返回字段，系统已停止重复请求"
    return f"自动补全已完成；仍有 {blocked_steps} 项无法自动处理的缺口"


def get_backfill_status() -> dict[str, Any]:
    cached = get_json("alis:data-platform:backfill:state")
    if isinstance(cached, dict):
        if str(cached.get("phase") or "") == "completed_with_gaps" and isinstance(cached.get("plan"), dict):
            blocked_steps = int(((cached.get("plan") or {}).get("summary") or {}).get("steps_blocked") or 0)
            cached["message"] = _completed_gap_message(cached["plan"], blocked_steps)
        if not cached.get("running") and str(cached.get("phase") or "") == "interrupted":
            cached = terminate_orphaned_backfill(reason="服务重启后原工作线程已不存在")
        _ensure_auto_resume_alive(cached)
        # A completed state already persists the last plan summary. Do not
        # rebuild the historical gap plan from a status poll: the dashboard
        # calls this endpoint frequently and a rebuild scans large tables.
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
        due = _resume_due(resume_at)
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


def terminate_orphaned_backfill(*, reason: str = "工作线程已不存在") -> dict[str, Any]:
    """Terminate a persisted task only after its in-process worker is gone."""
    cached = get_json("alis:data-platform:backfill:state")
    snapshot = dict(cached) if isinstance(cached, dict) else dict(_state)
    if snapshot.get("running"):
        return {"ok": False, **snapshot, "reason": "worker_still_running"}
    run_id = int(snapshot.get("run_id") or 0)
    now = _now()
    if run_id:
        with _conn() as c:
            c.execute(
                """UPDATE data_ingestion_runs SET status='cancelled',finished_at=?,error=?
                   WHERE id=? AND status IN ('running','interrupted')""",
                (now, f"孤儿任务已终止：{reason}", run_id),
            )
    stream_id = str(snapshot.get("stream_id") or "")
    if stream_id:
        try:
            ack_task(stream_id)
        except Exception:
            pass
    normalized = {
        **snapshot, "ok": True, "running": False, "phase": "cancelled",
        "message": "旧补全任务已终止；已写入数据保留，可一键重新生成补全计划",
        "finished_at": now, "error": "", "auto_resume": False,
        "can_resume": False, "resume_action": "", "current_step": None,
        "activity": None, "failure_detail": None, "stream_id": None,
    }
    with _lock:
        _state.clear()
        _state.update(normalized)
    set_json("alis:data-platform:backfill:state", normalized, ttl=86400)
    return normalized


def recover_interrupted_backfill() -> dict[str, Any]:
    """Kill Redis/DB tasks whose worker disappeared during a process restart."""
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
                    "UPDATE data_ingestion_runs SET status='cancelled',finished_at=?,error=? WHERE id=?",
                    (now, "数据中台进程重启，原工作线程已不存在；孤儿任务自动终止", stale_id),
                )
    if was_running or stale_ids:
        snapshot = {
            **(cached or {}), "running": False, "phase": "cancelled",
            "message": "服务重启后旧补全任务已自动终止；已写入数据保留，可一键重新生成补全计划",
            "finished_at": _now(), "error": "",
            "auto_resume": False,
            "can_resume": False, "resume_action": "",
            "current_step": None,
            "activity": None, "failure_detail": None, "stream_id": None,
        }
        with _lock:
            _state.clear()
            _state.update(snapshot)
        set_json("alis:data-platform:backfill:state", snapshot, ttl=86400)
        return {"recovered": True, "run_id": run_id, "stale_runs": stale_ids}
    if isinstance(cached, dict) and cached.get("phase") == "interrupted":
        terminated = terminate_orphaned_backfill(reason="启动检查发现历史中断状态")
        return {"recovered": True, "terminated": True, "run_id": run_id, "stale_runs": [], "state": terminated}
    if isinstance(cached, dict) and cached.get("phase") == "cancelled":
        return {"recovered": True, "run_id": run_id, "stale_runs": [], "auto_resume": False}
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
            message=f"全部数据补全失败：{error}；该错误不适合自动续跑，请手动处理",
        )
        return
    if AUTO_RESUME_MAX_ATTEMPTS > 0 and attempt > AUTO_RESUME_MAX_ATTEMPTS:
        _set(
            auto_resume=False,
            auto_resume_reason="max_attempts_reached",
            message=f"全部数据补全失败：{error}；已自动续跑 {AUTO_RESUME_MAX_ATTEMPTS} 次，暂停等待人工处理",
        )
        return
    delay = max(1, int(delay_override_seconds or _auto_resume_delay(attempt)))
    resume_at = (datetime.now(_BUSINESS_TZ) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    _set(
        auto_resume=True,
        auto_resume_attempt=attempt,
        auto_resume_max_attempts=AUTO_RESUME_MAX_ATTEMPTS or None,
        auto_resume_delay_seconds=delay,
        auto_resume_at=resume_at,
        message=f"全部数据补全失败：{error}；{delay} 秒后自动继续（第 {attempt}/{AUTO_RESUME_MAX_ATTEMPTS} 次）",
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
    if action == "sync_oos_hard_data_batch":
        return repair_oos_hard_data_batch(list(params.get("trade_dates") or []), date_gaps=params.get("date_gaps") or {})
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
    if action == "sync_financial_research_period":
        return sync_financial_vip_period(str(params.get("period") or ""), contract_mode="research_fields_v2")
    if action == "sync_financial_research_residual_batch":
        return enrich_financial_research_batch(
            str(params.get("period") or ""), limit=int(params.get("limit") or 100),
        )
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
            minimum_rows=int(params.get("minimum_rows") or 1),
            require_point_in_time=bool(params.get("require_point_in_time")),
        )
    if action == "sync_capital_history_batch":
        return backfill_capital_structure_history(
            start_date=str(params.get("start_date") or "20200101"),
            end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""),
            limit_dates=int(params.get("limit_dates") or 10),
        )
    if action == "sync_etf_history_batch":
        if str(params.get("mode") or "") == "daily":
            return backfill_etf_daily_incremental(
                start_date=str(params.get("start_date") or "20200101"),
                end_date=str(params.get("end_date") or ""),
                cursor=str(params.get("cursor") or ""),
                limit_dates=int(params.get("limit_dates") or 10),
                nav_limit_funds=int(params.get("nav_limit_funds") or 100),
            )
        return backfill_etf_price_history(
            start_date=str(params.get("start_date") or "20200101"),
            end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""),
            limit_funds=int(params.get("limit_funds") or 20),
        )
    if action == "sync_major_news_history_batch":
        return backfill_major_news_history(
            start_date=str(params.get("start_date") or ""),
            end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""),
            limit_days=int(params.get("limit_days") or 7),
        )
    if action == "sync_exchange_etf_pcf_batch":
        return backfill_exchange_etf_pcf(
            start_date=str(params.get("start_date") or ""), end_date=str(params.get("end_date") or ""),
            cursor=str(params.get("cursor") or ""), limit_dates=int(params.get("limit_dates") or 1),
            limit_funds=int(params.get("limit_funds") or 0),
        )
    if action == "materialize_theme_snapshots_batch":
        return materialize_theme_snapshot_history(
            end_date=str(params.get("end_date") or ""),
            target_sessions=int(params.get("target_sessions") or 250),
            limit_dates=int(params.get("limit_dates") or 20),
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


def _run_step_resumable(step: dict[str, Any]) -> dict[str, Any]:
    """Convert only database statement timeouts into a deferred step result."""
    try:
        return _run_step(step)
    except Exception as exc:  # noqa: BLE001 - unknown exceptions still escalate
        detail = str(exc)[:1000]
        if "statement timeout" not in detail.lower():
            raise
        return {
            "ok": False,
            "failed": 1,
            "deferred": True,
            "error_category": "database_statement_timeout",
            "error": detail,
            "results": [],
        }


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
                "sync_trade_calendar", "sync_market_batch", "sync_financial_batch", "sync_financial_research_period", "sync_financial_research_residual_batch",
                "sync_oos_hard_data_batch",
                "sync_announcements", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core",
                "sync_capital_history_batch", "sync_etf_history_batch", "sync_major_news_history_batch",
                "sync_exchange_etf_pcf_batch",
                "materialize_theme_snapshots_batch",
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
            result = _run_step_resumable(step)
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
                    elif int(result.get("failed") or result.get("subtasks_failed") or 0) > 0:
                        # A hard/permission/upstream gap must not starve later
                        # executable assets (notably V3 lifecycle labels). Keep
                        # the failed step in the persisted deferred list and
                        # let the bounded auto-resume retry it after the rest
                        # of the plan has advanced.
                        step_key = str(step.get("key") or step.get("id") or "")
                        deferred_keys.add(step_key)
                        deferred.append({
                            "step": {k: step.get(k) for k in ("id", "key", "title", "action")},
                            "reason": (detail or "本批存在未解决缺口，已让其他数据资产先继续")[:1000],
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
                    and s.get("action") in {"sync_market_batch", "sync_oos_hard_data_batch", "sync_financial_batch", "sync_financial_research_period", "sync_financial_research_residual_batch", "sync_announcements", "run_quality", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core", "sync_capital_history_batch", "sync_etf_history_batch", "sync_major_news_history_batch", "sync_exchange_etf_pcf_batch", "materialize_theme_snapshots_batch"}
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
                    and s.get("action") in {"sync_trade_calendar", "sync_market_batch", "sync_oos_hard_data_batch", "sync_financial_batch", "sync_financial_research_period", "sync_financial_research_residual_batch", "sync_announcements", "sync_stock_api_batch", "sync_date_api_batch", "sync_index_core", "sync_macro_core", "sync_capital_history_batch", "sync_etf_history_batch", "sync_major_news_history_batch", "sync_exchange_etf_pcf_batch", "materialize_theme_snapshots_batch"}
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
                        asset_key = str(current_remaining.get("source_asset_key") or "")
                        slice_key = str(current_remaining.get("source_slice_key") or "overall")
                        if asset_key:
                            mark_asset_terminal(
                                asset_key, slice_key, "upstream_unavailable",
                                f"连续 {DEFER_EMPTY_AFTER_ATTEMPTS} 个批次执行后缺口没有变化",
                            )
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
            message=("全部数据补全失败：" + error) if error else (
                f"自动补全已停止；{len(deferred)} 个步骤无进展或失败，仍有缺口待处理" if deferred
                else "全部数据补全已完成，当前无剩余缺口" if fully_done
                else _completed_gap_message(final_plan, blocked_steps)
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
        # A failed worker is terminal. Rebuilding/restarting it from a timer
        # made an explicitly stopped job appear to start forever.
        should_auto_resume = False
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:1000]
        invalidate_data_assets_cache()
        _set(
            running=False,
            phase="failed",
            message=f"全部数据补全失败：{error}",
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
        should_auto_resume = False
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
                    _completed_gap_message(plan, steps_blocked)
                    if steps_blocked > 0 else "全部数据补全已完成，当前无剩余缺口"
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
                "message": "正在启动全部数据补全",
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
    task_type = str(fields.get("task_type") or "")
    if task_type == MODEL_DATA_BACKFILL_TASK:
        try:
            payload = json.loads(fields.get("payload_json") or "{}")
        except Exception:
            payload = {}
        try:
            result = _process_plan_model_data_backfill(payload)
        except Exception as exc:
            _set_model_data_backfill_state(
                status="retry_wait",
                error=str(exc)[:1000],
                last_failure_at=_now(),
            )
            raise
        if not bool(result.get("ok")):
            _set_model_data_backfill_state(
                status="retry_wait",
                error=str(
                    result.get("error")
                    or result.get("reason")
                    or "plan_model_data_backfill_failed"
                )[:1000],
                last_failure_at=_now(),
            )
        return result
    if task_type != "data_platform.backfill":
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


def _set_model_data_backfill_state(**values: Any) -> dict[str, Any]:
    state = get_json(MODEL_DATA_BACKFILL_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
    state.update(values)
    state["updated_at"] = _now()
    set_json(MODEL_DATA_BACKFILL_STATE_KEY, state, ttl=86400 * 30)
    _model_backfill_status_cache.update({"at": 0.0, "value": None})
    return state


def _model_data_backfill_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "ok",
            "api",
            "total",
            "success",
            "failed",
            "rows_written",
            "days_processed",
            "months_processed",
            "periods_processed",
            "requests_executed",
            "requests_skipped",
            "normalized_available_time_rows",
            "cursor",
            "has_more",
            "cooldown",
            "cooldown_seconds",
            "error",
        )
        if result.get(key) is not None
    }


def enqueue_plan_model_data_backfill(
    *,
    phase: str = "moneyflow",
    cursor: str = "",
    trade_dates: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"phase": phase, "cursor": cursor}
    if trade_dates:
        payload["trade_dates"] = list(dict.fromkeys(str(item) for item in trade_dates if str(item)))[:120]
    message_id = enqueue_task(
        MODEL_DATA_BACKFILL_TASK,
        payload,
    )
    state = _set_model_data_backfill_state(
        status="queued" if message_id else "queue_unavailable",
        contract=MODEL_DATA_BACKFILL_CONTRACT,
        post_backfill_audit={},
        phase=phase,
        cursor=cursor,
        message_id=message_id or "",
        next_message_id="",
        residual_count=len(payload.get("trade_dates") or []),
    )
    return {"ok": bool(message_id), **state}


def _moneyflow_required_coverage() -> tuple[list[str], set[str], list[str]]:
    required_raw = PLAN_MODEL_REQUIRED_LABELED_SESSIONS + 30
    with _conn() as c:
        rows = c.execute(
            """SELECT cal.cal_date,
                      EXISTS (
                          SELECT 1
                          FROM dc_tushare_raw_records raw
                          WHERE raw.api_name='moneyflow'
                            AND raw.trade_date=cal.cal_date
                            AND raw.available_time<>''
                      ) covered
               FROM (
                   SELECT cal_date
                   FROM dc_trade_calendar
                   WHERE exchange='SSE' AND is_open=1 AND cal_date<=?
                   ORDER BY cal_date DESC
                   LIMIT ?
               ) cal
               ORDER BY cal.cal_date ASC""",
            (datetime.now().strftime("%Y%m%d"), required_raw),
        ).fetchall()
    trade_dates = [str(dict(row).get("cal_date") or "") for row in rows]
    covered_dates = {
        str(dict(row).get("cal_date") or "")
        for row in rows
        if bool(dict(row).get("covered"))
    }
    missing_dates = [item for item in trade_dates if item and item not in covered_dates]
    return trade_dates, covered_dates, missing_dates


def _model_moneyflow_checklist() -> dict[str, Any]:
    required_raw = PLAN_MODEL_REQUIRED_LABELED_SESSIONS + 30
    trade_dates, covered_dates, missing_dates = _moneyflow_required_coverage()
    raw_sessions = len(covered_dates)
    effective_sessions = max(0, raw_sessions - 30)
    return {
        "id": "capital_moneyflow_history",
        "label": "资金持续模型：全市场 moneyflow 历史",
        "status": (
            "completed"
            if len(trade_dates) >= required_raw and not missing_dates
            else "running"
        ),
        "target_raw_sessions": required_raw,
        "covered_raw_sessions": raw_sessions,
        "target_effective_sessions": PLAN_MODEL_REQUIRED_LABELED_SESSIONS,
        "covered_effective_sessions": effective_sessions,
        "missing_count": len(missing_dates),
        "missing_dates": missing_dates[:120],
        "window_start": trade_dates[0] if trade_dates else "",
        "window_end": trade_dates[-1] if trade_dates else "",
        "acceptance": (
            "每个交易日全市场批量 moneyflow 至少 500 行、具有点时 available_time；"
            "扣除 20 日特征窗口和 10 日未来标签后达到 1,577 日"
        ),
    }


def _news_source_progress(*, phase: str, cursor: str, status: str) -> dict[str, Any]:
    start = date(2020, 1, 1)
    end = datetime.now().date()
    total_days = max(1, (end - start).days + 1)
    processed_days = 0
    if phase == "major_news":
        try:
            cursor_date = datetime.strptime(cursor, "%Y%m%d").date()
            processed_days = max(0, min(total_days, (cursor_date - start).days + 1))
        except ValueError:
            processed_days = 0
    elif phase in {"event_research", "event_financial_disclosures"} or status == "completed":
        processed_days = total_days
    return {
        "id": "event_major_news_history",
        "label": "事件影响评估：重大新闻历史源检查",
        "status": (
            "completed"
            if status == "completed" or phase in {"event_research", "event_financial_disclosures"}
            else "running"
            if phase == "major_news"
            else "pending"
        ),
        "source_window_start": start.strftime("%Y%m%d"),
        "source_window_end": end.strftime("%Y%m%d"),
        "cursor": cursor if phase == "major_news" else "",
        "processed_calendar_days": processed_days,
        "target_calendar_days": total_days,
        "progress": round(processed_days / total_days, 6),
        "acceptance": (
            "按来源和自然日完整检查 major_news，800 行截断时递归拆分；"
            "完成只证明来源历史已检查，不等于事件模型已达到 1,577 个有效事件日"
        ),
    }


def _event_research_progress(*, phase: str, cursor: str, status: str) -> dict[str, Any]:
    start = date(2019, 1, 1)
    end = datetime.now().date()
    total_days = max(1, (end - start).days + 1)
    processed_days = 0
    if phase == "event_research":
        try:
            cursor_date = datetime.strptime(cursor, "%Y%m%d").date()
            processed_days = max(0, min(total_days, (cursor_date - start).days + 1))
        except ValueError:
            processed_days = 0
    elif phase == "event_financial_disclosures" or status == "completed":
        processed_days = total_days
    return {
        "id": "event_research_history",
        "label": "事件影响评估：一致预期与量化事件字段",
        "status": (
            "completed" if status == "completed" or phase == "event_financial_disclosures" else
            "running" if phase == "event_research" else "pending"
        ),
        "source_window_start": start.strftime("%Y%m%d"),
        "source_window_end": end.strftime("%Y%m%d"),
        "cursor": cursor if phase == "event_research" else "",
        "processed_calendar_days": processed_days,
        "target_calendar_days": total_days,
        "progress": round(processed_days / max(1, total_days), 6),
        "acceptance": (
            "report_rc 按机构/报告期/发布日保留点时预测；repurchase 与 stk_holdertrade "
            "按月切片补全；空结果留证，不伪造一致预期或事件金额"
        ),
    }


def _event_financial_periods() -> list[str]:
    periods: list[str] = []
    today = datetime.now().date()
    for year in range(2019, today.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            end = date(year, month, day)
            if end <= today:
                periods.append(end.strftime("%Y%m%d"))
    return periods


def _event_financial_period_complete(api_name: str, period: str) -> bool:
    with _conn() as c:
        row = c.execute(
            """SELECT 1 FROM dc_tushare_request_coverage
               WHERE api_name=? AND period=?
                 AND result_status IN ('success','valid_empty','not_applicable','deferred_empty')
               LIMIT 1""",
            (api_name, period),
        ).fetchone()
    return row is not None


def _event_financial_progress(*, phase: str, cursor: str, status: str) -> dict[str, Any]:
    periods = _event_financial_periods()
    processed = 0
    if phase == "event_financial_disclosures" and cursor:
        processed = sum(1 for period in periods if period <= cursor)
    elif status == "completed":
        processed = len(periods)
    return {
        "id": "event_financial_disclosures",
        "label": "事件影响评估：业绩预告与快报历史",
        "status": (
            "completed" if status == "completed" else
            "running" if phase == "event_financial_disclosures" else "pending"
        ),
        "cursor": cursor if phase == "event_financial_disclosures" else "",
        "processed_periods": processed,
        "target_periods": len(periods),
        "progress": round(processed / max(1, len(periods)), 6),
        "acceptance": (
            "forecast_vip 与 express_vip 按已结束报告期全市场批量拉取；"
            "预期差只允许同一规范化报告期与至少三家独立机构点时共识比较"
        ),
    }


def audit_plan_model_data_contract() -> dict[str, Any]:
    """Recompute real model-domain coverage after source backfill completes."""

    from app.services.plan_model_control import (
        _base_coverage_snapshot,
        _coverage_snapshot,
    )

    base = _base_coverage_snapshot()
    models = {
        model_key: _coverage_snapshot(model_key, base)
        for model_key in (
            "capital_persistence_multitask_v1",
            "event_impact_assessor_v2",
        )
    }
    return {
        "schema_version": "alis_plan_model_data_contract_audit_v1",
        "status": "completed",
        "audited_at": _now(),
        "models": models,
        "all_domain_minimum_ready": all(
            bool(item.get("domain_minimum_ready")) for item in models.values()
        ),
        "all_formal_history_ready": all(
            bool(item.get("formal_history_ready")) for item in models.values()
        ),
        "policy": (
            "audit_execution_may_complete_while_a_model_remains_not_ready;"
            "source_scan_never_fabricates_effective_event_sessions"
        ),
    }


def get_plan_model_data_backfill_status(*, cache_seconds: int = 15) -> dict[str, Any]:
    """Build the read-only work-order view consumed by the task center."""
    import time

    now = time.monotonic()
    cached = _model_backfill_status_cache.get("value")
    if (
        isinstance(cached, dict)
        and now - float(_model_backfill_status_cache.get("at") or 0)
        < max(1, cache_seconds)
    ):
        return cached
    state = get_json(MODEL_DATA_BACKFILL_STATE_KEY)
    if not isinstance(state, dict) or not state:
        result = {
            "ok": True,
            "schema_version": "alis_model_data_backfill_work_order_v3",
            "status": "not_started",
            "phase": "idle",
            "progress": 0.0,
            "checklist": [],
            "queue": stream_health(),
        }
        _model_backfill_status_cache.update({"at": now, "value": result})
        return result
    phase = str(state.get("phase") or "moneyflow")
    raw_status = str(state.get("status") or "queued")
    cursor = str(state.get("cursor") or "")
    post_backfill_audit = (
        state.get("post_backfill_audit")
        if isinstance(state.get("post_backfill_audit"), dict)
        else {}
    )
    message_id = str(state.get("next_message_id") or state.get("message_id") or "")
    attempts = task_attempts(message_id)
    moneyflow = _model_moneyflow_checklist()
    news = _news_source_progress(phase=phase, cursor=cursor, status=raw_status)
    event_research = _event_research_progress(
        phase=phase, cursor=cursor, status=raw_status
    )
    event_financial = _event_financial_progress(
        phase=phase, cursor=cursor, status=raw_status
    )
    trade_start = str(moneyflow.get("window_start") or "")
    trade_end = str(moneyflow.get("window_end") or "")
    processed_trade_dates = 0
    target_raw = int(moneyflow.get("target_raw_sessions") or 1)
    if phase == "moneyflow" and cursor and trade_start and trade_end:
        with _conn() as c:
            row = c.execute(
                """SELECT COUNT(*) n
                   FROM dc_trade_calendar
                   WHERE exchange='SSE' AND is_open=1
                     AND cal_date BETWEEN ? AND ? AND cal_date<=?""",
                (trade_start, trade_end, cursor),
            ).fetchone()
        processed_trade_dates = int(dict(row).get("n") or 0)
    elif phase in {"moneyflow_residual", "major_news", "event_research", "event_financial_disclosures"} or raw_status == "completed":
        processed_trade_dates = target_raw
    moneyflow["cursor"] = cursor if phase == "moneyflow" else trade_end
    moneyflow["processed_trade_dates"] = min(target_raw, processed_trade_dates)
    moneyflow["progress"] = round(
        min(target_raw, processed_trade_dates) / max(1, target_raw),
        6,
    )
    contract_current = str(state.get("contract") or "") == MODEL_DATA_BACKFILL_CONTRACT
    if raw_status == "completed" and not contract_current:
        normalized_status = "upgrade_required"
    elif raw_status == "completed":
        normalized_status = "completed"
    elif raw_status in {"failed", "queue_unavailable"}:
        normalized_status = "failed"
    elif raw_status == "retry_wait" or attempts > 0:
        normalized_status = (
            "failed" if attempts >= STREAM_MAX_ATTEMPTS else "retry_wait"
        )
    elif raw_status == "queued":
        normalized_status = "queued"
    else:
        normalized_status = "running"
    progress = round(
        (
            float(moneyflow.get("progress") or 0.0)
            + float(news.get("progress") or 0.0)
            + float(event_research.get("progress") or 0.0)
            + float(event_financial.get("progress") or 0.0)
        )
        / 4.0,
        6,
    )
    result = {
        "ok": True,
        "schema_version": "alis_model_data_backfill_work_order_v3",
        "work_order_id": "plan-model-history-20260731-v3",
        "task_type": MODEL_DATA_BACKFILL_TASK,
        "status": normalized_status,
        "raw_status": raw_status,
        "phase": phase,
        "contract": str(state.get("contract") or ""),
        "contract_current": contract_current,
        "cursor": cursor,
        "progress": 1.0 if normalized_status == "completed" else progress,
        "message_id": message_id,
        "attempt": attempts + 1 if normalized_status in {"running", "retry_wait"} else attempts,
        "max_attempts": STREAM_MAX_ATTEMPTS,
        "error": str(state.get("error") or ""),
        "updated_at": state.get("updated_at"),
        "last_result": state.get("last_result") or {},
        "checklist": [
            moneyflow,
            news,
            event_research,
            event_financial,
            {
                "id": "post_backfill_contract_audit",
                "label": "补数后重新计算模型数据门",
                "status": (
                    "completed"
                    if str(post_backfill_audit.get("status") or "") == "completed"
                    else "pending"
                    if normalized_status != "completed"
                    else "audit_missing"
                ),
                "result": post_backfill_audit,
                "acceptance": (
                    "资金按 1,577 个有效交易日验收；事件按真实可映射事件样本重新审计，"
                    "不得因为来源扫描完成就伪造 1,577 个事件日"
                ),
            },
        ],
        "queue": stream_health(),
        "intervention": {
            "automatic": (
                "瞬时异常由 Redis 持久消息最多重试 "
                f"{STREAM_MAX_ATTEMPTS} 次；重启后从游标恢复"
            ),
            "current": (
                "检查死信和上游/权限异常，保持模型 N/A"
                if normalized_status == "failed"
                else "系统自行处理，无需人工操作"
            ),
            "forbidden": "不得伪造空结果、放宽点时门或绕过模型评价合同",
        },
    }
    _model_backfill_status_cache.update({"at": now, "value": result})
    return result


def ensure_plan_model_data_backfill() -> dict[str, Any]:
    """Idempotently start the registered model-data checklist when absent."""
    status = get_plan_model_data_backfill_status(cache_seconds=1)
    current = str(status.get("status") or "not_started")
    if current in {"queued", "running", "retry_wait"}:
        return {"ok": True, "created": False, "reason": "already_active", "task": status}
    if current == "upgrade_required":
        previous_contract = str(status.get("contract") or "")
        upgrade_phase = (
            "event_financial_disclosures"
            if previous_contract == PREVIOUS_MODEL_DATA_BACKFILL_CONTRACT
            else "event_research"
        )
        queued = enqueue_plan_model_data_backfill(
            phase=upgrade_phase, cursor="20181231"
        )
        _model_backfill_status_cache.update({"at": 0.0, "value": None})
        return {
            "ok": bool(queued.get("ok")),
            "created": bool(queued.get("ok")),
            "reason": "contract_upgrade",
            "task": queued,
        }
    if current == "completed":
        return {"ok": True, "created": False, "reason": "work_order_completed", "task": status}
    if current == "failed":
        return {
            "ok": False,
            "created": False,
            "reason": "dead_letter_or_retry_budget_exhausted",
            "task": status,
        }
    queued = enqueue_plan_model_data_backfill(phase="moneyflow", cursor="")
    _model_backfill_status_cache.update({"at": 0.0, "value": None})
    return {"ok": bool(queued.get("ok")), "created": bool(queued.get("ok")), "task": queued}


def _normalize_moneyflow_available_time(
    trade_dates: list[str],
    *,
    chunk_size: int = 10,
) -> int:
    """Normalize historical economic availability without one oversized UPDATE."""
    normalized = 0
    size = max(1, min(int(chunk_size), 20))
    for offset in range(0, len(trade_dates), size):
        batch = trade_dates[offset:offset + size]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        with _conn() as c:
            result = c.execute(
                f"""UPDATE dc_tushare_raw_records
                    SET available_time=SUBSTRING(trade_date,1,4)||'-'||
                                       SUBSTRING(trade_date,5,2)||'-'||
                                       SUBSTRING(trade_date,7,2)||'T15:00:00'
                    WHERE api_name='moneyflow'
                      AND trade_date IN ({placeholders})
                      AND available_time<>SUBSTRING(trade_date,1,4)||'-'||
                                          SUBSTRING(trade_date,5,2)||'-'||
                                          SUBSTRING(trade_date,7,2)||'T15:00:00'""",
                tuple(batch),
            )
            normalized += max(0, int(getattr(result, "rowcount", 0) or 0))
    return normalized


def _normalize_event_disclosure_available_time(*, chunk_size: int = 50_000) -> int:
    """Repair legacy analyst-report rows written with ingestion-time availability.

    ``report_rc`` is published on its ``trade_date``.  Older ingestion code did
    not classify it as a disclosure API, so historical rows carried the fetch
    timestamp and became unusable to point-in-time event training.  The update
    is monotonic to the source publication date and leaves raw payloads intact.
    """
    normalized = 0
    size = max(1_000, min(int(chunk_size), 100_000))
    last_id = 0
    while True:
        with _conn() as c:
            date_clause = (
                "trade_date ~ '^[0-9]{8}$'"
                if getattr(c, "_is_postgres", False)
                else "trade_date GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'"
            )
            target_time = (
                "SUBSTRING(trade_date,1,4)||'-'||"
                "SUBSTRING(trade_date,5,2)||'-'||"
                "SUBSTRING(trade_date,7,2)||'T23:59:59'"
            )
            ids = c.execute(
                """SELECT id FROM dc_tushare_raw_records
                   WHERE api_name='report_rc' AND id>?
                   ORDER BY id LIMIT ?""",
                (last_id, size),
            ).fetchall()
            values = [
                int(dict(row).get("id") or 0)
                for row in ids
                if int(dict(row).get("id") or 0) > last_id
            ]
            if not values:
                return normalized
            upper_id = values[-1]
            if upper_id <= last_id:
                return normalized
            result = c.execute(
                f"""UPDATE dc_tushare_raw_records
                    SET available_time={target_time}
                    WHERE api_name='report_rc'
                      AND id>? AND id<=? AND {date_clause}
                      AND available_time<>{target_time}""",
                (last_id, upper_id),
            )
            changed = max(0, int(getattr(result, "rowcount", 0) or 0))
            normalized += changed
            last_id = upper_id


def _process_plan_model_data_backfill(payload: dict[str, Any]) -> dict[str, Any]:
    phase = str(payload.get("phase") or "moneyflow")
    cursor = str(payload.get("cursor") or "")
    _set_model_data_backfill_state(status="running", phase=phase, cursor=cursor, error="")
    legacy_event_rows = (
        _normalize_event_disclosure_available_time()
        if phase == "moneyflow" and not cursor
        else 0
    )
    if phase == "moneyflow":
        with _conn() as c:
            rows = c.execute(
                """SELECT cal_date
                   FROM dc_trade_calendar
                   WHERE exchange='SSE' AND is_open=1 AND cal_date<=?
                   ORDER BY cal_date DESC LIMIT ?""",
                (
                    datetime.now().strftime("%Y%m%d"),
                    PLAN_MODEL_REQUIRED_LABELED_SESSIONS + 30,
                ),
            ).fetchall()
        trade_dates = sorted(
            str(dict(row).get("cal_date") or "")
            for row in rows
            if dict(row).get("cal_date")
        )
        start_index = 0
        if cursor:
            start_index = bisect_right(trade_dates, cursor)
        batch_dates = trade_dates[start_index:start_index + 60]
        if not batch_dates:
            _, _, missing_dates = _moneyflow_required_coverage()
            if missing_dates:
                next_task = enqueue_plan_model_data_backfill(
                    phase="moneyflow_residual",
                    cursor="",
                    trade_dates=missing_dates,
                )
                _set_model_data_backfill_state(
                    status="running",
                    phase="moneyflow_residual",
                    cursor="",
                    residual_count=len(missing_dates),
                    next_message_id=next_task.get("message_id") or "",
                )
                return {
                    "ok": True,
                    "phase": phase,
                    "completed": False,
                    "next_phase": "moneyflow_residual",
                    "residual_count": len(missing_dates),
                }
            next_task = enqueue_plan_model_data_backfill(phase="major_news", cursor="20191231")
            _set_model_data_backfill_state(
                status="moneyflow_completed",
                phase="major_news",
                cursor=cursor,
                next_message_id=next_task.get("message_id") or "",
            )
            return {"ok": True, "phase": phase, "completed": True}
        normalized_rows = _normalize_moneyflow_available_time(batch_dates)
        result = sync_tushare_date_batch(
            "moneyflow",
            trade_dates=batch_dates,
            limit=60,
            require_rows=True,
            minimum_rows=500,
            require_point_in_time=True,
        )
        result["normalized_available_time_rows"] = normalized_rows
        result["normalized_event_disclosure_rows"] = legacy_event_rows
        if not result.get("ok"):
            _set_model_data_backfill_state(
                status="failed",
                phase=phase,
                error=str(result.get("error") or "moneyflow_backfill_failed"),
                last_result=_model_data_backfill_result_summary(result),
            )
            return result
        if int(result.get("total") or 0) > 0:
            next_cursor = batch_dates[-1]
            next_task = enqueue_plan_model_data_backfill(phase="moneyflow", cursor=next_cursor)
            _set_model_data_backfill_state(
                status="running",
                phase=phase,
                cursor=next_cursor,
                completed_in_batch=int(result.get("success") or 0),
                next_message_id=next_task.get("message_id") or "",
                last_result=_model_data_backfill_result_summary(result),
            )
        else:
            next_cursor = batch_dates[-1]
            next_task = enqueue_plan_model_data_backfill(phase="moneyflow", cursor=next_cursor)
            _set_model_data_backfill_state(
                status="running",
                phase=phase,
                cursor=next_cursor,
                next_message_id=next_task.get("message_id") or "",
                last_result=_model_data_backfill_result_summary(result),
            )
        return {"ok": True, "phase": phase, "result": result}
    if phase == "moneyflow_residual":
        batch_dates = list(
            dict.fromkeys(
                str(item)
                for item in (payload.get("trade_dates") or [])
                if str(item)
            )
        )[:60]
        if not batch_dates:
            _, _, batch_dates = _moneyflow_required_coverage()
            batch_dates = batch_dates[:60]
        if batch_dates:
            normalized_rows = _normalize_moneyflow_available_time(batch_dates)
            result = sync_tushare_date_batch(
                "moneyflow",
                trade_dates=batch_dates,
                limit=60,
                require_rows=True,
                minimum_rows=500,
                require_point_in_time=True,
            )
            result["normalized_available_time_rows"] = normalized_rows
            if not result.get("ok"):
                _set_model_data_backfill_state(
                    status="failed",
                    phase=phase,
                    error=str(result.get("error") or "moneyflow_residual_backfill_failed"),
                    last_result=_model_data_backfill_result_summary(result),
                )
                return result
        else:
            result = {
                "ok": True,
                "api": "moneyflow",
                "total": 0,
                "success": 0,
                "failed": 0,
            }
        _, _, remaining = _moneyflow_required_coverage()
        if remaining:
            next_task = enqueue_plan_model_data_backfill(
                phase="moneyflow_residual",
                cursor="",
                trade_dates=remaining,
            )
            _set_model_data_backfill_state(
                status="running",
                phase=phase,
                cursor="",
                residual_count=len(remaining),
                next_message_id=next_task.get("message_id") or "",
                last_result=_model_data_backfill_result_summary(result),
            )
        else:
            next_task = enqueue_plan_model_data_backfill(
                phase="major_news",
                cursor="20191231",
            )
            _set_model_data_backfill_state(
                status="moneyflow_completed",
                phase="major_news",
                cursor="20191231",
                residual_count=0,
                next_message_id=next_task.get("message_id") or "",
                last_result=_model_data_backfill_result_summary(result),
            )
        return {
            "ok": True,
            "phase": phase,
            "result": result,
            "remaining": len(remaining),
        }
    if phase == "major_news":
        _, _, moneyflow_missing = _moneyflow_required_coverage()
        if moneyflow_missing:
            next_task = enqueue_plan_model_data_backfill(
                phase="moneyflow_residual",
                cursor="",
                trade_dates=moneyflow_missing,
            )
            _set_model_data_backfill_state(
                status="running",
                phase="moneyflow_residual",
                cursor="",
                residual_count=len(moneyflow_missing),
                next_message_id=next_task.get("message_id") or "",
            )
            return {
                "ok": True,
                "phase": phase,
                "redirected_to": "moneyflow_residual",
                "residual_count": len(moneyflow_missing),
            }
        end_date = datetime.now().strftime("%Y%m%d")
        result = backfill_major_news_history(
            start_date="20200101",
            end_date=end_date,
            cursor=cursor,
            limit_days=7,
        )
        if not result.get("ok"):
            _set_model_data_backfill_state(
                status="failed",
                phase=phase,
                cursor=cursor,
                error="major_news_backfill_failed",
                last_result=_model_data_backfill_result_summary(result),
            )
            return result
        next_cursor = str(result.get("cursor") or cursor)
        if result.get("has_more"):
            next_task = enqueue_plan_model_data_backfill(phase="major_news", cursor=next_cursor)
            _set_model_data_backfill_state(
                status="running",
                phase=phase,
                cursor=next_cursor,
                next_message_id=next_task.get("message_id") or "",
                last_result=_model_data_backfill_result_summary(result),
            )
        else:
            next_task = enqueue_plan_model_data_backfill(
                phase="event_research", cursor="20181231"
            )
            _set_model_data_backfill_state(
                status="running",
                phase="event_research",
                cursor="20181231",
                last_result=_model_data_backfill_result_summary(result),
                next_message_id=next_task.get("message_id") or "",
            )
        return {
            "ok": True,
            "phase": phase,
            "result": result,
            "post_backfill_audit": {},
        }
    if phase == "event_research":
        normalized_keys = (
            normalize_report_rc_record_keys()
            if cursor == "20181231" else {}
        )
        result = backfill_event_research_history(
            start_date="20190101",
            end_date=datetime.now().strftime("%Y%m%d"),
            cursor=cursor,
            limit_months=1,
        )
        result["normalized_report_rc_keys"] = normalized_keys
        result["normalized_available_time_rows"] = (
            _normalize_event_disclosure_available_time()
        )
        if not result.get("ok"):
            _set_model_data_backfill_state(
                status="failed",
                phase=phase,
                cursor=cursor,
                error="event_research_backfill_failed",
                last_result=_model_data_backfill_result_summary(result),
            )
            return result
        next_cursor = str(result.get("cursor") or cursor)
        if result.get("has_more"):
            next_task = enqueue_plan_model_data_backfill(
                phase="event_research", cursor=next_cursor
            )
            _set_model_data_backfill_state(
                status="running",
                phase=phase,
                cursor=next_cursor,
                next_message_id=next_task.get("message_id") or "",
                last_result=_model_data_backfill_result_summary(result),
            )
            post_backfill_audit: dict[str, Any] = {}
        else:
            next_task = enqueue_plan_model_data_backfill(
                phase="event_financial_disclosures", cursor="20181231"
            )
            _set_model_data_backfill_state(
                status="running",
                phase="event_financial_disclosures",
                cursor="20181231",
                last_result=_model_data_backfill_result_summary(result),
                next_message_id=next_task.get("message_id") or "",
            )
            post_backfill_audit = {}
        return {
            "ok": True,
            "phase": phase,
            "result": result,
            "post_backfill_audit": post_backfill_audit,
        }
    if phase == "event_financial_disclosures":
        remaining = [
            period for period in _event_financial_periods()
            if not cursor or period > cursor
        ]
        if not remaining:
            post_backfill_audit = audit_plan_model_data_contract()
            _set_model_data_backfill_state(
                status="completed",
                phase=phase,
                cursor=cursor,
                post_backfill_audit=post_backfill_audit,
            )
            return {
                "ok": True,
                "phase": phase,
                "completed": True,
                "post_backfill_audit": post_backfill_audit,
            }
        period = remaining[0]
        results: list[dict[str, Any]] = []
        for api_name in ("forecast", "express"):
            if _event_financial_period_complete(api_name, period):
                results.append({
                    "ok": True,
                    "api": api_name,
                    "period": period,
                    "skipped": True,
                    "rows_written": 0,
                })
            else:
                results.append(
                    sync_tushare_stock_batch(api_name, period=period, limit=1)
                )
        result = {
            "ok": all(bool(item.get("ok")) for item in results),
            "period": period,
            "cursor": period,
            "periods_processed": 1,
            "has_more": len(remaining) > 1,
            "rows_written": sum(int(item.get("rows_written") or 0) for item in results),
            "failed": sum(1 for item in results if not item.get("ok")),
            "results": results,
        }
        if not result["ok"]:
            _set_model_data_backfill_state(
                status="failed",
                phase=phase,
                cursor=cursor,
                error="event_financial_disclosures_backfill_failed",
                last_result=_model_data_backfill_result_summary(result),
            )
            return result
        if result["has_more"]:
            next_task = enqueue_plan_model_data_backfill(
                phase=phase, cursor=period
            )
            _set_model_data_backfill_state(
                status="running",
                phase=phase,
                cursor=period,
                next_message_id=next_task.get("message_id") or "",
                last_result=_model_data_backfill_result_summary(result),
            )
            post_backfill_audit = {}
        else:
            post_backfill_audit = audit_plan_model_data_contract()
            _set_model_data_backfill_state(
                status="completed",
                phase=phase,
                cursor=period,
                last_result=_model_data_backfill_result_summary(result),
                post_backfill_audit=post_backfill_audit,
            )
        return {
            "ok": True,
            "phase": phase,
            "result": result,
            "post_backfill_audit": post_backfill_audit,
        }
    result = {"ok": False, "error": f"unknown_plan_model_data_backfill_phase:{phase}"}
    _set_model_data_backfill_state(status="failed", phase=phase, error=result["error"])
    return result
