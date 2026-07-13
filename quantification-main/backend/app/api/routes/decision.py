from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.db.store import get_account_balance, get_account_baseline, get_account_high_water_mark, get_backtest_result, get_positions, list_backtests, list_executions, list_operations
from app.services.backtest_data_source import BacktestDataSource
from app.services.data_health import build_data_health_report
from app.services.market_context import live_market_context
from app.services.model_registry import get_model_status
from app.services.quant_run_engine import list_consolidated_trade_dates
from app.services.operations_overview import build_live_portfolio_risk

router = APIRouter()

MIN_CASH_FOR_TRADE = 10_000.0
MAX_SINGLE_POSITION_PCT = 0.40

MARKET_STATE_LABELS = {
    "bull": "风险扩张",
    "sideways": "震荡分化",
    "bear": "风险收缩",
    "stress": "压力状态",
    "recovery": "恐慌修复",
}

MARKET_RISK_BUDGETS = {
    "bull": 1.00,
    "sideways": 0.85,
    "bear": 0.55,
    "stress": 0.55,
    "recovery": 0.70,
}


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
    context["market_state"]["risk_budget_multiplier"] = MARKET_RISK_BUDGETS.get(state, 0.55)
    # The current live context has no qualified theme observations. Keep this
    # explicit instead of turning an empty list into a neutral theme signal.
    context["themes"] = {
        "status": "unavailable",
        "items": [],
        "reason": "尚无满足多来源、至少5个关联标的和3个交易日确认门的真实主题观测",
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
        "portfolio_multiplier": portfolio_multiplier,
        "combined_risk_budget_multiplier": combined_multiplier,
        "reason": "组合风险存在硬约束" if risk_status == "blocked" else "组合风险存在告警，按收缩预算执行" if risk_status == "warning" else "市场与组合风险门控通过" if risk_status == "ready" else "组合风险数据不足，保守收缩预算",
    }
    context["market_state"]["effective_risk_budget_multiplier"] = combined_multiplier
    return context


def _recent_execution_summary() -> dict[str, Any] | None:
    executions = list_executions(limit=1)
    if not executions:
        return None
    ex = executions[0]
    ops = list_operations(execution_id=int(ex["id"]), limit=50)
    buy_count = sum(1 for o in ops if str(o.get("side") or "").upper() == "BUY")
    sell_count = sum(1 for o in ops if str(o.get("side") or "").upper() == "SELL")
    buy_cost = sum(
        float(o.get("price") or 0) * int(o.get("quantity") or 0)
        for o in ops
        if str(o.get("side") or "").upper() == "BUY"
    )
    return {
        "execution_id": int(ex["id"]),
        "executed_at": str(ex.get("executed_at") or ""),
        "strategy_id": int(ex.get("strategy_id") or 0),
        "budget": float(ex.get("budget") or 0),
        "top_n": int(ex.get("top_n") or 0),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_buy_cost": round(buy_cost, 2),
        "operations": [
            {
                "symbol": str(o.get("symbol") or ""),
                "name": str(o.get("name") or ""),
                "side": str(o.get("side") or ""),
                "price": float(o.get("price") or 0),
                "quantity": int(o.get("quantity") or 0),
                "at": str(o.get("at") or ""),
                "remark": str(o.get("remark") or ""),
            }
            for o in ops[:12]
        ],
    }


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
    ledger: dict[str, Any],
    model_status: dict[str, Any],
    market_overview: dict[str, Any],
) -> dict[str, Any]:
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
    if not bool(position_gate.get("allow_new_exposure", True)):
        return {
            "tone": "hold",
            "title": "组合风险门控限制新增仓位",
            "detail": str(position_gate.get("reason") or "组合风险状态不允许扩张风险预算"),
            "reason_code": "portfolio_risk_gate",
        }

    active = model_status.get("active_version")
    if not active:
        return {
            "tone": "hold",
            "title": "模型尚未就绪",
            "detail": "请先在模型策略页完成训练并生成可用版本。",
            "reason_code": "no_active_model",
        }

    validation = dict(active.get("validation") or {})
    if not active.get("is_active") and str(validation.get("status") or "") != "passed":
        return {
            "tone": "hold",
            "title": "模型尚未通过验证",
            "detail": "请先在模型策略页验证最新模型，再进入量化分析确认调仓。",
            "reason_code": "model_not_validated",
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
    if state in {"bear", "stress"}:
        multiplier = float((market_overview.get("market_state") or {}).get("risk_budget_multiplier") or .55)
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
            "detail": "存在弱因子覆盖或估值数据回退警告，建议先核对数据健康后再确认执行。",
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
        "detail": "数据、模型与资金门槛均满足，可在量化分析页生成目标组合并人工确认执行。",
        "reason_code": "ready",
    }


@router.get("/decision/summary")
def decision_summary(limit_days: int = Query(30, ge=1, le=250)) -> dict[str, Any]:
    data_health = build_data_health_report(limit_days=limit_days)
    ledger = _ledger_summary()
    model_status = get_model_status()
    market_overview = _market_overview()
    gate_decision = _build_gate_decision(
        data_health=data_health,
        ledger=ledger,
        model_status=model_status,
        market_overview=market_overview,
    )
    return {
        "data_health": data_health,
        "ledger": ledger,
        "model_status": model_status,
        "market_overview": market_overview,
        "gate_decision": gate_decision,
        "recent_execution": _recent_execution_summary(),
        "latest_model_validation": _latest_model_validation(model_status),
        "latest_analyze": None,
    }
