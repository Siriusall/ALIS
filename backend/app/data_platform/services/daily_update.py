from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn, _read_conn
from app.data_platform.services.data_center import (
    classify_tushare_error,
    enrich_financial_research_fields,
    ensure_data_center_seeded,
    financial_research_completeness,
    run_data_quality_checks,
    sync_tushare_api_raw,
    sync_cninfo_announcements,
    sync_market_bundle,
    sync_index_bundle,
    sync_stock_basic,
    sync_trade_calendar,
)
from app.data_platform.services.decision_data_contract import decision_data_contract, latest_decision_ready_trade_date
from app.data_platform.services.market_calendar import TradeCalendarNotReady, target_trade_date
from app.data_platform.services.snapshot_health import estimated_table_count, latest_usable_snapshot_date
from app.data_platform.tushare_client import is_tushare_compatible_source
from app.data_platform.services.exchange_etf_pcf import sync_exchange_etf_pcf
from app.data_platform.services.oos_hard_data import audit_oos_hard_data
from app.core.error_evidence import error_evidence

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "phase": "idle", "message": "等待更新"}
_scheduler_lock = threading.Lock()
_derived_research_lock = threading.Lock()
_derived_research_running = False


def _is_expected_scheduler_wait(exc: BaseException) -> bool:
    """Treat bounded polling timeout/aborted-transaction as a retry wait."""
    message = str(exc or "").lower()
    return any(marker in message for marker in (
        "statement timeout", "querycanceled", "query canceled",
        "current transaction is aborted", "connection timeout",
        "connection timeout expired",
    ))
_scheduler_started = False
_DAILY_UPDATE_ENABLED = str(os.getenv("ALIS_DAILY_UPDATE_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
_DAILY_UPDATE_INTERVAL_SEC = max(60, min(int(os.getenv("ALIS_DAILY_UPDATE_INTERVAL_SEC", "60")), 3600))
_DAILY_UPDATE_START_TIME = dtime(
    max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_HOUR", "15")), 23)),
    max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_MINUTE", "1")), 59)),
)
_DAILY_UPDATE_INTERNAL_FAULT_COOLDOWN_SEC = max(
    300,
    min(int(os.getenv("ALIS_DAILY_UPDATE_INTERNAL_FAULT_COOLDOWN_SEC", "1800")), 21600),
)
logger = logging.getLogger("quant.daily_update")
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


class DailyUpdateDataBlocked(RuntimeError):
    def __init__(self, message: str, *, category: str, retry_after_seconds: int):
        self.category = category
        self.retry_after_seconds = max(0, int(retry_after_seconds))
        super().__init__(message)


def _now() -> str:
    return datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds")


def _business_now() -> datetime:
    """Return the UTC+8 business clock used by market publication policy."""
    return datetime.now(_BUSINESS_TZ)


def _latest_trade_date() -> str:
    with _conn() as c:
        return target_trade_date(c)


def _latest_usable_snapshot_date(c: Any) -> str:
    stock_count = int(dict(c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone()).get("cnt") or 0)
    return latest_usable_snapshot_date(c, stock_count)


def _latest_decision_ready_trade_date(c: Any) -> str:
    return latest_decision_ready_trade_date(c)


def _benchmark_ready_for_date(c: Any, trade_date: str) -> bool:
    """Return whether the benchmark has a complete exact-session bar for T.

    Row presence alone is not enough: a partial raw response used to make the
    daily updater look healthy while attribution could not calculate a daily
    benchmark return. Real observation-time qualification belongs to the PIT
    version migration; this is the field-level safety gate before that change.
    """
    from app.data_platform.services.data_center import _strict_benchmark_bar

    return _strict_benchmark_bar(c, trade_date) is not None


def _adj_factor_ready_for_date(c: Any, trade_date: str) -> bool:
    """Require at least 500 valid immutable factor observations."""
    from app.data_platform.services.data_center import _strict_adj_factor_codes

    return len(_strict_adj_factor_codes(c, trade_date)) >= 500


def _fund_daily_execution_fields_ready(payload_json: Any) -> bool:
    """Return whether a raw ETF record can support an exact open execution.

    Row presence is not enough: a partial response can retain a valid key while
    omitting the prices or volume required by the frozen execution contract.
    Keep this parser local to the historical continuity audit so it can inspect
    the caller's connection without accidentally treating a later row as ready.
    """
    if isinstance(payload_json, dict):
        payload = payload_json
    else:
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(payload, dict):
        return False
    from app.services.defensive_assets import defensive_asset_execution_missing_fields

    return not defensive_asset_execution_missing_fields(payload)


def _missing_defensive_etf_sessions(
    c: Any,
    trade_date: str,
    ts_codes: list[str],
    *,
    limit: int = 250,
    include_target: bool = True,
) -> list[str]:
    """Return recent open sessions lacking an executable defensive ETF slice."""
    target = str(trade_date or "").replace("-", "")[:8]
    if len(target) != 8 or not ts_codes:
        return []
    required = {str(code or "").strip().upper() for code in ts_codes if str(code or "").strip()}
    if not required:
        return []
    placeholders = ",".join("?" for _ in required)
    target_operator = "<=" if include_target else "<"
    rows = c.execute(
        f"""SELECT cal_date FROM dc_trade_calendar
           WHERE exchange='SSE' AND is_open=1 AND cal_date{target_operator}?
           ORDER BY cal_date DESC LIMIT ?""",
        # Account valuation must remain continuous across the whole retained
        # simulation history. A rolling ten-session check allowed older ETF
        # gaps to fall permanently out of view, leaving the portfolio curve
        # blocked even though daily updates appeared healthy.
        (target, max(1, min(int(limit), 250))),
    ).fetchall()
    sessions = [str(dict(row).get("cal_date") or "").replace("-", "")[:8] for row in reversed(rows)]
    missing: list[str] = []
    for session in sessions:
        records = c.execute(
            f"""SELECT ts_code,payload_json
                FROM dc_tushare_raw_records
               WHERE api_name='fund_daily' AND trade_date=? AND ts_code IN ({placeholders})""",
            (session, *sorted(required)),
        ).fetchall()
        valid_codes = {
            str(dict(row).get("ts_code") or "").strip().upper()
            for row in records
            if _fund_daily_execution_fields_ready(dict(row).get("payload_json"))
        }
        if valid_codes != required:
            missing.append(session)
    return missing


def _repair_defensive_etf_continuity(trade_date: str, ts_codes: list[str]) -> dict[str, Any]:
    """Repair missing ETF sessions through the same provider race as T+1 data.

    Historical recovery must not pin itself to the default TuShare endpoint.
    Tencent is still accepted only when it can prove the exact requested bar;
    otherwise the race selects one of the configured TuShare-compatible
    endpoints and retains that provider on every raw record.
    """
    from app.data_platform.services.market_data_discovery import (
        discover_first_published,
        mark_provider_bulk_result,
    )
    from app.data_platform.services.tencent_etf_daily_fallback import repair_fund_daily_from_tencent
    from app.data_platform.services.eastmoney_etf_history_fallback import (
        repair_fund_daily_history_from_eastmoney,
    )

    with _conn() as conn:
        # The current session is not history. It may safely use the narrowly
        # verified Tencent quote fallback after a partial TuShare response.
        # Only older sessions are restricted to canonical TuShare repair.
        missing = _missing_defensive_etf_sessions(
            conn, trade_date, ts_codes, include_target=False,
        )
    if not missing:
        return {"ok": True, "status": "continuous", "missing_sessions": [], "results": []}
    results: list[dict[str, Any]] = []
    for session in missing:
        discovery = discover_first_published("defensive_fund_daily", session)
        winner = str(discovery.get("winner") or "")
        try:
            if is_tushare_compatible_source(winner):
                result = sync_tushare_api_raw(
                    "fund_daily", {"trade_date": session}, provider=winner,
                )
            elif winner == "tencent_etf_quote":
                result = repair_fund_daily_from_tencent(session, ts_codes)
            else:
                result = repair_fund_daily_history_from_eastmoney(
                    session, session, ts_codes, required_dates=[session],
                )
                if result.get("ok"):
                    winner = "eastmoney_etf_history_fallback"
            if not result.get("ok") and session < str(trade_date or "").replace("-", "")[:8]:
                fallback = repair_fund_daily_history_from_eastmoney(
                    session, session, ts_codes, required_dates=[session],
                )
                if fallback.get("ok"):
                    result = fallback
                    winner = "eastmoney_etf_history_fallback"
            if winner:
                mark_provider_bulk_result(
                    "defensive_fund_daily",
                    session,
                    winner,
                    ok=bool(result.get("ok")),
                    evidence={"status": result.get("status"), "historical_continuity": True},
                )
            results.append({
                "trade_date": session,
                "winner": winner,
                "discovery": discovery,
                **result,
            })
        except Exception as exc:  # noqa: BLE001 - preserve exact failed session evidence
            if winner:
                mark_provider_bulk_result(
                    "defensive_fund_daily",
                    session,
                    winner,
                    ok=False,
                    evidence={"status": "bulk_fetch_failed", "error": str(exc)[:300], "historical_continuity": True},
                )
            results.append({
                "trade_date": session,
                "winner": winner,
                "discovery": discovery,
                "ok": False,
                "error": str(exc)[:300],
            })
    with _conn() as conn:
        remaining = _missing_defensive_etf_sessions(
            conn, trade_date, ts_codes, include_target=False,
        )
    return {
        "ok": not remaining,
        "status": "repaired" if not remaining else "historical_source_incomplete",
        "missing_sessions": remaining,
        "requested_sessions": missing,
        "results": results,
    }


def _set_state(**values: Any) -> None:
    with _lock:
        _state.update(values)


_NON_RETRYABLE_DISCOVERY_FAILURES = frozenset({
    "authentication", "permission", "invalid_request", "unknown",
})


def _discovery_state(target: str, discovery: dict[str, Any]) -> dict[str, Any]:
    """Expose provider failures as errors instead of an endless probe spinner."""
    category = str(discovery.get("failure_category") or "").strip().lower()
    if category in _NON_RETRYABLE_DISCOVERY_FAILURES:
        return {
            "phase": "blocked",
            "message": f"{target} 数据源探针失败：{category}，等待外部依赖修复",
            "failure_category": category,
            "retry_after_seconds": int(discovery.get("retry_after_seconds") or 86400),
            "discovery": discovery,
        }
    return {
        "phase": "discovering",
        "message": f"正在探测 {target} 各行情源发布时间",
        "discovery": discovery,
    }


def _daily_update_retry_policy(c: Any, trade_date: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Return a persisted retry decision for the latest same-date failure."""

    row = c.execute(
        """SELECT finished_at,meta_json FROM data_ingestion_runs
           WHERE job_name='data_platform.daily_update' AND status='failed'
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return {"allowed": True, "reason": "no_failed_run"}
    item = dict(row)
    try:
        meta = json.loads(item.get("meta_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"allowed": True, "reason": "legacy_failure_without_policy"}
    if str(meta.get("trade_date") or "") != str(trade_date):
        return {"allowed": True, "reason": "different_trade_date"}
    category = str(meta.get("failure_category") or "")
    retry_after_seconds = int(meta.get("retry_after_seconds") or 0)
    if retry_after_seconds <= 0:
        return {"allowed": True, "reason": "retryable_failure", "category": category}
    try:
        finished_at = datetime.fromisoformat(str(item.get("finished_at") or ""))
    except ValueError:
        return {"allowed": True, "reason": "invalid_failure_timestamp", "category": category}
    current = (
        now.astimezone(_BUSINESS_TZ)
        if now and now.tzinfo
        else now.replace(tzinfo=_BUSINESS_TZ)
        if now
        else _business_now()
    )
    # Historical rows predate timezone-qualified persistence.  Those naive
    # timestamps are business timestamps, so never compare them directly to
    # an aware UTC+8 scheduler clock.
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=_BUSINESS_TZ)
    else:
        finished_at = finished_at.astimezone(_BUSINESS_TZ)
    remaining = max(0, int(retry_after_seconds - (current - finished_at).total_seconds()))
    return {
        "allowed": remaining <= 0,
        "reason": "cooldown_elapsed" if remaining <= 0 else "data_refresh_cooldown",
        "category": category,
        "retry_after_seconds": remaining,
        "next_retry_at": (current + timedelta(seconds=remaining)).isoformat(timespec="seconds"),
    }


def _scheduler_should_check(now: datetime, target_trade_date_value: str) -> bool:
    """Allow overdue slices to recover before today's close-data window.

    The close clock protects only the current trade date from intraday reads.
    Once the target is an earlier calendar date, keeping that same clock gate
    would strand an upstream-delayed slice until 16:05 on every following day.
    """

    if now.tzinfo is not None:
        now = now.astimezone(_BUSINESS_TZ)
    raw = str(target_trade_date_value or "").replace("-", "")[:8]
    if len(raw) != 8 or not raw.isdigit():
        return False
    try:
        target_date = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return False
    if target_date < now.date():
        return True
    return target_date == now.date() and now.weekday() < 5 and now.time() >= _DAILY_UPDATE_START_TIME


def _run_research_maintenance(trade_date: str) -> dict[str, Any]:
    """Refresh research pools after a durable market snapshot without affecting it."""
    from app.services.decision_store import list_pool_members
    from app.services.industry_event_engine import materialize_industry_state_snapshots
    from app.services.research_workflow import process_watch_pool, stage_watch_pool_candidates
    from app.services.stock_screener import run_daily_screener
    from app.services.theme_engine import materialize_theme_snapshots

    research_limit = max(1, min(int(os.getenv("ALIS_DAILY_RESEARCH_LIMIT", "100")), 500))
    # Independent read-models must not be stranded by one stale security in
    # the optional screener/valuation path. Materialize them first and keep
    # the rest of research maintenance best-effort.
    try:
        industry_states = materialize_industry_state_snapshots()
    except Exception as exc:  # noqa: BLE001
        logger.warning("industry snapshot deferred while preserving themes: %s", str(exc)[:300])
        industry_states = {"ok": False, "status": "deferred", "error": str(exc)[:300]}
    try:
        themes = materialize_theme_snapshots()
        if not themes.get("ok") and themes.get("reason") in {"snapshot_versions_missing_dates", "snapshot_versions_incomplete"}:
            # Keep each retry bounded; the scheduler will advance the next
            # missing sessions on subsequent cycles without monopolizing the
            # data connection pool for an entire historical range.
            missing_dates = [str(item) for item in themes.get("missing_dates") or []][:2]
            if not missing_dates:
                with _conn() as c:
                    calendar = [str(row["cal_date"]) for row in c.execute(
                        "SELECT cal_date FROM dc_trade_calendar WHERE is_open=1 AND cal_date<=? ORDER BY cal_date DESC LIMIT 20",
                        (trade_date,),
                    ).fetchall()]
                    stock_count = int(c.execute("SELECT COUNT(*) n FROM dc_stock_master").fetchone()["n"] or 0)
                    minimum = max(1, int(stock_count * 0.8))
                    for session in calendar:
                        eligible = int(c.execute(
                            """SELECT COUNT(*) n FROM dc_stock_daily_snapshot
                               WHERE trade_date=? AND industry<>'' AND close IS NOT NULL
                                 AND observed_at<>'' AND first_observed_at<>''
                                 AND economic_available_time<>'' AND available_time<>''
                                 AND updated_at<>''""", (session,),
                        ).fetchone()["n"] or 0)
                        if eligible < minimum:
                            missing_dates.append(session)
            repairs = []
            for missing_date in missing_dates:
                try:
                    repairs.append({"trade_date": missing_date, "result": sync_market_bundle(
                        missing_date, apis=["daily", "daily_basic", "stk_limit", "adj_factor"],
                    )})
                except Exception as exc:  # noqa: BLE001
                    repairs.append({"trade_date": missing_date, "ok": False, "error": str(exc)[:300]})
            themes = materialize_theme_snapshots()
            themes["missing_date_repairs"] = repairs
    except Exception as exc:  # noqa: BLE001
        logger.warning("theme snapshot deferred: %s", str(exc)[:300])
        themes = {"ok": False, "status": "deferred", "error": str(exc)[:300]}
    try:
        screen = run_daily_screener(
            top_n=max(100, research_limit * 3),
            research_limit=research_limit,
            trade_date=trade_date,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily screener deferred while preserving research snapshots: %s", str(exc)[:300])
        screen = {"ok": False, "status": "deferred", "error": str(exc)[:300]}
    staged = stage_watch_pool_candidates(limit=min(50, research_limit))
    candidate_enrichment = []
    enrichment_limit = max(1, min(int(os.getenv("ALIS_DAILY_CANDIDATE_ENRICH_LIMIT", "5")), 20))
    for member in list_pool_members("candidate"):
        if len(candidate_enrichment) >= enrichment_limit:
            break
        code = str(member.get("ts_code") or "").strip()
        if not code:
            continue
        before = financial_research_completeness(code, period_limit=5)
        if before.get("status") != "complete":
            enriched = enrich_financial_research_fields(code, period_limit=5, request_limit=20)
            candidate_enrichment.append({
                "ts_code": code,
                "before": before.get("status"),
                "after": (enriched.get("after") or {}).get("status"),
                "requested": enriched.get("requested"),
            })
    promote = process_watch_pool(limit=research_limit)
    return {
        "screen": screen,
        "stage": staged,
        "candidate_enrichment": candidate_enrichment,
        "promote": promote,
        "industry_states": industry_states,
        "themes": themes,
    }


def _derived_research_lag(c: Any, target: str) -> bool:
    """Detect durable industry/theme projections stranded behind market data."""
    target_s = str(target or "").replace("-", "")[:8]
    if len(target_s) != 8:
        return False
    rows = c.execute(
        """SELECT
             (SELECT MAX(as_of_date) FROM dc_industry_state_snapshot) industry_date,
             (SELECT MAX(as_of_date) FROM dc_theme_snapshot) theme_date"""
    ).fetchone()
    values = dict(rows) if rows else {}
    return any(str(values.get(key) or "") < target_s for key in ("industry_date", "theme_date"))


def _worker(parent_run_id: int) -> None:
    steps: list[dict[str, Any]] = []
    error = ""
    failure_category = ""
    retry_after_seconds = 0
    trade_date = ""
    try:
        _set_state(phase="preparing", message="正在检查数据目录", progress=10)
        ensure_data_center_seeded()

        with _conn() as c:
            stock_count = int(dict(c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone()).get("cnt") or 0)
        if stock_count == 0:
            _set_state(phase="stock_basic", message="首次使用，正在建立股票名单", progress=20)
            steps.append({"step": "stock_basic", "result": sync_stock_basic()})

        _set_state(phase="calendar", message="正在确认最近交易日", progress=35)
        today = _business_now().date()
        start = (today - timedelta(days=35)).strftime("%Y%m%d")
        end = (today + timedelta(days=7)).strftime("%Y%m%d")
        try:
            calendar_result = sync_trade_calendar(
                start,
                end,
                allow_market_observation_fallback=True,
            )
        except Exception as exc:  # noqa: BLE001 - calendar failures need bounded retry semantics
            category = classify_tushare_error(exc)
            retry_after = 86400 if category in {"authentication", "permission", "invalid_request", "unknown"} else 600
            raise DailyUpdateDataBlocked(
                f"交易日历同步失败（{category}）：{str(exc)[:300]}",
                category=category or "calendar_sync_failed",
                retry_after_seconds=retry_after,
            ) from exc
        steps.append({"step": "trade_calendar", "result": calendar_result})
        trade_date = _latest_trade_date()

        _set_state(phase="market", message=f"正在更新 {trade_date} 行情和估值", progress=50, trade_date=trade_date)
        with _conn() as c:
            already_ready_date = _latest_decision_ready_trade_date(c)
        market_result = (
            {
                "ok": True,
                "status": "canonical_slice_already_ready",
                "trade_date": trade_date,
                "policy": "skip_redundant_bulk_after_probe_discovery",
            }
            if already_ready_date == trade_date
            else sync_market_bundle(trade_date)
        )
        steps.append({"step": "market", "result": market_result})
        with _conn() as c:
            ready_date = _latest_decision_ready_trade_date(c)
        if not bool(market_result.get("ok")) or ready_date != trade_date:
            market_category = str(market_result.get("failure_category") or "")
            if market_category in {"truncated", "pagination_truncated"}:
                raise DailyUpdateDataBlocked(
                    f"{trade_date} stk_limit 批量切片疑似被分页截断，系统已停止无效重拉并进入有界分页诊断",
                    category="internal_fault_truncated_slice",
                    retry_after_seconds=_DAILY_UPDATE_INTERNAL_FAULT_COOLDOWN_SEC,
                )
            if market_category == "incomplete_slice":
                raise DailyUpdateDataBlocked(
                    f"{trade_date} stk_limit 分页结束后覆盖仍不足，系统已阻断正式消费并等待数据中台复核",
                    category="internal_fault_incomplete_slice",
                    retry_after_seconds=_DAILY_UPDATE_INTERNAL_FAULT_COOLDOWN_SEC,
                )
            if market_category == "repair_in_progress":
                raise DailyUpdateDataBlocked(
                    f"{trade_date} stk_limit 缺失股票正在由数据中台有界补齐，完成前禁止正式消费",
                    category="waiting_stk_limit_gap_repair",
                    retry_after_seconds=600,
                )
            if market_category == "upstream_not_published":
                raise DailyUpdateDataBlocked(
                    f"{trade_date} 各行情源探针均尚未发现完整收盘切片",
                    category="upstream_not_ready",
                    retry_after_seconds=60,
                )
            if market_category == "bulk_slice_contract_failed":
                raise DailyUpdateDataBlocked(
                    f"{trade_date} 首个已发布源的全量切片未通过合同，下一轮改选其他已发布源",
                    category="provider_bulk_contract_failed",
                    retry_after_seconds=60,
                )
            raise DailyUpdateDataBlocked(
                f"上游尚未发布 {trade_date} 完整收盘数据，自动日更将在冷却后复核",
                category="upstream_not_ready",
                retry_after_seconds=600,
            )

        with _conn() as c:
            adj_factor_ready = _adj_factor_ready_for_date(c, trade_date)
        if not adj_factor_ready:
            from app.data_platform.services.market_data_discovery import discover_first_published

            adj_discovery = discover_first_published("adj_factor", trade_date)
            adj_provider = str(adj_discovery.get("winner") or "")
            if adj_discovery.get("ok") and adj_provider:
                adj_result = sync_market_bundle(
                    trade_date, apis=["adj_factor"], provider=adj_provider,
                )
            else:
                adj_result = {
                    "ok": False,
                    "api": "adj_factor",
                    "status": adj_discovery.get("status") or "provider_failed",
                    "failure_category": adj_discovery.get("failure_category") or "upstream_not_ready",
                    "discovery": adj_discovery,
                }
            steps.append({"step": "adj_factor", "result": adj_result})
            from app.data_platform.services.market_data_discovery import mark_provider_bulk_result
            mark_provider_bulk_result(
                "adj_factor",
                trade_date,
                adj_provider or "tushare",
                ok=bool(adj_result.get("ok")),
                evidence={"status": adj_result.get("failure_category") or "completed"},
            )

        # The benchmark is not part of the stock decision completeness gate,
        # but it is a first-class daily output for account attribution. Keep
        # it in the same close-of-day loop so the chart never silently lags
        # behind the account curve again.
        _set_state(phase="benchmark", message=f"正在更新 {trade_date} 沪深300基准", progress=60, trade_date=trade_date)
        benchmark_result = sync_index_bundle(
            index_code="000300.SH",
            start_date=trade_date,
            end_date=trade_date,
        )
        steps.append({"step": "benchmark", "result": benchmark_result})
        if not bool(benchmark_result.get("benchmark_ready")):
            logger.warning("daily benchmark update deferred trade_date=%s result=%s", trade_date, benchmark_result)

        # Market state is a formal decision dependency, not a dashboard-only
        # derivative.  Previously the close slice could pass while this
        # materialization was left to a later plan read, creating a needless
        # `blocked_by_market_context` window (or a slow on-demand rebuild).
        # Persist it immediately after the core stock slice; optional research
        # enrichments are explicitly neutral/degraded inside the context
        # builder and cannot hold up this hard dependency.
        _set_state(phase="market_context", message=f"正在冻结 {trade_date} 市场状态", progress=64, trade_date=trade_date)
        try:
            from app.services.market_context import live_market_context

            market_context = live_market_context(persist=True)
        except Exception as exc:  # noqa: BLE001
            raise DailyUpdateDataBlocked(
                f"{trade_date} 市场状态物化失败：{type(exc).__name__}",
                category="market_context_materialization_failed",
                retry_after_seconds=60,
            ) from exc
        context_trade_date = str(market_context.get("trade_date") or "").replace("-", "")[:8]
        context_state = str((market_context.get("market_state") or {}).get("state") or "")
        steps.append({"step": "market_context", "result": market_context})
        if not bool(market_context.get("ok")) or context_trade_date != trade_date or context_state not in {"S0", "S1", "S2", "S3"}:
            raise DailyUpdateDataBlocked(
                f"{trade_date} 市场状态点时证据未就绪：{market_context.get('reason') or context_state or 'unknown'}",
                category="market_context_not_ready",
                retry_after_seconds=60,
            )

        _set_state(phase="etf_daily", message=f"正在按交易日更新 {trade_date} ETF 日线", progress=68)
        try:
            from app.data_platform.services.tencent_etf_daily_fallback import repair_fund_daily_from_tencent
            from app.data_platform.services.market_data_discovery import (
                discover_first_published,
                mark_provider_bulk_result,
            )
            from app.services.defensive_assets import DEFENSIVE_ASSETS, defensive_asset_execution_status

            defensive_codes = sorted(str(item["ts_code"]) for item in DEFENSIVE_ASSETS)
            continuity = _repair_defensive_etf_continuity(trade_date, defensive_codes)
            if continuity.get("ok"):
                etf_discovery = discover_first_published("defensive_fund_daily", trade_date)
                winner = str(etf_discovery.get("winner") or "")
                if is_tushare_compatible_source(winner):
                    # A successful coverage row can coexist with a retained
                    # ETF bar that misses an execution field.  Do not route
                    # that exact repair through the incremental cursor: it
                    # would skip the request again and strand the gap.
                    provider_result = sync_tushare_api_raw("fund_daily", {"trade_date": trade_date}, provider=winner)
                elif winner == "tencent_etf_quote":
                    provider_result = repair_fund_daily_from_tencent(trade_date, defensive_codes)
                else:
                    provider_result = {"ok": False, "status": "no_provider_published"}
            else:
                etf_discovery = {"ok": False, "status": "historical_etf_gap_requires_tushare", "winner": "tushare"}
                winner = "tushare"
                provider_result = continuity
            # This is intentionally the same field-level contract the frozen
            # T+1 executor uses. A retained raw record without open/pre_close/
            # volume must remain a visible, retryable gap rather than making
            # daily ingestion appear healthy until execution discovers it.
            execution_slice = defensive_asset_execution_status(trade_date, defensive_codes)
            slice_rows = len(execution_slice.get("ready") or [])
            slice_ready = bool(execution_slice.get("ok"))
            if winner:
                mark_provider_bulk_result(
                    "defensive_fund_daily",
                    trade_date,
                    winner,
                    ok=slice_ready,
                    coverage=(slice_rows / len(defensive_codes)) if defensive_codes else 0.0,
                    evidence={
                        "status": provider_result.get("status"),
                        "rows": slice_rows,
                        "required_rows": len(defensive_codes),
                        "missing": execution_slice.get("missing") or [],
                        "execution_contract": execution_slice.get("schema_version"),
                    },
                )
            steps.append({"step": "etf_daily", "result": {
                "ok": slice_ready,
                "provider_discovery_contract": "alis_market_publication_probe_v1",
                "winner": winner,
                "discovery": etf_discovery,
                "continuity": continuity,
                "provider_result": provider_result,
                "slice_rows": slice_rows,
                "required_rows": len(defensive_codes),
                "execution_slice": execution_slice,
            }})
            if not slice_ready:
                raise DailyUpdateDataBlocked(
                    f"{trade_date} 注册防御ETF执行切片未就绪："
                    + ",".join(execution_slice.get("missing") or ["unknown"]),
                    category="execution_slice_blocked",
                    retry_after_seconds=60,
                )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, DailyUpdateDataBlocked):
                raise
            logger.warning("daily ETF market update deferred: %s", str(exc)[:300])
            steps.append({"step": "etf_daily", "result": error_evidence(
                exc, operation="daily_update:etf_daily", source="data_platform", deferred=True,
                context={"trade_date": trade_date},
            )})
            raise DailyUpdateDataBlocked(
                f"{trade_date} 注册防御ETF执行切片检查失败：{type(exc).__name__}",
                category="execution_slice_check_failed",
                retry_after_seconds=60,
            ) from exc

        _set_state(phase="etf_pcf", message=f"正在更新 {trade_date} ETF 申购赎回篮子", progress=72)
        try:
            pcf = sync_exchange_etf_pcf(trade_date)
            steps.append({"step": "etf_pcf", "result": pcf})
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily exchange ETF PCF update deferred: %s", str(exc)[:300])
            steps.append({"step": "etf_pcf", "result": error_evidence(
                exc, operation="daily_update:etf_pcf", source="exchange", deferred=True,
                context={"trade_date": trade_date},
            )})

        _set_state(phase="announcements", message="正在更新最近公告", progress=75)
        today = _business_now().date()
        ann_start = (today - timedelta(days=3)).strftime("%Y%m%d")
        ann_end = today.strftime("%Y%m%d")
        steps.append({"step": "announcements", "result": sync_cninfo_announcements(
            start_date=ann_start, end_date=ann_end, page_size=30, max_pages=3,
        )})

        _set_state(phase="quality", message="正在检查数据完整性", progress=90)
        steps.append({"step": "quality", "result": run_data_quality_checks(trade_date)})
        steps.append({"step": "oos_hard_data", "result": audit_oos_hard_data(end_date=trade_date, include_dates=False)})

        # Research maintenance is downstream of a durable market snapshot. It
        # is deliberately non-fatal: a research failure must not roll back or
        # misreport a successfully completed market-data update.
        _set_state(phase="research", message="正在更新候选池和核心池研究", progress=95)
        try:
            # Research projections are optional for the daily market contract.
            # They may contain large historical joins, so bound their impact on
            # the ingestion worker and let the next cycle retry them.
            research_result: dict[str, Any] = {}
            research_error: list[BaseException] = []

            def _research_worker() -> None:
                try:
                    research_result.update(_run_research_maintenance(trade_date))
                except BaseException as exc:  # noqa: BLE001 - report below
                    research_error.append(exc)

            research_thread = threading.Thread(
                target=_research_worker,
                name=f"daily-research-{trade_date}",
                daemon=True,
            )
            research_thread.start()
            research_thread.join(timeout=120)
            if research_thread.is_alive():
                raise TimeoutError("daily research maintenance timed out after 120 seconds")
            if research_error:
                raise research_error[0]
            steps.append({"step": "research_pool", "result": research_result})
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily research maintenance deferred: %s", str(exc)[:300])
            steps.append({"step": "research_pool", "result": error_evidence(
                exc, operation="daily_update:research_pool", source="research", deferred=True,
                context={"trade_date": trade_date},
            )})
        # The market updater is successful only after the same strict decision
        # contract used by the executor passes.  A raw fetch/coverage row by
        # itself must never produce a healthy completion.
        with _conn() as c:
            final_contract = decision_data_contract(trade_date, conn=c)
        if not final_contract.get("ok"):
            raise DailyUpdateDataBlocked(
                str(final_contract.get("reason") or f"{trade_date} 决策数据严格合同未通过"),
                category="decision_contract_incomplete",
                retry_after_seconds=60,
            )
        final_status = "completed"
        _set_state(running=False, phase="completed", message=f"数据已更新到 {trade_date}", progress=100,
                   finished_at=_now(), steps=steps, decision_contract=final_contract)
    except Exception as exc:  # noqa: BLE001
        final_status, error = "failed", str(exc)[:1000]
        failure_category = str(getattr(exc, "category", "internal_fault"))
        retry_after_seconds = int(getattr(exc, "retry_after_seconds", 0) or 0)
        if failure_category == "internal_fault" and retry_after_seconds <= 0:
            classified = classify_tushare_error(exc)
            if classified != "unknown":
                failure_category = classified
            retry_after_seconds = (
                600 if classified in {"transient", "rate_limit", "upstream_not_ready", "empty_pending"}
                else _DAILY_UPDATE_INTERNAL_FAULT_COOLDOWN_SEC
            )
        _set_state(running=False, phase="failed", message=f"更新失败：{error}", error=error,
                   failure_category=failure_category, retry_after_seconds=retry_after_seconds,
                   finished_at=_now(), steps=steps)
    if final_status == "completed" and trade_date:
        try:
            with _conn() as c:
                recovered_contract = decision_data_contract(trade_date, conn=c)
            if recovered_contract.get("ok"):
                from app.services.evolution_orchestrator import enqueue_system_autonomy_recovery
                recovery_task = enqueue_system_autonomy_recovery(trade_date, recovered_contract)
                steps.append({"step": "system_autonomy_recovery", "result": recovery_task})
        except Exception as exc:  # noqa: BLE001
            logger.warning("决策数据恢复后自治补跑排队失败: %s", str(exc)[:300])
    with _conn() as c:
        c.execute(
            """UPDATE data_ingestion_runs SET status=?,finished_at=?,rows_read=?,rows_written=?,error=?,meta_json=?
            WHERE id=?""",
            (final_status, _now(), len(steps), len(steps), error,
             json.dumps({
                 "trade_date": trade_date,
                 "steps": steps,
                 "failure_category": failure_category,
                 "retry_after_seconds": retry_after_seconds,
             }, ensure_ascii=False, default=str), parent_run_id),
        )


def start_daily_update() -> dict[str, Any]:
    with _lock:
        if _state.get("running"):
            return {"ok": False, "reason": "already_running", **_state}
        try:
            with _conn() as c:
                target = target_trade_date(c)
        except TradeCalendarNotReady as exc:
            blocked = {
                "ok": False,
                "reason": exc.code,
                "failure_category": exc.code,
                "phase": "blocked",
                "message": str(exc),
                "error": str(exc),
                "retry_after_seconds": 60,
            }
            _set_state(running=False, **blocked)
            return {**blocked, **_state}
        with _conn() as c:
            retry_policy = _daily_update_retry_policy(c, target)
            if not retry_policy.get("allowed"):
                return {
                    **retry_policy,
                    "ok": False,
                    "reason": "cooldown_active",
                    "trade_date": target,
                    "cooldown_reason": retry_policy.get("reason"),
                }
            c.execute(
                """INSERT INTO data_ingestion_runs
                (job_name,source_code,status,started_at,rows_read,rows_written,meta_json)
                VALUES (?,?,?,?,?,?,?)""",
                ("data_platform.daily_update", "system", "running", _now(), 0, 0, "{}"),
            )
            run_id = c.last_insert_id()
        _state.clear()
        _state.update({"running": True, "run_id": run_id, "phase": "starting", "message": "正在启动更新", "progress": 0,
                       "started_at": _now()})
        threading.Thread(target=_worker, args=(run_id,), daemon=True, name=f"daily-update-{run_id}").start()
        return {"ok": True, **_state}


def _start_derived_research_repair(target: str) -> dict[str, Any]:
    """Refresh research projections without rerunning upstream market ingestion."""
    global _derived_research_running
    target_s = str(target or "").replace("-", "")[:8]
    if len(target_s) != 8:
        return {"ok": False, "reason": "invalid_target"}
    with _derived_research_lock:
        if _derived_research_running:
            return {"ok": False, "reason": "already_running", "trade_date": target_s}
        _derived_research_running = True

    def _run() -> None:
        global _derived_research_running
        try:
            result = _run_research_maintenance(target_s)
            logger.info("派生研究快照独立补建完成 target=%s result=%s", target_s, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("派生研究快照独立补建失败 target=%s error=%s", target_s, str(exc)[:300])
        finally:
            with _derived_research_lock:
                _derived_research_running = False

    threading.Thread(target=_run, name="derived-research-repair", daemon=True).start()
    return {"ok": True, "reason": "started", "trade_date": target_s}


def start_daily_update_scheduler() -> bool:
    """Start an independent close-of-day updater, even with no quant task."""
    global _scheduler_started
    if not _DAILY_UPDATE_ENABLED:
        return False
    with _scheduler_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True

    def _loop() -> None:
        from app.services.runtime_heartbeat import mark_started, beat
        mark_started("daily_update_scheduler", interval_seconds=_DAILY_UPDATE_INTERVAL_SEC)
        while True:
            try:
                now = _business_now()
                with _conn() as c:
                    target = target_trade_date(c, now=now)
                    should_check = _scheduler_should_check(now, target)
                    if should_check:
                        ready = _latest_decision_ready_trade_date(c)
                        benchmark_ready = _benchmark_ready_for_date(c, target)
                        adj_factor_ready = _adj_factor_ready_for_date(c, target)
                        retry_policy = _daily_update_retry_policy(c, target, now=now)
                        derived_research_lag = _derived_research_lag(c, target)
                    else:
                        ready = ""
                        benchmark_ready = False
                        adj_factor_ready = False
                        retry_policy = {"allowed": False, "reason": "before_close_data_window"}
                        derived_research_lag = _derived_research_lag(c, target)
                if should_check:
                    # Keep the scheduler's short metadata transaction separate
                    # from the per-ETF immutable-evidence reads. A statement
                    # timeout aborts a PostgreSQL transaction; doing the latter
                    # inside this context used to turn an optional ETF check
                    # into a misleading "current transaction is aborted" on
                    # the next scheduler query.
                    from app.services.defensive_assets import DEFENSIVE_ASSETS, defensive_asset_execution_status

                    defensive_codes = [str(item["ts_code"]) for item in DEFENSIVE_ASSETS]
                    defensive_execution_slice = defensive_asset_execution_status(target, defensive_codes)
                    # Core market data must catch up before optional research
                    # projections.  Running the expensive derived repair first
                    # can monopolize the worker while the next trading day's
                    # raw slices remain missing indefinitely.
                    core_market_ready = (
                        ready == target
                        and benchmark_ready
                        and adj_factor_ready
                        and defensive_execution_slice.get("ok")
                    )
                    if target and derived_research_lag and core_market_ready and retry_policy.get("allowed"):
                        result = _start_derived_research_repair(target)
                        if result.get("ok"):
                            logger.info("自动日更发现行业/主题派生快照落后，启动补建 target=%s", target)
                        elif result.get("reason") != "already_running":
                            logger.warning("行业/主题派生快照补建启动失败 target=%s result=%s", target, result)
                    elif target and (
                        ready != target
                        or not benchmark_ready
                        or not adj_factor_ready
                        or not defensive_execution_slice.get("ok")
                    ) and retry_policy.get("allowed"):
                        from app.data_platform.services.market_data_discovery import (
                            discover_first_published,
                            discover_market_core,
                        )

                        if ready != target:
                            discovery = discover_market_core(target)
                        elif not benchmark_ready:
                            discovery = discover_first_published("index_daily", target)
                        elif not adj_factor_ready:
                            discovery = discover_first_published("adj_factor", target)
                        else:
                            discovery = discover_first_published("defensive_fund_daily", target)
                        if discovery.get("ok"):
                            result = start_daily_update()
                            if result.get("ok"):
                                logger.info(
                                    "自动收盘日更发现已发布源并启动全量 target=%s previous_ready=%s discovery=%s",
                                    target,
                                    ready,
                                    discovery.get("winners") or discovery.get("winner"),
                                )
                            elif result.get("reason") != "already_running":
                                logger.warning("自动收盘日更启动失败 target=%s result=%s", target, result)
                        else:
                            _set_state(
                                running=False,
                                trade_date=target,
                                **_discovery_state(target, discovery),
                            )
                    elif target and (
                        ready != target or not benchmark_ready or not adj_factor_ready or not defensive_execution_slice.get("ok")
                    ) and retry_policy.get("reason") == "data_refresh_cooldown":
                        _set_state(
                            running=False,
                            phase="cooldown",
                            message="检测到同一数据或执行切片故障，系统按冷却窗口自主复核",
                            trade_date=target,
                            failure_category=retry_policy.get("category"),
                            retry_after_seconds=retry_policy.get("retry_after_seconds"),
                            next_retry_at=retry_policy.get("next_retry_at"),
                            defensive_execution_slice=defensive_execution_slice,
                        )
                beat("daily_update_scheduler")
            except TradeCalendarNotReady as exc:
                _set_state(
                    running=False,
                    phase="blocked",
                    message=str(exc),
                    error=str(exc),
                    failure_category=exc.code,
                    retry_after_seconds=60,
                    trade_date="",
                )
                logger.warning("自动收盘日更因交易日历未就绪而阻断: %s", exc)
                beat("daily_update_scheduler", error=str(exc))
            except Exception as exc:  # noqa: BLE001
                if _is_expected_scheduler_wait(exc):
                    _set_state(
                        running=False,
                        phase="waiting_data_probe",
                        message="收盘数据探测超时，已丢弃本次连接并等待下一周期重试",
                        error=str(exc)[:300],
                        failure_category="transient_database_poll_timeout",
                    )
                    logger.warning("自动收盘日更暂缓本轮探测，将在下一周期重试: %s", str(exc)[:300])
                    beat("daily_update_scheduler")
                else:
                    logger.warning("自动收盘日更检查失败: %s", str(exc)[:300])
                    beat("daily_update_scheduler", error=str(exc))
            _set_state(next_scheduler_check_at=(_business_now() + timedelta(seconds=_DAILY_UPDATE_INTERVAL_SEC)).isoformat(timespec="seconds"))
            threading.Event().wait(_DAILY_UPDATE_INTERVAL_SEC)

    threading.Thread(target=_loop, name="data-platform-daily-update", daemon=True).start()
    logger.info(
        "独立收盘日更调度已启动：当天 %s 后检查，跨日遗留缺口全天按冷却重试；每 %ss 轮询",
        _DAILY_UPDATE_START_TIME.strftime("%H:%M"),
        _DAILY_UPDATE_INTERVAL_SEC,
    )
    return True


def daily_update_status(*, compact: bool = False) -> dict[str, Any]:
    with _lock:
        state = {"ok": True, **_state}
    if str(state.get("phase") or "") in {"failed", "blocked"}:
        state["ok"] = False
    retry_after_seconds = max(0, int(state.get("retry_after_seconds") or 0))
    if retry_after_seconds and not str(state.get("next_retry_at") or "").strip():
        origin = state.get("finished_at") or state.get("updated_at")
        try:
            parsed_origin = datetime.fromisoformat(str(origin or "").replace("Z", "+00:00"))
            if parsed_origin.tzinfo is None:
                parsed_origin = parsed_origin.replace(tzinfo=_BUSINESS_TZ)
            else:
                parsed_origin = parsed_origin.astimezone(_BUSINESS_TZ)
        except ValueError:
            parsed_origin = _business_now()
        state["next_retry_at"] = (
            parsed_origin + timedelta(seconds=retry_after_seconds)
        ).isoformat(timespec="seconds")
    try:
        with _read_conn() as c:
            state["latest_usable_snapshot_date"] = _latest_usable_snapshot_date(c)
            # A compact status is a live polling projection, not a historical
            # readiness report.  Searching backwards through up to twelve
            # PIT contracts on every poll can monopolize the data connection
            # gate when today's slice is incomplete.  Report only the exact
            # target state; full diagnostics retain the historical lookup.
            if compact:
                target = target_trade_date(c)
                state["latest_decision_ready_trade_date"] = (
                    target if decision_data_contract(target, conn=c).get("ok") else ""
                )
            else:
                state["latest_decision_ready_trade_date"] = _latest_decision_ready_trade_date(c)
                target = target_trade_date(c)
            probe_rows = c.execute(
                """SELECT dataset,provider,status,probed_at,next_probe_at,latency_ms
                   FROM dc_market_provider_probe WHERE trade_date=?
                   ORDER BY dataset,probed_at,latency_ms""",
                (target,),
            ).fetchall()
            state["provider_discovery"] = {
                "trade_date": target,
                "items": [dict(row) for row in probe_rows],
                "policy": "adaptive_probe_then_hedged_first_complete_slice_v2",
            }
            performance_rows = c.execute(
                """SELECT dataset,provider,sample_date,bulk_latency_ms,coverage,success,failure_kind,completed_at
                   FROM dc_market_provider_performance
                   WHERE sample_date=(SELECT MAX(sample_date) FROM dc_market_provider_performance)
                   ORDER BY dataset,provider"""
            ).fetchall()
            state["provider_performance"] = [dict(row) for row in performance_rows]
            if not compact:
                # These are useful for the full data-center diagnostics but
                # are too expensive for the task-center poll: calibration
                # opens one connection per dataset/provider and the two
                # detailed projections are only explanatory evidence.
                from app.data_platform.services.market_data_discovery import provider_calibration_status
                state["provider_calibration"] = provider_calibration_status()
                audit_rows = c.execute(
                    """SELECT dataset,trade_date,primary_provider,fallback_provider,field_name,
                              compared_count,mismatch_count,mean_abs_diff,max_abs_diff,
                              mean_relative_diff,max_relative_diff,status,checked_at
                       FROM dc_market_provider_field_audit WHERE trade_date=?
                       ORDER BY dataset,field_name""",
                    (target,),
                ).fetchall()
                state["provider_field_audit"] = {
                    "trade_date": target,
                    "items": [dict(row) for row in audit_rows],
                    "contract": "alis_market_provider_field_audit_v1",
                }
                from app.data_platform.services.market_bulk_execution import recent_execution_status
                state["provider_bulk_executions"] = {
                    "trade_date": target,
                    "items": recent_execution_status(target),
                    "contract": "alis_market_bulk_checkpoint_v1",
                }
    except TradeCalendarNotReady as exc:
        state.update({
            "ok": False,
            "phase": "blocked",
            "failure_category": exc.code,
            "error": str(exc),
            "message": str(exc),
            "trade_date": "",
        })
    except Exception as exc:  # noqa: BLE001
        state["status_error"] = str(exc)[:200]
    if compact:
        # Completed runs retain potentially large per-step source evidence in
        # memory. The console only needs the current state and provider
        # publication summary; full status remains available for diagnostics.
        state.pop("steps", None)
    return state


def simple_health(*, compact: bool = False) -> dict[str, Any]:
    try:
        with _read_conn() as c:
            target = target_trade_date(c)
            contract = decision_data_contract(target, conn=c)
            snapshot_count, snapshot_count_estimated = estimated_table_count(c, "dc_stock_daily_snapshot")
            values = {
                "stock_count": c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone(),
                "snapshot_count": {"cnt": snapshot_count},
                "latest_trade_date": {"value": _latest_usable_snapshot_date(c)},
                # The full health route searches prior sessions when today's
                # target is incomplete.  That is valuable for diagnostics but
                # can take several seconds against a large PIT history.  A
                # compact task-center read only needs to know whether the
                # current target is executable; never substitute an older
                # session as if it were today's readiness.
                "latest_decision_ready_trade_date": {
                    "value": (
                        target if contract.get("ok") else ""
                    ) if compact else _latest_decision_ready_trade_date(c)
                },
                "latest_announcement": c.execute("SELECT MAX(publish_time) value FROM dc_announcement_raw").fetchone(),
                "failed_runs": c.execute("SELECT COUNT(*) cnt FROM data_ingestion_runs WHERE status='failed'").fetchone(),
                "recent_failed_runs": c.execute(
                    "SELECT COUNT(*) cnt FROM data_ingestion_runs WHERE status='failed' AND COALESCE(finished_at,started_at)>=?",
                    ((_business_now() - timedelta(hours=24)).isoformat(timespec="seconds"),),
                ).fetchone(),
            }
    except TradeCalendarNotReady as exc:
        return {
            "ok": False,
            "level": "blocked",
            "summary": str(exc),
            "target_trade_date": "",
            "latest_trade_date": "",
            "latest_decision_ready_trade_date": "",
            "decision_contract": {"ok": False, "error_code": exc.code, "error": str(exc)},
            "calendar": {"status": "blocked", "error_code": exc.code},
        }
    data = {key: dict(row) for key, row in values.items()}
    latest = str(data["latest_trade_date"].get("value") or "")
    decision_ready = str(data["latest_decision_ready_trade_date"].get("value") or "")
    stock_count = int(data["stock_count"].get("cnt") or 0)
    snapshot_count = int(data["snapshot_count"].get("cnt") or 0)
    if stock_count == 0 or snapshot_count == 0:
        level, summary = "needs_setup", "尚未完成首次数据更新"
    elif decision_ready == target:
        level, summary = "healthy", "数据状态正常"
    else:
        level, summary = "blocked", f"目标交易日 {target} 尚未完成决策数据更新"
    return {
        "ok": level == "healthy", "level": level, "summary": summary, "target_trade_date": target, "latest_trade_date": latest,
        "latest_decision_ready_trade_date": decision_ready,
        "decision_contract": contract,
        "stock_count": stock_count, "snapshot_count": snapshot_count,
        "snapshot_count_estimated": snapshot_count_estimated,
        "latest_announcement": data["latest_announcement"].get("value"),
        "historical_failed_runs": int(data["failed_runs"].get("cnt") or 0),
        "recent_failed_runs": int(data["recent_failed_runs"].get("cnt") or 0),
        # Backward compatibility only.  UI must not call this historical total
        # "pending" or "unresolved" failures.
        "failed_runs": int(data["failed_runs"].get("cnt") or 0),
    }
