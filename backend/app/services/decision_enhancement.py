from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.market_states import MARKET_STATES
from app.services.plan_model_control import PLAN_MODELS, model_family_status


FALLBACK_STATE_POLICY = {
    "bull": {"max_equity": 0.90, "risk_budget": 1.00, "first_pct": 0.85, "label": "风险扩张"},
    "sideways": {"max_equity": 0.65, "risk_budget": 0.75, "first_pct": 0.55, "label": "震荡分化"},
    "bear": {"max_equity": 0.40, "risk_budget": 0.50, "first_pct": 0.30, "label": "风险收缩"},
    "stress": {"max_equity": 0.25, "risk_budget": 0.30, "first_pct": 0.15, "label": "压力状态"},
    "recovery": {"max_equity": 0.50, "risk_budget": 0.55, "first_pct": 0.30, "label": "恐慌修复"},
}


MODEL_ROLE_CONTRACTS = {
    "medium_horizon_lambdarank_v1": {
        "decision_role": "选股排序",
        "owns": ["候选股票排序", "中期底仓优先级", "预期相对收益"],
        "must_not_own": ["仓位上限", "资金确认", "叙事退出"],
        "output": "rank_score / expected_alpha_bucket",
    },
    "capital_persistence_multitask_v1": {
        "decision_role": "资金确认",
        "owns": ["资金持续性", "加仓/减仓门", "资金撤退预警"],
        "must_not_own": ["股票最终仓位", "叙事生命周期"],
        "output": "capital_confirmed / flow_decay_risk / add_reduce_gate",
    },
    "leader_lambdarank_v1": {
        "decision_role": "主线龙头识别",
        "owns": ["主题内龙头排序", "主线内集中度建议"],
        "must_not_own": ["市场总仓位", "指数风险"],
        "output": "leader_score / theme_rank",
    },
    "event_impact_assessor_v2": {
        "decision_role": "事件条件影响与可交易性",
        "owns": ["事件相关性", "成本后正向概率", "已定价/延续/反转概率", "事件方向兑现概率", "负面尾部硬否决"],
        "must_not_own": ["常规底仓排序", "组合风险预算"],
        "output": "positive_impact_probability / priced_in_probability / continuation_probability / reversal_probability / direction_realization_probability / abnormal_return_q10_q50_q90 / event_hard_veto",
    },
    "narrative_transition_multitask_v1": {
        "decision_role": "叙事生命周期",
        "owns": ["启动/发酵/高潮/衰退", "叙事结束卖出", "持有周期偏置"],
        "must_not_own": ["个股基础排序", "资金持续性"],
        "output": "narrative_state / transition_prob / exit_signal",
    },
    "portfolio_risk_engine_v1": {
        "decision_role": "仓位与风险预算",
        "owns": ["总仓位上限", "单票上限", "集中度", "减仓优先级"],
        "must_not_own": ["选股排序", "资金叙事解释"],
        "output": "max_equity / max_single_position / risk_budget",
    },
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if n == n else default
    except Exception:
        return default


def _state_policy(market_overview: dict[str, Any]) -> dict[str, Any]:
    state = str((market_overview.get("market_state") or {}).get("state") or "sideways")
    policy = dict(MARKET_STATES.get(state) or FALLBACK_STATE_POLICY.get(state) or FALLBACK_STATE_POLICY["sideways"])
    policy.setdefault("label", (market_overview.get("market_state") or {}).get("label") or state)
    policy["state"] = state
    return policy


def market_position_policy(market_overview: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    policy = _state_policy(market_overview)
    position_gate = market_overview.get("position_gate") if isinstance(market_overview.get("position_gate"), dict) else {}
    multiplier = _num(position_gate.get("combined_risk_budget_multiplier"), _num(policy.get("risk_budget"), 0.65))
    max_equity = min(_num(policy.get("max_equity"), 0.60), max(0.05, multiplier))
    if not bool(position_gate.get("allow_new_exposure", True)):
        max_equity = min(max_equity, _num(ledger.get("position_market_value")) / max(_num(ledger.get("total_equity"), 1.0), 1.0))
    return {
        "state": policy["state"],
        "label": policy.get("label"),
        "max_equity": round(max_equity, 4),
        "current_equity": round(1 - _num(ledger.get("cash_pct")), 4),
        "cash_target": round(1 - max_equity, 4),
        "risk_budget": round(multiplier, 4),
        "max_single_position": round(min(0.18, max(0.06, max_equity / 4)), 4),
        "concentration_limit": round(min(0.55, max(0.25, max_equity * 0.75)), 4),
        "holding_period_bias": "延长持有" if max_equity >= 0.8 else "缩短持有" if max_equity <= 0.45 else "正常轮动",
        "explanation": f"{policy.get('label')}：总股票仓位上限 {max_equity * 100:.0f}%，单票上限约 {min(0.18, max(0.06, max_equity / 4)) * 100:.0f}%。",
    }


def extreme_market_protection(market_overview: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    observations = market_overview.get("observations") if isinstance(market_overview.get("observations"), dict) else {}
    portfolio_risk = market_overview.get("portfolio_risk") if isinstance(market_overview.get("portfolio_risk"), dict) else {}
    state = str((market_overview.get("market_state") or {}).get("state") or "")
    triggers: list[dict[str, str]] = []
    if state in {"S8", "S9", "S10", "bear", "stress"}:
        triggers.append({"code": "index_breakdown", "label": "指数/市场状态破位", "detail": "市场状态进入风险收缩或压力区。"})
    if _num(observations.get("volume_ratio"), 1.0) < 0.65:
        triggers.append({"code": "liquidity_collapse", "label": "流动性坍缩", "detail": "成交额显著低于近期均值。"})
    if _num(observations.get("breadth"), 0.5) < 0.32:
        triggers.append({"code": "breadth_collapse", "label": "市场宽度坍缩", "detail": "上涨家数占比过低。"})
    if _num(observations.get("crowding"), 0.0) > 0.78:
        triggers.append({"code": "crowding_high", "label": "行业/交易拥挤", "detail": "成交拥挤度偏高，容易踩踏。"})
    if str(portfolio_risk.get("status") or "") == "blocked":
        triggers.append({"code": "portfolio_hard_breach", "label": "组合硬约束", "detail": "组合风险已经触发硬约束。"})
    if _num(ledger.get("drawdown")) <= -0.08:
        triggers.append({"code": "account_drawdown", "label": "账户回撤保护", "detail": "账户回撤接近保护线。"})
    severity = "critical" if any(t["code"] in {"portfolio_hard_breach", "index_breakdown"} for t in triggers) and len(triggers) >= 2 else "warning" if triggers else "normal"
    return {
        "active": bool(triggers),
        "severity": severity,
        "triggers": triggers,
        "auto_actions": [] if not triggers else [
            "禁止新增进攻仓位",
            "优先执行减仓/防御调仓",
            "提高现金目标并压低单票上限",
        ],
        "release_conditions": [
            "市场状态离开风险收缩/压力区",
            "成交额与市场宽度恢复到阈值以上",
            "组合风险硬约束解除",
        ],
    }


def model_role_contract() -> dict[str, Any]:
    items = []
    for key, config in PLAN_MODELS.items():
        role = MODEL_ROLE_CONTRACTS.get(key, {})
        items.append({
            "model_key": key,
            "name": config.get("name"),
            "family": config.get("family"),
            "plan_role": config.get("role"),
            **role,
        })
    items.append({"model_key": "portfolio_risk_engine_v1", "name": "组合风险模型", "family": "risk optimizer", **MODEL_ROLE_CONTRACTS["portfolio_risk_engine_v1"]})
    return {"summary": "排序、资金、叙事、事件、风险各管一段；首页只展示合成结论。", "items": items}


def candidate_shadow_ab_status() -> dict[str, Any]:
    try:
        family = model_family_status()
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:160], "items": []}
    items = []
    for item in family.get("items") or []:
        latest = item.get("latest_version") or {}
        active = item.get("active_version") or {}
        latest_metrics = latest.get("metrics") or {}
        sessions = int(_num((latest.get("validation") or {}).get("live_observation_sessions"), _num((latest.get("validation") or {}).get("history_sessions"), 0)))
        strategy_impact = latest_metrics.get("strategy_impact_ab")
        strategy_impact = strategy_impact if isinstance(strategy_impact, dict) else {}
        ready = sessions >= 20
        strategy_passed = str(strategy_impact.get("status") or "") == "passed"
        recommendation = "ready_for_governance" if ready and strategy_passed else "observe"
        items.append({
            "model_key": item.get("model_key"),
            "name": item.get("name"),
            "champion_version": active.get("version"),
            "challenger_version": latest.get("version"),
            "observation_sessions": sessions,
            "minimum_sessions": 20,
            "controlled_strategy_ab_status": str(
                strategy_impact.get("status") or "not_run"
            ),
            "mean_return_delta": strategy_impact.get("mean_return_delta"),
            "recommendation": recommendation,
            "status": "review_ready" if ready else "shadow_running",
        })
    return {
        "ok": True,
        "policy": (
            "候选模型先通过任务原生 OOS 和冻结策略 A/B，再累计至少 "
            "20 个交易日影子证据。"
        ),
        "items": items,
    }


def revenue_target_dashboard(
    *,
    gate_decision: dict[str, Any],
    risk_gate_explanation: dict[str, Any],
    market_overview: dict[str, Any],
    ledger: dict[str, Any],
    recent_execution: dict[str, Any] | None,
    position_policy: dict[str, Any],
    protection: dict[str, Any],
) -> dict[str, Any]:
    needs_reduce = bool(risk_gate_explanation.get("requires_rebalance") or protection.get("active"))
    if needs_reduce:
        action = "先降风险，不新增"
    elif recent_execution:
        action = "已调仓，等待新信号"
    elif gate_decision.get("tone") == "go":
        action = "可生成/执行目标组合"
    else:
        action = "不动，继续观察"
    source = "中期景气排序 + 资金持续确认 + 龙头/事件增强"
    if protection.get("active"):
        source = "当前收益目标让位于极端行情保护"
    elif str((market_overview.get("themes") or {}).get("status")) == "ready":
        top_theme = ((market_overview.get("themes") or {}).get("items") or [{}])[0].get("theme")
        if top_theme:
            source = f"{top_theme} 主线龙头 + 中期景气确认"
    max_risk = protection.get("triggers", [{}])[0].get("label") if protection.get("active") else (risk_gate_explanation.get("constraints") or ["组合回撤与仓位约束"])[0]
    why_not = "" if gate_decision.get("tone") == "go" and not needs_reduce else (risk_gate_explanation.get("headline") or gate_decision.get("detail") or "")
    return {
        "today_action": action,
        "expected_return_source": source,
        "max_risk": max_risk,
        "why_not_move": why_not or "门控通过时由系统生成目标组合；无新增优势则保持原仓位。",
        "position_ceiling": position_policy,
        "risk_budget": risk_gate_explanation.get("risk_budget_multiplier"),
        "next_step": "系统自动处理；你只需要在模型上线/驳回时做人工确认。",
    }


def build_decision_enhancement(
    *,
    gate_decision: dict[str, Any],
    risk_gate_explanation: dict[str, Any],
    market_overview: dict[str, Any],
    ledger: dict[str, Any],
    recent_execution: dict[str, Any] | None,
    include_candidate_shadow_ab: bool = True,
) -> dict[str, Any]:
    position_policy = market_position_policy(market_overview, ledger)
    protection = extreme_market_protection(market_overview, ledger)
    return {
        "schema_version": "decision_enhancement_v1",
        "generated_at": datetime.now().isoformat(),
        "revenue_dashboard": revenue_target_dashboard(
            gate_decision=gate_decision,
            risk_gate_explanation=risk_gate_explanation,
            market_overview=market_overview,
            ledger=ledger,
            recent_execution=recent_execution,
            position_policy=position_policy,
            protection=protection,
        ),
        "market_position_policy": position_policy,
        "model_role_contract": model_role_contract(),
        "candidate_shadow_ab": candidate_shadow_ab_status() if include_candidate_shadow_ab else {
            "ok": True,
            "status": "deferred_to_strategy_lab",
            "items": [],
            "reason": "决策摘要只读快速合同；候选影子 A/B 深度证据位于策略进化页。",
        },
        "extreme_market_protection": protection,
    }
