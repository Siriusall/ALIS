from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.db.store import _conn as business_conn, create_model_version, get_active_model_version, get_model_version, list_model_versions, set_active_model_version, update_model_version_status, update_model_version_validation
from app.services.review_learning import materialize_decision_outcomes
from app.services.risk_limit_contract import DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT, risk_limit_for

STRATEGY_CONTROLLER_VERSION = "strategy_controller_v1"
PRETRADE_CONTROLLER_SCHEMA_VERSION = "alis_strategy_controller_pretrade_v2"
STRATEGY_MODEL_KEY = "strategy_controller"
MINIMUM_CANDIDATE_SHADOW_SESSIONS = 20

DEFAULT_STRATEGY_POLICY = {
    "schema_version": "strategy_policy_v1",
    "market_state_policy": {
        "risk_expansion": {"max_equity": 0.90, "max_single": DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT},
        "sideways": {"max_equity": 0.65, "max_single": 0.10},
        "risk_contraction": {"max_equity": 0.40, "max_single": 0.06},
        "stress": {"max_equity": 0.25, "max_single": 0.04},
    },
    "signal_thresholds": {
        "add_position_score": 80.0,
        "loss_review_threshold": -0.06,
        "profit_trim_threshold": 0.25,
        "risk_weight_gap": 0.01,
    },
    "rebalance_rules": {
        "min_weight_change": 0.01,
        "max_monthly_turnover": 0.30,
        "out_of_pool_action": "sell_exit",
    },
}


@dataclass
class StrategyActionContract:
    symbol: str
    name: str
    action: str
    side: str
    priority: int
    score: float | None
    trigger_models: list[str]
    expected_return_source: str
    holding_period: str
    narrative_lifecycle: str
    capital_confirmation: str
    risk_budget: dict[str, Any]
    exit_conditions: list[str]
    forbidden_conditions: list[str]
    data_status: str
    reason: str
    source: str


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number else None
    except Exception:
        return None


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def active_strategy_policy() -> dict[str, Any]:
    version = get_active_model_version(STRATEGY_MODEL_KEY)
    config = version.get("config") if version else {}
    policy = config.get("policy") if isinstance(config, dict) else {}
    return _deep_merge(DEFAULT_STRATEGY_POLICY, policy if isinstance(policy, dict) else {})


def active_strategy_policy_context() -> dict[str, Any]:
    active = ensure_default_strategy_policy_version()
    return {
        "strategy_policy": active_strategy_policy(),
        "strategy_policy_version": active.get("version") or "default",
        "strategy_policy_version_id": active.get("id"),
    }


def _threshold(policy: dict[str, Any], key: str, default: float) -> float:
    thresholds = policy.get("signal_thresholds") if isinstance(policy.get("signal_thresholds"), dict) else {}
    return _safe_float(thresholds.get(key)) or default


def _action_for_buy(score: float | None, policy: dict[str, Any] | None = None) -> str:
    add_score = _threshold(policy or DEFAULT_STRATEGY_POLICY, "add_position_score", 80.0)
    if score is not None and score >= add_score:
        return "add_position"
    return "buy_candidate"


def _return_source(action: str, row: dict[str, Any], context: dict[str, Any]) -> str:
    if action == "defensive_switch":
        return "风险预算收缩 + 防御资产补位"
    if action in {"sell_exit", "reduce_position"}:
        return "风险改善：释放无效或超配风险预算"
    strategy_template = context.get("strategy_template") if isinstance(context.get("strategy_template"), dict) else {}
    template_name = str(strategy_template.get("name") or strategy_template.get("id") or "").strip()
    if template_name:
        return f"{template_name} + 中期景气排序确认"
    score = _safe_float(row.get("score"))
    if score is not None:
        return "模型排序相对优势 + 组合优化确认"
    return "目标池入选 + 风险预算允许"


def _trigger_models(action: str, row: dict[str, Any]) -> list[str]:
    if action == "defensive_switch":
        return ["portfolio_risk_engine_v1", "market_state_policy_v1", "defensive_asset_policy_v1"]
    signals = row.get("model_signals") if isinstance(row.get("model_signals"), dict) else {}
    models = [
        str(item) for item in (signals.get("trigger_models") or [])
        if str(item)
    ]
    models.append("portfolio_risk_engine_v1")
    return list(dict.fromkeys(models))


def _risk_budget_for(symbol: str, context: dict[str, Any]) -> dict[str, Any]:
    risk_budget = context.get("risk_budget") if isinstance(context.get("risk_budget"), dict) else {}
    target_weights = context.get("target_weights") if isinstance(context.get("target_weights"), dict) else {}
    return {
        "target_position_pct": risk_budget.get("target_position_pct"),
        "max_single_position_pct": risk_budget.get("max_single_position_pct"),
        "target_weight": target_weights.get(symbol),
        "optimizer": context.get("optimizer"),
    }


def _position_cost(position: dict[str, Any]) -> float:
    return _safe_float(position.get("price")) or _safe_float(position.get("cost_price")) or 0.0


def diagnose_position_exit(
    *,
    symbol: str,
    position: dict[str, Any],
    current_price: float,
    current_weight: float | None,
    target_weight: float | None,
    in_target_pool: bool,
    thesis_status: str = "",
    model_degraded: bool = False,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []
    policy = policy or DEFAULT_STRATEGY_POLICY
    cost = _position_cost(position)
    pnl_pct = current_price / cost - 1.0 if cost > 0 and current_price > 0 else 0.0
    current = current_weight if current_weight is not None else 0.0
    target = 0.0 if not in_target_pool else target_weight if target_weight is not None else current
    if not in_target_pool:
        reasons.append({"code": "out_of_target_pool", "label": "不在目标池", "detail": "策略目标池已移除，优先退出或降风险"})
    if str(thesis_status or "") in {"invalidated", "failed"}:
        reasons.append({"code": "thesis_invalidated", "label": "投资逻辑失效", "detail": "叙事/投资逻辑状态已失效"})
    if target + _threshold(policy, "risk_weight_gap", 0.01) < current:
        reasons.append({"code": "risk_budget_shrink", "label": "风险预算收缩", "detail": f"目标权重 {target:.2%} 低于当前 {current:.2%}"})
    if pnl_pct < _threshold(policy, "loss_review_threshold", -0.06):
        reasons.append({"code": "loss_exceeds_expected", "label": "亏损超预期", "detail": f"浮亏 {pnl_pct:.2%}，需要复核买入假设"})
    if model_degraded:
        reasons.append({"code": "model_decay", "label": "模型失效降权", "detail": "生产模型健康监控触发降权"})
    if pnl_pct > _threshold(policy, "profit_trim_threshold", 0.25) and target < current:
        reasons.append({"code": "valuation_overextension", "label": "涨幅/估值透支", "detail": "已有较大盈利且组合目标降权"})
    action = "sell_exit" if any(item["code"] in {"out_of_target_pool", "thesis_invalidated", "model_decay"} for item in reasons) else "reduce_position" if reasons else "hold"
    return {
        "symbol": symbol,
        "action": action,
        "pnl_pct": round(pnl_pct, 6),
        "current_weight": round(current, 6),
        "target_weight": round(target, 6),
        "reasons": reasons or [{"code": "hold_contract_intact", "label": "继续持有", "detail": "目标池、风险预算和退出条件未触发"}],
    }


def _contract(
    *,
    row: dict[str, Any],
    action: str,
    side: str,
    priority: int,
    context: dict[str, Any],
    source: str,
) -> StrategyActionContract:
    symbol = str(row.get("symbol") or "").strip()
    score = _safe_float(row.get("score"))
    holding_period = (
        "风险状态解除前持有或再平衡"
        if action == "defensive_switch"
        else "5-20 sessions" if action == "buy_candidate"
        else "20-60 sessions" if action == "add_position"
        else "继续持有并等待下一次复核" if action == "hold"
        else "立即降风险或退出"
    )
    return StrategyActionContract(
        symbol=symbol,
        name=str(row.get("name") or ""),
        action=action,
        side=side,
        priority=priority,
        score=score,
        trigger_models=_trigger_models(action, row),
        expected_return_source=_return_source(action, row, context),
        holding_period=holding_period,
        narrative_lifecycle=str(row.get("narrative_lifecycle") or ("risk_hedge" if action == "defensive_switch" else "risk_exit" if side == "SELL" else "confirmed_or_watch")),
        capital_confirmation=str(row.get("capital_confirmation") or ("defensive_budget_confirmed" if action == "defensive_switch" else "reduce_required" if side == "SELL" else "model_or_proxy_confirmed")),
        risk_budget=_risk_budget_for(symbol, context),
        exit_conditions=[
            "资金持续性转弱",
            "叙事生命周期进入衰退/失效",
            "模型排序跌出阈值",
            "组合风险预算收缩",
            "交易成本后优势消失",
        ],
        forbidden_conditions=[
            "数据未冻结",
            "无生产模型",
            "停牌/涨跌停不可交易",
            "单票/行业/相关行业超限",
            "流动性容量不足",
        ],
        data_status=str(context.get("data_status") or "frozen"),
        reason=str(row.get("reason") or row.get("detail") or row.get("message") or "遵循策略控制器合成动作"),
        source=source,
    )


def build_strategy_action_contracts(
    *,
    buy_actions: list[dict[str, Any]] | None = None,
    sell_actions: list[dict[str, Any]] | None = None,
    hold_symbols: list[str] | None = None,
    blocked_actions: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ctx = dict(context or {})
    policy = ctx.get("strategy_policy") if isinstance(ctx.get("strategy_policy"), dict) else active_strategy_policy()
    defensive_weights = {
        str(key): float(value or 0.0)
        for key, value in (ctx.get("defensive_weights") or {}).items()
    } if isinstance(ctx.get("defensive_weights"), dict) else {}
    contracts: list[StrategyActionContract] = []
    priority = 1
    for row in sell_actions or []:
        quantity = int(row.get("quantity") or 0)
        action = "sell_exit" if quantity > 0 else "reduce_position"
        contracts.append(_contract(row=row, action=action, side="SELL", priority=priority, context=ctx, source="sell_rebalance"))
        priority += 1
    for row in buy_actions or []:
        action = str(row.get("action") or "") or _action_for_buy(_safe_float(row.get("score")), policy)
        contracts.append(_contract(row=row, action=action, side="BUY", priority=priority, context=ctx, source="buy_rebalance"))
        priority += 1
    for symbol in hold_symbols or []:
        contracts.append(_contract(row={"symbol": symbol, "reason": "仍在目标组合内，继续持有"}, action="hold", side="HOLD", priority=priority, context=ctx, source="target_hold"))
        priority += 1

    blocked = []
    for row in blocked_actions or []:
        blocked.append({
            "symbol": str(row.get("symbol") or ""),
            "side": str(row.get("side") or ""),
            "code": str(row.get("code") or row.get("block_code") or "blocked"),
            "message": str(row.get("message") or row.get("reason") or "策略/交易约束拦截"),
        })

    action_counts = {
        "buy": sum(1 for item in contracts if item.side == "BUY"),
        "sell": sum(1 for item in contracts if item.side == "SELL"),
        "hold": sum(1 for item in contracts if item.side == "HOLD"),
        "blocked": len(blocked),
    }
    trace = [
        {"step": "数据", "status": ctx.get("data_status") or "frozen", "detail": f"信号日 {ctx.get('signal_date') or '—'}，执行日 {ctx.get('execute_date') or '—'}"},
        {"step": "模型", "status": "attached", "detail": "排序、资金、龙头、事件、叙事模型作为策略输入；组合风险模型负责仓位约束"},
        {"step": "策略", "status": "ready", "detail": f"生成买入 {action_counts['buy']}、卖出 {action_counts['sell']}、持有 {action_counts['hold']}、拦截 {action_counts['blocked']}"},
        {"step": "组合", "status": "ready", "detail": f"优化器 {ctx.get('optimizer') or 'pending'}，目标仓位 {ctx.get('target_position_pct') or '—'}"},
        {"step": "执行", "status": "simulation_only", "detail": "仅生成模拟订单与决策快照，不连接券商"},
    ]
    return {
        "schema_version": STRATEGY_CONTROLLER_VERSION,
        "strategy_policy": {"schema_version": policy.get("schema_version"), "version": ctx.get("strategy_policy_version") or "production_or_default"},
        "generated_at": datetime.now().isoformat(),
        "action_counts": action_counts,
        "contracts": [asdict(item) for item in contracts],
        "blocked": blocked,
        "trace": trace,
        "summary": f"策略控制器生成 {len(contracts)} 个动作合同；弱约束与交易拦截 {len(blocked)} 条。",
    }


def build_pretrade_strategy_controller(
    *,
    candidate_symbols: list[str],
    holding_symbols: list[str],
    selections: dict[str, dict[str, Any]] | None = None,
    research_evidence: dict[str, dict[str, Any]] | None = None,
    expected_returns: dict[str, float] | None = None,
    current_weights: dict[str, float] | None = None,
    thesis_status_by_symbol: dict[str, str] | None = None,
    position_pnl_by_symbol: dict[str, float] | None = None,
    defensive_weights: dict[str, float] | None = None,
    position_weight_caps: dict[str, float] | None = None,
    hard_risk_breaches: list[dict[str, Any]] | None = None,
    max_single_position: float = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    risk_limit_contract: dict[str, Any] | None = None,
    execution_guard: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze Controller permissions before portfolio method competition."""

    ctx = {**active_strategy_policy_context(), **dict(context or {})}
    policy = ctx.get("strategy_policy") if isinstance(ctx.get("strategy_policy"), dict) else active_strategy_policy()
    selection_map = dict(selections or {})
    evidence_map = dict(research_evidence or {})
    expected_map = {str(key): float(value or 0.0) for key, value in (expected_returns or {}).items()}
    current = {str(key): max(0.0, float(value or 0.0)) for key, value in (current_weights or {}).items()}
    thesis = {str(key): str(value or "") for key, value in (thesis_status_by_symbol or {}).items()}
    position_pnl = {str(key): float(value or 0.0) for key, value in (position_pnl_by_symbol or {}).items()}
    defensive = {str(key): max(0.0, float(value or 0.0)) for key, value in (defensive_weights or {}).items()}
    position_caps = {
        str(key): max(0.0, min(1.0, float(value or 0.0)))
        for key, value in (position_weight_caps or {}).items()
    }
    hard_breach_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for breach in hard_risk_breaches or []:
        if str(breach.get("severity") or "") != "error":
            continue
        symbol = str(breach.get("symbol") or "")
        if symbol:
            hard_breach_by_symbol.setdefault(symbol, []).append(dict(breach))
    candidates = list(dict.fromkeys(str(value or "").strip() for value in candidate_symbols if str(value or "").strip()))
    holdings = list(dict.fromkeys(str(value or "").strip() for value in holding_symbols if str(value or "").strip()))
    candidate_set = set(candidates)
    holding_set = set(holdings)
    guard = dict(execution_guard or {})
    quarantined = str(guard.get("mode") or "") == "research_only_sell_reduce"
    single_cap = max(0.0, min(1.0, float(max_single_position or 0.0)))
    minimum_change = max(0.0, _safe_float((policy.get("rebalance_rules") or {}).get("min_weight_change")) or 0.01)
    rows: list[dict[str, Any]] = []

    for symbol in list(dict.fromkeys(holdings + candidates + list(defensive))):
        selection = selection_map.get(symbol) or {}
        research = evidence_map.get(symbol) or {}
        model_signals = selection.get("model_signals") if isinstance(selection.get("model_signals"), dict) else {}
        current_weight = current.get(symbol, 0.0)
        expected = expected_map.get(symbol, 0.0)
        is_holding = symbol in holding_set and current_weight > 1e-9
        is_candidate = symbol in candidate_set
        is_defensive = symbol in defensive
        contract_cap = risk_limit_for(risk_limit_contract, symbol, single_cap)
        symbol_cap = min(single_cap, contract_cap, position_caps.get(symbol, single_cap))
        hard_breaches = hard_breach_by_symbol.get(symbol) or []
        hard_weight_breach = bool(hard_breaches) and symbol_cap < current_weight - 1e-8
        thesis_invalid = thesis.get(symbol) in {"invalidated", "failed"}
        research_error = str(research.get("evaluation_status") or "") == "error"
        research_eligible = bool(research.get("eligible_for_core", is_candidate or is_defensive))
        add_eligible = bool(model_signals.get("add_position_eligible", True))
        proposed_action = "hold"
        action = "hold"
        allowed = True
        reason = "继续持有；未触发退出、减仓或加仓条件"
        min_weight = current_weight
        max_weight = current_weight
        intent = "hold"
        permission = {
            "allowed_sides": ["HOLD"],
            "can_buy": False,
            "can_sell": False,
        }
        obligation: dict[str, Any] = {
            "type": "none",
            "required": False,
            "target_max_weight": None,
            "reason_codes": [],
            "residual_breach": {
                "active": False,
                "remaining_excess_weight": 0.0,
                "remaining_excess_amount": 0.0,
                "reason_code": "",
            },
        }

        if is_holding and hard_weight_breach:
            proposed_action = action = "reduce_position"
            min_weight = 0.0
            max_weight = min(current_weight, symbol_cap)
            intent = "reduce_position"
            permission = {"allowed_sides": ["SELL"], "can_buy": False, "can_sell": True}
            obligation = {
                "type": "mandatory_reduce",
                "required": True,
                "target_max_weight": round(max_weight, 8),
                "reason_codes": sorted({str(item.get("code") or "hard_risk") for item in hard_breaches}),
            }
            reason = f"当前权重违反组合硬上限，必须以可执行数量降至 {symbol_cap:.2%} 以内"
        elif is_defensive:
            proposed_action = action = "defensive_switch"
            desired = min(defensive.get(symbol, 0.0), symbol_cap)
            min_weight = 0.0
            max_weight = max(current_weight, desired) if current_weight <= symbol_cap else symbol_cap
            intent = "defensive_switch"
            permission = {"allowed_sides": ["BUY", "SELL", "HOLD"], "can_buy": True, "can_sell": True}
            reason = "市场环境冻结的防御资产目标；仅硬风险上限形成强制义务"
        elif is_holding and research_error:
            allowed = False
            permission = {"allowed_sides": ["HOLD"], "can_buy": False, "can_sell": False}
            reason = "持仓研究刷新异常；缺少可证伪证据时冻结持有，禁止把系统错误解释为卖出信号"
        elif is_holding and (thesis_invalid or not research_eligible or not is_candidate):
            proposed_action = action = "sell_exit"
            min_weight = max_weight = 0.0
            intent = "sell_exit"
            permission = {"allowed_sides": ["SELL"], "can_buy": False, "can_sell": True}
            obligation = {
                "type": "mandatory_exit",
                "required": True,
                "target_max_weight": 0.0,
                "reason_codes": ["thesis_invalidated" if thesis_invalid else "research_admission_failed"],
            }
            reason = "持仓 Thesis 已失效，冻结退出" if thesis_invalid else "持仓未通过当日统一研究准入，冻结退出"
        elif is_holding and position_pnl.get(symbol, 0.0) <= -0.06 and expected <= -0.05:
            proposed_action = action = "sell_exit"
            min_weight = max_weight = 0.0
            intent = "sell_exit"
            permission = {"allowed_sides": ["SELL"], "can_buy": False, "can_sell": True}
            obligation = {
                "type": "mandatory_exit",
                "required": True,
                "target_max_weight": 0.0,
                "reason_codes": ["loss_and_negative_expected_return"],
            }
            reason = "浮亏超过6%且20日预期低于-5%，冻结完整退出"
        elif is_holding and expected < 0:
            proposed_action = action = "reduce_position"
            min_weight = 0.0
            max_weight = current_weight
            intent = "advisory_reduce"
            permission = {"allowed_sides": ["SELL", "HOLD"], "can_buy": False, "can_sell": True}
            reason = "20日预期收益为负，禁止加仓并建议降权；组合可因整手和成本约束保持"
        elif is_holding and is_candidate and add_eligible and expected > minimum_change:
            proposed_action = action = "add_position"
            min_weight = current_weight
            max_weight = max(current_weight, symbol_cap)
            intent = "advisory_add"
            permission = {"allowed_sides": ["BUY", "HOLD"], "can_buy": True, "can_sell": False}
            reason = "持仓继续通过准入且预期收益覆盖最小改善门，允许加仓"
        elif not is_holding and is_candidate and expected > 0:
            proposed_action = action = "buy_candidate"
            min_weight = 0.0
            max_weight = min(symbol_cap, 0.10)
            intent = "advisory_entry"
            permission = {"allowed_sides": ["BUY", "HOLD"], "can_buy": True, "can_sell": False}
            reason = "统一 Top K、研究与动态准入均通过，允许初始建仓"
        elif not is_holding and is_candidate:
            proposed_action = "research_only"
            action = "hold"
            min_weight = max_weight = 0.0
            intent = "research_only"
            permission = {"allowed_sides": ["HOLD"], "can_buy": False, "can_sell": False}
            reason = "研究与动态准入通过，但20日预期收益不为正，只记录研究观察并禁止建仓"

        if quarantined and not is_defensive and action in {"buy_candidate", "add_position"}:
            allowed = False
            min_weight = current_weight if is_holding else 0.0
            max_weight = current_weight if is_holding else 0.0
            intent = "research_only" if not is_holding else "hold"
            permission = {"allowed_sides": ["HOLD"], "can_buy": False, "can_sell": False}
            reason = "当前 Champion 正式 OOS 失败；冻结个股买入和加仓权限"

        source_row = {
            "symbol": symbol,
            "name": str(selection.get("name") or ""),
            "score": selection.get("score"),
            "reason": reason,
            "model_signals": model_signals,
        }
        contract = asdict(_contract(
            row=source_row,
            action=action,
            side="BUY" if action in {"buy_candidate", "add_position", "defensive_switch"} else "SELL" if action in {"reduce_position", "sell_exit"} else "HOLD",
            priority=len(rows) + 1,
            context={**ctx, "target_weights": {symbol: max_weight}, "optimizer": "pending_controller_bounded_competition"},
            source="pretrade_unified_decision",
        ))
        rows.append({
            **contract,
            "proposed_action": proposed_action,
            "permission_allowed": allowed,
            "current_weight": round(current_weight, 8),
            "min_weight": round(min_weight, 8),
            "max_weight": round(max_weight, 8),
            "expected_20d_return_before_trade_cost": round(expected, 8),
            "research_eligible": research_eligible,
            "is_defensive_asset": is_defensive,
            "permission": permission,
            "intent": intent,
            "obligation": obligation,
            "position_weight_cap": round(symbol_cap, 8),
        })

    bounds = {
        item["symbol"]: {
            "min_weight": item["min_weight"],
            "max_weight": item["max_weight"],
            "action": item["action"],
            "permission_allowed": item["permission_allowed"],
            "permission": item["permission"],
            "intent": item["intent"],
            "obligation": item["obligation"],
        }
        for item in rows
    }
    return {
        "schema_version": PRETRADE_CONTROLLER_SCHEMA_VERSION,
        "generated_at": str(ctx.get("data_as_of_time") or datetime.now().isoformat()),
        "strategy_policy_version": ctx.get("strategy_policy_version") or "production_or_default",
        "strategy_policy_version_id": ctx.get("strategy_policy_version_id"),
        "execution_mode": str(guard.get("mode") or "normal"),
        "champion_execution_guard": guard,
        "actions": rows,
        "weight_bounds": bounds,
        "risk_limit_contract": risk_limit_contract or {},
        "policy": {
            "controller_precedes_optimizer": True,
            "optimizer_may_not_change_action_direction": True,
            "mandatory_obligations_precede_objective": True,
            "negative_expected_return_new_entry_forbidden": True,
            "quarantine_equity_buy_add_forbidden": quarantined,
        },
    }


def build_target_weight_explanations(
    *,
    target_weights: dict[str, float],
    current_weights: dict[str, float] | None = None,
    score_map: dict[str, float] | None = None,
    returns_by_symbol: dict[str, list[float]] | None = None,
    liquidity_caps: dict[str, float] | None = None,
    defensive_weights: dict[str, float] | None = None,
    optimizer: str = "",
    risk_budget: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = {str(k): float(v or 0.0) for k, v in (current_weights or {}).items()}
    scores = {str(k): float(v or 0.0) for k, v in (score_map or {}).items()}
    returns = returns_by_symbol or {}
    liquidity = {str(k): float(v or 0.0) for k, v in (liquidity_caps or {}).items()}
    defensive = {str(k): float(v or 0.0) for k, v in (defensive_weights or {}).items()}
    max_single = _safe_float((risk_budget or {}).get("max_single_position_pct")) or _safe_float((constraints or {}).get("max_single_position_pct"))
    items: list[dict[str, Any]] = []
    for symbol, raw_weight in sorted((target_weights or {}).items(), key=lambda kv: (-float(kv[1] or 0.0), str(kv[0]))):
        target = max(0.0, float(raw_weight or 0.0))
        current_weight = max(0.0, current.get(str(symbol), 0.0))
        delta = target - current_weight
        is_defensive = str(symbol) in defensive
        sample_count = len(returns.get(str(symbol)) or [])
        reasons = []
        reasons.append("防御资产固定袖套" if is_defensive else "策略目标池入选")
        reasons.append("组合优化器分配目标权重")
        if max_single is not None and target >= max_single - 1e-6:
            reasons.append("受单票上限约束")
        if sample_count < 20:
            reasons.append("历史收益样本偏少，置信度降级")
        if liquidity.get(str(symbol), 0.0) > 0:
            reasons.append("流动性容量参与订单上限")
        items.append({
            "symbol": str(symbol),
            "target_weight": round(target, 6),
            "current_weight": round(current_weight, 6),
            "weight_delta": round(delta, 6),
            "action_hint": "increase" if delta > 0.002 else "decrease" if delta < -0.002 else "hold",
            "asset_role": "defensive" if is_defensive else "equity",
            "score": round(scores.get(str(symbol), 0.0), 6) if str(symbol) in scores else None,
            "return_sample_count": sample_count,
            "liquidity_capacity": round(liquidity.get(str(symbol), 0.0), 2) if str(symbol) in liquidity else None,
            "optimizer": optimizer,
            "why_this_weight": "；".join(reasons),
            "components": {
                "strategy_signal": "defensive_switch" if is_defensive else "selected_by_ranking_or_target_pool",
                "risk_budget": risk_budget or {},
                "constraints": constraints or {},
                "cost_liquidity": {"liquidity_cap": liquidity.get(str(symbol))},
            },
        })
    return {
        "schema_version": "target_weight_explanation_v1",
        "optimizer": optimizer,
        "count": len(items),
        "items": items,
    }


def build_strategy_rebalance_actions(
    *,
    target_symbols: list[str],
    held_positions: dict[str, dict[str, Any]],
    selection_map: dict[str, dict[str, Any]] | None = None,
    market_rows: dict[str, Any] | None = None,
    same_day_fallback: bool = False,
    mode: str = "full_rebalance",
    blocked_actions: list[dict[str, Any]] | None = None,
    current_weights: dict[str, float] | None = None,
    target_weights: dict[str, float] | None = None,
    thesis_status_by_symbol: dict[str, str] | None = None,
    model_degraded: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ctx = dict(context or {})
    policy = ctx.get("strategy_policy") if isinstance(ctx.get("strategy_policy"), dict) else active_strategy_policy()
    selections = dict(selection_map or {})
    rows = dict(market_rows or {})
    targets = [str(symbol).strip() for symbol in target_symbols if str(symbol).strip()]
    target_set = set(targets)
    held = {str(symbol).strip(): dict(pos or {}) for symbol, pos in held_positions.items() if str(symbol).strip()}
    current_weight_map = {str(key): float(value or 0.0) for key, value in (current_weights or {}).items()}
    target_weight_map = {str(key): float(value or 0.0) for key, value in (target_weights or {}).items()}
    defensive_weights = {
        str(key): float(value or 0.0)
        for key, value in (ctx.get("defensive_weights") or {}).items()
    } if isinstance(ctx.get("defensive_weights"), dict) else {}
    thesis_map = {str(key): str(value or "") for key, value in (thesis_status_by_symbol or {}).items()}
    buy_actions: list[dict[str, Any]] = []
    sell_actions: list[dict[str, Any]] = []

    for symbol in targets:
        if symbol in held:
            continue
        selection = selections.get(symbol) or {}
        model_signals = selection.get("model_signals") if isinstance(selection.get("model_signals"), dict) else {}
        narrative = model_signals.get("narrative") if isinstance(model_signals.get("narrative"), dict) else {}
        add_eligible = bool(model_signals.get("add_position_eligible", True))
        requested_action = _action_for_buy(_safe_float(selection.get("score")), policy)
        if requested_action == "add_position" and not add_eligible:
            requested_action = "buy_candidate"
        row = rows.get(symbol)
        buy_actions.append({
            "symbol": symbol,
            "name": selection.get("name") or str(getattr(row, "name", "") or ""),
            "action": "defensive_switch" if symbol in defensive_weights else requested_action,
            "score": selection.get("score"),
            "reason": (
                "策略总控：防御资产补位，等待组合层分配权重"
                if symbol in defensive_weights
                else "策略总控：生产模型合成通过，等待组合层分配权重"
            ),
            "price": ctx.get("price_by_symbol", {}).get(symbol) if isinstance(ctx.get("price_by_symbol"), dict) else None,
            "close": round(float(getattr(row, "close", 0) or 0), 4) if row else None,
            "change_pct": float(getattr(row, "change_pct", 0) or 0) if row else 0.0,
            "volume": float(getattr(row, "volume", 0) or 0) if row else 0.0,
            "capital_confirmation": (
                "defensive_budget_confirmed"
                if symbol in defensive_weights
                else str(model_signals.get("capital_confirmation") or "model_or_proxy_confirmed")
            ),
            "narrative_lifecycle": (
                "risk_hedge"
                if symbol in defensive_weights
                else str(narrative.get("lifecycle") or "confirmed_or_watch")
            ),
            "model_signals": model_signals,
        })

    if str(mode or "full_rebalance").lower() == "full_rebalance":
        for symbol, position in sorted(held.items()):
            row = rows.get(symbol)
            current_price = 0.0
            if isinstance(ctx.get("price_by_symbol"), dict):
                current_price = _safe_float(ctx["price_by_symbol"].get(symbol)) or 0.0
            if current_price <= 0 and row:
                current_price = _safe_float(getattr(row, "close", 0)) or 0.0
            diagnosis = diagnose_position_exit(
                symbol=symbol,
                position=position,
                current_price=current_price,
                current_weight=current_weight_map.get(symbol),
                target_weight=target_weight_map.get(symbol),
                in_target_pool=symbol in target_set,
                thesis_status=thesis_map.get(symbol, ""),
                model_degraded=model_degraded,
                policy=policy,
            )
            if diagnosis["action"] == "hold":
                continue
            primary_reason = (diagnosis.get("reasons") or [{}])[0]
            sell_actions.append({
                "symbol": symbol,
                "name": str(position.get("name") or ""),
                "quantity": int(position.get("quantity") or 0),
                "reason": f"策略总控：{primary_reason.get('label') or '触发退出条件'}",
                "detail": primary_reason.get("detail") or "",
                "price": ctx.get("price_by_symbol", {}).get(symbol) if isinstance(ctx.get("price_by_symbol"), dict) else None,
                "close": round(float(getattr(row, "close", 0) or 0), 4) if row else None,
                "pnl_pct": diagnosis.get("pnl_pct"),
                "current_weight": diagnosis.get("current_weight"),
                "target_weight": diagnosis.get("target_weight"),
                "exit_diagnostics": diagnosis.get("reasons") or [],
                "capital_confirmation": "reduce_required",
                "narrative_lifecycle": "risk_exit" if symbol not in target_set else "deleveraging",
            })

    hold_symbols = sorted(symbol for symbol in held if symbol in target_set)
    strategy_controller = build_strategy_action_contracts(
        buy_actions=buy_actions,
        sell_actions=sell_actions,
        hold_symbols=hold_symbols,
        blocked_actions=blocked_actions or [],
        context=ctx,
    )
    return {
        "buy": buy_actions,
        "sell": sell_actions,
        "hold": hold_symbols,
        "blocked": list(blocked_actions or []),
        "strategy_controller": strategy_controller,
        "strategy_action_contracts": strategy_controller["contracts"],
        "rebalance_decision_trace": strategy_controller["trace"],
    }


def ensure_default_strategy_policy_version() -> dict[str, Any]:
    active = get_active_model_version(STRATEGY_MODEL_KEY)
    if active:
        return active
    version = f"strategy_controller_default-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    version_id = create_model_version(
        model_key=STRATEGY_MODEL_KEY,
        version=version,
        status="active",
        artifact_path="builtin://strategy_controller/default_policy",
        config={"policy": DEFAULT_STRATEGY_POLICY},
        metrics={"source": "default_policy", "cost_after_backplay": None},
        validation={"status": "passed", "production_eligible": True, "review": {"status": "system_bootstrap"}},
        is_active=True,
    )
    return get_model_version(version_id) or {"id": version_id, "version": version, "config": {"policy": DEFAULT_STRATEGY_POLICY}, "is_active": True}


def strategy_controller_status() -> dict[str, Any]:
    active = ensure_default_strategy_policy_version()
    versions = list_model_versions(STRATEGY_MODEL_KEY, limit=20)
    visible_versions = [item for item in versions if item.get("is_active")][:1] + [item for item in versions if not item.get("is_active")][:2]
    return {
        "ok": True,
        "schema_version": "strategy_controller_status_v1",
        "model_key": STRATEGY_MODEL_KEY,
        "active_version": active,
        "policy": active_strategy_policy(),
        "versions": visible_versions,
        "version_count": len(versions),
        "generated_at": datetime.now().isoformat(),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _recent_strategy_patches(limit: int = 50) -> set[str]:
    patches: set[str] = set()
    for version in list_model_versions(STRATEGY_MODEL_KEY, limit=limit):
        config = version.get("config") if isinstance(version.get("config"), dict) else {}
        patch = config.get("patch") if isinstance(config, dict) else None
        if isinstance(patch, dict) and patch:
            patches.add(_canonical_json(patch))
    return patches


def create_strategy_policy_candidate(
    policy_patch: dict[str, Any], *, comment: str = "", learning_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = active_strategy_policy()
    policy = _deep_merge(base, policy_patch or {})
    version = f"strategy_controller_candidate-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    validation = {
        "status": "candidate",
        "production_eligible": False,
        "blocked_reasons": ["requires_backtest_and_human_review"],
        "review": {"status": "pending", "comment": comment},
    }
    version_id = create_model_version(
        model_key=STRATEGY_MODEL_KEY,
        version=version,
        status="candidate",
        artifact_path="builtin://strategy_controller/policy_candidate",
        config={
            "policy": policy,
            "patch": policy_patch or {},
            "comment": comment,
            "learning_evidence": learning_evidence or {},
        },
        metrics={"requires_backtest": True},
        validation=validation,
        is_active=False,
    )
    evaluation = evaluate_strategy_policy_version(version_id)
    return {"ok": True, "version": get_model_version(version_id), "evaluation": evaluation}


def _learning_signal_suggestions(values: dict[str, Any], context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map durable review signals to bounded candidate hypotheses.

    Data-quality findings are deliberately routed to governance rather than
    changed into a trading parameter.  Every returned patch remains a
    candidate and is evaluated through the existing validation gates.
    """
    signals = {
        str(item.get("code") or "")
        for item in (context.get("learning_signals") or [])
        if isinstance(item, dict) and str(item.get("severity") or "") in {"warning", "critical"}
    }
    errors = context.get("error_breakdown") if isinstance(context.get("error_breakdown"), dict) else {}
    error_types = errors.get("review_error_types") if isinstance(errors.get("review_error_types"), dict) else {}
    evidence = {
        "source": "periodic_performance_review_v1",
        "period": context.get("period"),
        "signals": sorted(signals),
        "error_types": error_types,
        "benchmark_coverage": context.get("benchmark_coverage") or {},
    }
    suggestions: list[dict[str, Any]] = []
    governance_actions: list[dict[str, Any]] = []
    if "high_turnover" in signals:
        suggestions.append({
            "name": "复盘驱动：收紧月度换手上限",
            "reason": "月度复盘识别高换手，生成更严格的成本控制候选",
            "patch": {"rebalance_rules": {"max_monthly_turnover": max(0.05, float(values["max_monthly_turnover"]) - 0.05)}},
            "learning_evidence": evidence,
        })
    if "negative_excess_return" in signals:
        suggestions.append({
            "name": "复盘驱动：提高新仓确认门槛",
            "reason": "周期相对基准为负，生成更高确认阈值的保守候选",
            "patch": {"signal_thresholds": {"add_position_score": min(95.0, float(values["add_position_score"]) + 3.0)}},
            "learning_evidence": evidence,
        })
        expansion = values.get("market_state_policy", {}).get("risk_expansion", {}) if isinstance(values.get("market_state_policy"), dict) else {}
        current_expansion_equity = _safe_float(expansion.get("max_equity")) or 0.90
        if current_expansion_equity < 1.0:
            suggestions.append({
                "name": "复盘驱动：扩张期满权益暴露",
                "reason": "正式 OOS 的主要差距指向上涨市场暴露不足；只在风险扩张状态将权益上限提高至100%，其余风险状态、单票上限与换手约束保持不变。",
                "patch": {"market_state_policy": {"risk_expansion": {"max_equity": 1.0}}},
                "learning_evidence": {**evidence, "hypothesis": "risk_expansion_full_equity_only"},
            })
    if "review_errors" in signals and int(error_types.get("模型失效") or 0) > 0:
        suggestions.append({
            "name": "复盘驱动：模型失效更早复核",
            "reason": "复核归因为模型失效，生成更早触发亏损复核的候选",
            "patch": {"signal_thresholds": {"loss_review_threshold": round(max(-0.15, float(values["loss_review_threshold"]) + 0.01), 6)}},
            "learning_evidence": evidence,
        })
    if int(error_types.get("数据质量") or 0) > 0:
        governance_actions.append({
            "target": "data_governance",
            "code": "review_data_quality_errors",
            "count": int(error_types.get("数据质量") or 0),
            "reason": "数据质量错误不得被转化为交易参数候选；保留给硬数据与数据质量治理。",
        })
    return suggestions, governance_actions


def run_strategy_policy_auto_optimization(*, force: bool = False) -> dict[str, Any]:
    active = ensure_default_strategy_policy_version()
    policy = active_strategy_policy()
    values = _policy_bounds(policy)["values"]
    baseline_suggestions = [
        {
            "name": "提高买入确认门槛",
            "reason": "减少弱信号新仓，降低无效换手",
            "patch": {"signal_thresholds": {"add_position_score": min(95.0, float(values["add_position_score"]) + 2.0)}},
        },
        {
            "name": "亏损复核更敏感",
            "reason": "更早识别买入假设失效",
            "patch": {"signal_thresholds": {"loss_review_threshold": max(-0.15, float(values["loss_review_threshold"]) + 0.005)}},
        },
        {
            "name": "降权触发更及时",
            "reason": "风险预算收缩时更快调整持仓",
            "patch": {"signal_thresholds": {"risk_weight_gap": max(0.002, float(values["risk_weight_gap"]) - 0.002)}},
        },
        {
            "name": "止盈再平衡更主动",
            "reason": "盈利且目标降权时更快释放估值透支风险",
            "patch": {"signal_thresholds": {"profit_trim_threshold": max(0.08, float(values["profit_trim_threshold"]) - 0.02)}},
        },
        {
            "name": "月度换手上限收紧",
            "reason": "费用后回放优先控制交易成本",
            "patch": {"rebalance_rules": {"max_monthly_turnover": max(0.05, float(values["max_monthly_turnover"]) - 0.03)}},
        },
    ]
    try:
        from app.services.performance_reports import latest_performance_learning_context
        learning_context = latest_performance_learning_context()
    except Exception as exc:
        learning_context = {"available": False, "reason": f"performance_context_unavailable:{str(exc)[:160]}"}
    learning_suggestions, governance_actions = _learning_signal_suggestions(values, learning_context)
    # Preserve the existing periodic candidate exploration and add only the
    # signal-specific hypotheses that have durable evidence.
    suggestions = baseline_suggestions + learning_suggestions
    existing = _recent_strategy_patches()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for suggestion in suggestions:
        patch_key = _canonical_json(suggestion["patch"])
        if not force and patch_key in existing:
            skipped.append({"name": suggestion["name"], "reason": "recent_duplicate_patch", "patch": suggestion["patch"]})
            continue
        result = create_strategy_policy_candidate(
            suggestion["patch"],
            comment=f"auto_optimization: {suggestion['name']}；{suggestion['reason']}",
            learning_evidence=suggestion.get("learning_evidence"),
        )
        version = result.get("version") or {}
        evaluation = result.get("evaluation") or {}
        created.append({
            "name": suggestion["name"],
            "reason": suggestion["reason"],
            "patch": suggestion["patch"],
            "version_id": version.get("id"),
            "version": version.get("version"),
            "status": evaluation.get("status") or version.get("status"),
            "metrics": evaluation.get("metrics") or version.get("metrics"),
            "validation": evaluation.get("validation") or version.get("validation"),
        })
    return {
        "ok": True,
        "schema_version": "strategy_policy_auto_optimization_v1",
        "active_version_id": active.get("id"),
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created": created,
        "skipped": skipped,
        "learning_context": learning_context,
        "learning_candidate_count": len(learning_suggestions),
        "governance_actions": governance_actions,
        "note": "只生成候选并自动评估；弱于生产、样本不足或越界会自动拦截，不自动上线。",
        "generated_at": datetime.now().isoformat(),
    }


def rollback_strategy_policy_version(version_id: int, *, reviewer: str = "operator", comment: str = "") -> dict[str, Any]:
    version = get_model_version(int(version_id))
    if not version or version.get("model_key") != STRATEGY_MODEL_KEY:
        return {"ok": False, "error": "strategy_policy_version_not_found", "version_id": version_id}
    validation = dict(version.get("validation") or {})
    validation_status = str(validation.get("status") or "")
    review_status = str((validation.get("review") or {}).get("status") or "")
    if validation_status != "passed" and review_status not in {"approved", "system_bootstrap"}:
        return {
            "ok": False,
            "error": "strategy_policy_not_rollback_eligible",
            "version_id": int(version_id),
            "validation_status": validation_status,
            "review_status": review_status,
        }
    validation["review"] = {
        "status": "rollback_activated",
        "reviewer": reviewer,
        "comment": comment,
        "reviewed_at": datetime.now().isoformat(),
    }
    validation["production_eligible"] = True
    validation["status"] = "passed"
    update_model_version_validation(int(version_id), validation=validation)
    set_active_model_version(int(version_id))
    return {"ok": True, "version": get_model_version(int(version_id))}


def _policy_bounds(policy: dict[str, Any]) -> dict[str, Any]:
    thresholds = policy.get("signal_thresholds") if isinstance(policy.get("signal_thresholds"), dict) else {}
    rebalance = policy.get("rebalance_rules") if isinstance(policy.get("rebalance_rules"), dict) else {}
    market = policy.get("market_state_policy") if isinstance(policy.get("market_state_policy"), dict) else {}
    add_score = _threshold(policy, "add_position_score", 80.0)
    loss = _threshold(policy, "loss_review_threshold", -0.06)
    profit = _threshold(policy, "profit_trim_threshold", 0.25)
    gap = _threshold(policy, "risk_weight_gap", 0.01)
    max_turnover = _safe_float(rebalance.get("max_monthly_turnover")) or 0.30
    max_equity_values = [
        _safe_float((item or {}).get("max_equity")) or 0.0
        for item in market.values()
        if isinstance(item, dict)
    ]
    gates = {
        "add_position_score_range": 50.0 <= add_score <= 95.0,
        "loss_review_threshold_range": -0.15 <= loss <= -0.02,
        "profit_trim_threshold_range": 0.08 <= profit <= 0.60,
        "risk_weight_gap_range": 0.002 <= gap <= 0.05,
        "max_monthly_turnover_range": 0.05 <= max_turnover <= 0.60,
        "max_equity_range": all(0.05 <= value <= 1.0 for value in max_equity_values) if max_equity_values else True,
    }
    return {
        "ok": all(gates.values()),
        "gates": gates,
        "blocked_reasons": [key for key, passed in gates.items() if not passed],
        "values": {
            "add_position_score": add_score,
            "loss_review_threshold": loss,
            "profit_trim_threshold": profit,
            "risk_weight_gap": gap,
            "max_monthly_turnover": max_turnover,
            "max_equity_max": max(max_equity_values) if max_equity_values else None,
            "market_state_policy": market,
        },
    }


def _policy_aggressiveness_penalty(policy: dict[str, Any], champion_policy: dict[str, Any]) -> float:
    values = _policy_bounds(policy)["values"]
    champion = _policy_bounds(champion_policy)["values"]
    penalty = 0.0
    penalty += max(0.0, float(champion["add_position_score"]) - float(values["add_position_score"])) * 0.00025
    penalty += max(0.0, abs(float(values["loss_review_threshold"])) - abs(float(champion["loss_review_threshold"]))) * 0.04
    penalty += max(0.0, float(values["profit_trim_threshold"]) - float(champion["profit_trim_threshold"])) * 0.01
    penalty += max(0.0, float(values["risk_weight_gap"]) - float(champion["risk_weight_gap"])) * 0.08
    penalty += max(0.0, float(values["max_monthly_turnover"]) - float(champion["max_monthly_turnover"])) * 0.01
    return round(penalty, 8)


def _historical_policy_replay_metrics(policy: dict[str, Any], champion_policy: dict[str, Any]) -> dict[str, Any]:
    materialize_decision_outcomes(limit=1000)
    with business_conn() as c:
        rows = [dict(row) for row in c.execute(
            """SELECT horizon_sessions,directional_return,correct,entry_date
               FROM decision_outcome_attributions
               WHERE horizon_sessions IN (5,20,60)
               ORDER BY entry_date,id"""
        ).fetchall()]
    mature = [row for row in rows if int(row.get("horizon_sessions") or 0) == 20]
    if not mature:
        return {
            "sample_rows": 0,
            "net_lift": 0.0,
            "walk_forward_lift": 0.0,
            "worst_window_net_lift": 0.0,
            "hit_rate": 0.0,
            "blocked_reasons": ["missing_mature_decision_outcomes"],
        }
    cost = 0.0025
    penalty = _policy_aggressiveness_penalty(policy, champion_policy)
    net_returns = [float(row.get("directional_return") or 0.0) - cost - penalty for row in mature]
    hit_rate = sum(1 for value in net_returns if value > 0) / len(net_returns)
    windows: list[float] = []
    window_count = min(4, len(net_returns))
    if window_count:
        size = max(1, len(net_returns) // window_count)
        for index in range(0, len(net_returns), size):
            chunk = net_returns[index:index + size]
            if chunk:
                windows.append(sum(chunk) / len(chunk))
    return {
        "sample_rows": len(mature),
        "net_lift": round(sum(net_returns) / len(net_returns), 8),
        "walk_forward_lift": round(sum(windows) / len(windows), 8) if windows else 0.0,
        "worst_window_net_lift": round(min(windows), 8) if windows else 0.0,
        "hit_rate": round(hit_rate, 6),
        "transaction_cost_bps": 25,
        "aggressiveness_penalty": penalty,
        "window_net_lifts": [round(value, 8) for value in windows],
        "blocked_reasons": [],
    }


def _missing_candidate_shadow_metrics() -> dict[str, Any]:
    """Never use Champion outcomes as evidence for a policy Challenger."""
    return {
        "sample_rows": 0,
        "net_lift": 0.0,
        "walk_forward_lift": 0.0,
        "worst_window_net_lift": 0.0,
        "hit_rate": 0.0,
        "shadow_sessions": 0,
        "transaction_cost_bps": 25,
        "aggressiveness_penalty": 0.0,
        "evaluation_scope": "candidate_specific_shadow_required",
        "blocked_reasons": ["missing_candidate_specific_shadow_outcomes"],
    }


def _shadow_action_for_decision(
    decision: dict[str, Any], policy: dict[str, Any],
) -> tuple[str, bool, float | None]:
    """Apply a candidate policy to one immutable Champion decision opportunity."""
    research = decision.get("research") if isinstance(decision.get("research"), dict) else {}
    contract = decision.get("target_weight_contract") if isinstance(decision.get("target_weight_contract"), dict) else {}
    score = _safe_float(research.get("score") or contract.get("score"))
    kind = str(decision.get("decision_type") or "").upper()
    if kind in {"BUY", "ADD"}:
        if score is None:
            return "observe_missing_score", False, None
        action = _action_for_buy(score, policy)
        return action, action == "add_position", score
    if kind in {"SELL", "REDUCE", "EXIT"}:
        return "sell_exit", True, score
    return "hold", False, score


def _ensure_strategy_policy_shadow_tables() -> None:
    with business_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS strategy_policy_shadow_observations (
            id BIGSERIAL PRIMARY KEY,
            candidate_version_id BIGINT NOT NULL,
            decision_snapshot_id BIGINT NOT NULL,
            decision_time VARCHAR(64) NOT NULL,
            decision_type VARCHAR(32) NOT NULL,
            shadow_action VARCHAR(32) NOT NULL,
            would_trade INTEGER NOT NULL DEFAULT 0,
            score DOUBLE PRECISION,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            UNIQUE(candidate_version_id,decision_snapshot_id)
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS idx_strategy_policy_shadow_candidate
            ON strategy_policy_shadow_observations(candidate_version_id,decision_time)""")


def materialize_strategy_policy_shadow_observations(*, limit_candidates: int = 30) -> dict[str, Any]:
    """Persist candidate-specific, post-creation shadow actions without touching the account."""
    _ensure_strategy_policy_shadow_tables()
    candidates = [
        item for item in list_model_versions(STRATEGY_MODEL_KEY, limit=max(1, min(int(limit_candidates), 200)))
        if not bool(item.get("is_active")) and str(item.get("status") or "") == "candidate"
    ]
    written = 0
    opportunities = 0
    now = datetime.now().isoformat()
    with business_conn() as c:
        for candidate in candidates:
            policy = ((candidate.get("config") or {}).get("policy") or DEFAULT_STRATEGY_POLICY)
            created_at = str(candidate.get("created_at") or "")
            rows = [dict(row) for row in c.execute(
                """SELECT id,decision_time,decision_type,research_state_json,target_weight_contract_json
                   FROM decision_snapshots WHERE decision_time>=? ORDER BY decision_time,id""",
                (created_at,),
            ).fetchall()]
            for row in rows:
                try:
                    research = json.loads(row.get("research_state_json") or "{}")
                    contract = json.loads(row.get("target_weight_contract_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    research, contract = {}, {}
                action, would_trade, score = _shadow_action_for_decision(
                    {**row, "research": research, "target_weight_contract": contract}, policy,
                )
                evidence = {
                    "schema_version": "strategy_policy_shadow_v1",
                    "candidate_version": candidate.get("version"),
                    "candidate_created_at": created_at,
                    "source": "immutable_champion_decision_opportunity",
                    "account_effect": "none",
                }
                c.execute(
                    """INSERT INTO strategy_policy_shadow_observations(
                        candidate_version_id,decision_snapshot_id,decision_time,decision_type,
                        shadow_action,would_trade,score,evidence_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_version_id,decision_snapshot_id) DO NOTHING""",
                    (
                        int(candidate["id"]), int(row["id"]), str(row.get("decision_time") or ""),
                        str(row.get("decision_type") or ""), action, int(would_trade), score,
                        json.dumps(evidence, ensure_ascii=False), now,
                    ),
                )
                written += 1
                opportunities += 1
    return {
        "ok": True,
        "schema_version": "strategy_policy_shadow_v1",
        "candidate_count": len(candidates),
        "opportunities_seen": opportunities,
        "observations_written": written,
        "policy": "candidate_specific_post_creation_shadow_only; no_account_effect",
    }


def _candidate_shadow_metrics(
    candidate_version_id: int, policy: dict[str, Any], champion_policy: dict[str, Any],
) -> dict[str, Any]:
    _ensure_strategy_policy_shadow_tables()
    with business_conn() as c:
        rows = [dict(row) for row in c.execute(
            """SELECT s.decision_time,a.directional_return,a.correct
               FROM strategy_policy_shadow_observations s
               JOIN decision_outcome_attributions a ON a.decision_snapshot_id=s.decision_snapshot_id
                 AND a.horizon_sessions=20
               WHERE s.candidate_version_id=? AND s.would_trade=1
               ORDER BY s.decision_time,s.id""",
            (int(candidate_version_id),),
        ).fetchall()]
    if not rows:
        return _missing_candidate_shadow_metrics()
    cost = 0.0025
    penalty = _policy_aggressiveness_penalty(policy, champion_policy)
    net_returns = [float(row.get("directional_return") or 0.0) - cost - penalty for row in rows]
    window_count = min(4, len(net_returns))
    size = max(1, len(net_returns) // window_count) if window_count else 1
    windows = [sum(net_returns[index:index + size]) / len(net_returns[index:index + size]) for index in range(0, len(net_returns), size)]
    return {
        "sample_rows": len(rows),
        "shadow_sessions": len({str(row.get("decision_time") or "")[:10] for row in rows}),
        "net_lift": round(sum(net_returns) / len(net_returns), 8),
        "walk_forward_lift": round(sum(windows) / len(windows), 8) if windows else 0.0,
        "worst_window_net_lift": round(min(windows), 8) if windows else 0.0,
        "hit_rate": round(sum(value > 0 for value in net_returns) / len(net_returns), 6),
        "transaction_cost_bps": 25,
        "aggressiveness_penalty": penalty,
        "window_net_lifts": [round(value, 8) for value in windows],
        "evaluation_scope": "candidate_specific_shadow_20d",
        "blocked_reasons": (
            [] if len({str(row.get("decision_time") or "")[:10] for row in rows}) >= MINIMUM_CANDIDATE_SHADOW_SESSIONS
            else ["minimum_candidate_shadow_sessions"]
        ),
    }


def evaluate_strategy_policy_version(version_id: int) -> dict[str, Any]:
    version = get_model_version(int(version_id))
    if not version or version.get("model_key") != STRATEGY_MODEL_KEY:
        return {"ok": False, "error": "strategy_policy_version_not_found", "version_id": version_id}
    active = get_active_model_version(STRATEGY_MODEL_KEY)
    policy = ((version.get("config") or {}).get("policy") or DEFAULT_STRATEGY_POLICY)
    champion_policy = ((active or {}).get("config") or {}).get("policy") or DEFAULT_STRATEGY_POLICY
    bounds = _policy_bounds(policy)
    metrics = (
        _historical_policy_replay_metrics(policy, champion_policy)
        if bounds["ok"] and bool(version.get("is_active")) else _candidate_shadow_metrics(int(version_id), policy, champion_policy)
    ) if bounds["ok"] else {
        "sample_rows": 0,
        "net_lift": 0.0,
        "walk_forward_lift": 0.0,
        "worst_window_net_lift": 0.0,
        "hit_rate": 0.0,
        "transaction_cost_bps": 25,
        "aggressiveness_penalty": 0.0,
        "blocked_reasons": bounds["blocked_reasons"],
    }
    shadow_sessions_ready = bool(version.get("is_active")) or (
        int(metrics.get("shadow_sessions") or 0) >= MINIMUM_CANDIDATE_SHADOW_SESSIONS
    )
    passed_metrics = (
        float(metrics.get("net_lift") or 0.0) > 0.0
        and float(metrics.get("walk_forward_lift") or 0.0) > 0.0
        and float(metrics.get("worst_window_net_lift") or 0.0) > 0.0
        and int(metrics.get("sample_rows") or 0) >= 5
        and shadow_sessions_ready
    )
    blocked = list(bounds["blocked_reasons"]) + list(metrics.get("blocked_reasons") or [])
    if not passed_metrics:
        for reason in ("net_lift_positive", "walk_forward_positive", "worst_window_positive", "minimum_samples", "minimum_candidate_shadow_sessions"):
            if reason == "net_lift_positive" and float(metrics.get("net_lift") or 0.0) > 0.0:
                continue
            if reason == "walk_forward_positive" and float(metrics.get("walk_forward_lift") or 0.0) > 0.0:
                continue
            if reason == "worst_window_positive" and float(metrics.get("worst_window_net_lift") or 0.0) > 0.0:
                continue
            if reason == "minimum_samples" and int(metrics.get("sample_rows") or 0) >= 5:
                continue
            if reason == "minimum_candidate_shadow_sessions" and shadow_sessions_ready:
                continue
            blocked.append(reason)
    insufficient_data = int(metrics.get("sample_rows") or 0) < 5 or not shadow_sessions_ready or bool(
        {"missing_mature_decision_outcomes", "missing_candidate_specific_shadow_outcomes"}
        & set(metrics.get("blocked_reasons") or [])
    )
    status = "passed" if bounds["ok"] and passed_metrics else "blocked_by_data" if bounds["ok"] and insufficient_data else "rejected"
    validation = {
        "status": status,
        "production_eligible": status == "passed",
        "blocked_reasons": sorted(set(blocked)),
        "bounds": bounds,
        "method": "strategy_policy_cost_after_decision_outcome_proxy_v1",
        "review": {"status": "pending" if status == "passed" else "waiting_for_data" if status == "blocked_by_data" else "auto_rejected", "reviewed_at": datetime.now().isoformat()},
    }
    update_model_version_validation(int(version_id), validation=validation)
    update_model_version_status(int(version_id), status="candidate" if status in {"passed", "blocked_by_data"} else "rejected", is_active=False)
    with business_conn() as c:
        c.execute(
            "UPDATE model_versions SET metrics_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metrics, ensure_ascii=False), datetime.now().isoformat(), int(version_id)),
        )
    return {"ok": True, "version_id": int(version_id), "status": status, "metrics": metrics, "validation": validation}


def advance_waiting_strategy_policy_candidates(*, limit: int = 30) -> dict[str, Any]:
    """Refresh mature outcomes and re-evaluate data-blocked policy candidates.

    This is a lifecycle advance only.  It can move a candidate to the existing
    review gate or reject it, but cannot make it active or alter the Champion.
    """
    materialized = materialize_decision_outcomes(limit=2000)
    shadows = materialize_strategy_policy_shadow_observations(limit_candidates=limit)
    candidates = [
        item for item in list_model_versions(STRATEGY_MODEL_KEY, limit=max(1, min(int(limit or 30), 200)))
        if not bool(item.get("is_active"))
        and str(item.get("status") or "") == "candidate"
        and str((item.get("validation") or {}).get("status") or "") == "blocked_by_data"
    ]
    results: list[dict[str, Any]] = []
    counts = {"re_evaluated": 0, "still_waiting": 0, "formal_oos_queued": 0, "rejected": 0}
    for candidate in candidates:
        evaluation = evaluate_strategy_policy_version(int(candidate["id"]))
        status = str(evaluation.get("status") or "unknown")
        counts["re_evaluated"] += 1
        if status == "blocked_by_data":
            counts["still_waiting"] += 1
        elif status == "passed":
            from app.services.evolution_orchestrator import create_strategy_controller_oos_task

            queued = create_strategy_controller_oos_task(int(candidate["id"]))
            counts["formal_oos_queued"] += 1
        elif status == "rejected":
            counts["rejected"] += 1
        results.append({
            "version_id": int(candidate["id"]),
            "version": candidate.get("version"),
            "status": status,
            "sample_rows": int((evaluation.get("metrics") or {}).get("sample_rows") or 0),
            "blocked_reasons": (evaluation.get("validation") or {}).get("blocked_reasons") or [],
            "formal_oos_task_id": (queued.get("task") or {}).get("id") if status == "passed" else None,
            "learning_evidence": (candidate.get("config") or {}).get("learning_evidence") or {},
        })
    return {
        "ok": True,
        "schema_version": "strategy_policy_candidate_maturity_v1",
        "outcomes_materialized": materialized,
        "shadow_observations": shadows,
        "counts": counts,
        "candidates": results,
        "policy": "20_distinct_future_shadow_sessions_then_formal_oos_then_automatic_champion_rebind",
    }


def strategy_policy_replay_preview(
    *,
    version_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 120,
) -> dict[str, Any]:
    version = get_model_version(int(version_id)) if version_id else get_active_model_version(STRATEGY_MODEL_KEY)
    if not version or version.get("model_key") != STRATEGY_MODEL_KEY:
        return {"ok": False, "error": "strategy_policy_version_not_found", "version_id": version_id}
    policy = ((version.get("config") or {}).get("policy") or DEFAULT_STRATEGY_POLICY)
    add_score = _threshold(policy, "add_position_score", 80.0)
    loss_threshold = _threshold(policy, "loss_review_threshold", -0.06)
    profit_threshold = _threshold(policy, "profit_trim_threshold", 0.25)
    materialize_decision_outcomes(limit=2000)
    where = ["1=1"]
    params: list[Any] = []
    if start_date:
        where.append("substr(d.decision_time,1,10) >= ?")
        params.append(str(start_date))
    if end_date:
        where.append("substr(d.decision_time,1,10) <= ?")
        params.append(str(end_date))
    params.append(max(1, min(int(limit or 120), 1000)))
    with business_conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT d.id,d.ts_code,d.decision_type,d.decision_time,d.price,d.reason,
                      d.research_state_json,d.portfolio_context_json,d.target_weight_contract_json,
                      a.horizon_sessions,a.directional_return,a.correct,a.exit_date
               FROM decision_snapshots d
               LEFT JOIN decision_outcome_attributions a
                 ON a.decision_snapshot_id=d.id AND a.horizon_sessions=20
               WHERE {' AND '.join(where)}
               ORDER BY d.decision_time DESC,d.id DESC
               LIMIT ?""",
            tuple(params),
        ).fetchall()]
    actions: list[dict[str, Any]] = []
    pending_outcomes = 0
    passed_returns: list[float] = []
    blocked_returns: list[float] = []
    for row in reversed(rows):
        research = {}
        contract = {}
        try:
            research = json.loads(row.get("research_state_json") or "{}")
            contract = json.loads(row.get("target_weight_contract_json") or "{}")
        except Exception:
            research = {}
            contract = {}
        score = _safe_float(research.get("score") or contract.get("score"))
        directional_return = _safe_float(row.get("directional_return"))
        decision_type = str(row.get("decision_type") or "").upper()
        action = "hold"
        would_trade = False
        reasons: list[str] = []
        if decision_type in {"BUY", "ADD"}:
            if score is None:
                action = "buy_candidate"
                would_trade = False
                reasons.append("缺少冻结分数，回放只观察不交易")
            elif score >= add_score:
                action = "add_position"
                would_trade = True
                reasons.append(f"分数 {score:.2f} >= 买入阈值 {add_score:.2f}")
            else:
                action = "buy_candidate"
                reasons.append(f"分数 {score:.2f} < 买入阈值 {add_score:.2f}")
        elif decision_type in {"SELL", "REDUCE"}:
            action = "sell_exit" if directional_return is not None and directional_return <= loss_threshold else "reduce_position"
            would_trade = True
            reasons.append("历史决策为卖出/减仓")
        if directional_return is None:
            pending_outcomes += 1
        elif would_trade:
            passed_returns.append(float(directional_return))
        else:
            blocked_returns.append(float(directional_return))
        if directional_return is not None and directional_return >= profit_threshold:
            reasons.append(f"收益达到止盈复核阈值 {profit_threshold:.2%}")
        actions.append({
            "decision_snapshot_id": int(row.get("id") or 0),
            "symbol": str(row.get("ts_code") or "").split(".")[0],
            "decision_time": str(row.get("decision_time") or ""),
            "historical_decision_type": decision_type,
            "replayed_action": action,
            "would_trade": would_trade,
            "score": score,
            "horizon_sessions": int(row.get("horizon_sessions") or 20),
            "directional_return": directional_return,
            "reason": "；".join(reasons) if reasons else "策略参数回放观察",
        })
    avg_trade_return = sum(passed_returns) / len(passed_returns) if passed_returns else 0.0
    avg_blocked_return = sum(blocked_returns) / len(blocked_returns) if blocked_returns else 0.0
    return {
        "ok": True,
        "schema_version": "strategy_policy_replay_preview_v1",
        "method": "decision_snapshot_replay_preview_v1",
        "maturity": "preview_not_shadow_observation",
        "version_id": version.get("id"),
        "version": version.get("version"),
        "date_range": {"start_date": start_date, "end_date": end_date},
        "metrics": {
            "sample_rows": len(actions),
            "trade_rows": len(passed_returns),
            "blocked_rows": len(blocked_returns),
            "pending_outcomes": pending_outcomes,
            "avg_trade_directional_return": round(avg_trade_return, 8),
            "avg_blocked_directional_return": round(avg_blocked_return, 8),
            "spread_vs_blocked": round(avg_trade_return - avg_blocked_return, 8),
            "transaction_cost_bps": 25,
        },
        "protocol": {
            "purge_embargo": "declared_not_enforced_until_full_daily_replay",
            "shadow_observation_sessions_required": 20,
            "can_auto_promote": False,
            "why": "这是基于已冻结决策快照的回放预览，用于发现候选方向；完整逐日重建与影子观察需要时间序列积累。",
        },
        "actions": actions[-max(1, min(int(limit or 120), 200)):],
        "generated_at": datetime.now().isoformat(),
    }


def review_strategy_policy_version(version_id: int, *, decision: str, reviewer: str = "operator", comment: str = "") -> dict[str, Any]:
    version = get_model_version(int(version_id))
    if not version or version.get("model_key") != STRATEGY_MODEL_KEY:
        return {"ok": False, "error": "strategy_policy_version_not_found", "version_id": version_id}
    if decision not in {"approved", "rejected"}:
        return {"ok": False, "error": "invalid_decision"}
    if decision == "approved":
        evaluation = evaluate_strategy_policy_version(int(version_id))
        if not evaluation.get("ok") or str(evaluation.get("status")) != "passed":
            return {"ok": False, "error": "strategy_policy_validation_failed", "evaluation": evaluation}
        version = get_model_version(int(version_id)) or version
    validation = dict(version.get("validation") or {})
    validation["review"] = {"status": decision, "reviewer": reviewer, "comment": comment, "reviewed_at": datetime.now().isoformat()}
    validation["production_eligible"] = decision == "approved"
    validation["blocked_reasons"] = [] if decision == "approved" else ["operator_rejected"]
    validation["status"] = "passed" if decision == "approved" else "rejected"
    update_model_version_validation(int(version_id), validation=validation)
    if decision == "approved":
        set_active_model_version(int(version_id))
        from app.services.production_strategy import ensure_production_strategy_champion

        ensure_production_strategy_champion(force_rebind=True)
    else:
        update_model_version_status(int(version_id), status="rejected", is_active=False)
    return {"ok": True, "version": get_model_version(int(version_id))}
