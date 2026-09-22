from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, time as dtime, timedelta
from zoneinfo import ZoneInfo
import os
from time import perf_counter
from typing import Any, Callable, Optional

from app.integrations.tushare_client import reset_tushare_client
from app.data_platform.db import _conn as data_conn, _read_conn as data_read_conn
from app.db.store import (
    _conn as business_conn,
    get_account_balance,
    get_quant_replay_waterline,
    get_quant_run_task_health,
    get_quant_run_task_runtime_config,
    create_or_reuse_autonomous_quant_run_task,
    claim_quant_run_task_start,
    create_quant_run_task_record,
    get_quant_run_task,
    list_active_quant_run_tasks,
    list_quant_run_task_records,
    upsert_quant_run_task_health,
    upsert_quant_replay_waterline,
    update_quant_run_task,
)
from app.services.cache_consolidation import trigger_backfill_merge_meta_async
from app.services.backtest_data_source import BacktestDataSource
from app.data_platform.tushare_client import is_tushare_compatible_source
from app.services.quant_run_engine import (
    HistoricalDataMissingError,
    ReplayRiskGateError,
    require_consolidated_trade_dates,
    run_for_trade_date,
    list_consolidated_trade_dates,
)
from app.services.quant_risk import RiskPauseDecision, evaluate_account_drawdown_gate, evaluate_consecutive_loss_gate, evaluate_data_health_gate, evaluate_latest_market_day_gate, evaluate_model_readiness_gate, no_pause, pause_decision
from app.services.quant_task_state_machine import can_transition, normalize_status

logger = logging.getLogger("quant.quant_task_runner")
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

_task_lock = threading.Lock()
_running_task_ids: set[int] = set()
_autonomous_task_lock = threading.Lock()
_account_execution_lock = threading.Lock()
_REPLAY_BATCH_STEPS = max(1, min(int(os.getenv("QUANT_REPLAY_BATCH_STEPS", "3")), 5))
_PRAGMATIC_MAX_GAP_DAYS = max(1, min(int(os.getenv("QUANT_PRAGMATIC_MAX_GAP_DAYS", "5")), 30))

# These failures mean execution inputs must be refreshed and prepared again.
# They must leave a frozen plan retryable; turning one into a contract terminal
# state would prevent the publication/account-snapshot loop from recovering.
_RETRYABLE_EXECUTION_CODES = {
    "paused_frozen_execution_data_not_ready",
    "paused_frozen_execution_order_data_not_ready",
    "paused_data_not_ready",
    "future_guard_execute_snapshot",
    "historical_data_missing_508",
    "execution_account_snapshot_changed",
}
_ENFORCE_TRADING_SESSION_GATE = str(os.getenv("QUANT_ENFORCE_TRADING_SESSION_GATE", "0")).strip().lower() in {"1", "true", "yes", "on"}
_DEFAULT_SCORING_TIMEOUT_SEC = max(10, min(int(os.getenv("QUANT_SCORING_TIMEOUT_SEC", "90")), 1800))
_DEFAULT_MIN_ACCOUNT_BALANCE = max(0.0, float(os.getenv("QUANT_MIN_ACCOUNT_BALANCE", "0") or 0))
_DEFAULT_MAX_CONSECUTIVE_LOSSES = max(0, min(int(os.getenv("QUANT_MAX_CONSECUTIVE_LOSSES", "3")), 30))
_DEFAULT_ENABLE_DATA_HEALTH_GATE = str(os.getenv("QUANT_ENABLE_DATA_HEALTH_GATE", "1")).strip().lower() in {"1", "true", "yes", "on"}
_AUTO_SIMULATION_ENABLED = str(os.getenv("QUANT_AUTO_SIMULATION", "1")).strip().lower() in {"1", "true", "yes", "on"}
_AUTO_SIMULATION_INTERVAL_SEC = max(60, min(int(os.getenv("QUANT_AUTO_SIMULATION_INTERVAL_SEC", "60")), 3600))
_AUTO_SIMULATION_CLOSE_TIME = dtime(
    max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_HOUR", "15")), 23)),
    max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_MINUTE", "1")), 59)),
)
_DEFENSIVE_EXECUTION_REFRESH_INTERVAL_SEC = max(
    60,
    min(int(os.getenv("QUANT_DEFENSIVE_EXECUTION_REFRESH_INTERVAL_SEC", "300")), 3600),
)
_auto_scheduler_started = False
_auto_scheduler_watchdog_started = False
_defensive_execution_refresh_lock = threading.Lock()
_defensive_execution_refresh_attempts: dict[str, float] = {}
_plan_materialization_retry_lock = threading.Lock()
_plan_materialization_retry_after: dict[str, float] = {}
_PLAN_MATERIALIZATION_TIMEOUT_COOLDOWN_SEC = max(
    30,
    min(int(os.getenv("QUANT_PLAN_TIMEOUT_COOLDOWN_SEC", "60")), 600),
)


def _business_now() -> datetime:
    """Return wall-clock time in ALIS's UTC+8 business timezone."""
    return datetime.now(_BUSINESS_TZ)


def _wait_for_auto_simulation_retry(
    beat_fn: Callable[[str], Any],
    wait_fn: Callable[[float], Any] | None = None,
) -> None:
    """Keep scheduler liveness current while an expected retry is deferred."""
    beat_fn("auto_simulation_scheduler")
    (wait_fn or threading.Event().wait)(_AUTO_SIMULATION_INTERVAL_SEC)


def _is_expected_scheduler_wait(exc: BaseException) -> bool:
    """Classify bounded close-data polling failures as a retryable wait."""
    message = str(exc or "").lower()
    return any(marker in message for marker in (
        "statement timeout", "querycanceled", "query canceled",
        "current transaction is aborted", "connection timeout",
        "connection timeout expired",
    ))


def _release_retryable_execution_claim(
    active_claim: tuple[str, str] | None,
    *,
    reason: BaseException,
) -> bool:
    """Release a recoverable execution lease so the next run prepares anew."""
    if not active_claim:
        return False
    from app.services.profit_loop import release_daily_rebalance_execution_claim

    signal_date, claim_token = active_claim
    release_daily_rebalance_execution_claim(
        signal_date,
        claim_token=claim_token,
        reason=reason,
    )
    return True


def _trade_date_key(value: object) -> str:
    """Normalize supported trade-date representations for identity checks."""
    normalized = str(value or "").strip().replace("-", "")[:8]
    return normalized if len(normalized) == 8 and normalized.isdigit() else ""


def _waiting_schedule_projection() -> dict[str, Any]:
    """Project the next real frozen plan instead of retaining a stale attempt date."""
    with business_conn() as conn:
        row = conn.execute(
            """SELECT trade_date,planned_execute_date
                 FROM daily_rebalance_plans
                WHERE status='planned'
                ORDER BY planned_execute_date,version DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {
            "replay_gap_days": 0,
            "replay_current_execute_date": None,
            "replay_target_execute_date": None,
            "last_message": "等待收盘调度与下一份到期正式调仓计划",
        }
    item = dict(row)
    signal_date = _trade_date_key(item.get("trade_date"))
    execute_date = _trade_date_key(item.get("planned_execute_date"))
    return {
        "replay_gap_days": 1,
        "replay_current_execute_date": execute_date or None,
        "replay_target_execute_date": execute_date or None,
        "last_message": f"等待正式计划 {signal_date}→{execute_date} 到期执行",
    }


def _refresh_performance_reports(task_id: int) -> None:
    """Refresh persisted period reports after a batch is durably written.

    The calculation remains owned by the performance API so the UI and the
    scheduler share exactly one aggregation definition. Failure is non-fatal:
    the next scheduler tick retries it and the task ledger is never rolled
    back because a report refresh is unavailable.
    """
    def _request() -> None:
        try:
            # Import lazily to avoid the API/router -> task-runner import
            # cycle during application startup. Calling the same aggregation
            # function directly also works for single-worker deployments.
            from app.api.routes.quant_runs import build_quant_run_performance
            result = build_quant_run_performance(task_id=int(task_id), persist_reports=True)
            if result.get("persisted_reports") is None:
                raise RuntimeError("performance reports were not persisted")
        except Exception as exc:
            logger.warning("performance report refresh deferred task_id=%s: %s", task_id, exc)

    # Do not call the same sync API while the task worker is still finalizing
    # its transaction. A short daemon timer lets the worker release DB state
    # and avoids the self-request connection reset observed under uvicorn.
    threading.Timer(0.5, _request).start()


def _refresh_research_cycle(task_id: int) -> None:
    """After a durable trading batch, maintain managed theses from new evidence."""
    limit = max(1, min(int(os.getenv("QUANT_RESEARCH_MAINTENANCE_LIMIT", "100")), 500))

    def _run() -> None:
        try:
            from app.data_platform.services.data_center import enrich_financial_research_fields, financial_research_completeness
            from app.services.decision_store import list_pool_members
            from app.services.industry_event_engine import materialize_event_followups, materialize_industry_state_snapshots, review_linked_event_theses
            from app.services.thesis_store import generate_watch_pool_theses, review_due_theses
            from app.services.theme_engine import materialize_theme_snapshots
            from app.services.defensive_assets import materialize_defensive_asset_history
            from app.services.research_engine import materialize_pool_research_snapshots
            from app.services.research_workflow import process_watch_pool

            pool_codes: list[str] = []
            for pool_type in ("core", "candidate", "watch"):
                for member in list_pool_members(pool_type):
                    code = str(member.get("ts_code") or "")
                    if code and code not in pool_codes:
                        pool_codes.append(code)
            enriched: list[dict[str, Any]] = []
            enrich_limit = max(0, min(int(os.getenv("QUANT_RESEARCH_ENRICH_STOCKS", "1")), 5))
            for code in pool_codes:
                if len(enriched) >= enrich_limit:
                    break
                completeness = financial_research_completeness(code, period_limit=5)
                if completeness.get("status") != "complete":
                    result = enrich_financial_research_fields(code, period_limit=5, request_limit=20)
                    enriched.append({"ts_code": code, "status": result.get("after", {}).get("status"), "requested": result.get("requested")})
            generated = generate_watch_pool_theses(limit=limit)
            promoted = process_watch_pool(limit=limit)
            due = review_due_theses(limit=limit, review_interval_days=1)
            event_reviews = review_linked_event_theses(limit=limit)
            followups = materialize_event_followups(limit=500, thesis_reviews=event_reviews.get("reviewed") or [])
            themes = materialize_theme_snapshots()
            research_snapshots = materialize_pool_research_snapshots(
                limit=max(1, min(int(os.getenv("QUANT_RESEARCH_SNAPSHOT_STOCKS", "100")), 500)),
            )
            today = _business_now().date()
            defensive_assets = materialize_defensive_asset_history(
                start_date=(today - timedelta(days=10)).strftime("%Y%m%d"),
                end_date=today.strftime("%Y%m%d"),
            )
            logger.info(
                "research cycle refreshed task_id=%s watch=%s due=%s events=%s",
                task_id, generated.get("candidates", 0), due.get("due", 0),
                event_reviews.get("reviewed_count", len(event_reviews.get("reviewed") or [])),
            )
            if enriched:
                materialized = materialize_industry_state_snapshots()
                logger.info("research financial enrichment task_id=%s items=%s", task_id, enriched)
                logger.info("industry states materialized task_id=%s result=%s", task_id, materialized)
            logger.info("themes materialized task_id=%s result=%s", task_id, themes)
            logger.info("research snapshots materialized task_id=%s result=%s", task_id, research_snapshots)
            logger.info("watch pool processed task_id=%s result=%s", task_id, promoted)
            logger.info("event followups materialized task_id=%s result=%s", task_id, followups)
            logger.info("defensive assets refreshed task_id=%s result=%s", task_id, defensive_assets)
        except Exception as exc:
            logger.warning("research cycle refresh deferred task_id=%s: %s", task_id, exc)

    threading.Timer(1.0, _run).start()


def _utc_now_iso() -> str:
    # Historical name retained for storage compatibility; scheduling and task
    # evidence use the UTC+8 business clock rather than host defaults.
    return _business_now().isoformat()


def _is_running(task_id: int) -> bool:
    with _task_lock:
        return int(task_id) in _running_task_ids


def _mark_running(task_id: int, running: bool) -> None:
    with _task_lock:
        if running:
            _running_task_ids.add(int(task_id))
        else:
            _running_task_ids.discard(int(task_id))


def _market_slice_ready(stock_count: int, counts: dict[str, int]) -> bool:
    """Apply the complete T-day decision contract."""
    return bool(stock_count) and (
        counts["daily_n"] / stock_count >= 0.995
        and counts["valuation_n"] / stock_count >= 0.95
        and counts["limit_n"] / stock_count >= 0.99
    )


def _execution_slice_ready(stock_count: int, counts: dict[str, int]) -> bool:
    """Require only market and limit data needed by a frozen T+1 plan."""
    execution_price_n = int(counts.get("execution_price_n", counts.get("daily_n", 0)) or 0)
    return bool(stock_count) and (
        execution_price_n / stock_count >= 0.995
        and counts["limit_n"] / stock_count >= 0.99
    )


def _strict_execution_observation_record(record: dict[str, Any], *, cutoff: datetime) -> bool:
    """Check that one immutable row can safely support a live execution gate.

    Kept only for the explicit non-PostgreSQL compatibility path. Production
    PostgreSQL status checks use the direct-hash SQL contract below and never
    materialize a full version payload set in Python.
    """

    def _parse(value: Any) -> datetime | None:
        raw = str(value or "").strip().replace("Z", "+00:00")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(_BUSINESS_TZ)

    observed = _parse(record.get("observed_at"))
    first_observed = _parse(record.get("first_observed_at"))
    economic = _parse(record.get("economic_available_time"))
    created = _parse(record.get("created_at"))
    provenance = str(record.get("observation_provenance") or "").strip().lower()
    return bool(
        str(record.get("source_code") or "").strip()
        and str(record.get("content_hash") or "").strip()
        and provenance
        and not provenance.startswith("legacy_")
        and observed is not None
        and first_observed is not None
        and economic is not None
        and created is not None
        and first_observed <= observed <= cutoff
        and economic <= observed
        and created <= cutoff
    )


def _snapshot_data_status(trade_date: str = "") -> tuple[str, dict[str, int]]:
    """Return immutable exact-date market coverage without applying policy.

    This is a scheduler hint only; per-order immutable evidence remains the
    final execution gate. It must nevertheless not treat a mutable projection
    as publication proof, otherwise an incomplete late rewrite can advertise a
    close slice as ready before the exact frozen-order audit runs.
    """
    requested = _trade_date_key(trade_date)
    from app.data_platform.services.market_calendar import TradeCalendarNotReady, target_trade_date
    from app.data_platform.services.decision_data_contract import decision_data_contract
    from app.data_platform.services.observation_clock import load_raw_record_versions, load_snapshot_versions

    observation_cutoff = _business_now()
    cutoff_text = observation_cutoff.isoformat(timespec="seconds")
    # Scheduler status reads never need to share a mutation transaction.  An
    # autocommit read avoids retaining an ``idle in transaction`` backend while
    # a later daily-update or task step is being scheduled.
    with data_read_conn() as c:
        try:
            target = requested or target_trade_date(c)
        except TradeCalendarNotReady as exc:
            logger.error("交易日历未就绪，收盘数据检查阻断: %s", exc)
            return "", {
                "stock_count": 0,
                "daily_n": 0,
                "execution_price_n": 0,
                "valuation_n": 0,
                "limit_n": 0,
                "immutable_schema_ready": 0,
                "calendar_ready": 0,
                "calendar_error": exc.code,
            }
        if bool(getattr(c, "_is_postgres", False)):
            # Reuse the exact direct-hash/PIT contract used by plan
            # generation. It performs one bounded target-date query and also
            # returns the execution-only open/pre-close intersection; avoid a
            # second scheduler-specific aggregate over the 41 GB projection.
            contract = decision_data_contract(
                target,
                conn=c,
                as_of_time=cutoff_text,
                include_diagnostics=False,
            )
            contract_counts = dict(contract.get("counts") or {})
            return target, {
                "stock_count": max(0, int(contract.get("stock_count") or 0)),
                "daily_n": max(0, int(contract_counts.get("daily") or 0)),
                "execution_price_n": max(0, int(contract.get("execution_price_rows") or 0)),
                "valuation_n": max(0, int(contract_counts.get("daily_basic") or 0)),
                "limit_n": max(0, int(contract_counts.get("stk_limit") or 0)),
                "immutable_schema_ready": int(bool(
                    (contract.get("observation_contract") or {}).get("strict_schema_ready")
                )),
                "decision_ready": int(bool(contract.get("ok"))),
            }
        stock_rows = c.execute("SELECT ts_code FROM dc_stock_master").fetchall()
        stock_codes = {str(dict(row).get("ts_code") or "") for row in stock_rows}
        stock_codes.discard("")
        stock_count = len(stock_codes)
        snapshot_versions = load_snapshot_versions(c, cutoff=cutoff_text, trade_date=target)
        daily_versions = load_raw_record_versions(
            c, "daily", cutoff=cutoff_text, trade_date_start=target, trade_date_end=target,
        )
        basic_versions = load_raw_record_versions(
            c, "daily_basic", cutoff=cutoff_text, trade_date_start=target, trade_date_end=target,
        )
        limit_versions = load_raw_record_versions(
            c, "stk_limit", cutoff=cutoff_text, trade_date_start=target, trade_date_end=target,
        )
    # A missing version schema is fail-closed in production. SQLite's fixture
    # compatibility is intentionally not used by the autonomous scheduler.
    if any(value is None for value in (snapshot_versions, daily_versions, basic_versions, limit_versions)):
        return target, {
            "stock_count": stock_count,
            "daily_n": 0,
            "execution_price_n": 0,
            "valuation_n": 0,
            "limit_n": 0,
            "immutable_schema_ready": 0,
        }

    def _payload(item: dict) -> dict:
        import json
        try:
            value = json.loads(item.get("payload_json") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _finite(value: Any, *, positive: bool = False) -> bool:
        if isinstance(value, bool):
            return False
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return False
        return parsed == parsed and abs(parsed) != float("inf") and (parsed > 0 if positive else True)

    def _eligible_rows(versions: list[dict] | None) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for raw in versions or []:
            item = dict(raw)
            if not _strict_execution_observation_record(item, cutoff=observation_cutoff):
                continue
            payload = _payload(item)
            code = str(item.get("ts_code") or "").strip().upper()
            payload_code = str(payload.get("ts_code") or code).strip().upper()
            row_date = str(item.get("trade_date") or "").replace("-", "")[:8]
            payload_date = str(payload.get("trade_date") or row_date).replace("-", "")[:8]
            if not code or code not in stock_codes or payload_code != code or row_date != target or payload_date != target:
                continue
            result[code] = payload
        return result

    daily_by_code = _eligible_rows(daily_versions)
    basic_by_code = _eligible_rows(basic_versions)
    limit_by_code = _eligible_rows(limit_versions)
    snapshot_by_code: dict[str, dict] = {}
    for item in snapshot_versions or []:
        item = dict(item)
        if not _strict_execution_observation_record(item, cutoff=observation_cutoff):
            continue
        payload = _payload(item)
        code = str(item.get("ts_code") or "").strip().upper()
        payload_code = str(payload.get("ts_code") or code).strip().upper()
        row_date = str(item.get("trade_date") or "").replace("-", "")[:8]
        payload_date = str(payload.get("trade_date") or row_date).replace("-", "")[:8]
        if code and code in stock_codes and payload_code == code and row_date == target and payload_date == target:
            snapshot_by_code[code] = payload
    daily_n = sum(_finite(row.get("close"), positive=True) for row in daily_by_code.values())
    execution_price_n = sum(
        _finite(row.get("open"), positive=True) and _finite(row.get("pre_close"), positive=True)
        and _finite(snapshot_by_code.get(code, {}).get("open"), positive=True)
        and _finite(snapshot_by_code.get(code, {}).get("pre_close"), positive=True)
        for code, row in daily_by_code.items()
    )
    valuation_n = sum(
        _finite(row.get("total_mv"), positive=True)
        and _finite(snapshot_by_code.get(code, {}).get("total_mv"), positive=True)
        for code, row in basic_by_code.items()
    )
    limit_n = sum(
        _finite(limit_by_code.get(code, {}).get("up_limit"))
        and _finite(limit_by_code.get(code, {}).get("down_limit"))
        and _finite(snapshot_by_code.get(code, {}).get("up_limit"))
        and _finite(snapshot_by_code.get(code, {}).get("down_limit"))
        for code in daily_by_code
    )
    counts = {
        "daily_n": int(daily_n),
        "execution_price_n": int(execution_price_n),
        "valuation_n": int(valuation_n),
        "limit_n": int(limit_n),
        "immutable_schema_ready": 1,
    }
    return target, {"stock_count": stock_count, **counts}


def _close_data_status() -> tuple[str, bool, dict[str, int]]:
    """Return the close-of-day decision slice status for the current target.

    The simulator must never silently fall back to yesterday merely because
    today's market bundle has not been ingested yet.  Keep this check close to
    the scheduler instead of relying on the short-lived readiness cache. The
    aggregate counts remain useful execution evidence, but the decision-ready
    result must come from the same strict field/PIT contract used by plan
    generation. Otherwise a partial valuation or turnover slice can suppress
    the refresh that would repair it.
    """
    target, counts = _snapshot_data_status()
    if not target:
        return target, False, counts
    if "decision_ready" in counts:
        return target, bool(counts.get("decision_ready")), counts
    try:
        from app.data_platform.services.decision_data_contract import decision_data_contract

        contract = decision_data_contract(target)
        ready = bool(contract.get("ok")) if isinstance(contract, dict) else False
    except Exception as exc:  # noqa: BLE001 - a scheduler data gate is fail-closed
        logger.warning(
            "收盘严格决策数据合同检查失败 target=%s error=%s",
            target,
            str(exc)[:200],
        )
        ready = False
    return target, ready, counts


def _requires_replay_opinion_gates(mode: str) -> bool:
    """Only research replay may reconsider data, drawdown, loss or cash opinions."""
    return str(mode or "") != "autonomous_daily"


def _evaluate_frozen_execution_data_gate(
    execute_date: str,
    required_defensive_ts_codes: list[str] | tuple[str, ...] | None = None,
) -> RiskPauseDecision:
    """Validate only the exact-date market fields allowed by the T+1 contract."""
    target, counts = _snapshot_data_status(execute_date)
    from app.services.defensive_assets import defensive_asset_execution_status

    defensive_status = defensive_asset_execution_status(
        execute_date,
        required_defensive_ts_codes or (),
    )
    stock_ready = target == _trade_date_key(execute_date) and _execution_slice_ready(
        int(counts.get("stock_count") or 0), counts,
    )
    if stock_ready and defensive_status.get("ok"):
        return no_pause()
    missing_defensive = list(defensive_status.get("missing") or [])
    missing_detail = f"；防御ETF缺失 {','.join(missing_defensive)}" if missing_defensive else ""
    return pause_decision(
        code="paused_frozen_execution_data_not_ready",
        message=f"执行日 {target or execute_date} 的股票或ETF交易安全切片未就绪{missing_detail}。",
        recovery_condition="补齐冻结执行日的 daily、stk_limit 与所需 fund_daily 后由调度器自动重试。",
        exit_condition="当日仍无法取得安全切片时记录漏跑，不得跨日补成交。",
        severity="error",
        evidence={
            "schema_version": "alis_frozen_t1_execution_data_gate_v3",
            "execute_date": _trade_date_key(execute_date),
            "target_trade_date": target,
            "counts": counts,
            "required_fields": ["daily.open", "daily.pre_close", "stk_limit"],
            "defensive_asset_required_fields": [
                "fund_daily.trade_date=execute_date",
                "fund_daily.open",
                "fund_daily.high",
                "fund_daily.low",
                "fund_daily.close",
                "fund_daily.pre_close",
                "fund_daily.vol",
                "fund_daily.amount",
            ],
            "defensive_assets": defensive_status,
            "daily_basic_required": False,
        },
    )


def _refresh_missing_frozen_defensive_assets(
    execute_date: str,
    required_defensive_ts_codes: list[str] | tuple[str, ...] | None,
) -> dict[str, Any]:
    """Bound exact-date ETF refreshes so an unpublished slice is not hammered."""
    from app.services.defensive_assets import (
        defensive_asset_execution_status,
        materialize_defensive_asset_history,
    )

    required = sorted({str(value or "").strip().upper() for value in required_defensive_ts_codes or () if str(value or "").strip()})
    status = defensive_asset_execution_status(execute_date, required)
    missing = list(status.get("missing") or [])
    if not missing:
        return {**status, "refresh": "not_needed"}
    refresh_key = f"{_trade_date_key(execute_date)}:{','.join(missing)}"
    now = perf_counter()
    with _defensive_execution_refresh_lock:
        last_attempt = float(_defensive_execution_refresh_attempts.get(refresh_key) or 0.0)
        if last_attempt and now - last_attempt < _DEFENSIVE_EXECUTION_REFRESH_INTERVAL_SEC:
            return {**status, "refresh": "throttled"}
        _defensive_execution_refresh_attempts[refresh_key] = now
    from app.data_platform.services.tencent_etf_daily_fallback import repair_fund_daily_from_tencent
    from app.data_platform.services.market_data_discovery import (
        discover_first_published,
        mark_provider_bulk_result,
    )

    discovery = discover_first_published("defensive_fund_daily", execute_date)
    winner = str(discovery.get("winner") or "")
    if is_tushare_compatible_source(winner):
        provider_result = materialize_defensive_asset_history(
            start_date=execute_date,
            end_date=execute_date,
            ts_codes=missing,
            provider=winner,
        )
    elif winner == "tencent_etf_quote":
        provider_result = repair_fund_daily_from_tencent(execute_date, required)
    else:
        provider_result = {"ok": False, "status": "no_provider_published", "rows_written": 0}
    refreshed = defensive_asset_execution_status(execute_date, required)
    if winner:
        mark_provider_bulk_result(
            "defensive_fund_daily",
            execute_date,
            winner,
            ok=not bool(refreshed.get("missing")),
            evidence={"status": provider_result.get("status"), "missing": refreshed.get("missing") or []},
        )
    return {
        **refreshed,
        "refresh": "attempted",
        "provider_discovery_contract": "alis_market_publication_probe_v1",
        "winner": winner,
        "discovery": discovery,
        "provider_result": provider_result,
        "refresh_result": provider_result if is_tushare_compatible_source(winner) else None,
        "fallback_result": provider_result if winner == "tencent_etf_quote" else None,
    }


def _refresh_missing_frozen_stock_orders(
    execute_date: str,
    contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """Repair only stock symbols in the frozen order set that fail the gate.

    The market bundle's coverage threshold is intentionally broad.  A frozen
    order is narrower and must be repaired exactly, so a 99.5% universe cannot
    hide one missing order behind the aggregate ratio.
    """
    from app.data_platform.services.data_center import sync_market_bundle
    from app.services.profit_loop import audit_frozen_execution_orders
    from app.data_platform.services.tencent_limit_fallback import repair_stk_limit_from_tencent

    audit = audit_frozen_execution_orders(contract or {}, execute_date)
    missing_items = [
        item for item in audit.get("orders") or []
        if not bool(item.get("ready")) and not bool(item.get("is_etf"))
    ]
    if not missing_items:
        return {"refresh": "not_needed", "audit": audit}
    target = _trade_date_key(execute_date)
    requested_apis: set[str] = set()
    for item in missing_items:
        fields = set(str(value) for value in item.get("missing_fields") or [])
        if any(value.startswith("daily.") or value.startswith("snapshot.") for value in fields):
            requested_apis.add("daily")
        if any(value.startswith("stk_limit.") for value in fields):
            requested_apis.add("stk_limit")

    # A frozen order gap is exact, but its data contract is still a market
    # slice.  Reuse the publication race and full-slice validators so the
    # repair cannot quietly pin itself to the primary endpoint or retain a
    # per-symbol partial response as executable evidence.
    results: list[dict[str, Any]] = []
    if requested_apis:
        try:
            result = sync_market_bundle(target, apis=sorted(requested_apis))
            results.append({
                "ts_codes": [str(item.get("ts_code") or "").strip().upper() for item in missing_items],
                "apis": sorted(requested_apis),
                "result": result,
            })
        except Exception as exc:
            results.append({
                "apis": sorted(requested_apis),
                "error": str(exc)[:240],
            })
    refreshed = audit_frozen_execution_orders(contract or {}, execute_date)
    if not refreshed.get("ok") and any(
        any(str(value).startswith("stk_limit.") for value in item.get("missing_fields") or [])
        for item in refreshed.get("orders") or []
        if not item.get("is_etf")
    ):
        try:
            fallback = repair_stk_limit_from_tencent(target)
        except Exception as exc:
            fallback = {"ok": False, "status": "fallback_request_failed", "error": str(exc)[:240]}
        results.append({"api": "stk_limit", "fallback": fallback})
        refreshed = audit_frozen_execution_orders(contract or {}, execute_date)
    return {"refresh": "attempted", "audit": refreshed, "results": results}


def _ensure_close_data_ready() -> tuple[str, bool]:
    """Refresh the full bundle while independently reporting T+1 readiness.

    ``start_daily_update`` is single-flight, so scheduler ticks safely join an
    in-progress update. Missing valuation data blocks today's new decision, but
    it must not block yesterday's immutable plan once daily and limit data are
    available for execution safety checks.
    """
    target, decision_ready, counts = _close_data_status()
    execution_ready = _execution_slice_ready(int(counts.get("stock_count") or 0), counts)
    if decision_ready:
        return target, True
    from app.data_platform.services.daily_update import start_daily_update

    result = start_daily_update()
    if result.get("ok"):
        logger.info("收盘日更已启动 target=%s counts=%s", target, counts)
    elif result.get("reason") != "already_running":
        logger.warning("收盘日更未启动 target=%s result=%s", target, result)
    return target, execution_ready


def _reconcile_missed_plans_by_wall_clock(now: datetime) -> int:
    """Close prior-date plans even when the scheduler wakes on a weekend.

    The latest available market date remains Friday throughout the weekend,
    so it cannot by itself prove that Friday's exact execution window is still
    open.  Natural-date reconciliation only closes strictly earlier dates and
    therefore never pre-empts a plan due later on the same day.
    """
    from app.services.profit_loop import (
        mark_missed_daily_rebalance_execution_inputs,
    )

    return mark_missed_daily_rebalance_execution_inputs(
        now.strftime("%Y%m%d")
    )


def _is_exchange_trade_day(value: datetime) -> bool:
    """Use the retained exchange calendar before the bundled fallback.

    The scheduler can affect an immutable plan, so an unknown date is not
    enough evidence to begin execution-data polling.  The static holiday list
    only protects the system while the calendar database is temporarily
    unavailable; it must not override an explicit exchange closure.
    """
    trade_date = value.strftime("%Y%m%d")
    try:
        with data_conn() as conn:
            row = conn.execute(
                "SELECT is_open FROM dc_trade_calendar WHERE exchange='SSE' AND cal_date=? LIMIT 1",
                (trade_date,),
            ).fetchone()
        if row is not None:
            return bool(int(dict(row).get("is_open") or 0))
        logger.warning("交易所日历缺少执行日期，拒绝启动执行探测 date=%s", trade_date)
        return False
    except Exception as exc:  # noqa: BLE001 - execution must fail closed without exchange evidence
        logger.error("交易所日历不可读，拒绝启动执行探测 date=%s error=%s", trade_date, str(exc)[:160])
        return False


def _auto_simulation_check_due(now: datetime, close_time: dtime) -> bool:
    """Run close execution probes only on an A-share trading session.

    Calendar weekdays include exchange holidays.  Starting the close-data
    workflow on those dates creates avoidable upstream requests and can make a
    normal holiday look like an execution-data delay.
    """
    business_now = now.astimezone(_BUSINESS_TZ) if now.tzinfo else now.replace(tzinfo=_BUSINESS_TZ)
    return _is_exchange_trade_day(business_now) and business_now.time() >= close_time


def start_auto_simulation_scheduler() -> bool:
    """Start the close-of-day simulator trigger once per process.

    This only resumes the existing simulated quant-run tasks. The real/manual
    broker route remains protected by ``ensure_manual_trade_allowed``.
    """
    global _auto_scheduler_started, _auto_scheduler_watchdog_started
    if not _AUTO_SIMULATION_ENABLED:
        return False
    with _task_lock:
        if _auto_scheduler_started:
            return False
        _auto_scheduler_started = True

    def _loop() -> None:
        from app.services.runtime_heartbeat import mark_started, beat
        mark_started("auto_simulation_scheduler", interval_seconds=_AUTO_SIMULATION_INTERVAL_SEC)
        while True:
            try:
                now = _business_now()
                missed_count = _reconcile_missed_plans_by_wall_clock(now)
                if missed_count:
                    logger.error(
                        "正式调仓计划已错过自然日冻结窗口，转为审计终态 date=%s count=%s",
                        now.strftime("%Y%m%d"),
                        missed_count,
                    )
                from app.services.profit_loop import effective_rebalance_clock
                close_time = effective_rebalance_clock()
                if _auto_simulation_check_due(now, close_time):
                    target, decision_data_ready = _ensure_close_data_ready()
                    dates = list_consolidated_trade_dates()
                    latest = str(dates[-1]) if dates else ""
                    target_key = _trade_date_key(target)
                    if not target_key or _trade_date_key(latest) != target_key:
                        # Do not execute against the latest old slice while
                        # the exact execution-date bundle is still ingesting.
                        logger.warning("收盘日更已通过但合并行情尚未就绪 target=%s latest=%s", target, latest)
                        _wait_for_auto_simulation_retry(beat)
                        continue
                    if latest:
                        # Execution eligibility belongs to the immutable plan
                        # whose execute-date slice is now available. It must not
                        # depend on whether today's new T-day plan can be generated.
                        due_context = _resolve_autonomous_plan_context(
                            dates,
                            decision_data_ready=decision_data_ready,
                        )
                        formal_plan_claimable = bool(due_context.get("ok"))
                        if formal_plan_claimable:
                            execution_gate = _evaluate_frozen_execution_data_gate(
                                str(due_context.get("current_execute_date") or ""),
                                due_context.get("required_defensive_ts_codes") or [],
                            )
                            if execution_gate.should_pause:
                                try:
                                    _refresh_missing_frozen_stock_orders(
                                        str(due_context.get("current_execute_date") or ""),
                                        due_context.get("execution_contract") or {},
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "冻结计划股票精确补数失败 execute_date=%s: %s",
                                        due_context.get("current_execute_date"), str(exc)[:200],
                                    )
                                refresh = _refresh_missing_frozen_defensive_assets(
                                    str(due_context.get("current_execute_date") or ""),
                                    due_context.get("required_defensive_ts_codes") or [],
                                )
                                logger.warning(
                                    "冻结计划执行数据尚未就绪 execute_date=%s defensive=%s",
                                    due_context.get("current_execute_date"),
                                    refresh,
                                )
                                _wait_for_auto_simulation_retry(beat)
                                continue
                        if formal_plan_claimable:
                            active_tasks = list_active_quant_run_tasks()
                            autonomous_tasks = [
                                task for task in active_tasks
                                if str(task.get("replay_mode") or "") == "autonomous_daily"
                            ]
                            # Once the dedicated production task exists, legacy
                            # paused/manual replay tasks must never create a second
                            # set of simulated orders for the same session.
                            for task in autonomous_tasks or active_tasks:
                                status = str(task.get("status") or "").lower()
                                if status not in {"pending", "paused"}:
                                    continue
                                # The due frozen plan row is authoritative. A
                                # task progress field may describe an in-flight
                                # attempt that later failed contract validation;
                                # using it as a pre-claim dedupe key can strand a
                                # still-planned row forever. The atomic plan
                                # status/claim token remains the duplicate guard.
                                run_quant_task_async(int(task["id"]))
                        elif not decision_data_ready:
                            logger.info(
                                "冻结计划已无到期项；等待当天新决策数据完整 target=%s",
                                target,
                            )
                            _wait_for_auto_simulation_retry(beat)
                            continue
                beat("auto_simulation_scheduler")
            except Exception as exc:  # noqa: BLE001
                if _is_expected_scheduler_wait(exc):
                    logger.warning("自动模拟调度暂缓本轮数据探测，将在下一周期重试: %s", str(exc)[:200])
                    beat("auto_simulation_scheduler")
                else:
                    logger.warning("自动模拟调度检查失败: %s", str(exc)[:200])
                    beat("auto_simulation_scheduler", error=str(exc))
            threading.Event().wait(_AUTO_SIMULATION_INTERVAL_SEC)

    scheduler_thread = threading.Thread(target=_loop, name="quant-auto-simulation", daemon=True)
    scheduler_thread.start()
    with _task_lock:
        watchdog_needed = not _auto_scheduler_watchdog_started
        _auto_scheduler_watchdog_started = True
    if watchdog_needed:
        def _watchdog() -> None:
            from app.services.runtime_heartbeat import beat

            pulse_seconds = max(5, min(30, _AUTO_SIMULATION_INTERVAL_SEC // 3))
            while scheduler_thread.is_alive():
                beat("auto_simulation_scheduler")
                threading.Event().wait(pulse_seconds)

        threading.Thread(target=_watchdog, name="quant-auto-simulation-heartbeat", daemon=True).start()
    logger.info(
        "自动模拟调度已启动；%s 后随发布探针持续检查，数据完整即执行；每 %ss 检查日更与执行条件",
        _AUTO_SIMULATION_CLOSE_TIME.strftime("%H:%M"),
        _AUTO_SIMULATION_INTERVAL_SEC,
    )
    return True


def _is_tushare_auth_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return ("无效的 token" in msg) or ("invalid token" in msg) or ("token" in msg and "invalid" in msg)


def _as_bool(v: object, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(v: object, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _as_float(v: object, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _normalized_replay_mode(value: object) -> str:
    mode = str(value or "pragmatic_recover").strip().lower()
    return mode if mode in {"strict_replay", "pragmatic_recover", "autonomous_daily"} else "pragmatic_recover"


def _compute_start_index(dates: list[str], task: dict, done_signals: set[str]) -> int:
    if not dates:
        return 0
    if done_signals:
        latest = sorted(done_signals)[-1]
        if latest in dates:
            return dates.index(latest) + 1
    start_date = str(task.get("start_date") or "").strip()
    if start_date:
        start_date = start_date.replace("/", "-").replace(".", "-")
        if len(start_date) == 8 and start_date.isdigit():
            start_date = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        if start_date in dates:
            # 任务起点语义：从该 signal_date 开始执行（包含该日），而非“该日之后”。
            return dates.index(start_date)
        # 非交易日起点：对齐到起始日之后的第一个可用交易日，避免错误退回到“最近一天”。
        for idx, d0 in enumerate(dates):
            if d0 >= start_date:
                return idx
    return max(0, len(dates) - 2)


def _is_trading_session_now(now: datetime | None = None) -> bool:
    """Evaluate the A-share session in UTC+8 regardless of host timezone."""
    now = (now.astimezone(_BUSINESS_TZ) if now and now.tzinfo else now) or _business_now()
    if not _is_exchange_trade_day(now):
        return False
    t = now.time()
    # 实盘模拟执行窗口：09:25~15:05（午休不做细分）
    return dtime(9, 25) <= t <= dtime(15, 5)


def _derive_last_execute_date(task: dict, records: list[dict], waterline: dict | None) -> str:
    # 任务内进度优先，避免被其它任务写入的全局 waterline 覆盖。
    cands: list[str] = []
    task_last = task.get("last_execute_date")
    if task_last:
        cands.append(str(task_last))
    for r in records:
        d0 = str(r.get("execute_date") or "")
        if d0:
            cands.append(d0)
    if cands:
        return max(cands)
    wl = (waterline or {}).get("last_successful_execute_date")
    return str(wl) if wl else ""


def _successful_task_progress(records: list[dict], waterline: dict | None) -> tuple[str, str]:
    """Recover only committed progress; in-flight task fields are not evidence."""
    signals = [str(row.get("signal_date") or "") for row in records if row.get("signal_date")]
    executions = [str(row.get("execute_date") or "") for row in records if row.get("execute_date")]
    signal = max(signals) if signals else str((waterline or {}).get("last_successful_signal_date") or "")
    execute = max(executions) if executions else str((waterline or {}).get("last_successful_execute_date") or "")
    return signal, execute


def _preflight_check_step(
    *,
    signal_date: str,
    execute_date: str,
    min_account_balance: float,
    enforce_session_gate: bool,
    require_min_account_balance: bool = True,
) -> RiskPauseDecision:
    if enforce_session_gate and (not _is_trading_session_now()):
        return pause_decision(
            code="paused_waiting_trading_session",
            message="当前非交易时段，暂停任务执行。",
            recovery_condition="进入交易时段(09:25-15:05)后点击继续。",
            exit_condition="若今日已过可交易窗口且未完成执行，停止当日任务并等待下一交易日重新评估。",
            evidence={"signal_date": signal_date, "execute_date": execute_date},
        )
    bal = float(get_account_balance() or 0)
    if require_min_account_balance and bal <= float(min_account_balance):
        return pause_decision(
            code="paused_funds_not_ready",
            message=f"可用资金不足（当前 {bal:.2f}），暂停任务执行。",
            recovery_condition=f"补充或释放资金，确保可用资金 > {float(min_account_balance):.2f} 后点击继续。",
            exit_condition="若不计划补充资金，停止该任务，避免后续信号持续空跑。",
            evidence={"balance": bal, "min_account_balance": float(min_account_balance)},
        )
    ds = BacktestDataSource()
    sig_rows = ds.load_snapshot(signal_date.replace("-", ""))
    sig_rows = [x for x in sig_rows if str(getattr(x, "date", "")) <= str(signal_date)]
    if not sig_rows:
        return pause_decision(
            code="paused_signal_snapshot_missing",
            message=f"信号日快照缺失（{signal_date}），暂停任务执行。",
            recovery_condition="补齐该日快照或重新执行 POST /api/init 后点击继续。",
            exit_condition="若该交易日数据无法补齐，停止当前补跑批次，避免用不完整信号交易。",
            severity="error",
            evidence={"signal_date": signal_date, "execute_date": execute_date},
        )
    exe_rows = ds.load_snapshot(execute_date.replace("-", ""))
    exe_rows = [x for x in exe_rows if str(getattr(x, "date", "")) <= str(execute_date)]
    if not exe_rows:
        return pause_decision(
            code="paused_execute_snapshot_missing",
            message=f"执行日快照缺失（{execute_date}），暂停任务执行。",
            recovery_condition="补齐该日快照或重新执行 POST /api/init 后点击继续。",
            exit_condition="若执行日数据无法补齐，停止当前补跑批次，避免按错误价格成交。",
            severity="error",
            evidence={"signal_date": signal_date, "execute_date": execute_date},
        )
    return no_pause()


def _step_preflight_decision(
    *,
    frozen_autonomous_execution: bool,
    signal_date: str,
    execute_date: str,
    min_account_balance: float,
    enforce_session_gate: bool,
) -> RiskPauseDecision:
    """Skip mutable research preflight only for an already-frozen formal plan."""
    if frozen_autonomous_execution:
        return no_pause()
    return _preflight_check_step(
        signal_date=signal_date,
        execute_date=execute_date,
        min_account_balance=min_account_balance,
        enforce_session_gate=enforce_session_gate,
        require_min_account_balance=True,
    )


def _plan_replay_window(dates: list[str], last_exec_date: str, mode: str, start_idx_override: int | None = None) -> dict:
    end_idx = len(dates) - 2
    if end_idx < 0:
        return {"ok": False, "reason": "no_executable_dates"}

    start_idx = 0
    if start_idx_override is not None:
        start_idx = max(0, min(int(start_idx_override), end_idx))
    elif last_exec_date and last_exec_date in dates:
        start_idx = dates.index(last_exec_date)
    elif mode == "pragmatic_recover":
        start_idx = max(0, end_idx - (_PRAGMATIC_MAX_GAP_DAYS - 1))

    if start_idx > end_idx:
        return {"ok": False, "reason": "no_gap"}

    # pragmatic 模式只补最近 N 天缺口；strict 模式补全缺口。
    if mode == "pragmatic_recover" and start_idx_override is None:
        start_idx = max(start_idx, end_idx - (_PRAGMATIC_MAX_GAP_DAYS - 1))

    target_end_idx = end_idx
    batch_end_idx = min(target_end_idx, start_idx + _REPLAY_BATCH_STEPS - 1)
    gap_days = target_end_idx - start_idx + 1
    batch_steps = batch_end_idx - start_idx + 1
    return {
        "ok": True,
        "start_idx": start_idx,
        "batch_end_idx": batch_end_idx,
        "target_end_idx": target_end_idx,
        "gap_days": gap_days,
        "batch_steps": batch_steps,
        "current_execute_date": dates[start_idx + 1],
        "target_execute_date": dates[target_end_idx + 1],
    }


def _transition_task_status(task_id: int, target_status: str, event: str, **fields) -> bool:
    task = get_quant_run_task(task_id)
    if not task:
        return False
    current = normalize_status(task.get("status"))
    target = normalize_status(target_status)
    if not can_transition(current, target, event=event):
        logger.warning(
            "illegal status transition blocked task_id=%s current=%s target=%s event=%s",
            task_id,
            current,
            target,
            event,
        )
        return False
    payload = dict(fields)
    payload["status"] = target
    return update_quant_run_task(task_id, **payload)


def _handle_pause(task_id: int, *, replay_status: str, message: str, error: str | None = None, decision: RiskPauseDecision | None = None) -> None:
    error_payload = error or message
    if decision is not None:
        import json
        error_payload = json.dumps(decision.to_dict(), ensure_ascii=False)
    _transition_task_status(
        task_id,
        "paused",
        "risk_pause",
        replay_status=replay_status,
        error=(error_payload or "")[:1000] if error_payload else None,
        finished_at=None,
        last_message=(message or "")[:160],
    )


def _execute_step_with_retry(
    *,
    task_id: int,
    task_live: dict,
    signal_date: str,
    execute_date: str,
    progress_callback,
    scoring_timeout_sec: int,
    claim_observer: Optional[Callable[[str, str], None]] = None,
):
    authoritative_plan = None
    if str(task_live.get("replay_mode") or "") == "autonomous_daily":
        from app.services.profit_loop import (
            block_daily_rebalance_plan_contract,
            claim_daily_rebalance_execution_input,
        )
        claim_token = f"quant-task:{int(task_id)}:{signal_date}:{execute_date}"
        try:
            authoritative_plan = claim_daily_rebalance_execution_input(
                signal_date,
                execute_date,
                claim_token=claim_token,
                expected_strategy_id=int(task_live.get("strategy_id") or 0),
            )
            if authoritative_plan and claim_observer is not None:
                claim_observer(signal_date, claim_token)
        except ReplayRiskGateError as exc:
            error_code = str(getattr(exc, "code", "") or "invalid_authoritative_plan")
            if error_code not in {"formal_plan_not_due", *_RETRYABLE_EXECUTION_CODES}:
                block_daily_rebalance_plan_contract(
                    signal_date,
                    execute_date,
                    code=error_code,
                    message=str(exc),
                )
            raise
    if str(task_live.get("replay_mode") or "") == "autonomous_daily" and authoritative_plan is None:
        raise ReplayRiskGateError(
            "正式调仓计划不存在、已执行或已被其他执行器领取",
            code="formal_plan_not_claimable",
        )
    def _run_execution():
        from app.services.profit_loop import (
            _persist_frozen_order_audit,
            audit_frozen_execution_orders,
            frozen_order_data_gate_error,
            mark_daily_rebalance_plan_executed,
        )

        autonomous = str(task_live.get("replay_mode") or "") == "autonomous_daily"
        if autonomous:
            contract = (authoritative_plan or {}).get("decision_contract") or {}
            order_audit = audit_frozen_execution_orders(
                contract,
                execute_date,
                observed_at=str((authoritative_plan or {}).get("execution_observation_cutoff") or ""),
            )
            if not order_audit.get("ok"):
                raise frozen_order_data_gate_error(order_audit)
            if authoritative_plan is not None:
                authoritative_plan["execution_observation_cutoff"] = str(order_audit.get("checked_at") or "")
                authoritative_plan["execution_data_evidence"] = order_audit
            plan_id = int((authoritative_plan or {}).get("plan_id") or 0)
            if plan_id:
                # Evidence persistence is intentionally a separate short
                # transaction. Market/PIT preparation must never keep the
                # simulation-account transaction open.
                _persist_frozen_order_audit(
                    plan_id=plan_id,
                    audit=order_audit,
                )

        def _finalize(execution_id: int | None) -> None:
            if autonomous:
                mark_daily_rebalance_plan_executed(
                    signal_date,
                    execute_date,
                    execution_id,
                    claim_token=str((authoritative_plan or {}).get("execution_claim_token") or ""),
                )

        return run_for_trade_date(
            strategy_id=int(task_live.get("strategy_id") or 0),
            signal_date=signal_date,
            execute_date=execute_date,
            top_n=int(task_live.get("top_n") or 10),
            budget_ratio=float(task_live.get("budget_ratio") or 1.0),
            exchange_group=str(task_live.get("exchange_group") or "HS"),
            progress_callback=progress_callback,
            strict_no_future=True,
            scoring_timeout_sec=scoring_timeout_sec,
            authoritative_plan=authoritative_plan,
            finalize_execution=_finalize,
        )

    try:
        return _run_execution()
    except Exception as e_step:  # noqa: BLE001
        if not _is_tushare_auth_error(e_step):
            raise
        logger.warning(
            "quant task step auth error, reset client then retry once task_id=%s signal=%s | %s",
            task_id,
            signal_date,
            str(e_step)[:160],
        )
        reset_tushare_client()
        return _run_execution()


def _build_replay_plan_context(
    *,
    task: dict,
    dates: list[str],
    done_signals: set[str],
    records: list[dict],
    waterline: dict | None,
    mode: str,
) -> tuple[dict, str]:
    last_exec_date = _derive_last_execute_date(task, records, waterline)
    start_idx_override = None
    has_task_history = bool(done_signals) or bool(task.get("last_execute_date"))
    if str(task.get("start_date") or "").strip() and (not has_task_history):
        start_idx_override = _compute_start_index(dates, task, done_signals)
        last_exec_date = ""
    plan = _plan_replay_window(
        dates,
        last_exec_date=last_exec_date,
        mode=mode,
        start_idx_override=start_idx_override,
    )
    return plan, last_exec_date


def _build_autonomous_plan_context(dates: list[str]) -> dict:
    """Select only the immutable plan due on the latest available session.

    Autonomous execution is a live T+1 path, not a historical catch-up runner.
    A plan missed on its frozen execution date must stay visible for audit and
    replay, but must never be executed late against another day's prices.
    """
    from app.services.profit_loop import list_pending_daily_rebalance_execution_inputs

    normalized_dates = {str(value).replace("-", ""): str(value) for value in dates}
    latest_execute_key = str(dates[-1]).replace("-", "")[:8] if dates else ""
    pending = sorted(
        list_pending_daily_rebalance_execution_inputs(),
        key=lambda item: (
            str(item.get("signal_date") or "").replace("-", "")[:8],
            str(item.get("execute_date") or "").replace("-", "")[:8],
        ),
    )
    for item in pending:
        signal_key = str(item.get("signal_date") or "").replace("-", "")[:8]
        execute_key = str(item.get("execute_date") or "").replace("-", "")[:8]
        if execute_key != latest_execute_key:
            continue
        signal_date = normalized_dates.get(signal_key)
        execute_date = normalized_dates.get(execute_key)
        # The frozen signal and exact T+1 execution-date slices must both exist.
        if signal_date and execute_date:
            return {
                "ok": True,
                "start_idx": dates.index(signal_date),
                "batch_end_idx": dates.index(signal_date),
                "target_end_idx": dates.index(signal_date),
                "gap_days": 1,
                "batch_steps": 1,
                "signal_date": signal_date,
                "current_execute_date": execute_date,
                "target_execute_date": execute_date,
                "required_defensive_ts_codes": list(item.get("required_defensive_ts_codes") or []),
                "execution_contract": item.get("execution_contract") or {},
            }
    return {"ok": False, "reason": "no_due_formal_plan"}


def _materialize_current_plan_and_find_due_context(dates: list[str]) -> dict:
    """Execute an already due frozen plan before doing today's heavy research."""
    due_context = _build_autonomous_plan_context(dates)
    if due_context.get("ok"):
        return {
            **due_context,
            "today_materialization": {
                "ok": True,
                "status": "deferred_until_due_plan_processed",
                "reason": "due_frozen_plan_has_execution_priority",
            },
        }
    latest = str(dates[-1]) if dates else ""
    materialization: dict = {}
    if latest:
        retry_key = _trade_date_key(latest)
        now = perf_counter()
        with _plan_materialization_retry_lock:
            retry_after = float(_plan_materialization_retry_after.get(retry_key) or 0.0)
        if retry_after > now:
            return {
                **_build_autonomous_plan_context(dates),
                "today_materialization": {
                    "ok": False,
                    "status": "database_timeout_cooldown",
                    "retry_after_seconds": max(1, int(retry_after - now)),
                    "error": "previous_plan_materialization_query_timed_out",
                },
            }
        try:
            from app.services.profit_loop import materialize_daily_rebalance_plan

            # Generate from the latest completed market session, not from the
            # wall-clock target after midnight.  This preserves the prior
            # session's T+1 plan opportunity when close data arrived late.
            result = materialize_daily_rebalance_plan(signal_date=latest)
            materialization = result if isinstance(result, dict) else {"result": result}
        except Exception as exc:  # noqa: BLE001 - due execution remains independent
            logger.warning("每日正式调仓方案生成失败 trade_date=%s: %s", latest, str(exc)[:200])
            materialization = {"ok": False, "status": "error", "error": str(exc)[:300]}
            if _is_expected_scheduler_wait(exc):
                with _plan_materialization_retry_lock:
                    _plan_materialization_retry_after[retry_key] = (
                        perf_counter() + _PLAN_MATERIALIZATION_TIMEOUT_COOLDOWN_SEC
                    )
                materialization.update({
                    "status": "database_timeout_cooldown",
                    "retry_after_seconds": _PLAN_MATERIALIZATION_TIMEOUT_COOLDOWN_SEC,
                })
        else:
            with _plan_materialization_retry_lock:
                _plan_materialization_retry_after.pop(retry_key, None)
    return {**_build_autonomous_plan_context(dates), "today_materialization": materialization}


def _resolve_autonomous_plan_context(
    dates: list[str], *, decision_data_ready: bool,
) -> dict:
    """Give a due frozen plan priority over today's decision-data contract.

    A T+1 plan is allowed to consume only its exact execution slice.  Its
    safety must therefore not depend on whether today's new plan can be
    generated, which also requires valuation and other close-data fields.
    """
    due_context = _build_autonomous_plan_context(dates)
    if due_context.get("ok"):
        return {
            **due_context,
            "today_materialization": {
                "ok": True,
                "status": "deferred_until_due_plan_processed",
                "reason": "due_frozen_plan_has_execution_priority",
            },
        }
    # A current wall-clock target can be one session ahead of the latest
    # completed close.  Still attempt materialization with the explicit latest
    # signal date; the portfolio gate will fail closed if that slice is not
    # complete, while a completed prior close can immediately produce T+1.
    return _materialize_current_plan_and_find_due_context(dates)


def _persist_step_result(
    *,
    task_id: int,
    result,
    processed_total: int,
    step_total: int,
    strategy_id: int,
    exchange_group: str,
    mode: str,
) -> int:
    detail = dict(result.detail or {})
    current_cash_only = not bool(detail.get("current_positions"))
    consecutive_cash_days = 1 if current_cash_only else 0
    if current_cash_only:
        for record in list_quant_run_task_records(task_id, limit=20):
            previous_detail = record.get("detail") or {}
            if "current_positions" not in previous_detail:
                break
            if previous_detail.get("current_positions"):
                break
            consecutive_cash_days += 1
    policy = {
        "consecutive_cash_days": consecutive_cash_days,
        "status": "blocked" if current_cash_only else "invested",
        "reason": "研究、估值、风险或交易约束未形成可执行且通过门控的权益订单" if current_cash_only else "组合持有可交易资产",
        "reentry_conditions": ["数据质量门恢复", "至少一项研究逻辑与安全边际通过", "市场状态允许新增风险暴露"],
        "defensive_asset_evaluation": {
            "assets": ["货币ETF", "债券ETF", "黄金ETF", "低波动ETF", "红利ETF"],
            "status": "required" if current_cash_only else "not_due",
            "decision": "当日立即进入防御资产配置，不允许等待或长期纯现金" if current_cash_only else "账户已持有可交易资产",
        },
    }
    detail["cash_only_policy"] = policy
    if isinstance(detail.get("daily_report"), dict):
        detail["daily_report"] = {**detail["daily_report"], "cash_only_policy": policy}
    result.detail = detail
    provenance = detail.get("plan_provenance") if isinstance(detail.get("plan_provenance"), dict) else {}
    rec_id = create_quant_run_task_record(
        task_id=task_id,
        signal_date=result.signal_date,
        execute_date=result.execute_date,
        sold_count=result.sold_count,
        bought_count=result.bought_count,
        total_buy_cost=result.total_buy_cost,
        execution_id=result.execution_id,
        message=result.message,
        detail=detail,
        # The autonomous execution step has already atomically bound the
        # frozen plan before this evidence row is created.  Repeating the
        # transition here would turn a correctly executed plan into a false
        # lease error.
        plan_claim_token="",
    )
    if rec_id == 0:
        logger.info("quant task replay idempotent skip task_id=%s signal_date=%s already_replayed", task_id, result.signal_date)
    upsert_quant_replay_waterline(
        strategy_id=strategy_id,
        exchange_group=exchange_group,
        last_successful_signal_date=result.signal_date,
        last_successful_execute_date=result.execute_date,
        last_run_mode=mode,
        last_run_status="running",
    )
    update_quant_run_task(
        task_id,
        processed_steps=processed_total,
        last_signal_date=result.signal_date,
        last_execute_date=result.execute_date,
        last_message=f"已完成 {processed_total}/{step_total}：{result.message}",
    )
    return rec_id


def run_quant_task_async(task_id: int) -> bool:
    task_id = int(task_id)
    # Reserve synchronously, before the thread is created.  Checking and only
    # marking from inside ``_worker`` leaves a small but real scheduler race in
    # which two ticks can start duplicate workers for the same task.
    with _task_lock:
        if task_id in _running_task_ids:
            return False
        _running_task_ids.add(task_id)
    try:
        task = get_quant_run_task(task_id)
        if not task:
            _mark_running(task_id, False)
            return False
        if str(task.get("status") or "").lower() in {"failed", "stopped", "stopping", "running"}:
            _mark_running(task_id, False)
            return False
        if not claim_quant_run_task_start(
            task_id,
            replay_mode=_normalized_replay_mode(task.get("replay_mode")),
            replay_status="planning",
            started_at=task.get("started_at") or _utc_now_iso(),
            error=None,
            last_message="正在生成补跑计划",
            stop_requested=0,
        ):
            # Another process owns the row, or a stop request won the race.
            _mark_running(task_id, False)
            return False
    except Exception:
        _mark_running(task_id, False)
        raise

    def _worker() -> None:
        active_claim: tuple[str, str] | None = None

        def _quarantine_active_claim(reason: str) -> None:
            nonlocal active_claim
            if not active_claim:
                return
            signal_date, claim_token = active_claim
            try:
                from app.services.profit_loop import quarantine_daily_rebalance_execution_claim

                quarantined = quarantine_daily_rebalance_execution_claim(
                    signal_date,
                    claim_token=claim_token,
                    reason=reason,
                )
                if quarantined:
                    logger.error(
                        "quarantined failed autonomous plan task_id=%s signal=%s",
                        task_id,
                        signal_date,
                    )
            except Exception:
                logger.exception("failed to quarantine autonomous plan task_id=%s signal=%s", task_id, signal_date)
            finally:
                active_claim = None

        try:
            task0 = get_quant_run_task(task_id)
            if not task0:
                return
            now = _utc_now_iso()
            runtime_cfg = get_quant_run_task_runtime_config(task_id)
            enforce_session_gate = _as_bool(runtime_cfg.get("enforce_trading_session_gate"), _ENFORCE_TRADING_SESSION_GATE)
            enable_data_health_gate = _as_bool(runtime_cfg.get("enable_data_health_gate"), _DEFAULT_ENABLE_DATA_HEALTH_GATE)
            scoring_timeout_sec = max(10, min(_as_int(runtime_cfg.get("scoring_timeout_sec"), _DEFAULT_SCORING_TIMEOUT_SEC), 3600))
            min_account_balance = max(0.0, _as_float(runtime_cfg.get("min_account_balance"), _DEFAULT_MIN_ACCOUNT_BALANCE))
            max_consecutive_losses = max(0, min(_as_int(runtime_cfg.get("max_consecutive_losses"), _DEFAULT_MAX_CONSECUTIVE_LOSSES), 30))
            mode = _normalized_replay_mode(task0.get("replay_mode"))
            replay_opinion_gates = _requires_replay_opinion_gates(mode)
            frozen_autonomous_execution = not replay_opinion_gates
            health0 = {
                "heartbeat_at": now,
                "last_step_latency_ms": 0,
                "avg_step_latency_ms": 0,
                "step_count": 0,
                "risk_hit_count": int(task0.get("risk_hit_count") or 0),
                "consecutive_pause_count": int(task0.get("consecutive_pause_count") or 0),
                "last_risk_code": str(task0.get("last_risk_code") or ""),
            }
            upsert_quant_run_task_health(task_id, **health0)
            if enable_data_health_gate and replay_opinion_gates:
                data_decision = evaluate_data_health_gate(limit_days=5)
                if data_decision.should_pause:
                    _handle_pause(
                        task_id,
                        replay_status=data_decision.code,
                        message=data_decision.message,
                        decision=data_decision,
                    )
                    h = get_quant_run_task_health(task_id)
                    upsert_quant_run_task_health(
                        task_id,
                        heartbeat_at=_utc_now_iso(),
                        last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                        avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                        step_count=int(h.get("step_count") or 0),
                        risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                        consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                        last_risk_code=data_decision.code,
                    )
                    return
                latest_day_decision = evaluate_latest_market_day_gate()
                if latest_day_decision.should_pause:
                    try:
                        _ensure_close_data_ready()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("latest market day gate tried to start daily update but failed: %s", str(exc)[:160])
                    _handle_pause(
                        task_id,
                        replay_status=latest_day_decision.code,
                        message=latest_day_decision.message,
                        decision=latest_day_decision,
                    )
                    h = get_quant_run_task_health(task_id)
                    upsert_quant_run_task_health(
                        task_id,
                        heartbeat_at=_utc_now_iso(),
                        last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                        avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                        step_count=int(h.get("step_count") or 0),
                        risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                        consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                        last_risk_code=latest_day_decision.code,
                    )
                    return

            model_decision = evaluate_model_readiness_gate()
            if model_decision.should_pause:
                _handle_pause(
                    task_id,
                    replay_status=model_decision.code,
                    message=model_decision.message,
                    decision=model_decision,
                )
                h = get_quant_run_task_health(task_id)
                upsert_quant_run_task_health(
                    task_id,
                    heartbeat_at=_utc_now_iso(),
                    last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                    avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                    step_count=int(h.get("step_count") or 0),
                    risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                    consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                    last_risk_code=model_decision.code,
                )
                return

            dates = require_consolidated_trade_dates(min_count=2)

            records = list_quant_run_task_records(task_id, limit=5000)
            done_signals = {str(x.get("signal_date") or "") for x in records if x.get("signal_date")}
            processed_total = 0

            loss_decision = (
                no_pause()
                if frozen_autonomous_execution
                else evaluate_consecutive_loss_gate(
                    task_id=task_id,
                    max_consecutive_losses=max_consecutive_losses,
                )
            )
            drawdown_decision = (
                no_pause()
                if frozen_autonomous_execution
                else evaluate_account_drawdown_gate(max_drawdown=0.10)
            )
            if drawdown_decision.should_pause:
                _handle_pause(
                    task_id,
                    replay_status=drawdown_decision.code,
                    message=drawdown_decision.message,
                    decision=drawdown_decision,
                )
                return
            if loss_decision.should_pause:
                _handle_pause(
                    task_id,
                    replay_status=loss_decision.code,
                    message=loss_decision.message,
                    decision=loss_decision,
                )
                h = get_quant_run_task_health(task_id)
                upsert_quant_run_task_health(
                    task_id,
                    heartbeat_at=_utc_now_iso(),
                    last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                    avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                    step_count=int(h.get("step_count") or 0),
                    risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                    consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                    last_risk_code=loss_decision.code,
                )
                return

            strategy_id = int(task0.get("strategy_id") or 0)
            exchange_group = str(task0.get("exchange_group") or "HS")
            waterline = get_quant_replay_waterline(strategy_id=strategy_id, exchange_group=exchange_group)
            if mode == "autonomous_daily":
                plan = _build_autonomous_plan_context(dates)
                last_exec_date = _derive_last_execute_date(task0, records, waterline)
            else:
                plan, last_exec_date = _build_replay_plan_context(
                    task=task0,
                    dates=dates,
                    done_signals=done_signals,
                    records=records,
                    waterline=waterline,
                    mode=mode,
                )
            if not plan.get("ok"):
                waiting_for_schedule = mode == "autonomous_daily"
                waiting_projection = _waiting_schedule_projection() if waiting_for_schedule else {}
                _transition_task_status(
                    task_id,
                    "pending" if waiting_for_schedule else "paused",
                    "schedule_wait" if waiting_for_schedule else "batch_completed",
                    replay_mode=mode,
                    replay_status=("waiting_schedule" if waiting_for_schedule else "no_gap"),
                    replay_gap_days=int(waiting_projection.get("replay_gap_days") or 0),
                    replay_current_execute_date=waiting_projection.get("replay_current_execute_date"),
                    replay_target_execute_date=waiting_projection.get("replay_target_execute_date"),
                    processed_steps=0,
                    total_steps=0,
                    error=None,
                    finished_at=None,
                    last_message=str(waiting_projection.get("last_message") or "任务已暂停：当前无可补跑缺口"),
                )
                upsert_quant_replay_waterline(
                    strategy_id=strategy_id,
                    exchange_group=exchange_group,
                    last_successful_signal_date=(waterline or {}).get("last_successful_signal_date"),
                    last_successful_execute_date=last_exec_date or (waterline or {}).get("last_successful_execute_date"),
                    last_run_mode=mode,
                    last_run_status="no_gap",
                )
                return

            # A frozen T+1 plan may only execute against its exact-date market
            # slice. This is a production execution contract, not an optional
            # runtime data-health opinion, so autonomous execution cannot turn
            # it off through task metadata or an environment override.
            if mode == "autonomous_daily":
                execution_data_decision = _evaluate_frozen_execution_data_gate(
                    str(plan.get("current_execute_date") or ""),
                    plan.get("required_defensive_ts_codes") or [],
                )
                if execution_data_decision.should_pause:
                    try:
                        _refresh_missing_frozen_stock_orders(
                            str(plan.get("current_execute_date") or ""),
                            plan.get("execution_contract") or {},
                        )
                    except Exception as exc:
                        logger.warning(
                            "冻结计划股票精确补数失败 execute_date=%s: %s",
                            plan.get("current_execute_date"), str(exc)[:200],
                        )
                    try:
                        _ensure_close_data_ready()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("frozen execution gate tried to start daily update but failed: %s", str(exc)[:160])
                    try:
                        _refresh_missing_frozen_defensive_assets(
                            str(plan.get("current_execute_date") or ""),
                            plan.get("required_defensive_ts_codes") or [],
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("frozen execution gate ETF refresh failed: %s", str(exc)[:160])
                    _handle_pause(
                        task_id,
                        replay_status=execution_data_decision.code,
                        message=execution_data_decision.message,
                        decision=execution_data_decision,
                    )
                    h = get_quant_run_task_health(task_id)
                    upsert_quant_run_task_health(
                        task_id,
                        heartbeat_at=_utc_now_iso(),
                        last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                        avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                        step_count=int(h.get("step_count") or 0),
                        risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                        consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                        last_risk_code=execution_data_decision.code,
                    )
                    return

            start_idx = int(plan["start_idx"])
            batch_end_idx = int(plan["batch_end_idx"])
            target_end_idx = int(plan["target_end_idx"])
            total_planned = int(plan["batch_steps"])
            total_gap_days = int(plan["gap_days"])
            update_quant_run_task(
                task_id,
                replay_mode=mode,
                replay_status="planned",
                replay_gap_days=total_gap_days,
                replay_current_execute_date=str(plan["current_execute_date"]),
                replay_target_execute_date=str(plan["target_execute_date"]),
                total_steps=total_planned,
                processed_steps=0,
                last_message=(
                    f"缺口天数: {total_gap_days}，当前补到: {plan['current_execute_date']} / 目标: {plan['target_execute_date']}"
                ),
            )

            # 时段门（可选）：默认不限制；仅在显式开启后，非交易时段只生成计划不执行。
            if enforce_session_gate and not frozen_autonomous_execution and (not _is_trading_session_now()):
                session_decision = pause_decision(
                    code="planned_waiting_session",
                    message="已生成补跑计划，但当前非交易时段，等待交易时段执行。",
                    recovery_condition="进入交易时段(09:25-15:05)后点击继续或由调度器重新触发。",
                    exit_condition="若今日不再执行，停止任务并在下一交易日前重新评估。",
                    evidence={"gap_days": total_gap_days, "current_execute_date": plan["current_execute_date"], "target_execute_date": plan["target_execute_date"]},
                )
                _transition_task_status(
                    task_id,
                    "pending",
                    "checkpoint_saved",
                    replay_status="planned_waiting_session",
                    finished_at=None,
                    error=__import__("json").dumps(session_decision.to_dict(), ensure_ascii=False)[:1000],
                    last_message=session_decision.message,
                )
                upsert_quant_replay_waterline(
                    strategy_id=strategy_id,
                    exchange_group=exchange_group,
                    last_successful_signal_date=(waterline or {}).get("last_successful_signal_date"),
                    last_successful_execute_date=last_exec_date or (waterline or {}).get("last_successful_execute_date"),
                    last_run_mode=mode,
                    last_run_status="planned_waiting_session",
                )
                upsert_quant_run_task_health(
                    task_id,
                    heartbeat_at=_utc_now_iso(),
                    last_step_latency_ms=health0["last_step_latency_ms"],
                    avg_step_latency_ms=health0["avg_step_latency_ms"],
                    step_count=health0["step_count"],
                    risk_hit_count=health0["risk_hit_count"],
                    consecutive_pause_count=health0["consecutive_pause_count"],
                    last_risk_code="planned_waiting_session",
                )
                return

            # 资金门：资金不足时暂停补跑，避免盲目执行。
            account_balance = float(get_account_balance() or 0)
            if not frozen_autonomous_execution and account_balance <= min_account_balance:
                funds_decision = pause_decision(
                    code="paused_funds_not_ready",
                    message=f"可用资金不足（当前 {account_balance:.2f}），暂停补跑。",
                    recovery_condition=f"补充或释放资金，确保可用资金 > {float(min_account_balance):.2f} 后点击继续。",
                    exit_condition="若不计划补充资金，停止该任务，避免后续信号持续空跑。",
                    evidence={"balance": account_balance, "min_account_balance": float(min_account_balance)},
                )
                _handle_pause(
                    task_id,
                    replay_status=funds_decision.code,
                    message=funds_decision.message,
                    decision=funds_decision,
                )
                upsert_quant_replay_waterline(
                    strategy_id=strategy_id,
                    exchange_group=exchange_group,
                    last_successful_signal_date=(waterline or {}).get("last_successful_signal_date"),
                    last_successful_execute_date=last_exec_date or (waterline or {}).get("last_successful_execute_date"),
                    last_run_mode=mode,
                    last_run_status="paused_funds_not_ready",
                )
                health0["risk_hit_count"] = int(health0["risk_hit_count"]) + 1
                health0["consecutive_pause_count"] = int(health0["consecutive_pause_count"]) + 1
                health0["last_risk_code"] = "paused_funds_not_ready"
                upsert_quant_run_task_health(
                    task_id,
                    heartbeat_at=_utc_now_iso(),
                    last_step_latency_ms=health0["last_step_latency_ms"],
                    avg_step_latency_ms=health0["avg_step_latency_ms"],
                    step_count=health0["step_count"],
                    risk_hit_count=health0["risk_hit_count"],
                    consecutive_pause_count=health0["consecutive_pause_count"],
                    last_risk_code=health0["last_risk_code"],
                )
                return

            for i in range(start_idx, batch_end_idx + 1):
                task_live = get_quant_run_task(task_id)
                if not task_live:
                    return
                if bool(task_live.get("stop_requested")):
                    _transition_task_status(
                        task_id,
                        "stopped",
                        "manual_stop_finalize",
                        finished_at=_utc_now_iso(),
                        last_message="任务已停止",
                    )
                    return
                signal_date = dates[i]
                execute_date = dates[i + 1]
                if signal_date in done_signals:
                    processed_total += 1
                    continue
                # A frozen V2 autonomous plan has already fixed its signal-day
                # selection and the execution gate above has just audited the
                # exact T+1 order slice.  Re-reading the mutable full-market
                # signal snapshot here adds no execution evidence and can
                # strand a valid claimed plan when historical replay storage
                # is compacted.  Research replay keeps the full preflight.
                pre_decision = _step_preflight_decision(
                    frozen_autonomous_execution=frozen_autonomous_execution,
                    signal_date=signal_date,
                    execute_date=execute_date,
                    min_account_balance=min_account_balance,
                    enforce_session_gate=enforce_session_gate,
                )
                if pre_decision.should_pause:
                    _handle_pause(
                        task_id,
                        replay_status=pre_decision.code,
                        message=pre_decision.message[:160],
                        decision=pre_decision,
                    )
                    h = get_quant_run_task_health(task_id)
                    upsert_quant_run_task_health(
                        task_id,
                        heartbeat_at=_utc_now_iso(),
                        last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                        avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                        step_count=int(h.get("step_count") or 0),
                        risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                        consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                        last_risk_code=pre_decision.code,
                    )
                    return
                step_idx = processed_total + 1
                step_total = max(1, int(total_planned or 1))
                _transition_task_status(
                    task_id,
                    "running",
                    "step_running",
                    processed_steps=processed_total,
                    total_steps=step_total,
                    last_message=f"正在计算：信号日 {signal_date} -> 执行日 {execute_date}（{step_idx}/{step_total}）",
                )

                def _step_phase(phase_text: str) -> None:
                    _transition_task_status(
                        task_id,
                        "running",
                        "step_running",
                        processed_steps=processed_total,
                        total_steps=step_total,
                        last_message=f"{phase_text}（{step_idx}/{step_total}）",
                    )
                step_t0 = perf_counter()
                # The simulator has one shared cash account and one position
                # book.  Different task ids must not execute or checkpoint a
                # trade concurrently inside the same service process.
                def _record_claim(claim_signal: str, claim_token: str) -> None:
                    nonlocal active_claim
                    active_claim = (claim_signal, claim_token)

                with _account_execution_lock:
                    result = _execute_step_with_retry(
                        task_id=task_id,
                        task_live=task_live,
                        signal_date=signal_date,
                        execute_date=execute_date,
                        progress_callback=_step_phase,
                        scoring_timeout_sec=scoring_timeout_sec,
                        claim_observer=_record_claim,
                    )
                    step_ms = max(1, int((perf_counter() - step_t0) * 1000))
                    prev_cnt = int(health0["step_count"])
                    prev_avg = int(health0["avg_step_latency_ms"])
                    new_cnt = prev_cnt + 1
                    new_avg = int((prev_avg * prev_cnt + step_ms) / new_cnt) if new_cnt > 0 else step_ms
                    health0["step_count"] = new_cnt
                    health0["last_step_latency_ms"] = step_ms
                    health0["avg_step_latency_ms"] = new_avg
                    health0["heartbeat_at"] = _utc_now_iso()
                    health0["last_risk_code"] = ""
                    upsert_quant_run_task_health(
                        task_id,
                        heartbeat_at=health0["heartbeat_at"],
                        last_step_latency_ms=health0["last_step_latency_ms"],
                        avg_step_latency_ms=health0["avg_step_latency_ms"],
                        step_count=health0["step_count"],
                        risk_hit_count=health0["risk_hit_count"],
                        consecutive_pause_count=0,
                        last_risk_code="",
                    )
                    _persist_step_result(
                        task_id=task_id,
                        result=result,
                        processed_total=processed_total + 1,
                        step_total=step_total,
                        strategy_id=strategy_id,
                        exchange_group=exchange_group,
                        mode=mode,
                    )
                    active_claim = None
                done_signals.add(result.signal_date)
                processed_total += 1
                # 若在本步执行期间收到停止请求，本步落账后立即停止，不再继续后续步骤。
                task_after_step = get_quant_run_task(task_id)
                if task_after_step and bool(task_after_step.get("stop_requested")):
                    _transition_task_status(
                        task_id,
                        "stopped",
                        "manual_stop_finalize",
                        finished_at=_utc_now_iso(),
                        last_message="任务已停止",
                    )
                    return

            remaining_days = max(0, target_end_idx - batch_end_idx)
            _refresh_performance_reports(task_id)
            _refresh_research_cycle(task_id)
            task_end = get_quant_run_task(task_id)
            if task_end and bool(task_end.get("stop_requested")):
                _transition_task_status(
                    task_id,
                    "stopped",
                    "manual_stop_finalize",
                    processed_steps=processed_total,
                    finished_at=_utc_now_iso(),
                    last_message="任务已停止",
                )
                return
            if remaining_days > 0:
                next_exec = dates[batch_end_idx + 2] if (batch_end_idx + 2) < len(dates) else dates[-1]
                _transition_task_status(
                    task_id,
                    "pending",
                    "checkpoint_saved",
                    replay_status="checkpoint_saved",
                    replay_gap_days=remaining_days,
                    replay_current_execute_date=next_exec,
                    replay_target_execute_date=dates[target_end_idx + 1],
                    processed_steps=processed_total,
                    finished_at=None,
                    last_message=f"本批补跑完成 {processed_total}/{total_planned}，剩余缺口 {remaining_days} 天",
                )
                upsert_quant_replay_waterline(
                    strategy_id=strategy_id,
                    exchange_group=exchange_group,
                    last_successful_signal_date=(task_end or {}).get("last_signal_date") or (waterline or {}).get("last_successful_signal_date"),
                    last_successful_execute_date=(task_end or {}).get("last_execute_date") or last_exec_date,
                    last_run_mode=mode,
                    last_run_status="checkpoint_saved",
                )
            else:
                waiting_for_schedule = mode == "autonomous_daily"
                _transition_task_status(
                    task_id,
                    "pending" if waiting_for_schedule else "paused",
                    "schedule_wait" if waiting_for_schedule else "batch_completed",
                    replay_status="waiting_schedule" if waiting_for_schedule else "completed",
                    replay_gap_days=0,
                    replay_current_execute_date=None,
                    replay_target_execute_date=None,
                    processed_steps=processed_total,
                    error=None,
                    finished_at=None,
                    last_message=(
                        f"本次正式计划已完成 {processed_total} 步；等待下一交易日收盘调度"
                        if waiting_for_schedule
                        else f"本批执行 {processed_total} 步；等待下一交易日自动继续"
                    ),
                )
                upsert_quant_replay_waterline(
                    strategy_id=strategy_id,
                    exchange_group=exchange_group,
                    last_successful_signal_date=(task_end or {}).get("last_signal_date") or (waterline or {}).get("last_successful_signal_date"),
                    last_successful_execute_date=(task_end or {}).get("last_execute_date") or last_exec_date,
                    last_run_mode=mode,
                    last_run_status="completed",
                )
        except HistoricalDataMissingError as e:
            # A missing exact-date slice is recoverable.  Release any claim so
            # the next publication probe can retry; only an unknown/ledger
            # failure is allowed to quarantine an execution.
            if active_claim:
                signal_claim, token_claim = active_claim
                try:
                    from app.services.profit_loop import release_daily_rebalance_execution_claim

                    release_daily_rebalance_execution_claim(
                        signal_claim,
                        claim_token=token_claim,
                        reason=e,
                    )
                finally:
                    active_claim = None
            trigger_backfill_merge_meta_async(reason=f"quant_task_{task_id}_508")
            logger.warning("quant task paused by 508 historical data missing task_id=%s | %s", task_id, str(e)[:160])
            decision = pause_decision(
                code="historical_data_missing_508",
                message="任务已暂停：历史数据不足(508)，系统已自动触发 backfill。",
                recovery_condition="等待 backfill 完成后检查 /api/check/data-health，再点击继续。",
                exit_condition="若 backfill 后仍缺历史交易日或快照，停止任务并人工补齐数据源。",
                severity="error",
                evidence={"error": str(e)[:500], "backfill_triggered": True},
            )
            _handle_pause(
                task_id,
                replay_status=decision.code,
                message=decision.message,
                decision=decision,
            )
            h = get_quant_run_task_health(task_id)
            upsert_quant_run_task_health(
                task_id,
                heartbeat_at=_utc_now_iso(),
                last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                step_count=int(h.get("step_count") or 0),
                risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                last_risk_code="historical_data_missing_508",
            )
        except ReplayRiskGateError as e:
            error_code = str(getattr(e, "code", "") or "replay_risk_gate")
            retryable_execution_codes = _RETRYABLE_EXECUTION_CODES
            if error_code in retryable_execution_codes and active_claim:
                try:
                    _release_retryable_execution_claim(
                        active_claim,
                        reason=e,
                    )
                finally:
                    active_claim = None
            elif error_code not in retryable_execution_codes:
                _quarantine_active_claim(str(e))
            contract_rejections = {
                "legacy_authoritative_plan",
                "invalid_authoritative_plan",
                "invalid_authoritative_plan_hash",
                "invalid_planned_execute_at",
                "incompatible_frozen_system_release",
            }
            if mode == "autonomous_daily" and error_code in {
                "formal_plan_not_due",
                "formal_plan_not_claimable",
            }:
                _transition_task_status(
                    task_id,
                    "pending",
                    "schedule_wait",
                    replay_status="waiting_schedule",
                    error=None,
                    finished_at=None,
                    **_waiting_schedule_projection(),
                )
                logger.info("autonomous plan is not claimable yet task_id=%s code=%s", task_id, error_code)
                return
            if mode == "autonomous_daily" and error_code in retryable_execution_codes:
                account_snapshot_changed = error_code == "execution_account_snapshot_changed"
                _transition_task_status(
                    task_id,
                    "pending",
                    "schedule_wait",
                    replay_status=(
                        "waiting_execution_reprepare"
                        if account_snapshot_changed
                        else "waiting_execution_data"
                    ),
                    error=None,
                    finished_at=None,
                    **_waiting_schedule_projection(),
                )
                logger.info(
                    "autonomous plan will release claim and prepare again task_id=%s code=%s",
                    task_id,
                    error_code,
                )
                return
            if mode == "autonomous_daily" and error_code in contract_rejections:
                committed_records = list_quant_run_task_records(task_id, limit=5000)
                committed_waterline = get_quant_replay_waterline(
                    strategy_id=int(task0.get("strategy_id") or 0),
                    exchange_group=str(task0.get("exchange_group") or "HS"),
                )
                successful_signal, successful_execute = _successful_task_progress(
                    committed_records,
                    committed_waterline,
                )
                _transition_task_status(
                    task_id,
                    "pending",
                    "schedule_wait",
                    replay_status="waiting_schedule",
                    last_signal_date=successful_signal or None,
                    last_execute_date=successful_execute or None,
                    error=None,
                    finished_at=None,
                    **_waiting_schedule_projection(),
                )
                logger.error(
                    "invalid autonomous frozen plan quarantined task_id=%s code=%s",
                    task_id,
                    error_code,
                )
                return
            logger.warning("quant task paused by risk gate task_id=%s code=%s | %s", task_id, error_code, str(e)[:160])
            decision = pause_decision(
                code=error_code,
                message=str(e)[:160],
                recovery_condition="按暂停原因补齐数据、资金或时段条件后点击继续。",
                exit_condition="若同一风控原因连续触发，停止任务并人工复盘策略/数据源。",
                severity="warning",
                evidence={"error": str(e)[:500]},
            )
            _handle_pause(
                task_id,
                replay_status=decision.code,
                message=decision.message,
                decision=decision,
            )
            h = get_quant_run_task_health(task_id)
            upsert_quant_run_task_health(
                task_id,
                heartbeat_at=_utc_now_iso(),
                last_step_latency_ms=int(h.get("last_step_latency_ms") or 0),
                avg_step_latency_ms=int(h.get("avg_step_latency_ms") or 0),
                step_count=int(h.get("step_count") or 0),
                risk_hit_count=int(h.get("risk_hit_count") or 0) + 1,
                consecutive_pause_count=int(h.get("consecutive_pause_count") or 0) + 1,
                last_risk_code=error_code,
            )
        except Exception as e:  # noqa: BLE001
            _quarantine_active_claim(str(e))
            if _is_tushare_auth_error(e):
                logger.warning("quant task paused by tushare auth error task_id=%s | %s", task_id, str(e)[:160])
                decision = pause_decision(
                    code="paused_tushare_auth_invalid",
                    message="任务已暂停：TuShare token 无效。",
                    recovery_condition="更新 TUSHARE_TOKEN，确认 GET /api/check/tushare 通过后点击继续。",
                    exit_condition="若 token 权限长期不可用，停止依赖 TuShare 的策略任务。",
                    severity="error",
                    evidence={"error": str(e)[:500]},
                )
                _handle_pause(
                    task_id,
                    replay_status=decision.code,
                    message=decision.message,
                    decision=decision,
                )
            else:
                logger.exception("quant task failed task_id=%s", task_id)
                _transition_task_status(
                    task_id,
                    "failed",
                    "infra_fail",
                    error=str(e)[:500],
                    finished_at=_utc_now_iso(),
                    last_message=f"任务失败：{str(e)[:120]}",
                )
        finally:
            _mark_running(task_id, False)

    try:
        t = threading.Thread(target=_worker, daemon=True, name=f"quant-task-{task_id}")
        t.start()
    except Exception:
        _mark_running(task_id, False)
        raise
    return True


def _is_autonomous_schedule_wait(task: dict) -> bool:
    if str(task.get("replay_mode") or "") != "autonomous_daily":
        return False
    if str(task.get("error") or "").strip():
        return False
    replay_status = str(task.get("replay_status") or "")
    if replay_status in {"waiting_schedule", "waiting_formal_plan", "no_gap", "completed"}:
        return True
    message = str(task.get("last_message") or "")
    return (
        not task.get("started_at")
        and int(task.get("processed_steps") or 0) == 0
        and int(task.get("total_steps") or 0) == 0
        and message in {
            "任务已创建，等待执行",
            "自动任务参数已与当前 Champion 同步",
            "后端重启，任务已安全暂停；下一收盘调度会自动继续，也可手动继续",
            "自治任务等待下一份到期正式调仓计划",
            "等待收盘调度与下一份到期正式调仓计划",
        }
    )


def reconcile_quant_tasks_on_startup() -> dict[str, int]:
    """Classify restart recovery without conflating idle, queued and blocked work."""
    tasks = list_active_quant_run_tasks()
    summary = {"interrupted": 0, "stopping_finalized": 0, "waiting_schedule": 0, "pending_preserved": 0}
    for t in tasks:
        status = normalize_status(t.get("status"))
        task_id = int(t.get("id") or 0)
        if status == "stopping":
            # A stop request has no worker left to acknowledge after a
            # process restart.  Finalize it explicitly so it cannot occupy
            # the autonomous singleton forever.
            if _transition_task_status(
                task_id,
                "stopped",
                "manual_stop_finalize",
                stop_requested=1,
                finished_at=_utc_now_iso(),
                replay_status="stopped_after_restart",
                last_message="进程重启后收敛遗留停止请求，任务已停止",
            ):
                summary["stopping_finalized"] += 1
            continue
        if status == "running" and can_transition(status, "paused", event="startup_pause"):
            if _transition_task_status(
                task_id,
                "paused",
                "startup_pause",
                replay_status="recovery_pending",
                finished_at=None,
                last_message="运行被进程重启中断，等待安全恢复检查",
            ):
                summary["interrupted"] += 1
            continue
        if _is_autonomous_schedule_wait(t) and status in {"pending", "paused"}:
            if _transition_task_status(
                task_id,
                "pending",
                "schedule_wait",
                replay_status="waiting_schedule",
                error=None,
                finished_at=None,
                **_waiting_schedule_projection(),
            ):
                summary["waiting_schedule"] += 1
            continue
        if status == "pending":
            # A fresh queued task has not lost an executing thread.  Preserve it.
            # Checkpointed manual replays remain pending for explicit continuation.
            summary["pending_preserved"] += 1
    return summary


def ensure_autonomous_simulation_task() -> dict:
    """Ensure exactly one scheduler-owned simulation task for the Champion."""
    # Startup and scheduler recovery can call this concurrently.  The process
    # lock reduces local contention; the database-backed helper is the actual
    # cross-process singleton boundary.
    with _autonomous_task_lock:
        from app.services.production_strategy import resolve_production_strategy_champion

        champion = resolve_production_strategy_champion()
        strategy = champion.get("strategy") if isinstance(champion.get("strategy"), dict) else {}
        content = strategy.get("content") if isinstance(strategy.get("content"), dict) else {}
        rebalance = content.get("rebalance") if isinstance(content.get("rebalance"), dict) else {}
        desired_top_n = int(
            rebalance.get("top_n")
            or ((champion.get("strategy_policy") or {}).get("top_n"))
            or 10
        )
        tasks = list_active_quant_run_tasks()
        autonomous_tasks = [
            task for task in tasks if str(task.get("replay_mode") or "") == "autonomous_daily"
        ]
        matching = [
            task for task in autonomous_tasks
            if int(task.get("strategy_id") or 0) == int(champion["strategy_id"])
        ]
        existing = matching[-1] if matching else None
        for stale in autonomous_tasks:
            if existing and int(stale.get("id") or 0) == int(existing.get("id") or 0):
                continue
            update_quant_run_task(
                int(stale["id"]),
                status="stopped",
                finished_at=_utc_now_iso(),
                last_message="自动任务已由新 Champion 调度任务替代",
            )
        if existing:
            if int(existing.get("top_n") or 0) != desired_top_n:
                update_quant_run_task(
                    int(existing["id"]),
                    top_n=desired_top_n,
                    last_message="自动任务参数已与当前 Champion 同步",
                )
                existing = get_quant_run_task(int(existing["id"])) or existing
            return {
                "ok": True,
                "created": False,
                "task": existing,
                "champion_version": champion.get("champion_version"),
                "policy": "scheduler_owned_simulation_only",
            }

        dates = list_consolidated_trade_dates()
        start_date = str(dates[-1]) if dates else None
        task_id, created = create_or_reuse_autonomous_quant_run_task(
            strategy_id=int(champion["strategy_id"]),
            top_n=desired_top_n,
            budget_ratio=1.0,
            exchange_group="HS",
            start_date=start_date,
            max_steps=1,
        )
        task = get_quant_run_task(task_id)
        return {
            "ok": True,
            "created": bool(created),
            "task": task,
            "champion_version": champion.get("champion_version"),
            "policy": "scheduler_owned_simulation_only",
        }
