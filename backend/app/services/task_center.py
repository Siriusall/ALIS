from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.data_platform.db import _conn as data_conn
from app.data_platform.services.daily_update import daily_update_status
from app.data_platform.services.backfill import get_plan_model_data_backfill_status
from app.data_platform.services.runtime_logs import list_runtime_logs
from app.db.store import list_quant_run_tasks
from app.services.evolution_orchestrator import (
    evolution_runtime_status,
    evolution_schedule_status,
    get_evolution_task,
    list_evolution_tasks,
)
from app.data_platform.services.daily_update import simple_health
from app.core.read_only_diagnostics import degraded_read
from app.services.runtime_heartbeat import status as runtime_heartbeat_status


MODULES = [
    ("data", "数据中台", "行情、估值、交易约束、快照与质量门"),
    ("factor", "因子平台", "因子物化、未来评价与自动治理"),
    ("model", "模型系统", "训练、健康监控、候选与自动优化"),
    ("strategy", "策略演化", "Challenger、正式 OOS、影子观察与晋升"),
    ("portfolio", "组合决策", "目标权重、风险预算与方法竞赛"),
    ("execution", "模拟执行", "调仓计划、成本、公司行动与独立账本"),
    ("review", "收益复盘", "日周月年归因与学习信号"),
    ("research", "研究资产", "行业、政策、主题、公告与研究维护"),
    ("llm", "LLM 代理", "研究提案、数据运维与问题解释"),
    ("autonomy", "自治总线", "跨模块编排、自愈与事件监督"),
]

STEP_MODULE = {
    "hard_data_gate": "data", "sealed_holdout_data_gate": "data", "decision_data_gate": "data",
    "review_data_quality_governance": "data", "factor_daily_cycle": "factor",
    "plan_model_autonomy": "model", "strategy_policy_maturity": "strategy",
    "strategy_policy_candidates": "strategy", "profit_and_portfolio": "portfolio",
    "execution_cost_calibration": "execution", "simulation_corporate_actions": "execution",
    "simulation_ledger_audit": "execution", "periodic_performance_review": "review",
    "research_maintenance": "research", "free_industry_financials": "research",
    "pmi_demand_proxies": "research", "cn_ppi_raw": "research", "cn_ppi_price_proxies": "research",
    "official_policy_documents": "research", "official_policy_supply": "research",
    "official_capacity_utilization": "research", "futures_price_proxies": "research",
    "theme_revenue_exposure": "research", "research_data_assets": "research",
    "llm_research_agent": "llm", "llm_research_governance": "llm", "llm_proposal_router": "llm",
    "data_operations": "data", "llm_data_operations": "llm", "llm_incident_supervisor": "llm",
    "autonomous_self_healing": "autonomy",
}

STEP_LABELS = {
    "hard_data_gate": "正式 OOS 硬数据门", "sealed_holdout_data_gate": "密封验证集",
    "decision_data_gate": "最新决策数据门", "factor_daily_cycle": "因子日物化与评价",
    "plan_model_autonomy": "模型自动优化", "periodic_performance_review": "周期收益复盘",
    "review_data_quality_governance": "复盘数据质量治理", "strategy_policy_maturity": "策略候选成熟度",
    "strategy_policy_candidates": "策略候选生成", "execution_cost_calibration": "执行成本校准",
    "simulation_corporate_actions": "模拟公司行动", "simulation_ledger_audit": "独立账本审计",
    "system_release_governance": "系统版本发布治理",
    "profit_and_portfolio": "收益与组合决策", "research_maintenance": "研究维护",
    "free_industry_financials": "行业财务序列", "pmi_demand_proxies": "PMI 需求代理",
    "cn_ppi_raw": "PPI 原始数据", "cn_ppi_price_proxies": "PPI 价格代理",
    "official_policy_documents": "官方政策文档", "official_policy_supply": "政策供给信号",
    "official_capacity_utilization": "官方产能利用率", "futures_price_proxies": "期货价格代理",
    "theme_revenue_exposure": "主题收入暴露", "research_data_assets": "研究资产审计",
    "llm_research_agent": "LLM 研究提案", "llm_research_governance": "LLM 提案治理",
    "llm_proposal_router": "LLM 实验路由", "data_operations": "确定性数据运维", "llm_data_operations": "LLM 数据运维",
    "autonomous_self_healing": "确定性自愈", "llm_incident_supervisor": "问题解释与归类",
}

TASK_TYPE_MODULE = {
    "factor_daily_cycle": "factor", "strategy_shadow_cycle": "strategy",
    "strategy_evolution_cycle": "strategy", "strategy_controller_oos": "strategy",
    "strategy_candidate_generation": "strategy", "strategy_environment_backtest": "strategy",
    "strategy_cluster_distillation": "strategy", "strategy_portfolio_research": "strategy",
    "strategy_failure_model_training": "strategy", "strategy_portfolio_formal_queue": "strategy",
    "model_candidate_research": "model",
    "model_strategy_impact_validation": "model",
    "model_shadow_observation": "model",
    "plan_model_autonomy": "model",
    "system_autonomy_cycle": "autonomy", "system_release_governance": "autonomy",
    "sealed_holdout_adj_factor_backfill": "data",
}

TASK_TYPE_LABEL = {
    "factor_daily_cycle": "因子日评价", "strategy_shadow_cycle": "策略影子观察",
    "strategy_evolution_cycle": "正式策略演化", "strategy_controller_oos": "策略控制器 OOS",
    "strategy_candidate_generation": "策略候选生成", "strategy_environment_backtest": "策略环境回测",
    "strategy_cluster_distillation": "策略簇蒸馏", "strategy_portfolio_research": "策略组合研究",
    "strategy_failure_model_training": "策略失败归因训练", "strategy_portfolio_formal_queue": "策略组合正式入队",
    "model_candidate_research": "模型候选分层筛选",
    "model_strategy_impact_validation": "模型完整策略影响验证",
    "model_shadow_observation": "模型未来影子观察",
    "plan_model_autonomy": "任务原生模型自治调度",
    "system_autonomy_cycle": "每日自治循环", "system_release_governance": "系统版本发布治理",
    "sealed_holdout_adj_factor_backfill": "密封验证集批量补全",
}

RETIRED_TASK_TYPES = {"factor_research_cycle"}


def _is_terminal_research_contract_failure(task: dict[str, Any]) -> bool:
    """Do not turn an exhausted, non-retriable research contract into a live outage.

    These failures remain visible in the task/evidence history.  They are
    already terminal and the scheduler must create a fresh task after the
    governing data or contract changes; presenting them as the current module
    state makes the health console stay red forever.
    """
    if str(task.get("status") or "").lower() not in {"failed", "error"}:
        return False
    try:
        attempts = int(task.get("attempt") or 0)
        max_attempts = int(task.get("max_attempts") or 0)
    except (TypeError, ValueError):
        return False
    if max_attempts <= 0 or attempts < max_attempts:
        return False
    task_type = str(task.get("task_type") or "")
    if task_type not in {
        "strategy_shadow_cycle",
        "strategy_evolution_cycle",
        "strategy_environment_backtest",
        "model_shadow_observation",
        "model_strategy_impact_validation",
    }:
        return False
    evidence = f"{task.get('message') or ''} {task.get('error') or ''} {task.get('result_json') or ''}".lower()
    return any(marker in evidence for marker in (
        "oos 永久硬数据未就绪",
        "最新决策数据未就绪",
        "冻结策略包缺少因子绑定",
        "未知模型绑定",
        "blocked_by_decision_data",
        "blocked_incomplete_factor_bindings",
    ))


def _iso(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse(value: Any) -> datetime | None:
    text = _iso(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _status(value: Any, *, phase: Any = None) -> str:
    raw = str(value or "unknown").lower()
    phase_raw = str(phase or "").lower()
    if raw == "running":
        return "running"
    if raw in {"queued", "retry_wait"}:
        return "waiting"
    if raw == "pending":
        return "waiting"
    if raw == "observing":
        return "observing"
    if raw in {"paused", "stopped"}:
        return "paused"
    if raw in {"failed", "error"}:
        return "failed"
    if raw == "cancelled":
        return "cancelled"
    if raw in {"completed", "success", "ready"}:
        return "completed"
    if raw == "blocked":
        return "blocked"
    if raw in {"degraded", "partial"}:
        return raw
    if raw == "cooldown" or phase_raw == "cooldown":
        return "waiting"
    return raw or "unknown"


def _status_with_evidence(value: Any, *, phase: Any = None, message: Any = None, error: Any = None) -> str:
    state = _status(value, phase=phase)
    if state != "failed":
        return state
    evidence = f"{message or ''} {error or ''}".lower()
    waiting_markers = (
        "尚未发布", "等待上游", "冷却后", "等待更新", "自然前移", "未来样本",
        "retry_wait", "cooldown", "upstream_not_published",
    )
    blocked_markers = (
        "未就绪", "冻结", "安全阻断", "禁止", "blocked", "not_ready",
    )
    if any(marker in evidence for marker in waiting_markers):
        return "waiting"
    if any(marker in evidence for marker in blocked_markers):
        return "blocked"
    return state


def _schedule_for(module: str, schedule: dict[str, Any]) -> str | None:
    candidates: list[str | None]
    if module == "factor":
        candidates = [_iso(schedule.get("next_factor_cycle_at"))]
    elif module == "strategy":
        candidates = [_iso(schedule.get("next_shadow_cycle_at")), _iso(schedule.get("next_formal_cycle_at"))]
    else:
        candidates = [_iso(schedule.get("next_system_cycle_at"))]
    dated = [(value, _parse(value)) for value in candidates if value]
    dated = [(value, parsed) for value, parsed in dated if parsed]
    return min(dated, key=lambda item: item[1])[0] if dated else None


def _evolution_next_step(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "")
    phase = str(task.get("phase") or "")
    if status == "running":
        return {
            "starting": "领取任务并冻结输入版本", "quick_diagnostic": "完成快速淘汰门后决定是否进入正式 OOS",
            "formal_oos": "完成当前窗口并持久化检查点", "shadow_observation": "积累下一未来交易日影子证据",
            "factor_materialization": "完成因子截面后运行评价与治理", "system_autonomy": "继续执行下一自治步骤",
            "sealed_holdout_adj_factor_backfill": "继续下一批 adj_factor 补全，完成后自动复核 126/126",
            "model_candidate_quick_diagnostic": "快速淘汰后仅将排名第一的合格候选送入正式五窗口",
            "model_strategy_impact_validation": "完成固定策略包五窗口 A/B 后决定是否进入未来观察",
            "model_shadow_observation": "累计真实未来收益日并写回模型生产资格门",
            "plan_model_autonomy": "按资源预算启动下一模型训练或等待数据",
        }.get(phase, "继续当前步骤并写入检查点")
    if status == "retry_wait":
        return "到达重试时间后从检查点恢复"
    if status == "queued":
        return "单 worker 领取后开始执行"
    if status == "completed":
        return "等待下一计划周期自动创建任务"
    if status == "failed":
        return "按失败分类决定自动重试、隔离或等待外部条件"
    if status == "cancelled":
        return "保留证据，不再执行；后续周期重新评估是否创建新任务"
    return "等待调度器复核"


def _normalise_evolution(task: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
    task_type = str(task.get("task_type") or "")
    module = TASK_TYPE_MODULE.get(task_type, "autonomy")
    activity_scope = "history" if task_type in RETIRED_TASK_TYPES else None
    if _is_terminal_research_contract_failure(task):
        activity_scope = "history"
    next_at = _iso(task.get("next_run_at")) if str(task.get("status")) in {"queued", "retry_wait"} else _schedule_for(module, schedule)
    return {
        "id": f"evolution:{task.get('id')}", "source": "evolution", "source_id": int(task.get("id") or 0),
        "module": module, "task_type": task_type, "name": TASK_TYPE_LABEL.get(task_type, task_type or "自治任务"),
        "status": _status_with_evidence(
            task.get("status"), phase=task.get("phase"), message=task.get("message"), error=task.get("error"),
        ), "raw_status": str(task.get("status") or ""),
        "phase": str(task.get("phase") or ""), "progress": float(task.get("progress") or 0),
        "message": str(task.get("message") or ""), "error": _iso(task.get("error")),
        "current_step": str(task.get("message") or task.get("phase") or "等待调度"),
        "next_step": _evolution_next_step(task), "next_run_at": next_at,
        "created_at": _iso(task.get("created_at")), "started_at": _iso(task.get("started_at")),
        "heartbeat_at": _iso(task.get("heartbeat_at")), "updated_at": _iso(task.get("updated_at")),
        "finished_at": _iso(task.get("finished_at")), "attempt": int(task.get("attempt") or 0),
        "max_attempts": int(task.get("max_attempts") or 0),
        "checkpoint": task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {},
        "estimated_remaining_seconds": task.get("estimated_remaining_seconds"),
        **({
            "activity_scope": activity_scope,
            "history_reason": "V10 事件驱动研究已替代旧连续因子任务",
        } if activity_scope else {}),
    }


def _step_times(step: dict[str, Any], cursor: datetime | None) -> tuple[str | None, str | None, datetime | None]:
    explicit_start = _parse(step.get("started_at"))
    explicit_finish = _parse(step.get("finished_at"))
    start = explicit_start or cursor
    duration = max(0.0, float(step.get("duration_seconds") or 0.0))
    finish = explicit_finish or (start + timedelta(seconds=duration) if start else None)
    return start.isoformat() if start else None, finish.isoformat() if finish else None, finish or cursor


def _normalise_cycle_steps(task: dict[str, Any], schedule: dict[str, Any]) -> list[dict[str, Any]]:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    steps = result.get("steps") if isinstance(result.get("steps"), list) else []
    cursor = _parse(task.get("started_at"))
    items = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or f"step_{index + 1}")
        module = STEP_MODULE.get(name, "autonomy")
        result_item = step.get("result") if isinstance(step.get("result"), dict) else {}
        health = step.get("health") if isinstance(step.get("health"), dict) else {}
        result_status = str(result_item.get("status") or "")
        raw_status = str(health.get("status") or result_status or ("completed" if step.get("ok") else "failed"))
        display_status = (
            _status(result_status)
            if result_status in {"blocked", "observing", "degraded", "partial", "cooldown"}
            else _status_with_evidence(
                raw_status,
                message=health.get("reason") or result_item.get("reason"),
                error=step.get("error"),
            )
        )
        started_at, finished_at, cursor = _step_times(step, cursor)
        items.append({
            "id": f"cycle:{task.get('id')}:{name}", "source": "autonomy_step", "source_id": int(task.get("id") or 0),
            "module": module, "task_type": name, "name": STEP_LABELS.get(name, name.replace("_", " ")),
            "status": display_status, "raw_status": raw_status, "phase": name,
            "progress": 1.0, "message": str(health.get("reason") or result_item.get("reason") or raw_status),
            "error": _iso(step.get("error")), "current_step": "本周期步骤已完成" if step.get("ok") else "步骤保留隔离证据",
            "next_step": "下一自治周期按冻结合同重新复核", "next_run_at": _schedule_for(module, schedule),
            "created_at": started_at, "started_at": started_at, "heartbeat_at": finished_at,
            "updated_at": finished_at, "finished_at": finished_at, "attempt": int(task.get("attempt") or 0),
            "max_attempts": int(task.get("max_attempts") or 0), "duration_seconds": float(step.get("duration_seconds") or 0),
            "time_source": "recorded" if step.get("started_at") else "derived_from_cycle_duration",
            "activity_scope": "current_cycle",
        })
    return items


def _data_runs(limit: int) -> list[dict[str, Any]]:
    with data_conn() as conn:
        rows = conn.execute(
            """SELECT id,job_name,source_code,status,started_at,finished_at,rows_read,rows_written,error,meta_json
               FROM data_ingestion_runs ORDER BY id DESC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        state = _status_with_evidence(
            item.get("status"), message=item.get("meta_json"), error=item.get("error"),
        )
        items.append({
            "id": f"data:{item.get('id')}", "source": "data_ingestion", "source_id": int(item.get("id") or 0),
            "module": "data", "task_type": str(item.get("job_name") or "data_ingestion"),
            "name": str(item.get("job_name") or "数据任务"), "status": state, "raw_status": str(item.get("status") or ""),
            "phase": str(item.get("source_code") or ""), "progress": 1.0 if state in {"completed", "failed", "cancelled"} else 0.5,
            "message": f"读取 {int(item.get('rows_read') or 0):,}，写入 {int(item.get('rows_written') or 0):,}",
            "error": _iso(item.get("error")), "current_step": str(item.get("source_code") or "数据采集与落库"),
            "next_step": "完成后运行覆盖率与质量复核" if state == "running" else "由日更、自愈或补全计划按缺口决定是否重试",
            "next_run_at": None, "created_at": _iso(item.get("started_at")), "started_at": _iso(item.get("started_at")),
            "heartbeat_at": _iso(item.get("finished_at")) or _iso(item.get("started_at")),
            "updated_at": _iso(item.get("finished_at")) or _iso(item.get("started_at")), "finished_at": _iso(item.get("finished_at")),
            "attempt": 1, "max_attempts": 1,
        })
    return items


def _daily_task(status: dict[str, Any], generated_at: datetime) -> dict[str, Any]:
    retry_seconds = max(0, int(status.get("retry_after_seconds") or 0))
    next_at = (generated_at + timedelta(seconds=retry_seconds)).isoformat() if retry_seconds else _iso(status.get("next_scheduler_check_at"))
    phase = str(status.get("phase") or "idle")
    raw_status = "running" if status.get("running") else phase
    return {
        "id": f"daily:{status.get('run_id') or 'scheduler'}", "source": "daily_update", "source_id": int(status.get("run_id") or 0),
        "module": "data", "task_type": "daily_market_update", "name": "每日收盘数据更新",
        "status": _status(raw_status, phase=phase), "raw_status": raw_status, "phase": phase,
        "progress": float(status.get("progress") or 0) / 100.0, "message": str(status.get("message") or "等待调度"),
        "error": _iso(status.get("error")), "current_step": str(status.get("message") or phase),
        "next_step": "冷却结束后重新拉取并复核决策数据门" if phase == "cooldown" else "完成当前数据切片并运行质量复核" if status.get("running") else "等待下一个收盘日更周期",
        "next_run_at": next_at, "created_at": _iso(status.get("started_at")), "started_at": _iso(status.get("started_at")),
        "heartbeat_at": _iso(status.get("finished_at")) or _iso(status.get("started_at")),
        "updated_at": _iso(status.get("finished_at")) or _iso(status.get("started_at")), "finished_at": _iso(status.get("finished_at")),
        "attempt": 1, "max_attempts": 1,
    }


def _model_data_backfill_task() -> dict[str, Any] | None:
    try:
        payload = get_plan_model_data_backfill_status()
    except Exception:
        return None
    raw_status = str(payload.get("status") or "not_started")
    if raw_status == "not_started":
        return None
    checklist = [
        item
        for item in (payload.get("checklist") or [])
        if isinstance(item, dict)
    ]
    current = next(
        (
            item
            for item in checklist
            if str(item.get("status") or "") in {"running", "pending"}
        ),
        checklist[-1] if checklist else {},
    )
    current_label = str(current.get("label") or payload.get("phase") or "模型历史数据补全")
    if current.get("covered_raw_sessions") is not None:
        current_label += (
            f"；资金原始覆盖 {int(current.get('covered_raw_sessions') or 0):,}/"
            f"{int(current.get('target_raw_sessions') or 0):,} 日"
        )
    elif current.get("processed_calendar_days") is not None:
        current_label += (
            f"；新闻源已检查 {int(current.get('processed_calendar_days') or 0):,}/"
            f"{int(current.get('target_calendar_days') or 0):,} 日"
        )
    normalized = _status(raw_status)
    intervention = payload.get("intervention") if isinstance(payload.get("intervention"), dict) else {}
    return {
        "id": f"model-data-backfill:{payload.get('work_order_id') or 'current'}",
        "source": "data_work_order",
        "source_id": 0,
        "module": "data",
        "task_type": "plan_model_data_backfill",
        "name": "资金与事件模型历史数据补全",
        "status": normalized,
        "raw_status": str(payload.get("raw_status") or raw_status),
        "phase": str(payload.get("phase") or ""),
        "progress": float(payload.get("progress") or 0.0),
        "message": str(
            payload.get("error")
            or intervention.get("current")
            or "系统按登记清单自动补数"
        ),
        "error": _iso(payload.get("error")),
        "current_step": current_label,
        "next_step": (
            str(intervention.get("current") or "检查异常并保持模型 N/A")
            if normalized == "failed"
            else "完成当前清单项后自动推进下一项并重新计算模型数据门"
        ),
        "next_run_at": None,
        "created_at": None,
        "started_at": None,
        "heartbeat_at": _iso(payload.get("updated_at")),
        "updated_at": _iso(payload.get("updated_at")),
        "finished_at": (
            _iso(payload.get("updated_at")) if normalized == "completed" else None
        ),
        "attempt": int(payload.get("attempt") or 0),
        "max_attempts": int(payload.get("max_attempts") or 0),
        "checkpoint": payload,
        "activity_scope": "current",
    }


def _decision_data_task(health: dict[str, Any], daily: dict[str, Any], generated_at: datetime) -> dict[str, Any]:
    ready = bool(health.get("ok"))
    retry_seconds = max(0, int(daily.get("retry_after_seconds") or 0))
    next_at = ((generated_at + timedelta(seconds=retry_seconds)).isoformat() if retry_seconds
               else _iso(daily.get("next_scheduler_check_at")))
    target = str(health.get("target_trade_date") or "")
    latest = str(health.get("latest_decision_ready_trade_date") or "")
    contract = health.get("decision_contract") if isinstance(health.get("decision_contract"), dict) else {}
    counts = contract.get("counts") if isinstance(contract.get("counts"), dict) else {}
    expected_rows = contract.get("expected_rows") if isinstance(contract.get("expected_rows"), dict) else {}
    stock_count = int(expected_rows.get("stk_limit") or contract.get("stock_count") or 0)
    limit_count = int(counts.get("stk_limit") or 0)
    coverage_text = (
        f"；stk_limit {limit_count:,}/{stock_count:,}（{limit_count / stock_count * 100:.2f}%）"
        if stock_count else ""
    )
    return {
        "id": f"decision-data:{target or 'latest'}", "source": "decision_data_gate", "source_id": 0,
        "module": "data", "task_type": "latest_decision_data_gate", "name": "最新交易日决策数据门",
        "status": "completed" if ready else "blocked", "raw_status": str(health.get("level") or ""), "phase": "decision_ready" if ready else "blocked",
        "progress": 1.0 if ready else 0.0, "message": str(health.get("summary") or "等待决策数据门"), "error": None,
        "current_step": f"目标 {target or '未知'}；最近可决策 {latest or '无'}{coverage_text}",
        "next_step": "允许生成正式计划" if ready else "日更补齐缺失切片后重新计算数据门",
        "next_run_at": next_at, "created_at": None, "started_at": _iso(daily.get("started_at")),
        "heartbeat_at": _iso(daily.get("finished_at")) or generated_at.isoformat(), "updated_at": generated_at.isoformat(),
        "finished_at": _iso(daily.get("finished_at")) if ready else None, "attempt": 1, "max_attempts": 1,
        "checkpoint": contract,
    }


def _quant_tasks(limit: int) -> list[dict[str, Any]]:
    items = []
    tasks = list_quant_run_tasks(limit=max(1, min(limit, 50)))
    current_autonomous_id = next(
        (int(task.get("id") or 0) for task in tasks if str(task.get("replay_mode") or "") == "autonomous_daily"),
        0,
    )
    for task in tasks:
        total = int(task.get("total_steps") or 0)
        processed = int(task.get("processed_steps") or 0)
        raw_status = str(task.get("status") or "").lower()
        replay_status = str(task.get("replay_status") or "")
        autonomous = str(task.get("replay_mode") or "") == "autonomous_daily"
        task_error = str(task.get("error") or "").strip()
        task_message = str(task.get("last_message") or "")
        legacy_schedule_wait = (
            autonomous
            and not task_error
            and processed == 0
            and total == 0
            and (
                raw_status == "pending"
                or task_message in {
                    "任务已创建，等待执行",
                    "自动任务参数已与当前 Champion 同步",
                    "后端重启，任务已安全暂停；下一收盘调度会自动继续，也可手动继续",
                    "自治任务等待下一份到期正式调仓计划",
                    "等待收盘调度与下一份到期正式调仓计划",
                }
            )
        )
        waiting_schedule = autonomous and (
            replay_status in {"waiting_schedule", "waiting_formal_plan"}
            or legacy_schedule_wait
        )
        recovery_pending = replay_status == "recovery_pending"
        risk_paused = raw_status == "paused" and bool(task_error)
        state = (
            "waiting_schedule" if waiting_schedule
            else "recovery_pending" if recovery_pending
            else "blocked" if risk_paused
            else _status(raw_status)
        )
        current_step = (
            "等待收盘时间、完整收盘数据和到期正式计划"
            if waiting_schedule
            else "上次运行被进程重启中断，等待执行认领、账本和检查点复核"
            if recovery_pending
            else str(task.get("last_message") or "风险门保持暂停")
            if risk_paused
            else f"已处理 {processed}/{total or '?'} 个交易日"
        )
        next_step = (
            "满足收盘数据门后由调度器自动领取正式计划"
            if waiting_schedule
            else "安全恢复检查通过后，自治任务自动继续；手工回放等待明确继续"
            if recovery_pending
            else "解除风险阻断后按状态机恢复"
            if risk_paused
            else "处理下一个交易日并写入模拟账本"
            if state == "running"
            else "按状态机复核"
        )
        items.append({
            "id": f"quant:{task.get('id')}", "source": "quant_run", "source_id": int(task.get("id") or 0),
            "module": "execution", "task_type": "simulation_strategy_execution", "name": f"模拟策略执行 #{task.get('id')}",
            "status": state, "raw_status": raw_status, "phase": replay_status,
            "progress": 0.0 if waiting_schedule or recovery_pending else processed / total if total > 0 else (1.0 if state == "completed" else 0.0),
            "message": str(task.get("last_message") or task.get("error") or "等待执行"), "error": _iso(task.get("error")),
            "current_step": current_step,
            "next_step": next_step,
            "next_run_at": None, "created_at": _iso(task.get("created_at")), "started_at": _iso(task.get("started_at")),
            "heartbeat_at": _iso(task.get("updated_at")), "updated_at": _iso(task.get("updated_at")),
            "finished_at": _iso(task.get("finished_at")), "attempt": 1, "max_attempts": 1,
            "activity_scope": "current" if int(task.get("id") or 0) == current_autonomous_id else "history",
        })
    return items


def _latest_failure_summary(tasks: list[dict[str, Any]]) -> tuple[int, int]:
    """Separate unresolved latest failures from historical failed records."""
    latest_by_contract: dict[tuple[str, str], dict[str, Any]] = {}
    for item in tasks:
        if item.get("activity_scope") == "history":
            continue
        key = (str(item.get("source") or ""), str(item.get("task_type") or ""))
        if key not in latest_by_contract:
            latest_by_contract[key] = item
    unresolved = sum(1 for item in latest_by_contract.values() if item.get("status") == "failed")
    historical = sum(1 for item in tasks if item.get("status") == "failed")
    return unresolved, historical


def _assign_activity_scope(tasks: list[dict[str, Any]]) -> None:
    latest_contracts: set[tuple[str, str]] = set()
    for item in tasks:
        if item.get("activity_scope"):
            continue
        key = (str(item.get("source") or ""), str(item.get("task_type") or ""))
        if key in latest_contracts:
            item["activity_scope"] = "history"
        else:
            item["activity_scope"] = "current"
            latest_contracts.add(key)


def _business_task_views(tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Keep resident processes separate from work that advances the business loop.

    A healthy scheduler is necessary, but it is not itself a data update,
    decision, execution, or research task.  The owner console needs that
    distinction to answer whether anything is actually progressing.
    """
    current = [item for item in tasks if item.get("activity_scope") != "history"]
    runtime_services = [item for item in current if item.get("source") == "runtime"]
    business = [item for item in current if item.get("source") != "runtime"]
    active_states = {
        "running", "waiting", "waiting_schedule", "recovery_pending", "observing",
        "blocked", "degraded", "partial", "failed",
    }
    active = [item for item in business if str(item.get("status") or "") in active_states]
    summary = {
        "active": len(active),
        "running": sum(1 for item in active if item.get("status") == "running"),
        "waiting": sum(1 for item in active if item.get("status") in {"waiting", "waiting_schedule", "recovery_pending", "observing"}),
        "blocked": sum(1 for item in active if item.get("status") == "blocked"),
        "failed": sum(1 for item in active if item.get("status") == "failed"),
        "runtime_healthy": sum(1 for item in runtime_services if item.get("status") == "running"),
        "runtime_total": len(runtime_services),
    }
    return active, runtime_services, summary


def _evolution_runtime_tasks() -> list[dict[str, Any]]:
    runtime = evolution_runtime_status()
    observed_at = datetime.now().astimezone().isoformat()
    items = []
    for key, label in (("worker", "自治任务 Worker"), ("scheduler", "自治计划 Scheduler")):
        state = (runtime.get("components") or {}).get(key) or {}
        disabled = bool(state.get("disabled"))
        healthy = bool(state.get("healthy"))
        status = "completed" if disabled else "running" if healthy else "failed"
        heartbeat = _iso(state.get("heartbeat_at"))
        updated_at = heartbeat or observed_at
        items.append({
            "id": f"runtime:evolution:{key}", "source": "runtime", "source_id": 0,
            "module": "autonomy", "task_type": f"evolution_{key}_runtime", "name": label,
            "status": status, "raw_status": "disabled" if disabled else "alive" if healthy else "unhealthy",
            "phase": "runtime_health", "progress": 1.0 if healthy or disabled else 0.0,
            "message": "已禁用" if disabled else "心跳正常" if healthy else str(state.get("last_error") or "心跳超时或线程未启动"),
            "error": None if healthy or disabled else str(state.get("last_error") or "runtime_heartbeat_unhealthy"),
            "current_step": "持续领取持久任务" if key == "worker" else "持续创建交易日自治任务",
            "next_step": "保持心跳" if healthy else "服务自动重启后恢复；健康页保持失败状态",
            "next_run_at": None, "created_at": None, "started_at": None,
            "heartbeat_at": heartbeat, "updated_at": updated_at, "finished_at": None,
            "attempt": 1, "max_attempts": 1, "activity_scope": "current",
            "checkpoint": {"heartbeat_age_seconds": state.get("heartbeat_age_seconds")},
        })
    return items


def _background_runtime_tasks() -> list[dict[str, Any]]:
    """Expose non-Evolution resident components using the same runtime contract."""
    labels = {
        "redis_stream_worker": ("Redis 数据流 Worker", "持续领取并确认数据中台任务"),
        "manual_review_scheduler": ("人工复核 Scheduler", "持续执行复核队列预审"),
        "model_governance_scheduler": ("旧模型治理 Scheduler（已退休）", "不再训练、比较或晋升任何模型"),
        "auto_simulation_scheduler": ("自动模拟 Scheduler", "等待收盘数据并执行模拟任务"),
        "daily_update_scheduler": ("收盘日更 Scheduler", "检查并启动收盘数据更新"),
    }
    items = []
    observed_at = datetime.now().astimezone().isoformat()
    runtime = runtime_heartbeat_status()
    for key, (label, current_step) in labels.items():
        state = runtime.get(key)
        if not state:
            continue
        healthy = bool(state.get("healthy"))
        heartbeat = _iso(state.get("heartbeat_at"))
        items.append({
            "id": f"runtime:{key}", "source": "runtime", "source_id": 0,
            "module": "autonomy", "task_type": f"{key}_runtime", "name": label,
            "status": "running" if healthy else "failed",
            "raw_status": "alive" if healthy else "unhealthy", "phase": "runtime_health",
            "progress": 1.0 if healthy else 0.0,
            "message": "心跳正常" if healthy else str(state.get("last_error") or state.get("health_reason") or "心跳超时"),
            "error": None if healthy else str(state.get("last_error") or state.get("health_reason") or "runtime_heartbeat_unhealthy"),
            "current_step": current_step,
            "next_step": "保持心跳" if healthy else "服务重启后恢复并重新检查",
            "next_run_at": None, "created_at": _iso(state.get("started")),
            "started_at": _iso(state.get("started")), "heartbeat_at": heartbeat,
            "updated_at": heartbeat or observed_at, "finished_at": None,
            "attempt": 1, "max_attempts": 1, "activity_scope": "current",
            "checkpoint": {"heartbeat_age_seconds": state.get("heartbeat_age_seconds"), "errors": state.get("errors", 0)},
        })
    return items


def build_task_center(
    *,
    limit: int = 50,
    log_limit: int = 200,
    include_slo: bool = True,
    include_cycle_details: bool = True,
    compact_cycle: bool = False,
) -> dict[str, Any]:
    generated = datetime.now().astimezone()
    read_diagnostics: list[dict[str, Any]] = []

    def safe_read(component: str, reader, fallback):
        try:
            return reader()
        except Exception as exc:  # noqa: BLE001 - one optional read must not erase the task center
            read_diagnostics.append(degraded_read(component, exc))
            return fallback

    schedule = safe_read("evolution_schedule", evolution_schedule_status, {})
    evolution = safe_read("evolution_tasks", lambda: list_evolution_tasks(limit=max(limit, 20)), [])
    tasks = [_normalise_evolution(task, schedule) for task in evolution]
    latest_cycle = next(
        (
            task
            for task in evolution
            if str(task.get("task_type")) == "system_autonomy_cycle"
        ),
        None,
    )
    if latest_cycle and include_cycle_details:
        detailed_cycle = safe_read(
            "evolution_task_detail",
            lambda: get_evolution_task(
                int(latest_cycle.get("id") or 0),
                compact=compact_cycle,
            ),
            None,
        )
        if detailed_cycle:
            latest_cycle = detailed_cycle
    if latest_cycle:
        tasks.extend(_normalise_cycle_steps(latest_cycle, schedule))
    daily = safe_read("daily_update_status", daily_update_status, {})
    tasks.append(_daily_task(daily, generated))
    try:
        health = simple_health()
    except Exception as exc:  # noqa: BLE001 - task center must expose degraded state
        health = degraded_read("decision_data_health", exc)
    tasks.append(_decision_data_task(health, daily, generated))
    model_data_backfill = _model_data_backfill_task()
    if model_data_backfill:
        tasks.append(model_data_backfill)
    tasks.extend(_evolution_runtime_tasks())
    tasks.extend(_background_runtime_tasks())
    tasks.extend(safe_read("data_ingestion_runs", lambda: _data_runs(min(limit, 50)), []))
    tasks.extend(safe_read("quant_run_tasks", lambda: _quant_tasks(min(limit, 30)), []))
    tasks.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    _assign_activity_scope(tasks)
    completed_holdout = next(
        (item for item in tasks
         if item.get("task_type") == "sealed_holdout_adj_factor_backfill"
         and item.get("status") == "completed"
         and ((item.get("checkpoint") or {}).get("eligible_sessions") == (item.get("checkpoint") or {}).get("required_sessions"))),
        None,
    )
    if completed_holdout:
        completed_at = str(completed_holdout.get("updated_at") or "")
        for item in tasks:
            if item.get("task_type") == "sealed_holdout_data_gate" and str(item.get("updated_at") or "") <= completed_at:
                item["activity_scope"] = "history"
    unresolved_failures, failed_records = _latest_failure_summary(tasks)
    current_tasks = [item for item in tasks if item.get("activity_scope") != "history"]
    active_business_tasks, runtime_services, business_summary = _business_task_views(tasks)
    if include_slo:
        from app.services.system_autonomy import build_autonomy_slo
        try:
            autonomy_slo = build_autonomy_slo()
        except Exception as exc:  # noqa: BLE001 - admin diagnostics must degrade safely
            autonomy_slo = degraded_read("autonomy_slo", exc)
        from app.services.profit_loop import build_execution_chain_slo
        try:
            execution_slo = build_execution_chain_slo()
        except Exception as exc:  # noqa: BLE001 - admin diagnostics must degrade safely
            execution_slo = degraded_read("execution_slo", exc)
    else:
        autonomy_slo = {"status": "deferred", "deferred": True, "reason": "紧凑任务中心只返回页面所需任务与日志"}
        execution_slo = {"status": "deferred", "deferred": True, "reason": "紧凑任务中心只返回页面所需任务与日志"}

    module_items = []
    for key, name, description in MODULES:
        related = [item for item in current_tasks if item.get("module") == key]
        # Failed tasks are operationally active incidents until explicitly
        # superseded; do not let a later idle/completed task hide the failure
        # on the module card.
        active = next((item for item in related if item.get("status") in {"running", "waiting", "waiting_schedule", "recovery_pending", "observing", "blocked", "degraded", "partial", "failed"}), None)
        latest = active or (related[0] if related else None)
        next_at = latest.get("next_run_at") if latest and latest.get("next_run_at") else _schedule_for(key, schedule)
        module_items.append({
            "key": key, "name": name, "description": description,
            "status": str((latest or {}).get("status") or "idle"),
            "current_task_id": (latest or {}).get("id"), "current_step": str((latest or {}).get("current_step") or "当前无运行任务"),
            "next_step": str((latest or {}).get("next_step") or "等待计划周期"), "next_run_at": next_at,
            "updated_at": (latest or {}).get("updated_at"),
            "progress": float((latest or {}).get("progress") or 0),
            "estimated_remaining_seconds": (latest or {}).get("estimated_remaining_seconds"),
            "checkpoint": (latest or {}).get("checkpoint") if isinstance((latest or {}).get("checkpoint"), dict) else {},
            "running_count": sum(1 for item in related if item.get("status") == "running"),
            "waiting_count": sum(1 for item in related if item.get("status") in {"waiting", "waiting_schedule", "recovery_pending", "observing", "paused"}),
            "failed_count": sum(1 for item in related if item.get("status") == "failed"),
        })

    return {
        "ok": True, "schema_version": "alis_task_center_v2", "generated_at": generated.isoformat(),
        "summary": {
            "running": sum(1 for item in current_tasks if item.get("status") == "running"),
            "waiting": sum(1 for item in current_tasks if item.get("status") in {"waiting", "waiting_schedule", "recovery_pending", "observing"}),
            "current_internal_errors": unresolved_failures,
            "recent_failed_records": failed_records,
            "failed": unresolved_failures,
            "completed": sum(1 for item in current_tasks if item.get("status") == "completed"),
        },
        "modules": module_items, "tasks": tasks[: max(20, min(limit * 3, 200))],
        # The two explicit projections prevent clients from presenting a
        # resident scheduler as a user-visible business operation.
        "active_business_tasks": active_business_tasks,
        "runtime_services": runtime_services,
        "business_summary": business_summary,
        "autonomy_slo": autonomy_slo,
        "execution_slo": execution_slo,
        "logs": safe_read(
            "runtime_logs",
            lambda: list_runtime_logs(limit=max(20, min(log_limit, 500))),
            [],
        ),
        "schedule": schedule,
        "read_diagnostics": read_diagnostics,
        "policy": {
            "read_only": True,
            "time_zone": str(generated.tzinfo),
            "historical_step_time": "旧自治步骤按周期开始时间和逐步 duration_seconds 推导；新周期直接记录 started_at/finished_at",
            "intervention": "任务中心先提供可视化；取消、隔离和安全重试继续走既有受约束接口",
        },
    }
