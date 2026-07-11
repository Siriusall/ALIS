from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, time as dtime
import os
from time import perf_counter
from typing import Tuple

from app.integrations.tushare_client import reset_tushare_client
from app.core.trade_calendar import is_trade_day
from app.db.store import (
    get_account_balance,
    get_quant_replay_waterline,
    get_quant_run_task_health,
    get_quant_run_task_runtime_config,
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
from app.services.quant_run_engine import (
    HistoricalDataMissingError,
    ReplayRiskGateError,
    require_consolidated_trade_dates,
    run_for_trade_date,
)
from app.services.quant_risk import RiskPauseDecision, evaluate_consecutive_loss_gate, evaluate_data_health_gate, no_pause, pause_decision
from app.services.quant_task_state_machine import can_transition, normalize_status

logger = logging.getLogger("quant.quant_task_runner")

_task_lock = threading.Lock()
_running_task_ids: set[int] = set()
_REPLAY_BATCH_STEPS = max(1, min(int(os.getenv("QUANT_REPLAY_BATCH_STEPS", "3")), 5))
_PRAGMATIC_MAX_GAP_DAYS = max(1, min(int(os.getenv("QUANT_PRAGMATIC_MAX_GAP_DAYS", "5")), 30))
_ENFORCE_TRADING_SESSION_GATE = str(os.getenv("QUANT_ENFORCE_TRADING_SESSION_GATE", "0")).strip().lower() in {"1", "true", "yes", "on"}
_DEFAULT_SCORING_TIMEOUT_SEC = max(10, min(int(os.getenv("QUANT_SCORING_TIMEOUT_SEC", "90")), 1800))
_DEFAULT_MIN_ACCOUNT_BALANCE = max(0.0, float(os.getenv("QUANT_MIN_ACCOUNT_BALANCE", "0") or 0))
_DEFAULT_MAX_CONSECUTIVE_LOSSES = max(0, min(int(os.getenv("QUANT_MAX_CONSECUTIVE_LOSSES", "3")), 30))
_DEFAULT_ENABLE_DATA_HEALTH_GATE = str(os.getenv("QUANT_ENABLE_DATA_HEALTH_GATE", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_running(task_id: int) -> bool:
    with _task_lock:
        return int(task_id) in _running_task_ids


def _mark_running(task_id: int, running: bool) -> None:
    with _task_lock:
        if running:
            _running_task_ids.add(int(task_id))
        else:
            _running_task_ids.discard(int(task_id))


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


def _is_trading_session_now() -> bool:
    now = datetime.now()
    if not is_trade_day(now.date()):
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


def _preflight_check_step(
    *,
    signal_date: str,
    execute_date: str,
    min_account_balance: float,
    enforce_session_gate: bool,
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
    if bal <= float(min_account_balance):
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
):
    try:
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
        )
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
        )


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
    rec_id = create_quant_run_task_record(
        task_id=task_id,
        signal_date=result.signal_date,
        execute_date=result.execute_date,
        sold_count=result.sold_count,
        bought_count=result.bought_count,
        total_buy_cost=result.total_buy_cost,
        execution_id=result.execution_id,
        message=result.message,
        detail=result.detail,
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
    if _is_running(task_id):
        return False
    task = get_quant_run_task(task_id)
    if not task:
        return False
    if str(task.get("status") or "").lower() in {"failed"}:
        return False

    def _worker() -> None:
        _mark_running(task_id, True)
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
            mode = str(task0.get("replay_mode") or "pragmatic_recover").strip().lower()
            if mode not in {"strict_replay", "pragmatic_recover"}:
                mode = "pragmatic_recover"
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
            _transition_task_status(
                task_id,
                "running",
                "start_planning",
                stop_requested=0,
                started_at=task0.get("started_at") or now,
                replay_mode=mode,
                replay_status="planning",
                last_message="正在生成补跑计划",
                error=None,
            )

            if enable_data_health_gate:
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

            dates = require_consolidated_trade_dates(min_count=2)

            records = list_quant_run_task_records(task_id, limit=5000)
            done_signals = {str(x.get("signal_date") or "") for x in records if x.get("signal_date")}
            processed_total = 0

            loss_decision = evaluate_consecutive_loss_gate(
                task_id=task_id,
                max_consecutive_losses=max_consecutive_losses,
            )
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
            plan, last_exec_date = _build_replay_plan_context(
                task=task0,
                dates=dates,
                done_signals=done_signals,
                records=records,
                waterline=waterline,
                mode=mode,
            )
            if not plan.get("ok"):
                _transition_task_status(
                    task_id,
                    "paused",
                    "batch_completed",
                    replay_mode=mode,
                    replay_status="no_gap",
                    replay_gap_days=0,
                    replay_current_execute_date=None,
                    replay_target_execute_date=None,
                    processed_steps=0,
                    total_steps=0,
                    finished_at=None,
                    last_message="任务已暂停：当前无可补跑缺口",
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
            if enforce_session_gate and (not _is_trading_session_now()):
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
            if account_balance <= min_account_balance:
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
                pre_decision = _preflight_check_step(
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
                    update_quant_run_task(task_id, last_signal_date=signal_date, last_execute_date=execute_date)
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
                    last_signal_date=signal_date,
                    last_execute_date=execute_date,
                    last_message=f"正在计算：信号日 {signal_date} -> 执行日 {execute_date}（{step_idx}/{step_total}）",
                )

                def _step_phase(phase_text: str) -> None:
                    _transition_task_status(
                        task_id,
                        "running",
                        "step_running",
                        processed_steps=processed_total,
                        total_steps=step_total,
                        last_signal_date=signal_date,
                        last_execute_date=execute_date,
                        last_message=f"{phase_text}（{step_idx}/{step_total}）",
                    )
                step_t0 = perf_counter()
                result = _execute_step_with_retry(
                    task_id=task_id,
                    task_live=task_live,
                    signal_date=signal_date,
                    execute_date=execute_date,
                    progress_callback=_step_phase,
                    scoring_timeout_sec=scoring_timeout_sec,
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
                _transition_task_status(
                    task_id,
                    "paused",
                    "batch_completed",
                    replay_status="completed",
                    replay_gap_days=0,
                    replay_current_execute_date=None,
                    replay_target_execute_date=None,
                    processed_steps=processed_total,
                    finished_at=None,
                    last_message=f"任务已暂停：本批执行 {processed_total} 步，等待手动继续",
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
            logger.warning("quant task paused by risk gate task_id=%s code=%s | %s", task_id, getattr(e, "code", ""), str(e)[:160])
            decision = pause_decision(
                code=getattr(e, "code", "replay_risk_gate"),
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
                last_risk_code=getattr(e, "code", "replay_risk_gate"),
            )
        except Exception as e:  # noqa: BLE001
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

    t = threading.Thread(target=_worker, daemon=True, name=f"quant-task-{task_id}")
    t.start()
    return True


def resume_quant_tasks_on_startup() -> int:
    tasks = list_active_quant_run_tasks()
    paused = 0
    for t in tasks:
        status = normalize_status(t.get("status"))
        if status in {"pending", "running"} and can_transition(status, "paused", event="startup_pause"):
            if _transition_task_status(
                int(t.get("id") or 0),
                "paused",
                "startup_pause",
                finished_at=None,
                last_message="后端重启，任务已自动暂停，请手动点击继续",
            ):
                paused += 1
    return paused
