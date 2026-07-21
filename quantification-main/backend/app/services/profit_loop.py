from __future__ import annotations

import json
from datetime import datetime
from statistics import mean
from typing import Any

from app.data_platform.db import _conn as data_conn
from app.db.store import _conn as business_conn, get_positions
from app.services.backtest_data_source import BacktestDataSource
from app.services.operations_overview import build_live_portfolio_risk
from app.services.plan_model_control import model_family_status
from app.services.review_learning import materialize_decision_outcomes, model_feedback_gate, review_learning_overview
from app.services.two_group_optimizer import two_group_cvar_optimizer


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except Exception:
        return default


def _ensure_tables() -> None:
    with business_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS model_live_health (
            model_key VARCHAR(128) PRIMARY KEY,
            active_version VARCHAR(160) NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL,
            live_ic DOUBLE PRECISION,
            hit_rate DOUBLE PRECISION,
            turnover DOUBLE PRECISION,
            drawdown_contribution DOUBLE PRECISION,
            decay DOUBLE PRECISION,
            weight_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            reason TEXT NOT NULL DEFAULT '',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            updated_at VARCHAR(64) NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS simulation_trade_learning_labels (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(32) NOT NULL,
            label_date VARCHAR(16) NOT NULL,
            label VARCHAR(64) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            pnl_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
            expected_vs_actual_json TEXT NOT NULL DEFAULT '{}',
            created_at VARCHAR(64) NOT NULL,
            UNIQUE(symbol,label_date,label)
        )""")


def _latest_report(period_type: str = "daily") -> dict[str, Any]:
    with business_conn() as c:
        row = c.execute(
            "SELECT payload_json,period FROM quant_performance_reports WHERE period_type=? ORDER BY period DESC,id DESC LIMIT 1",
            (period_type,),
        ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(dict(row).get("payload_json") or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _latest_trade_date() -> str:
    with data_conn() as c:
        row = c.execute("SELECT MAX(trade_date) trade_date FROM dc_stock_daily_snapshot").fetchone()
    return str(dict(row).get("trade_date") or "") if row else ""


def _position_returns(symbols: list[str], limit: int = 120) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    with data_conn() as c:
        for symbol in symbols:
            code = symbol if "." in symbol else f"{symbol}.SH" if symbol.startswith(("5", "6", "9")) else f"{symbol}.SZ"
            rows = [dict(row) for row in c.execute(
                """SELECT change_pct FROM dc_stock_daily_snapshot
                   WHERE ts_code IN (?,?) AND change_pct IS NOT NULL
                   ORDER BY trade_date DESC LIMIT ?""",
                (symbol, code, limit),
            ).fetchall()]
            returns = [float(row["change_pct"]) / 100.0 for row in reversed(rows) if row.get("change_pct") is not None]
            if returns:
                result[symbol] = returns
    return result


def build_return_attribution_loop() -> dict[str, Any]:
    materialized = materialize_decision_outcomes(limit=2000)
    daily = _latest_report("daily")
    monthly = _latest_report("monthly")
    with business_conn() as c:
        row = c.execute("""SELECT
            COALESCE(SUM(market_contribution),0) market,
            COALESCE(SUM(industry_contribution),0) industry,
            COALESCE(SUM(selection_contribution),0) selection,
            COALESCE(AVG(CASE WHEN correct=1 THEN 1.0 ELSE 0.0 END),0) hit_rate
            FROM decision_outcome_attributions
            WHERE calculated_at >= COALESCE((SELECT MAX(calculated_at) FROM decision_outcome_attributions), '')""").fetchone()
    outcome = dict(row or {})
    cost = _safe_float(daily.get("total_cost", daily.get("transaction_cost", 0.0)))
    return_pct = _safe_float(daily.get("return_pct", daily.get("return", 0.0)))
    drawdown = _safe_float(daily.get("max_drawdown_pct", daily.get("drawdown", 0.0)))
    attribution = {
        "model_selection": _safe_float(daily.get("stock_selection_contribution"), _safe_float(outcome.get("selection"))),
        "industry_exposure": _safe_float(daily.get("industry_allocation_contribution"), _safe_float(outcome.get("industry"))),
        "position_sizing": _safe_float(monthly.get("position_contribution", 0.0)),
        "timing_gate": -abs(_safe_float((daily.get("market_state_gate") or {}).get("blocked_signals", 0.0))) * 0.001,
        "trading_cost": -abs(cost),
        "risk_drag": min(0.0, drawdown),
    }
    explained = sum(attribution.values())
    attribution["unexplained"] = return_pct - explained
    return {
        "ok": True,
        "schema_version": "profit_attribution_loop_v1",
        "latest_daily_return_pct": return_pct,
        "latest_monthly_return_pct": _safe_float(monthly.get("return_pct")),
        "components": {key: round(value, 6) for key, value in attribution.items()},
        "hit_rate": _safe_float(outcome.get("hit_rate")),
        "materialized": materialized,
        "interpretation": "每天把收益拆成模型选股、行业暴露、仓位、择时、成本、风控拖累；未解释部分继续进入复盘。",
    }


def build_model_live_monitor() -> dict[str, Any]:
    _ensure_tables()
    family = model_family_status()
    rows = []
    for item in family.get("items") or []:
        active = item.get("active_version") or {}
        latest = item.get("latest_version") or {}
        version = str((active or latest).get("version") or "")
        metrics = (active or latest).get("metrics") or {}
        feedback = model_feedback_gate(version) if version else {"weighted_accuracy": None, "should_degrade": False}
        hit_rate = _safe_float(metrics.get("live_hit_rate"), _safe_float(feedback.get("weighted_accuracy"), 0.0))
        live_ic = _safe_float(metrics.get("live_ic"), 0.0)
        turnover = _safe_float(metrics.get("live_turnover"), 0.0)
        drawdown_contribution = -abs(_safe_float(metrics.get("live_drawdown_contribution", 0.0)))
        has_live_signal = any(key in metrics for key in ("live_hit_rate", "live_ic", "live_turnover", "live_drawdown_contribution")) or bool(feedback.get("samples"))
        production = str(item.get("status") or "") == "production"
        decay = max(0.0, min(1.0, 0.5 - live_ic)) if has_live_signal and live_ic < 0.05 else max(0.0, 0.45 - hit_rate) if has_live_signal else 0.0
        should_degrade = production and has_live_signal and (bool(feedback.get("should_degrade")) or live_ic < -0.02 or (hit_rate > 0 and hit_rate < 0.38))
        multiplier = 0.55 if should_degrade else 0.8 if production and decay > 0.25 else 1.0
        status = "degraded" if should_degrade else "watch" if production and not has_live_signal else "watch" if multiplier < 1 else str(item.get("status") or "unknown")
        reason = "上线后指标失效，自动降权" if should_degrade else "上线后样本不足，先观察不降权" if production and not has_live_signal else "指标衰减，降低影响权重" if multiplier < 1 else "非生产模型不参与降权" if not production else "生产健康"
        payload = {
            "model_key": item.get("model_key"),
            "name": item.get("name"),
            "active_version": version,
            "status": status,
            "live_ic": round(live_ic, 6),
            "hit_rate": round(hit_rate, 6),
            "turnover": round(turnover, 6),
            "drawdown_contribution": round(drawdown_contribution, 6),
            "decay": round(decay, 6),
            "weight_multiplier": round(multiplier, 4),
            "reason": reason,
            "feedback": feedback,
        }
        with business_conn() as c:
            c.execute("""INSERT INTO model_live_health
                (model_key,active_version,status,live_ic,hit_rate,turnover,drawdown_contribution,decay,weight_multiplier,reason,metrics_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(model_key) DO UPDATE SET
                active_version=excluded.active_version,status=excluded.status,live_ic=excluded.live_ic,hit_rate=excluded.hit_rate,
                turnover=excluded.turnover,drawdown_contribution=excluded.drawdown_contribution,decay=excluded.decay,
                weight_multiplier=excluded.weight_multiplier,reason=excluded.reason,metrics_json=excluded.metrics_json,updated_at=excluded.updated_at""",
                (str(item.get("model_key") or ""), version, status, live_ic, hit_rate, turnover, drawdown_contribution,
                 decay, multiplier, reason, json.dumps(payload, ensure_ascii=False), _now()))
        rows.append(payload)
    return {"ok": True, "schema_version": "model_live_monitor_v1", "items": rows, "degraded_count": sum(1 for x in rows if x["weight_multiplier"] < 1)}


def build_portfolio_reoptimization() -> dict[str, Any]:
    positions = get_positions()
    symbols = [str(p.get("symbol") or "") for p in positions if str(p.get("symbol") or "")]
    returns = _position_returns(symbols)
    total_cost = sum(_safe_float(p.get("quantity")) * _safe_float(p.get("price")) for p in positions) or 1.0
    current_weights = {str(p.get("symbol")): _safe_float(p.get("quantity")) * _safe_float(p.get("price")) / total_cost for p in positions}
    expected = {symbol: (mean(values[-20:]) * 20 if values else 0.0) for symbol, values in returns.items()}
    risk = build_live_portfolio_risk()
    contracts = risk.get("target_weight_contracts") or []
    buckets = {str(c.get("symbol") or ""): str(c.get("function_bucket") or "narrative") for c in contracts}
    trading_costs = {symbol: 0.0015 for symbol in symbols}
    optimizer = two_group_cvar_optimizer(
        returns,
        expected_returns=expected,
        buckets=buckets,
        current_weights=current_weights,
        trading_costs=trading_costs,
        crowding={symbol: 0.0 for symbol in symbols},
    )
    return {
        "ok": True,
        "schema_version": "portfolio_reoptimization_v1",
        "optimizer": optimizer,
        "current_weights": {key: round(value, 6) for key, value in current_weights.items()},
        "risk_budget": {
            "status": risk.get("status"),
            "target_weights": risk.get("target_weights") or {},
            "breaches": risk.get("breaches") or [],
        },
        "interpretation": "组合层统一预期收益、波动、相关性、换手成本和风险预算；这里只生成模拟目标权重，不连接券商。",
    }


def build_sell_rebalance_diagnostics(model_monitor: dict[str, Any] | None = None, portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    positions = get_positions()
    ds = BacktestDataSource()
    trade_date = _latest_trade_date()
    model_degraded = bool((model_monitor or {}).get("degraded_count"))
    target_weights = ((portfolio or {}).get("optimizer") or {}).get("weights") or {}
    current_weights = (portfolio or {}).get("current_weights") or {}
    items = []
    learning_written = 0
    for pos in positions:
        symbol = str(pos.get("symbol") or "")
        cost = _safe_float(pos.get("price"))
        current = _safe_float(ds.get_price_on_date(symbol, trade_date, field="close"), cost)
        pnl_pct = current / cost - 1.0 if cost > 0 else 0.0
        current_weight = _safe_float(current_weights.get(symbol))
        target_weight = _safe_float(target_weights.get(symbol), current_weight)
        reasons = []
        if pnl_pct < -0.06:
            reasons.append({"code": "loss_exceeds_expected", "label": "亏损超预期", "detail": f"浮亏 {pnl_pct:.2%}，需要复核买入假设"})
        if target_weight + 0.01 < current_weight:
            reasons.append({"code": "risk_budget_shrink", "label": "风险预算收缩", "detail": f"目标权重 {target_weight:.2%} 低于当前 {current_weight:.2%}"})
        if model_degraded:
            reasons.append({"code": "model_decay", "label": "模型失效降权", "detail": "生产模型健康监控触发降权"})
        if pnl_pct > 0.25 and target_weight < current_weight:
            reasons.append({"code": "valuation_overextension", "label": "估值/涨幅透支", "detail": "已有较大盈利且组合目标降权"})
        action = "sell_or_reduce" if any(x["code"] in {"risk_budget_shrink", "model_decay", "loss_exceeds_expected"} for x in reasons) else "hold"
        if reasons:
            with business_conn() as c:
                c.execute("""INSERT INTO simulation_trade_learning_labels
                    (symbol,label_date,label,reason,pnl_pct,expected_vs_actual_json,created_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (symbol, trade_date, str(reasons[0]["code"]), str(reasons[0]["detail"]), pnl_pct,
                     json.dumps({"cost": cost, "current": current, "target_weight": target_weight, "current_weight": current_weight}, ensure_ascii=False), _now()))
            learning_written += 1
        items.append({
            "symbol": symbol,
            "name": pos.get("name") or "",
            "pnl_pct": round(pnl_pct, 6),
            "current_weight": round(current_weight, 6),
            "target_weight": round(target_weight, 6),
            "action": action,
            "reasons": reasons or [{"code": "thesis_intact", "label": "继续持有", "detail": "未触发模型、风险或亏损退出条件"}],
        })
    return {"ok": True, "schema_version": "sell_rebalance_diagnostics_v1", "items": items, "learning_labels_written": learning_written}


def build_simulation_learning_loop() -> dict[str, Any]:
    materialized = materialize_decision_outcomes(limit=2000)
    overview = review_learning_overview()
    with business_conn() as c:
        labels = [dict(row) for row in c.execute(
            "SELECT label,COUNT(*) count FROM simulation_trade_learning_labels GROUP BY label ORDER BY count DESC"
        ).fetchall()]
    return {"ok": True, "schema_version": "simulation_learning_loop_v1", "materialized": materialized, "overview": overview, "sell_labels": labels}


def build_profit_loop() -> dict[str, Any]:
    attribution = build_return_attribution_loop()
    monitor = build_model_live_monitor()
    portfolio = build_portfolio_reoptimization()
    sells = build_sell_rebalance_diagnostics(monitor, portfolio)
    learning = build_simulation_learning_loop()
    return {
        "ok": True,
        "schema_version": "profit_loop_v1",
        "generated_at": _now(),
        "attribution": attribution,
        "model_monitor": monitor,
        "portfolio_optimization": portfolio,
        "sell_rebalance": sells,
        "learning": learning,
        "disclaimer": "仅用于研究和模拟交易，不连接券商、不自动实盘下单。",
    }
