from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, time as dtime
from statistics import mean
from typing import Any

from app.data_platform.db import _conn as data_conn
from app.db.store import _conn as business_conn, get_account_baseline, get_positions
from app.services.backtest_data_source import BacktestDataSource
from app.services.decision_store import list_pool_members
from app.services.industry_allocator import build_industry_allocation
from app.services.market_context import live_market_context
from app.services.operations_overview import build_live_portfolio_risk
from app.services.plan_model_control import model_family_status
from app.services.review_learning import materialize_decision_outcomes, model_feedback_gate, review_learning_overview
from app.services.two_group_optimizer import two_group_cvar_optimizer


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
        c.execute("""CREATE TABLE IF NOT EXISTS daily_rebalance_plans (
            id BIGSERIAL PRIMARY KEY,
            trade_date VARCHAR(16) NOT NULL,
            planned_execute_date VARCHAR(16) NOT NULL DEFAULT '',
            version INTEGER NOT NULL,
            input_hash VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'planned',
            proposal_json TEXT NOT NULL,
            execution_id BIGINT,
            generated_at VARCHAR(64) NOT NULL,
            executed_at VARCHAR(64),
            UNIQUE(trade_date,version),
            UNIQUE(trade_date,input_hash)
        )""")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS planned_execute_date VARCHAR(16) NOT NULL DEFAULT ''")
        c.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS planned_execute_at VARCHAR(40) NOT NULL DEFAULT ''")


def _fallback_execution_clock() -> dtime:
    hour = max(0, min(int(os.getenv("QUANT_AUTO_SIMULATION_CLOSE_HOUR", "16")), 23))
    minute = max(0, min(int(os.getenv("QUANT_AUTO_SIMULATION_CLOSE_MINUTE", "5")), 59))
    return dtime(hour, minute)


def remembered_first_rebalance_clock() -> dtime | None:
    """Return the first formal rebalance's actual local clock, if recorded."""
    try:
        with business_conn() as c:
            row = c.execute(
                """SELECT executed_at FROM daily_rebalance_plans
                   WHERE status='executed' AND COALESCE(executed_at,'')<>''
                   ORDER BY executed_at ASC,id ASC LIMIT 1"""
            ).fetchone()
        value = str(dict(row).get("executed_at") or "") if row else ""
        parsed = datetime.fromisoformat(value)
        return dtime(parsed.hour, parsed.minute)
    except Exception:
        # Startup and schema bootstrap must keep the configured 16:05 fallback
        # even when the plan table is not available yet.
        return None


def effective_rebalance_clock() -> dtime:
    """Use 16:05 bootstrap time until the first formal execution fixes the clock."""
    return remembered_first_rebalance_clock() or _fallback_execution_clock()


def _planned_execute_at(execute_date: str) -> str:
    clock = effective_rebalance_clock()
    date_value = str(execute_date or "")[:10]
    if len(date_value) == 8 and date_value.isdigit():
        date_value = f"{date_value[:4]}-{date_value[4:6]}-{date_value[6:8]}"
    return f"{date_value}T{clock.hour:02d}:{clock.minute:02d}:00+08:00" if date_value else ""


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
            if not returns:
                raw_rows = [dict(row) for row in c.execute(
                    """SELECT payload_json FROM dc_tushare_raw_records
                       WHERE api_name='fund_daily' AND ts_code IN (?,?)
                       ORDER BY trade_date DESC LIMIT ?""",
                    (symbol, code, limit),
                ).fetchall()]
                parsed = []
                for row in reversed(raw_rows):
                    try:
                        payload = json.loads(row.get("payload_json") or "{}")
                        change = payload.get("pct_chg")
                        if change is not None:
                            parsed.append(float(change) / 100.0)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                returns = parsed
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
    capital = max(_safe_float(get_account_baseline(), 0.0), 1.0)
    cost = _safe_float(daily.get("total_cost", daily.get("transaction_cost", 0.0)))
    return_pct = _safe_float(daily.get("return_pct", daily.get("return", 0.0)))
    report_status = str(daily.get("attribution_status") or (daily.get("attribution") or {}).get("status") or "unknown")
    has_report_attribution = report_status == "complete"
    to_pct = lambda amount: _safe_float(amount) / capital * 100.0  # noqa: E731
    attribution = {
        "model_selection": to_pct(daily.get("stock_selection_contribution")) if has_report_attribution else _safe_float(outcome.get("selection")) * 100.0,
        "industry_exposure": to_pct(daily.get("industry_allocation_contribution")) if has_report_attribution else _safe_float(outcome.get("industry")) * 100.0,
        "position_sizing": _safe_float(monthly.get("position_contribution", 0.0)),
        "timing_gate": -abs(_safe_float((daily.get("market_state_gate") or {}).get("blocked_signals", 0.0))) * 0.001,
        "trading_cost": -abs(to_pct(cost)),
        "risk_overlay": to_pct(daily.get("cash_defensive_contribution")) if has_report_attribution else 0.0,
    }
    explained = sum(attribution.values())
    attribution["unexplained"] = return_pct - explained
    return {
        "ok": True,
        "schema_version": "profit_attribution_loop_v1",
        "latest_daily_return_pct": return_pct,
        "latest_monthly_return_pct": _safe_float(monthly.get("return_pct")),
        "unit": "percentage_points",
        "status": report_status,
        "components": {key: round(value, 6) for key, value in attribution.items()},
        "hit_rate": _safe_float(outcome.get("hit_rate")),
        "materialized": materialized,
        "limitations": list(daily.get("attribution_limitations") or []),
        "interpretation": "各项统一为收益率百分点；回撤只作为风险指标，不再伪装成负收益贡献。未解释部分继续进入复盘。",
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
        has_live_ic = metrics.get("live_ic") is not None
        has_hit_rate = metrics.get("live_hit_rate") is not None or feedback.get("weighted_accuracy") is not None
        hit_rate = _safe_float(metrics.get("live_hit_rate"), _safe_float(feedback.get("weighted_accuracy"), 0.0))
        live_ic = _safe_float(metrics.get("live_ic"), 0.0)
        turnover = _safe_float(metrics.get("live_turnover"), 0.0)
        drawdown_contribution = -abs(_safe_float(metrics.get("live_drawdown_contribution", 0.0)))
        has_live_signal = has_live_ic or has_hit_rate or bool(feedback.get("samples"))
        production = str(item.get("status") or "") == "production"
        decay = max(0.0, min(1.0, 0.05 - live_ic)) if has_live_ic and live_ic < 0.05 else max(0.0, 0.45 - hit_rate) if has_hit_rate else 0.0
        should_degrade = production and has_live_signal and (bool(feedback.get("should_degrade")) or (has_live_ic and live_ic < -0.02) or (has_hit_rate and hit_rate < 0.38))
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
    return {"ok": True, "schema_version": "model_live_monitor_v1", "items": rows, "degraded_count": sum(1 for x in rows if x["status"] == "degraded")}


def build_portfolio_reoptimization() -> dict[str, Any]:
    positions = get_positions()
    names = {str(p.get("symbol") or ""): str(p.get("name") or "") for p in positions}
    risk = build_live_portfolio_risk()
    holding_symbols = [str(p.get("symbol") or "") for p in positions if str(p.get("symbol") or "")]
    core_members = list_pool_members("core")
    candidate_members = list_pool_members("candidate")
    watch_members = list_pool_members("watch")
    core_symbols = [str(item.get("ts_code") or "").split(".")[0] for item in core_members if str(item.get("status") or "active") == "active"]
    candidate_symbols = [
        str(item.get("ts_code") or "").split(".")[0]
        for item in candidate_members[:20]
        if str(item.get("status") or "active") == "active"
    ]
    core_member_by_symbol = {
        str(item.get("ts_code") or "").split(".")[0]: item
        for item in core_members if str(item.get("status") or "active") == "active"
    }
    # Optimisation needs a real choice set.  Recently researched candidates
    # may compete for weight, while execution safety remains downstream.
    symbols = list(dict.fromkeys(holding_symbols + core_symbols + candidate_symbols))
    missing_names = [symbol for symbol in symbols if not names.get(symbol)]
    if missing_names:
        with data_conn() as c:
            placeholders = ",".join("?" for _ in missing_names)
            for row in c.execute(f"SELECT split_part(ts_code,'.',1) symbol,name FROM dc_stock_master WHERE split_part(ts_code,'.',1) IN ({placeholders})", tuple(missing_names)).fetchall():
                item = dict(row)
                names[str(item.get("symbol") or "")] = str(item.get("name") or "")
    returns = _position_returns(symbols)
    current_weights = {
        str(item.get("symbol") or ""): _safe_float(item.get("weight"))
        for item in (risk.get("exposures") or [])
        if str(item.get("symbol") or "")
    }
    if not current_weights:
        total_cost = sum(_safe_float(p.get("quantity")) * _safe_float(p.get("price")) for p in positions) or 1.0
        current_weights = {str(p.get("symbol")): _safe_float(p.get("quantity")) * _safe_float(p.get("price")) / total_cost for p in positions}
    expected = {symbol: (mean(values[-20:]) * 20 if values else 0.0) for symbol, values in returns.items()}
    contracts = risk.get("target_weight_contracts") or []
    buckets = {str(c.get("symbol") or ""): str(c.get("function_bucket") or "narrative") for c in contracts}
    exposures = {str(item.get("symbol") or ""): item for item in (risk.get("exposures") or [])}
    position_map = {str(item.get("symbol") or ""): item for item in positions}
    from app.services.execution_cost_calibration import active_execution_cost_contract
    execution_cost_contract = active_execution_cost_contract()
    one_way_cost = max(
        0.0003,
        _safe_float(execution_cost_contract.get("round_trip_rate")) / 2.0,
    )
    trading_costs = {symbol: one_way_cost for symbol in symbols}
    try:
        context = live_market_context()
    except Exception:
        context = {}
    state = str((context.get("market_state") or {}).get("state") or "S1")
    market_budget = {"S0": 1.0, "S1": 0.85, "S2": 0.55, "S3": 0.70}.get(state, 0.55)
    profile_budget = max(0.85, min(1.0, _safe_float((context.get("market_profile") or {}).get("risk_budget_multiplier"), 1.0)))
    risk_status = str(risk.get("status") or "unknown")
    portfolio_budget = 0.0 if risk_status == "blocked" else 0.75 if risk_status == "warning" else 1.0 if risk_status == "ready" else 0.5
    equity_budget = round(market_budget * profile_budget * portfolio_budget, 6)
    equity_candidates = [symbol for symbol in symbols if symbol not in {"510880", "511010", "511880", "512890", "518880"}]
    industry_allocation = build_industry_allocation(
        symbols=equity_candidates,
        symbol_scores={symbol: _safe_float(expected.get(symbol)) * 100 for symbol in equity_candidates},
        equity_budget=equity_budget,
        market_state=state,
    )
    admitted_industries = set((industry_allocation.get("industry_targets") or {}).keys())
    symbol_industries = industry_allocation.get("symbol_industries") or {}
    candidate_blocks = []
    for symbol in list(dict.fromkeys(core_symbols + candidate_symbols)):
        industry = str(symbol_industries.get(symbol) or "未分类")
        if industry not in admitted_industries:
            candidate_blocks.append({"symbol": symbol, "name": names.get(symbol, ""), "industry": industry, "code": "industry_soft_penalty", "reason": "所属行业未获得优先预算，已施加软惩罚但未禁止入选"})
    industry_penalty = {
        symbol: (0.002 if symbol in equity_candidates and str(symbol_industries.get(symbol) or "未分类") not in admitted_industries else 0.0)
        for symbol in symbols
    }
    optimizer = two_group_cvar_optimizer(
        returns,
        expected_returns=expected,
        buckets=buckets,
        current_weights=current_weights,
        trading_costs=trading_costs,
        crowding=industry_penalty,
    )
    raw_weights = dict(optimizer.get("weights") or {})
    from app.services.portfolio_method_competition import compete_portfolio_methods
    method_competition = compete_portfolio_methods(
        returns_by_symbol=returns,
        expected_returns=expected,
        current_weights=current_weights,
        trading_costs=trading_costs,
        equity_budget=equity_budget,
        cvar_weights=raw_weights,
    )
    scaled_weights = {
        symbol: round(_safe_float(weight), 8)
        for symbol, weight in (method_competition.get("weights") or {}).items()
    }
    optimizer["method_competition"] = method_competition
    optimizer["selected_method"] = method_competition.get("selected_method")
    if method_competition.get("ok"):
        optimizer["ok"] = True
        optimizer["status"] = f"selected_{method_competition.get('selected_method')}"
    selected_weights_before_forced_exits = dict(scaled_weights)
    block_by_symbol = {str(item.get("symbol") or ""): item for item in candidate_blocks}
    # A negative trend estimate may justify holding a diversifier, but never an automatic increase.
    for symbol, target in list(scaled_weights.items()):
        if _safe_float(expected.get(symbol)) < 0 and target > _safe_float(current_weights.get(symbol)):
            scaled_weights[symbol] = round(_safe_float(current_weights.get(symbol)), 8)
        position = position_map.get(symbol) or {}
        quantity = _safe_float(position.get("quantity"))
        cost = _safe_float(position.get("price"))
        market_value = _safe_float((exposures.get(symbol) or {}).get("market_value"))
        live_price = market_value / quantity if quantity > 0 and market_value > 0 else cost
        pnl_pct = live_price / cost - 1.0 if cost > 0 else 0.0
        if pnl_pct <= -0.06 and _safe_float(expected.get(symbol)) <= -0.05:
            scaled_weights[symbol] = 0.0
    if scaled_weights != selected_weights_before_forced_exits:
        optimizer["selected_method"] = "forced_risk_exit"
        optimizer["status"] = "forced_risk_exit"
        optimizer["method_competition"]["selected_method"] = "forced_risk_exit"
        optimizer["forced_exit_policy"] = "仅对满足冻结止损合同的持仓执行退出；不得以 hold_current 名义表达减仓或清仓"
    error_breaches = [item for item in risk.get("breaches") or [] if str(item.get("severity") or "") == "error"]
    preliminary_improvement = sum(
        (_safe_float(scaled_weights.get(symbol)) - _safe_float(current_weights.get(symbol))) * _safe_float(expected.get(symbol))
        for symbol in set(current_weights) | set(scaled_weights)
    )
    preliminary_cost = sum(
        abs(_safe_float(scaled_weights.get(symbol)) - _safe_float(current_weights.get(symbol))) * _safe_float(trading_costs.get(symbol))
        for symbol in set(current_weights) | set(scaled_weights)
    )
    if not error_breaches and preliminary_improvement <= preliminary_cost:
        scaled_weights = {symbol: round(weight, 8) for symbol, weight in current_weights.items()}
        optimizer["status"] = "hold_current_cost_gate"
        optimizer["cost_gate"] = {
            "triggered": True,
            "expected_improvement": round(preliminary_improvement, 8),
            "estimated_one_way_cost": round(preliminary_cost, 8),
            "reason": "目标组合的预期改善不能覆盖交易成本，保持当前持仓",
        }
    else:
        optimizer["cost_gate"] = {"triggered": False}
    optimizer["unscaled_weights"] = raw_weights
    optimizer["weights"] = scaled_weights
    optimizer["equity_budget"] = equity_budget
    actual_equity_budget = round(sum(scaled_weights.values()), 8)
    optimizer["cash_target"] = round(1.0 - actual_equity_budget, 6)
    optimizer["weight_sum"] = actual_equity_budget
    proposal = []
    account_equity = _safe_float((risk.get("account") or {}).get("equity"))
    explanations = optimizer.get("explanations") or {}
    for symbol in sorted(set(current_weights) | set(scaled_weights)):
        current_weight = _safe_float(current_weights.get(symbol))
        target_weight = _safe_float(scaled_weights.get(symbol))
        delta = target_weight - current_weight
        detail = explanations.get(symbol) or {}
        expected_return = _safe_float(expected.get(symbol))
        position = position_map.get(symbol) or {}
        quantity = _safe_float(position.get("quantity"))
        cost_basis = _safe_float(position.get("price"))
        market_value = _safe_float((exposures.get(symbol) or {}).get("market_value"))
        live_price = market_value / quantity if quantity > 0 and market_value > 0 else cost_basis
        pnl_pct = live_price / cost_basis - 1.0 if cost_basis > 0 else 0.0
        full_exit = current_weight > 0.002 and target_weight <= 0.002
        new_candidate = current_weight <= 0.002 and symbol in set(core_symbols + candidate_symbols)
        proposal.append({
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "action": "sell" if full_exit else "buy" if delta > 0.002 else "observe" if new_candidate else "reduce" if delta < -0.002 else "hold",
            "current_weight": round(current_weight, 8),
            "target_weight": round(target_weight, 8),
            "weight_change": round(delta, 8),
            "current_amount": round(current_weight * account_equity, 2),
            "target_amount": round(target_weight * account_equity, 2),
            "estimated_trade_amount": round(abs(delta) * account_equity, 2),
            "expected_20d_return": round(expected_return, 8),
            "expected_return_contribution_before": round(current_weight * expected_return, 8),
            "expected_return_contribution_after": round(target_weight * expected_return, 8),
            "estimated_one_way_cost": round(abs(delta) * _safe_float(trading_costs.get(symbol)), 8),
            "cvar_contribution_after": round(_safe_float(detail.get("cvar_contribution")) * equity_budget, 8),
            "function_bucket": buckets.get(symbol, "narrative"),
            "reason": str((block_by_symbol.get(symbol) or {}).get("reason") or "核心池候选尚未形成正向收益与风险预算共振") if new_candidate and delta <= 0.002
                else "浮亏超过6%且20日预期低于-5%，触发完整退出" if full_exit and pnl_pct <= -0.06 and expected_return <= -0.05
                else "目标权重归零，退出组合" if full_exit
                else "负预期收益不新增仓位" if expected_return < 0 and target_weight >= current_weight
                else "目标组合提高该资产配置" if delta > 0.002
                else "风险预算或组合相关性要求降低配置" if delta < -0.002
                else "权重偏差不足以覆盖调仓成本",
            "candidate_source": "core_pool" if symbol in core_symbols and symbol not in holding_symbols else "candidate_pool" if symbol in candidate_symbols and symbol not in holding_symbols else "holding",
            "candidate_lineage": ({
                "entry_pool": "core",
                "entered_at": str((core_member_by_symbol.get(symbol) or {}).get("entered_at") or ""),
                "source_reason": str((core_member_by_symbol.get(symbol) or {}).get("reason") or ""),
                "path": ["全市场", "基础过滤", "候选池", "财务研究", "行业预算", "核心池", "组合优化"],
            } if symbol in core_symbols and symbol not in holding_symbols else ({
                "entry_pool": "candidate",
                "entered_at": "",
                "source_reason": "active_candidate_pool",
                "path": ["全市场", "基础过滤", "候选池", "组合优化"],
            } if symbol in candidate_symbols and symbol not in holding_symbols else None)),
        })
    current_expected = sum(_safe_float(item["expected_return_contribution_before"]) for item in proposal)
    target_expected = sum(_safe_float(item["expected_return_contribution_after"]) for item in proposal)
    estimated_cost = sum(_safe_float(item["estimated_one_way_cost"]) for item in proposal)
    turnover = sum(abs(_safe_float(item["weight_change"])) for item in proposal)
    expected_improvement = target_expected - current_expected
    try:
        from app.services.simulation_ledger_audit import audit_simulation_ledger
        ledger_audit = audit_simulation_ledger(persist=False)
    except Exception as exc:
        ledger_audit = {"ok": False, "status": "error", "issues": [{"message": str(exc)[:300]}]}
    try:
        from app.services.data_health import assess_latest_decision_data_health
        decision_data_gate = assess_latest_decision_data_health()
    except Exception as exc:
        decision_data_gate = {
            "decision_ready": False,
            "status": "blocked",
            "issues": [{"message": f"决策数据门检查失败：{str(exc)[:200]}"}],
            "readiness": {},
            "latest_snapshot": {},
        }
    ready_trade_date = str((decision_data_gate.get("readiness") or {}).get("latest_decision_ready_trade_date") or "")
    target_trade_date = str((decision_data_gate.get("readiness") or {}).get("target_trade_date") or "")
    decision_issue_text = "；".join(
        str(item.get("message") or "") for item in decision_data_gate.get("issues") or [] if item.get("message")
    )
    execution_checks = [
        {"key": "market_data", "label": "最新决策数据门", "status": "pass" if decision_data_gate.get("decision_ready") else "block", "detail": (
            f"{ready_trade_date or target_trade_date} 收盘数据已通过完整性检查"
            if decision_data_gate.get("decision_ready")
            else decision_issue_text or f"目标交易日 {target_trade_date or '未知'} 未完成，最近可用 {ready_trade_date or '无'}"
        )},
        {"key": "simulation_ledger", "label": "模拟账户账本一致", "status": "pass" if ledger_audit.get("ok") else "block", "detail": (
            f"现金与 {len(ledger_audit.get('actual_positions') or {})} 个持仓均可由账本独立重算"
            if ledger_audit.get("ok")
            else "；".join(str(item.get("message") or "") for item in ledger_audit.get("issues") or []) or "账本审计不可用"
        )},
        {"key": "optimizer", "label": "组合优化已完成", "status": "pass" if optimizer.get("ok") else "block", "detail": (
            "目标组合改善不足以覆盖成本，保持当前持仓" if (optimizer.get("cost_gate") or {}).get("triggered")
            else f"已生成目标权重（{optimizer.get('status') or 'optimal'}）" if optimizer.get("ok")
            else "；".join(optimizer.get("blocked_reasons") or ["优化器没有有效解"])
        )},
        {"key": "risk_gate", "label": "风险门控", "status": "block" if error_breaches else "warn" if risk.get("breaches") else "pass", "detail": f"{len(error_breaches)} 项阻断，{len(risk.get('breaches') or []) - len(error_breaches)} 项提醒"},
        {"key": "cost_benefit", "label": "收益改善覆盖成本", "status": "pass" if expected_improvement > estimated_cost else "warn", "detail": f"预期改善 {expected_improvement:.2%}，单边成本 {estimated_cost:.2%}"},
    ]
    return {
        "ok": True,
        "schema_version": "portfolio_reoptimization_v1",
        "optimizer": optimizer,
        "current_weights": {key: round(value, 6) for key, value in current_weights.items()},
        "rebalance_proposal": proposal,
        "portfolio_comparison": {
            "current": {"expected_20d_return": round(current_expected, 8), "cash_weight": round(max(0.0, 1.0 - sum(current_weights.values())), 8), "max_position_weight": round(max(current_weights.values(), default=0.0), 8)},
            "target": {"expected_20d_return": round(target_expected, 8), "cash_weight": optimizer["cash_target"], "max_position_weight": round(max(scaled_weights.values(), default=0.0), 8)},
            "expected_improvement": round(expected_improvement, 8), "estimated_one_way_cost": round(estimated_cost, 8),
            "net_expected_improvement": round(expected_improvement - estimated_cost, 8), "turnover": round(turnover, 8),
            "method": "当前与目标权重乘以各资产历史20日均值；未承诺未来收益",
        },
        "execution_checks": execution_checks,
        "execution_authorized": not any(item["status"] == "block" for item in execution_checks),
        "decision_data_gate": {
            "status": str(decision_data_gate.get("status") or "blocked"),
            "decision_ready": bool(decision_data_gate.get("decision_ready")),
            "target_trade_date": target_trade_date,
            "latest_decision_ready_trade_date": ready_trade_date,
            "issues": decision_data_gate.get("issues") or [],
        },
        "proposal_unit": "portfolio_fraction",
        "candidate_universe": {
            "holding_count": len(holding_symbols),
            "watch_count": len(watch_members),
            "candidate_count": len(candidate_members),
            "core_count": len(core_symbols),
            "optimization_count": len(returns),
            "new_core_symbols": [symbol for symbol in core_symbols if symbol not in holding_symbols],
            "industry_allocation": industry_allocation,
            "blocked_candidates": candidate_blocks,
            "admission_policy": "当前持仓、核心池及最近20只有效候选共同优化；行业偏好作为软惩罚，账户与交易安全由执行层硬门控",
        },
        "execution_cost_contract": execution_cost_contract,
        "risk_budget": {
            "status": risk.get("status"),
            "equity_budget": actual_equity_budget,
            "cash_target": round(1.0 - actual_equity_budget, 6),
            "target_weights": risk.get("target_weights") or {},
            "breaches": risk.get("breaches") or [],
        },
        "interpretation": "组合层统一预期收益、波动、相关性、换手成本和风险预算；这里只生成模拟目标权重，不连接券商。",
    }


def build_portfolio_candles(limit: int = 90) -> dict[str, Any]:
    """Reconstruct simulated account OHLC and its point-in-time benchmark."""
    limit = max(20, min(int(limit), 250))
    with business_conn() as c:
        operations = [dict(row) for row in c.execute("""SELECT symbol,side,quantity,price,at,
            COALESCE(commission,0) commission,COALESCE(transfer_fee,0) transfer_fee,
            COALESCE(stamp_tax,0) stamp_tax FROM operations
            WHERE UPPER(side) IN ('BUY','SELL') ORDER BY at,id""").fetchall()]
    if not operations:
        return {"ok": True, "schema_version": "portfolio_candles_v1", "series": [], "position_count": 0,
            "unit": "indexed_account_equity", "base": 100, "data_as_of_date": "",
            "benchmark": {"symbol": "000300.SH", "name": "沪深300", "available": False},
            "latest_comparison": {},
            "corporate_actions": {"count": 0, "cash_dividend_total": 0.0, "stock_dividend_shares": 0},
            "methodology": "无模拟成交账本记录。"}
    symbols = sorted({str(item.get("symbol") or "") for item in operations if item.get("symbol")})
    bars: dict[str, dict[str, dict[str, float]]] = {}
    with data_conn() as c:
        for symbol in symbols:
            code = symbol if "." in symbol else f"{symbol}.SH" if symbol.startswith(("5", "6", "9")) else f"{symbol}.SZ"
            raw = [dict(row) for row in c.execute("""SELECT trade_date,payload_json
                FROM dc_tushare_raw_records WHERE api_name IN ('daily','fund_daily') AND ts_code IN (?,?)
                ORDER BY trade_date DESC LIMIT ?""", (symbol, code, limit)).fetchall()]
            rows = []
            for item in raw:
                try:
                    payload = json.loads(item.get("payload_json") or "{}")
                    rows.append({"trade_date": item.get("trade_date"), **payload})
                except json.JSONDecodeError:
                    continue
            bars[symbol] = {str(row.get("trade_date") or ""): {field: _safe_float(row.get(field)) for field in ("open", "high", "low", "close")}
                for row in rows if row.get("trade_date") and all(_safe_float(row.get(field)) > 0 for field in ("open", "high", "low", "close"))}
    first_date = str(operations[0].get("at") or "")[:10].replace("-", "")
    dates = sorted({date for symbol_bars in bars.values() for date in symbol_bars if date >= first_date})[-limit:]
    from app.services.corporate_actions import build_replay_corporate_actions, load_dividend_events
    dividend_events = load_dividend_events(
        symbols,
        as_of_date=dates[-1] if dates else first_date,
        strict_event_time=True,
    )
    corporate_actions = build_replay_corporate_actions(operations, dividend_events)
    cash = _safe_float(get_account_baseline())
    quantities: dict[str, float] = {}
    op_index = 0
    action_index = 0
    raw_series: list[tuple[str, dict[str, float]]] = []
    for date in dates:
        while op_index < len(operations) and str(operations[op_index].get("at") or "")[:10].replace("-", "") <= date:
            operation = operations[op_index]
            symbol = str(operation.get("symbol") or "")
            quantity = _safe_float(operation.get("quantity"))
            amount = quantity * _safe_float(operation.get("price"))
            fees = sum(_safe_float(operation.get(key)) for key in ("commission", "transfer_fee", "stamp_tax"))
            if str(operation.get("side") or "").upper() == "BUY":
                quantities[symbol] = quantities.get(symbol, 0.0) + quantity
                cash -= amount + fees
            else:
                quantities[symbol] = max(0.0, quantities.get(symbol, 0.0) - quantity)
                cash += amount - fees
            op_index += 1
        while action_index < len(corporate_actions) and str(corporate_actions[action_index].get("event_date") or "") <= date:
            action = corporate_actions[action_index]
            symbol = str(action.get("symbol") or "")
            if str(action.get("action_type") or "") == "cash_dividend":
                cash += _safe_float(action.get("cash_amount"))
            elif str(action.get("action_type") or "") == "stock_dividend":
                quantities[symbol] = quantities.get(symbol, 0.0) + int(action.get("share_quantity") or 0)
            action_index += 1
        point = {field: cash for field in ("open", "high", "low", "close")}
        complete = True
        for symbol, quantity in quantities.items():
            if quantity <= 0:
                continue
            bar = bars.get(symbol, {}).get(date)
            if not bar:
                complete = False
                break
            for field in point:
                point[field] += bar[field] * quantity
        if complete:
            raw_series.append((date, point))
    base = raw_series[0][1]["close"] if raw_series else 0.0
    series = [{
        "date": date,
        **{field: round(point[field] / base * 100.0, 4) if base > 0 else 0.0 for field in ("open", "high", "low", "close")},
        "change_pct": round((point["close"] / raw_series[index - 1][1]["close"] - 1) * 100, 4) if index > 0 else 0.0,
    } for index, (date, point) in enumerate(raw_series)]
    benchmark_closes = _load_point_in_time_benchmark_closes([item["date"] for item in series])
    series, latest_comparison = _attach_benchmark_series(series, benchmark_closes)
    return {
        "ok": True,
        "schema_version": "portfolio_candles_v2",
        "series": series,
        "position_count": len([quantity for quantity in quantities.values() if quantity > 0]),
        "unit": "indexed_account_equity",
        "base": 100,
        "data_as_of_date": series[-1]["date"] if series else "",
        "benchmark": {
            "symbol": "000300.SH",
            "name": "沪深300",
            "available": bool(benchmark_closes),
            "point_in_time_cutoff": "每个交易日15:00",
        },
        "latest_comparison": latest_comparison,
        "corporate_actions": {
            "count": len(corporate_actions),
            "cash_dividend_total": round(sum(_safe_float(item.get("cash_amount")) for item in corporate_actions), 2),
            "stock_dividend_shares": sum(int(item.get("share_quantity") or 0) for item in corporate_actions),
            "policy": "现金分红在派息日入账，送股在除权日增加数量；资格按股权登记日模拟持仓冻结。",
        },
        "methodology": "从首笔模拟成交开始逐日重放买卖、费用、分红和现金，聚合当日持仓开高低收；账户与点时沪深300均在共同首日归一化为100。",
    }


def _load_point_in_time_benchmark_closes(dates: list[str]) -> dict[str, float]:
    normalized_dates = sorted({str(value or "").replace("-", "")[:8] for value in dates if value})
    if not normalized_dates:
        return {}
    try:
        with data_conn() as c:
            rows = [
                dict(row) for row in c.execute(
                    """SELECT id,trade_date,payload_json,available_time
                       FROM dc_tushare_raw_records
                       WHERE api_name='index_daily' AND ts_code='000300.SH'
                         AND trade_date>=? AND trade_date<=?
                       ORDER BY trade_date,available_time,id""",
                    (normalized_dates[0], normalized_dates[-1]),
                ).fetchall()
            ]
    except Exception:
        return {}
    wanted = set(normalized_dates)
    result: dict[str, float] = {}
    for row in rows:
        trade_date = str(row.get("trade_date") or "").replace("-", "")[:8]
        if trade_date not in wanted or not _available_by_market_close(row.get("available_time"), trade_date):
            continue
        try:
            close = float(json.loads(str(row.get("payload_json") or "{}")).get("close") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if close > 0:
            result[trade_date] = close
    return result


def _available_by_market_close(value: Any, trade_date: str) -> bool:
    if not value:
        return False
    try:
        available = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        cutoff = datetime.strptime(str(trade_date), "%Y%m%d").replace(hour=15)
        if available.tzinfo is not None:
            cutoff = cutoff.replace(tzinfo=available.tzinfo)
        return available <= cutoff
    except (TypeError, ValueError):
        return False


def _attach_benchmark_series(
    series: list[dict[str, Any]],
    benchmark_closes: dict[str, float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    common = [
        item for item in series
        if _safe_float(benchmark_closes.get(str(item.get("date") or ""))) > 0
    ]
    if not common:
        return series, {}
    benchmark_base = _safe_float(benchmark_closes[str(common[0]["date"])])
    previous_benchmark_index: float | None = None
    enriched: list[dict[str, Any]] = []
    for item in series:
        output = dict(item)
        raw_close = _safe_float(benchmark_closes.get(str(item.get("date") or "")))
        if raw_close > 0 and benchmark_base > 0:
            benchmark_index = raw_close / benchmark_base * 100.0
            benchmark_change = (
                (benchmark_index / previous_benchmark_index - 1.0) * 100.0
                if previous_benchmark_index and previous_benchmark_index > 0 else 0.0
            )
            portfolio_close = _safe_float(item.get("close"))
            output.update({
                "benchmark_close": round(benchmark_index, 4),
                "benchmark_change_pct": round(benchmark_change, 4),
                "excess_close": round(portfolio_close - benchmark_index, 4),
                "excess_change_pct": round(_safe_float(item.get("change_pct")) - benchmark_change, 4),
            })
            previous_benchmark_index = benchmark_index
        else:
            output.update({
                "benchmark_close": None,
                "benchmark_change_pct": None,
                "excess_close": None,
                "excess_change_pct": None,
            })
        enriched.append(output)
    latest = next(
        (item for item in reversed(enriched) if item.get("benchmark_close") is not None),
        None,
    )
    if not latest:
        return enriched, {}
    portfolio_return = _safe_float(latest.get("close")) - 100.0
    benchmark_return = _safe_float(latest.get("benchmark_close")) - 100.0
    return enriched, {
        "date": latest.get("date"),
        "portfolio_return_pct": round(portfolio_return, 4),
        "benchmark_return_pct": round(benchmark_return, 4),
        "excess_return_pct": round(portfolio_return - benchmark_return, 4),
        "portfolio_daily_return_pct": latest.get("change_pct"),
        "benchmark_daily_return_pct": latest.get("benchmark_change_pct"),
        "daily_excess_return_pct": latest.get("excess_change_pct"),
    }


def build_model_return_contribution() -> dict[str, Any]:
    """Expose honest model evidence while counterfactual portfolio returns accumulate."""
    _ensure_tables()
    with business_conn() as c:
        rows = [dict(row) for row in c.execute("""SELECT d.model_version,a.horizon_sessions,
            COUNT(*) samples,
            AVG(a.directional_return) avg_directional_return,
            AVG(a.benchmark_return) avg_benchmark_return,
            AVG(a.directional_return-COALESCE(a.benchmark_return,0)) avg_excess_return,
            AVG(CASE WHEN a.correct=1 THEN 1.0 ELSE 0.0 END) hit_rate
            FROM decision_outcome_attributions a
            JOIN decision_snapshots d ON d.id=a.decision_snapshot_id
            GROUP BY d.model_version,a.horizon_sessions
            ORDER BY d.model_version,a.horizon_sessions""").fetchall()]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("model_version") or "unknown"), []).append({
            "horizon_sessions": int(row.get("horizon_sessions") or 0),
            "samples": int(row.get("samples") or 0),
            "avg_directional_return_pct": round(_safe_float(row.get("avg_directional_return")) * 100, 6),
            "avg_benchmark_return_pct": round(_safe_float(row.get("avg_benchmark_return")) * 100, 6),
            "avg_excess_return_pct": round(_safe_float(row.get("avg_excess_return")) * 100, 6),
            "hit_rate": round(_safe_float(row.get("hit_rate")), 6),
        })
    family = model_family_status()
    items = []
    for model in family.get("items") or []:
        active = model.get("active_version") or model.get("latest_version") or {}
        version = str(active.get("version") or "")
        evidence = grouped.get(version, [])
        mature = next((x for x in evidence if x["horizon_sessions"] == 20), None)
        items.append({
            "model_key": model.get("model_key"),
            "name": model.get("name"),
            "model_version": version,
            "decision_evidence": evidence,
            "preferred_20d_evidence": mature,
            "portfolio_return_contribution_pct": None,
            "marginal_return_contribution_pct": None,
            "contribution_status": "insufficient_samples",
            "required_method": "leave_one_model_out_shadow_portfolio_v1",
            "minimum_shadow_sessions": 20,
            "reason": "决策结果可用于验证方向与超额收益，但没有逐日影子组合净值，不能可靠归因为账户收益贡献。",
        })
    return {
        "ok": True,
        "schema_version": "model_return_contribution_v1",
        "items": items,
        "methodology": {
            "decision_evidence": "按模型版本和固定持有期聚合已成熟决策结果",
            "portfolio_contribution": "必须用完整组合与剔除单模型的影子组合逐日净值差计算",
            "no_fabrication": True,
        },
    }


def materialize_daily_rebalance_plan(portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist one immutable plan version per materially different daily input."""
    _ensure_tables()
    portfolio = portfolio or build_portfolio_reoptimization()
    decision_gate = portfolio.get("decision_data_gate") if isinstance(portfolio.get("decision_data_gate"), dict) else {}
    trade_date = str(decision_gate.get("target_trade_date") or _latest_trade_date())
    with data_conn() as c:
        next_row = c.execute(
            "SELECT MIN(cal_date) value FROM dc_trade_calendar WHERE is_open=1 AND cal_date>?", (trade_date,)
        ).fetchone() if trade_date else None
    planned_execute_date = str(dict(next_row or {}).get("value") or "")
    planned_execute_at = _planned_execute_at(planned_execute_date)
    proposal = portfolio.get("rebalance_proposal") or []
    execution_authorized = bool(portfolio.get("execution_authorized"))
    from app.services.decision_version_manifest import build_decision_version_manifest
    decision_version_manifest = build_decision_version_manifest(
        trade_date=trade_date,
        portfolio=portfolio,
    )
    contract = {
        "trade_date": trade_date,
        "proposal": proposal,
        "risk_budget": portfolio.get("risk_budget") or {},
        "execution_authorized": execution_authorized,
        "decision_data_gate": decision_gate,
        "decision_version_manifest": decision_version_manifest,
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with business_conn() as c:
        existing = c.execute(
            "SELECT * FROM daily_rebalance_plans WHERE trade_date=? AND input_hash=?",
            (trade_date, input_hash),
        ).fetchone()
        if existing:
            row = dict(existing)
        else:
            latest = c.execute(
                "SELECT COALESCE(MAX(version),0) version FROM daily_rebalance_plans WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            version = int(dict(latest or {}).get("version") or 0) + 1
            if version > 1:
                c.execute("UPDATE daily_rebalance_plans SET status='superseded' WHERE trade_date=? AND status='planned'", (trade_date,))
            plan_status = "planned" if execution_authorized else "blocked_by_decision_data"
            c.execute("""INSERT INTO daily_rebalance_plans
                (trade_date,planned_execute_date,planned_execute_at,version,input_hash,status,proposal_json,generated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (trade_date, planned_execute_date, planned_execute_at, version, input_hash, plan_status, canonical, _now()))
            row = dict(c.execute(
                "SELECT * FROM daily_rebalance_plans WHERE trade_date=? AND version=?", (trade_date, version)
            ).fetchone())
    return {
        "id": int(row["id"]),
        "trade_date": trade_date,
        "data_as_of_date": trade_date,
        "planned_execute_date": str(row.get("planned_execute_date") or planned_execute_date),
        "planned_execute_at": str(row.get("planned_execute_at") or planned_execute_at),
        "valid_until_date": str(row.get("planned_execute_date") or planned_execute_date),
        "version": int(row["version"]),
        "plan_version": f"{trade_date}-v{int(row['version'])}",
        "status": row.get("status") or ("planned" if execution_authorized else "blocked_by_decision_data"),
        "is_executed": str(row.get("status") or "") == "executed",
        "execution_authorized": execution_authorized and str(row.get("status") or "") in {"planned", "executed"},
        "blocked_reason": "" if execution_authorized else "最新交易日决策数据门未通过，当前方案仅作为不可执行预览",
        "execution_id": row.get("execution_id"),
        "generated_at": row.get("generated_at"),
        "executed_at": row.get("executed_at"),
        "input_hash": input_hash,
        "decision_version_manifest": decision_version_manifest,
        "generation_policy": "仅在最新交易日决策数据门通过后生成可执行版本；未通过时保留不可执行预览",
        "execution_policy": (
            f"执行日{effective_rebalance_clock().strftime('%H:%M')}后检查；"
            "首次正式调仓后沿用其实际执行时分；收盘数据完整时自动执行，每个交易日最多一次"
        ),
    }


def get_daily_rebalance_execution_input(signal_date: str, execute_date: str) -> dict[str, Any] | None:
    """Return the latest immutable dashboard plan when it is due for execution.

    Historical replay deliberately gets no override: only a still-planned row
    whose signal and execution dates exactly match may drive live simulation.
    """
    _ensure_tables()
    signal = str(signal_date or "").replace("-", "")[:8]
    execute = str(execute_date or "").replace("-", "")[:8]
    with business_conn() as c:
        row = c.execute(
            """SELECT * FROM daily_rebalance_plans
               WHERE replace(trade_date,'-','')=?
                 AND replace(planned_execute_date,'-','')=?
                 AND status='planned'
               ORDER BY version DESC LIMIT 1""",
            (signal, execute),
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    try:
        contract = json.loads(record.get("proposal_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    proposal = contract.get("proposal") if isinstance(contract, dict) else None
    if not isinstance(proposal, list):
        return None
    target_weights = {
        str(item.get("symbol") or "").strip(): max(0.0, _safe_float(item.get("target_weight")))
        for item in proposal if str(item.get("symbol") or "").strip()
    }
    target_symbols = [symbol for symbol, weight in target_weights.items() if weight > 0.002]
    return {
        "plan_id": int(record["id"]),
        "plan_version": f"{record['trade_date']}-v{int(record['version'])}",
        "input_hash": str(record.get("input_hash") or ""),
        "signal_date": signal,
        "execute_date": execute,
        "target_symbols": target_symbols,
        "target_weights": target_weights,
        "proposal": proposal,
        "risk_budget": contract.get("risk_budget") or {},
        "decision_version_manifest": contract.get("decision_version_manifest") or {},
    }


def mark_daily_rebalance_plan_executed(signal_date: str, execute_date: str, execution_id: int | None) -> None:
    """Bind execution to the immutable plan built from the prior close signal."""
    signal = str(signal_date or "").replace("-", "")[:8]
    executed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with business_conn() as c:
        c.execute("""UPDATE daily_rebalance_plans SET status='executed',execution_id=?,executed_at=?
            WHERE id=(SELECT id FROM daily_rebalance_plans
                      WHERE trade_date=? AND status='planned' ORDER BY version DESC LIMIT 1)""",
            (execution_id, executed_at, signal))


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
        if model_degraded and target_weight + 0.01 < current_weight:
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
    rebalance_plan = materialize_daily_rebalance_plan(portfolio)
    portfolio_candles = build_portfolio_candles()
    sells = build_sell_rebalance_diagnostics(monitor, portfolio)
    learning = build_simulation_learning_loop()
    model_contribution = build_model_return_contribution()
    return {
        "ok": True,
        "schema_version": "profit_loop_v1",
        "generated_at": _now(),
        "attribution": attribution,
        "model_monitor": monitor,
        "model_return_contribution": model_contribution,
        "portfolio_optimization": portfolio,
        "portfolio_candles": portfolio_candles,
        "rebalance_plan": rebalance_plan,
        "sell_rebalance": sells,
        "learning": learning,
        "disclaimer": "仅用于研究和模拟交易，不连接券商、不自动实盘下单。",
    }
