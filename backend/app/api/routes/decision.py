from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.data_platform.db import _conn as data_conn
from app.data_platform.services.decision_data_contract import decision_data_contract
from app.db.store import get_account_balance, get_account_baseline, get_account_high_water_mark, get_backtest_result, get_positions, list_backtests, list_executions, list_operations
from app.services.backtest_data_source import BacktestDataSource
from app.services.data_health import build_data_health_report
from app.services.market_context import live_market_context
from app.services.model_registry import get_model_status
from app.services.quant_run_engine import list_consolidated_trade_dates
from app.services.operations_overview import build_live_portfolio_risk
from app.services.theme_engine import list_themes
from app.services.daily_rebalance import build_daily_rebalance_report
from app.services.decision_enhancement import build_decision_enhancement
from app.services.quant_risk import evaluate_model_readiness_gate

router = APIRouter()

MIN_CASH_FOR_TRADE = 10_000.0
MAX_SINGLE_POSITION_PCT = 0.40
REQUIRED_DECISION_MARKET_APIS = ("daily", "daily_basic", "stk_limit")

MARKET_STATE_LABELS = {
    "S0": "风险扩张", "S1": "震荡分化", "S2": "风险收缩", "S3": "恐慌修复",
    "bull": "风险扩张",
    "sideways": "震荡分化",
    "bear": "风险收缩",
    "stress": "压力状态",
    "recovery": "恐慌修复",
}

MARKET_RISK_BUDGETS = {
    "S0": 1.00, "S1": 0.85, "S2": 0.55, "S3": 0.70,
    "bull": 1.00,
    "sideways": 0.85,
    "bear": 0.55,
    "stress": 0.55,
    "recovery": 0.70,
}


def _equity_budget_breach(ledger: dict[str, Any], position_gate: dict[str, Any]) -> tuple[bool, float, float]:
    """Return whether the live equity allocation is already above the effective risk budget."""
    total_equity = float(ledger.get("total_equity") or 0)
    if total_equity <= 0:
        return False, 0.0, 0.0
    current_equity = float(ledger.get("position_market_value") or 0) / total_equity
    risk_budget = float(position_gate.get("combined_risk_budget_multiplier") or 0)
    return risk_budget > 0 and current_equity > risk_budget + 0.005, current_equity, risk_budget


def _ledger_summary() -> dict[str, Any]:
    cash_balance = float(get_account_balance() or 0)
    baseline = float(get_account_baseline() or 0)
    positions = get_positions()
    latest_trade_dates = list_consolidated_trade_dates()
    latest_date = latest_trade_dates[-1] if latest_trade_dates else ""
    ds = BacktestDataSource()
    market_value = 0.0
    by_symbol_value: dict[str, float] = {}
    for p in positions:
        sym = str(p.get("symbol") or "").strip()
        qty = int(p.get("quantity") or 0)
        if not sym or qty <= 0:
            continue
        px = float(ds.get_price_on_date(sym, latest_date, field="close") or 0) if latest_date else 0.0
        if px <= 0:
            px = float(p.get("price") or 0)
        mv = round(px * qty, 2)
        market_value += mv
        by_symbol_value[sym] = mv
    market_value = round(market_value, 2)
    total_equity = round(cash_balance + market_value, 2)
    equity_delta = round(total_equity - baseline, 2)
    cash_pct = round(cash_balance / total_equity, 4) if total_equity > 0 else 0.0
    max_position_pct = 0.0
    max_position_symbol = ""
    if total_equity > 0 and by_symbol_value:
        sym, val = max(by_symbol_value.items(), key=lambda x: x[1])
        max_position_pct = round(val / total_equity, 4)
        max_position_symbol = sym
    account_risks: list[dict[str, str]] = []
    if cash_balance < MIN_CASH_FOR_TRADE:
        account_risks.append(
            {
                "code": "low_cash",
                "level": "warning",
                "message": f"可用现金低于 {MIN_CASH_FOR_TRADE:.0f}，可能无法完成最小一手调仓。",
            }
        )
    if max_position_pct > MAX_SINGLE_POSITION_PCT:
        account_risks.append(
            {
                "code": "concentrated_position",
                "level": "warning",
                "message": f"单票 {max_position_symbol} 占比约 {max_position_pct * 100:.1f}%，集中度偏高。",
            }
        )
    high_water_mark = max(float(get_account_high_water_mark() or 0), baseline, total_equity)
    drawdown = round(total_equity / high_water_mark - 1, 6) if high_water_mark > 0 else 0.0
    return {
        "cash_balance": round(cash_balance, 2),
        "position_market_value": market_value,
        "total_equity": total_equity,
        "equity_delta": equity_delta,
        "position_count": len(positions),
        "valuation_date": latest_date,
        "cash_pct": cash_pct,
        "max_position_pct": max_position_pct,
        "max_position_symbol": max_position_symbol,
        "initial_cash": baseline,
        "high_water_mark": round(high_water_mark, 2),
        "drawdown": drawdown,
        "account_risks": account_risks,
    }


def _market_overview() -> dict[str, Any]:
    """Return a provenance-aware market snapshot without inventing missing inputs."""
    try:
        context = live_market_context()
    except Exception as exc:  # one data-domain failure must not blank the overview
        return {
            "ok": False,
            "status": "degraded",
            "reason": f"市场环境读取失败：{str(exc)[:160]}",
            "market_state": None,
            "sentiment": None,
            "themes": {"status": "unavailable", "items": [], "reason": "真实主题观测尚未接入"},
            "limitations": ["市场环境接口异常，当前不得据此提高风险预算"],
        }
    if not context.get("ok"):
        return {
            **context,
            "status": "insufficient_data",
            "market_state": None,
            "sentiment": None,
            "themes": {"status": "unavailable", "items": [], "reason": "真实主题观测尚未接入"},
            "limitations": [str(context.get("reason") or "市场观测不足")],
        }
    state = str((context.get("market_state") or {}).get("state") or "unknown")
    context["market_state"]["label"] = MARKET_STATE_LABELS.get(state, "状态待确认")
    parent_multiplier = MARKET_RISK_BUDGETS.get(state, 0.55)
    profile_multiplier = max(.85, min(1.0, float((context.get("market_profile") or {}).get("risk_budget_multiplier") or 1.0)))
    context["market_state"]["parent_risk_budget_multiplier"] = parent_multiplier
    context["market_state"]["profile_risk_budget_multiplier"] = profile_multiplier
    context["market_state"]["risk_budget_multiplier"] = round(parent_multiplier * profile_multiplier, 4)
    try:
        theme_items = list_themes(promotable_only=True, limit=20).get("items") or []
    except Exception as exc:  # noqa: BLE001
        theme_items = []
        theme_error = str(exc)[:160]
    else:
        theme_error = ""
    context["themes"] = {
        "status": "ready" if theme_items else "insufficient_data",
        "items": theme_items,
        "reason": "" if theme_items else theme_error or "尚无满足两类来源、至少5个关联标的和3个交易日确认门的主题",
    }
    try:
        portfolio_risk = build_live_portfolio_risk()
    except Exception as exc:  # noqa: BLE001
        portfolio_risk = {"ok": False, "status": "unknown", "breaches": [], "limitations": [str(exc)[:160]]}
    state_multiplier = float(context["market_state"]["risk_budget_multiplier"])
    risk_status = str(portfolio_risk.get("status") or "unknown")
    portfolio_multiplier = 0.0 if risk_status == "blocked" else 0.75 if risk_status == "warning" else 1.0 if risk_status == "ready" else 0.5
    combined_multiplier = round(state_multiplier * portfolio_multiplier, 4)
    context["portfolio_risk"] = portfolio_risk
    context["position_gate"] = {
        "allow_new_exposure": bool(context.get("ok")) and risk_status != "blocked",
        "market_multiplier": state_multiplier,
        "parent_market_multiplier": parent_multiplier,
        "market_profile_multiplier": profile_multiplier,
        "portfolio_multiplier": portfolio_multiplier,
        "combined_risk_budget_multiplier": combined_multiplier,
        "reason": "组合风险存在硬约束" if risk_status == "blocked" else "组合风险存在告警，按收缩预算执行" if risk_status == "warning" else "市场与组合风险门控通过" if risk_status == "ready" else "组合风险数据不足，保守收缩预算",
    }
    context["market_state"]["effective_risk_budget_multiplier"] = combined_multiplier
    return context


def _recent_execution_summary() -> dict[str, Any] | None:
    executions = list_executions(limit=50)
    if not executions:
        return None
    ex = executions[0]
    latest_trade_date = str(ex.get("executed_at") or "")[:10]
    day_executions = [item for item in executions if str(item.get("executed_at") or "")[:10] == latest_trade_date]
    ops = []
    for execution in day_executions:
        ops.extend(list_operations(execution_id=int(execution["id"]), limit=100))
    position_names = {str(item.get("symbol") or ""): str(item.get("name") or "") for item in get_positions()}
    symbols = {str(operation.get("symbol") or "") for operation in ops if operation.get("symbol")}
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        with data_conn() as conn:
            rows = conn.execute(
                f"SELECT ts_code,name FROM dc_stock_master WHERE split_part(ts_code,'.',1) IN ({placeholders})",
                tuple(sorted(symbols)),
            ).fetchall()
        for row in rows:
            item = dict(row)
            position_names[str(item.get("ts_code") or "").split(".")[0]] = str(item.get("name") or "")
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for operation in ops:
        symbol = str(operation.get("symbol") or "")
        side = str(operation.get("side") or "").upper()
        if not symbol or side not in {"BUY", "SELL"}:
            continue
        key = (symbol, side)
        quantity = int(operation.get("quantity") or 0)
        price = float(operation.get("price") or 0)
        item = grouped.setdefault(key, {
            "symbol": symbol, "name": str(operation.get("name") or position_names.get(symbol) or ""),
            "side": side, "quantity": 0, "gross_amount": 0.0,
            "at": str(operation.get("at") or ""), "remark": str(operation.get("remark") or ""),
        })
        item["quantity"] += quantity
        item["gross_amount"] += price * quantity
        if not item["name"]:
            item["name"] = position_names.get(symbol, "")
    aggregated_ops = []
    for item in grouped.values():
        quantity = int(item.pop("quantity") or 0)
        gross = float(item.pop("gross_amount") or 0)
        aggregated_ops.append({**item, "quantity": quantity, "price": round(gross / quantity, 4) if quantity else 0.0, "amount": round(gross, 2)})
    aggregated_ops.sort(key=lambda item: (0 if item["side"] == "BUY" else 1, -float(item.get("amount") or 0)))
    buy_count = sum(1 for o in aggregated_ops if o["side"] == "BUY")
    sell_count = sum(1 for o in aggregated_ops if o["side"] == "SELL")
    buy_cost = sum(
        float(o.get("amount") or 0) for o in aggregated_ops if o["side"] == "BUY"
    )
    return {
        "execution_id": int(ex["id"]),
        "execution_ids": [int(item["id"]) for item in day_executions],
        "trade_date": latest_trade_date,
        "executed_at": str(ex.get("executed_at") or ""),
        "strategy_id": int(ex.get("strategy_id") or 0),
        "budget": round(sum(float(item.get("budget") or 0) for item in day_executions), 2),
        "top_n": int(ex.get("top_n") or 0),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_buy_cost": round(buy_cost, 2),
        "operations": aggregated_ops[:20],
    }


def _latest_decision_data_contract() -> dict[str, Any]:
    return decision_data_contract()


def _latest_model_validation(model_status: dict[str, Any]) -> dict[str, Any] | None:
    active = model_status.get("active_version")
    if not active:
        return None
    active_id = int(active.get("id") or 0)
    for row in list_backtests(limit=20):
        if int(row.get("model_version_id") or 0) != active_id:
            continue
        detail = get_backtest_result(int(row["id"]))
        if not detail:
            continue
        return {
            "backtest_id": int(row["id"]),
            "model_version_id": active_id,
            "model_version": str(row.get("model_version") or active.get("version") or ""),
            "start_date": str(row.get("start_date") or ""),
            "end_date": str(row.get("end_date") or ""),
            "metrics": detail.get("metrics") or {},
        }
    return None


def _build_gate_decision(
    *,
    data_health: dict[str, Any],
    latest_data_contract: dict[str, Any],
    ledger: dict[str, Any],
    model_status: dict[str, Any],
    market_overview: dict[str, Any],
) -> dict[str, Any]:
    if not latest_data_contract.get("ok"):
        missing = list(latest_data_contract.get("missing_apis") or [])
        missing_text = "、".join(missing) if missing else "当日快照"
        return {
            "tone": "stop",
            "title": "今天先不要调仓",
            "detail": f"最新交易日 {latest_data_contract.get('latest_trade_date') or '—'} 还缺 {missing_text}，或当日快照尚未构建；必须先完成收盘后入库。",
            "reason_code": "latest_market_day_not_materialized",
        }
    issues = list(data_health.get("issues") or [])
    error_count = sum(1 for x in issues if str(x.get("level") or "") == "error")
    warning_count = sum(1 for x in issues if str(x.get("level") or "") == "warning")
    weak_warnings = [x for x in issues if str(x.get("code") or "") in {"weak_factor_coverage", "daily_basic_missing"}]

    if error_count > 0:
        return {
            "tone": "stop",
            "title": "今天先不要调仓",
            "detail": "本地数据存在错误级问题，模型调仓建议与回测口径可能失真。",
            "reason_code": "data_error",
        }

    if not market_overview.get("ok"):
        return {
            "tone": "hold",
            "title": "市场环境数据不足，维持现状",
            "detail": "市场状态与情绪无法形成合格观测，禁止据此新增风险敞口。",
            "reason_code": "market_context_unavailable",
        }

    position_gate = market_overview.get("position_gate") if isinstance(market_overview.get("position_gate"), dict) else {}
    budget_breached, current_equity, risk_budget = _equity_budget_breach(ledger, position_gate)
    if budget_breached:
        return {
            "tone": "hold",
            "title": "当前仓位超过风险预算，暂停新增仓位",
            "detail": f"当前股票仓位约 {current_equity * 100:.1f}%，高于风险预算 {risk_budget * 100:.1f}%；先通过减仓或防御调仓回到约束内。",
            "reason_code": "equity_budget_exceeded",
        }
    if not bool(position_gate.get("allow_new_exposure", True)):
        portfolio_risk = market_overview.get("portfolio_risk") if isinstance(market_overview.get("portfolio_risk"), dict) else {}
        deleveraging_actions = portfolio_risk.get("deleveraging_actions") if isinstance(portfolio_risk.get("deleveraging_actions"), list) else []
        return {
            "tone": "hold",
            "title": "不新增仓位，执行降风险计划" if deleveraging_actions else "组合风险门控限制新增仓位",
            "detail": "风险门控只限制买入扩张；减仓、降权和防御调仓仍会继续执行。" if deleveraging_actions else str(position_gate.get("reason") or "组合风险状态不允许扩张风险预算"),
            "reason_code": "portfolio_risk_gate",
        }

    model_decision = evaluate_model_readiness_gate(model_status)
    if model_decision.should_pause:
        return {
            "tone": "hold",
            "title": "生产模型尚未就绪",
            "detail": model_decision.message,
            "reason_code": model_decision.code,
            "recovery_condition": model_decision.recovery_condition,
        }

    cash = float(ledger.get("cash_balance") or 0)
    if cash < MIN_CASH_FOR_TRADE:
        return {
            "tone": "stop",
            "title": "今天先不要调仓",
            "detail": f"可用现金 {cash:.2f} 低于最小调仓门槛 {MIN_CASH_FOR_TRADE:.0f}。",
            "reason_code": "insufficient_cash",
        }

    state = str((market_overview.get("market_state") or {}).get("state") or "")
    if state in {"S2", "bear", "stress"}:
        multiplier = float(
            position_gate.get("combined_risk_budget_multiplier")
            or (market_overview.get("market_state") or {}).get("risk_budget_multiplier")
            or .55
        )
        return {
            "tone": "hold",
            "title": "市场处于风险收缩，限制新增仓位",
            "detail": f"当前组合风险预算乘数为 {multiplier:.2f}；允许风险收缩和防御调整，不允许按常态预算扩张。",
            "reason_code": "market_risk_contraction",
        }

    if weak_warnings:
        return {
            "tone": "hold",
            "title": "可以分析，但需谨慎",
            "detail": "存在弱因子覆盖或估值数据回退警告，建议先核对数据健康，再运行自动模拟调仓。",
            "reason_code": "weak_data_coverage",
        }

    if warning_count > 0:
        return {
            "tone": "hold",
            "title": "可以分析，但需谨慎",
            "detail": f"数据存在 {warning_count} 条警告，执行前请查看数据可信度与调仓预览。",
            "reason_code": "data_warning",
        }

    return {
        "tone": "go",
        "title": "可以去量化分析",
        "detail": "数据、模型与资金门槛均满足，可在回测验证页生成目标组合并自动模拟执行。",
        "reason_code": "ready",
    }


def _risk_gate_explanation(
    *,
    gate_decision: dict[str, Any],
    latest_data_contract: dict[str, Any],
    ledger: dict[str, Any],
    model_status: dict[str, Any],
    market_overview: dict[str, Any],
    recent_execution: dict[str, Any] | None,
) -> dict[str, Any]:
    position_gate = market_overview.get("position_gate") if isinstance(market_overview.get("position_gate"), dict) else {}
    portfolio_risk = market_overview.get("portfolio_risk") if isinstance(market_overview.get("portfolio_risk"), dict) else {}
    market_state = market_overview.get("market_state") if isinstance(market_overview.get("market_state"), dict) else {}
    cash = float(ledger.get("cash_balance") or 0)
    drawdown = float(ledger.get("drawdown") or 0)
    risk_budget = float(position_gate.get("combined_risk_budget_multiplier") or market_state.get("risk_budget_multiplier") or 0)
    deleveraging_actions = portfolio_risk.get("deleveraging_actions") if isinstance(portfolio_risk.get("deleveraging_actions"), list) else []
    budget_breached, current_equity, equity_budget = _equity_budget_breach(ledger, position_gate)
    requires_rebalance = bool(deleveraging_actions) or budget_breached
    allow_new = bool(position_gate.get("allow_new_exposure", True)) and not budget_breached and gate_decision.get("tone") == "go"
    reasons: list[dict[str, Any]] = []
    constraints: list[str] = []
    plan: list[dict[str, str]] = []
    release: list[str] = []

    if not latest_data_contract.get("ok"):
        reasons.append({"level": "block", "title": "当日数据未冻结", "detail": str(latest_data_contract.get("reason") or "最新交易日快照未完成")})
        constraints.append("不根据不完整收盘数据新增仓位")
        plan.append({"step": "等待数据", "detail": "日更任务会继续复核最新交易日行情、估值和涨跌停数据"})
        release.append("最新交易日 daily/daily_basic/stk_limit 和快照全部入库")
    if not model_status.get("active_version"):
        reasons.append({"level": "block", "title": "生产模型未接管", "detail": "当前没有可用于正式决策的 active 模型版本"})
        constraints.append("不允许生产模型缺位时自动新增风险敞口")
        plan.append({"step": "模型治理", "detail": "等待候选模型完成验证并通过上线控制"})
        release.append("模型治理页存在 active production 版本")
    if not bool(position_gate.get("allow_new_exposure", True)):
        breaches = [str(item.get("message") or item.get("code") or "") for item in (portfolio_risk.get("breaches") or []) if item]
        reasons.append({
            "level": "block",
            "title": "组合风险硬约束",
            "detail": "；".join(breaches[:3]) or str(position_gate.get("reason") or "组合风险状态不允许扩张风险预算"),
        })
        constraints.append("不新增股票风险敞口，只允许降风险或防御调整")
        for action in deleveraging_actions[:3]:
            symbol = str(action.get("symbol") or action.get("industry") or "组合")
            target = action.get("target_weight")
            target_text = f"目标 {float(target) * 100:.1f}%" if isinstance(target, (int, float)) else "降到约束内"
            plan.append({"step": "自动降风险", "detail": f"{symbol} {target_text}，不被新增仓位门控拦截"})
        if not deleveraging_actions:
            plan.append({"step": "风险复核", "detail": "系统不新增仓位；若出现超配、逻辑失效或回撤约束，会优先生成减仓/防御调仓"})
        release.append("组合风险状态恢复为 ready，且没有硬约束 breaches")
    if budget_breached:
        reasons.append({
            "level": "block",
            "title": "股票仓位超过风险预算",
            "detail": f"当前股票仓位约 {current_equity * 100:.1f}%，高于当前风险预算 {equity_budget * 100:.1f}%",
        })
        constraints.append("暂停新增股票仓位，优先减仓或迁移到防御资产")
        plan.append({"step": "回到风险预算", "detail": f"将股票仓位从 {current_equity * 100:.1f}% 降至 {equity_budget * 100:.1f}% 以内"})
        release.append(f"股票仓位回到 {equity_budget * 100:.1f}% 风险预算以内")
    if risk_budget <= 0:
        reasons.append({"level": "block", "title": "风险预算为 0", "detail": "组合/市场门控合成后的新增风险预算为 0"})
        constraints.append("新增仓位目标权重被压到 0")
        release.append("合成风险预算大于 0")
    elif risk_budget < 0.8:
        reasons.append({"level": "warn", "title": "风险预算收缩", "detail": f"当前最终风险预算约 {risk_budget * 100:.0f}%，不按常态扩张"})
        if allow_new:
            constraints.append("新增仓位必须落在收缩后的风险预算内")
        else:
            constraints.append("减仓或防御迁移后，股票仓位必须落在收缩后的风险预算内")
    if cash < MIN_CASH_FOR_TRADE:
        reasons.append({"level": "warn", "title": "可用现金不足", "detail": f"可用现金 {cash:,.2f} 元，低于最小交易门槛 {MIN_CASH_FOR_TRADE:,.0f} 元"})
        constraints.append("可能无法完成最小一手调仓")
        release.append(f"可用现金恢复到 {MIN_CASH_FOR_TRADE:,.0f} 元以上")
    if drawdown <= -0.08:
        reasons.append({"level": "warn", "title": "账户回撤接近风险线", "detail": f"当前回撤 {drawdown * 100:.2f}%，优先控制组合波动"})
        constraints.append("优先处理减仓和风险复盘")
        release.append("账户回撤回到 -8% 以内")
    if recent_execution:
        plan.append({"step": "复盘成交", "detail": "最近执行已入账，系统只在出现新信号或风险变化时再给交易"})
    if not plan:
        plan.append({"step": "生成分析", "detail": "数据、模型和风险门控通过后，系统生成量化分析与目标组合"})
    if not release:
        release.append("数据、模型、现金和组合风险门控全部通过")
    return {
        "allow_new_exposure": allow_new,
        "allow_sell": True,
        "allow_reduce": True,
        "allow_defensive": True,
        "requires_rebalance": requires_rebalance,
        "risk_budget_multiplier": round(risk_budget, 4),
        "reason_code": gate_decision.get("reason_code"),
        "headline": "允许新增仓位" if allow_new else "不新增仓位，系统优先降风险" if requires_rebalance else "暂不新增仓位",
        "reasons": reasons or [{"level": "ok", "title": "门控通过", "detail": "数据、模型、现金和组合风险均满足新增条件"}],
        "constraints": constraints or ["允许按目标组合和交易约束新增仓位"],
        "today_plan": plan,
        "release_conditions": release,
    }


@router.get("/decision/daily-report")
def decision_daily_report() -> dict[str, Any]:
    """P3-7 每日调仓报告"""
    return build_daily_rebalance_report()
@router.get("/decision/summary")
def decision_summary(limit_days: int = Query(30, ge=1, le=250)) -> dict[str, Any]:
    data_health = build_data_health_report(limit_days=limit_days)
    latest_data_contract = _latest_decision_data_contract()
    ledger = _ledger_summary()
    model_status = get_model_status()
    market_overview = _market_overview()
    gate_decision = _build_gate_decision(
        data_health=data_health,
        latest_data_contract=latest_data_contract,
        ledger=ledger,
        model_status=model_status,
        market_overview=market_overview,
    )
    recent_execution = _recent_execution_summary()
    if recent_execution and str(recent_execution.get("trade_date") or "").replace("-", "") == str(latest_data_contract.get("latest_trade_date") or "").replace("-", "")[:8]:
        gate_decision = {
            "tone": "go",
            "title": "今日全仓配置已执行",
            "detail": (
                f"已买入 {int(recent_execution.get('buy_count') or 0)} 只、卖出 {int(recent_execution.get('sell_count') or 0)} 只；"
                f"成交买入金额 {float(recent_execution.get('total_buy_cost') or 0):,.2f} 元，当前剩余现金 {float(ledger.get('cash_balance') or 0):,.2f} 元。"
            ),
            "reason_code": "daily_allocation_executed",
        }
    risk_gate_explanation = _risk_gate_explanation(
        gate_decision=gate_decision,
        latest_data_contract=latest_data_contract,
        ledger=ledger,
        model_status=model_status,
        market_overview=market_overview,
        recent_execution=recent_execution,
    )
    decision_enhancement = build_decision_enhancement(
        gate_decision=gate_decision,
        risk_gate_explanation=risk_gate_explanation,
        market_overview=market_overview,
        ledger=ledger,
        recent_execution=recent_execution,
    )
    return {
        "data_health": data_health,
        "latest_data_contract": latest_data_contract,
        "ledger": ledger,
        "model_status": model_status,
        "market_overview": market_overview,
        "gate_decision": gate_decision,
        "risk_gate_explanation": risk_gate_explanation,
        "decision_enhancement": decision_enhancement,
        "recent_execution": recent_execution,
        "latest_model_validation": _latest_model_validation(model_status),
        "latest_analyze": None,
    }
