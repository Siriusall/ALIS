from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional
from types import SimpleNamespace

from app.api.state import add_log
from app.core.trading_constraints import check_open_trade_constraints
from app.core.a_share_fees import ETF_FEE_SCHEDULE_VERSION, FEE_SCHEDULE_VERSION, is_exchange_etf, sell_cost_breakdown
from app.core.exchange_groups import matches_exchange_group, normalize_exchange_group
from app.db.store import (
    create_and_apply_buy_execution,
    create_execution,
    get_account_balance,
    get_positions,
    get_strategy,
    list_operations,
    link_recorded_operations_to_execution,
    sell_position,
)
from app.integrations.broker_adapter import OrderRequest
from app.services.backtest_data_source import BacktestDataSource
from app.services.backtest_engine import _apply_filters, _apply_universe_controls, _factor_value, _score_rows
from app.services.execution_engine import simulate_open_execution
from app.services.rebalance_allocator import allocate_buy_orders, planned_to_order_results
from app.services.decision_store import create_decision_snapshot
from app.services.advanced_controls import black_litterman_weights
from app.services.stock_pool_sync import sync_pool_from_confirmed_execution
from app.services.cache_consolidation import list_available_trade_dates
from app.services.strategy_recommender import recommend_strategy_template
from app.services.strategy_config import (
    get_strategy_factor_names,
    get_strategy_score_for_market_state,
    parse_strategy_content,
    resolve_selection_market_state,
)
from app.services.trend_model import analyze_trend
from app.services.quant_risk import cvar_position_multiplier, constrained_cvar_weights, enforce_marginal_cvar_limit, hrp_fallback_weights, ledoit_wolf_covariance
from app.services.industry_allocator import apply_industry_targets, build_industry_allocation
from app.services.strategy_controller import build_strategy_action_contracts, build_strategy_rebalance_actions, build_target_weight_explanations
from app.services.production_strategy import champion_execution_gate, champion_strategy_policy_context, resolve_production_strategy_champion
from app.services.point_in_time import signal_available_time

_SCORING_HEARTBEAT_EVERY = max(100, min(int(os.getenv("QUANT_SCORING_HEARTBEAT_EVERY", "300")), 2000))
_SCORING_TIMEOUT_SEC = max(10, min(int(os.getenv("QUANT_SCORING_TIMEOUT_SEC", "90")), 1800))


class HistoricalDataMissingError(Exception):
    """
    历史数据缺失（统一映射为 508）。
    用于任务线程/接口层识别后触发 backfill。
    """

    def __init__(self, message: str = "历史交易数据不足，无法执行任务", code: str = "historical_data_missing_508"):
        self.status_code = 508
        self.code = code
        super().__init__(message)


class ReplayRiskGateError(Exception):
    """补跑风控门触发（数据门/资金门/时段门）。"""

    def __init__(self, message: str, code: str = "replay_risk_gate"):
        self.code = code
        super().__init__(message)


def _require_consistent_simulation_ledger() -> None:
    """Stop new mutations when the account projection no longer matches its ledger."""
    from app.services.simulation_ledger_audit import audit_simulation_ledger

    audit = audit_simulation_ledger(persist=False)
    if audit.get("ok"):
        return
    raise ReplayRiskGateError(
        "模拟账户独立账本校验未通过，已阻断新成交：" + "；".join(
            str(item.get("message") or "") for item in (audit.get("issues") or [])
        ),
        code="paused_simulation_ledger_drift",
    )


@dataclass
class QuantRunExecResult:
    signal_date: str
    execute_date: str
    sold_count: int
    bought_count: int
    total_buy_cost: float
    execution_id: int | None
    message: str
    planned_buy_count: int = 0
    executed_buy_count: int = 0
    fill_rate_pct: float = 0.0
    avg_slippage_bps: float = 0.0
    exec_latency_ms: int = 0
    detail: Dict[str, Any] | None = None


def _ts_code(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if "." in value:
        return value
    return f"{value}.SH" if value[:1] in {"5", "6", "9"} else f"{value}.SZ"


def _apply_research_admission(target_symbols: List[str], *, as_of_time: str | None = None) -> tuple[List[str], List[Dict[str, Any]]]:
    """Admit model selections only after current core-pool evidence gates pass."""
    from app.services.decision_store import list_pool_members
    from app.services.research_workflow import evaluate_core_pool_eligibility

    core_codes = {
        str(item.get("ts_code") or "").strip().upper()
        for item in list_pool_members("core")
        if not as_of_time or (item.get("entered_at") and str(item.get("entered_at")) <= str(as_of_time))
    }
    admitted: List[str] = []
    blocked: List[Dict[str, Any]] = []
    for symbol in target_symbols:
        code = _ts_code(symbol)
        if code not in core_codes:
            blocked.append({"symbol": symbol, "ts_code": code, "side": "BUY", "code": "not_in_core_pool", "message": "模型排名不能绕过核心研究池准入"})
            continue
        eligibility = evaluate_core_pool_eligibility(code, as_of_time=as_of_time)
        if not eligibility.get("eligible_for_core"):
            failed = [gate.get("label") for gate in eligibility.get("gates") or [] if not gate.get("passed")]
            blocked.append({"symbol": symbol, "ts_code": code, "side": "BUY", "code": "core_evidence_gate_failed", "message": f"核心候选证据已失效：{'、'.join(str(x) for x in failed)}", "eligibility": eligibility})
            continue
        admitted.append(symbol)
    return admitted, blocked


def _apply_dynamic_strategy_gate(
    target_symbols: List[str], *, as_of_time: str,
) -> tuple[List[str], List[Dict[str, Any]], Dict[str, Any]]:
    """Apply P2 environment/expectation/valuation gates to already research-admitted names."""
    from app.services.market_context import market_context_as_of
    from app.services.defensive_assets import defensive_allocation_for_state
    from app.services.research_engine import build_stock_research
    from app.services.strategy_gate import evaluate_strategy_gate
    from app.services.theme_engine import best_theme_for_stock

    market = market_context_as_of(as_of_time)
    market_state = str((market.get("market_state") or {}).get("state") or "unknown")
    market_profile_multiplier = float((market.get("market_profile") or {}).get("risk_budget_multiplier") or 1.0)
    sentiment_score = float((market.get("sentiment") or {}).get("score") or 50)
    admitted: List[str] = []
    blocked: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []
    for symbol in target_symbols:
        research = build_stock_research(_ts_code(symbol), persist=True, as_of_time=as_of_time)
        dimensions = research.get("dimensions") or {}
        research_state = research.get("research_state") or {}
        ready_scores = [
            float(value.get("score") or 0) for key, value in dimensions.items()
            if key not in {"expectation", "valuation"} and value.get("status") == "ready"
        ]
        research_score = sum(ready_scores) / len(ready_scores) if ready_scores else 0.0
        expectation_score = float((dimensions.get("expectation") or {}).get("score") or 0)
        valuation_score = float((dimensions.get("valuation") or {}).get("score") or 0)
        valuation = (research.get("linked_states") or {}).get("valuation") or {}
        scenarios = valuation.get("scenarios") or {}
        current_price = float(valuation.get("current_price") or 0)
        valuation_upside = (
            float(scenarios.get("base")) / current_price - 1.0
            if current_price > 0 and scenarios.get("base") is not None else None
        )
        theme = best_theme_for_stock(_ts_code(symbol), as_of_time=as_of_time)
        gate = evaluate_strategy_gate(
            research_score=research_score,
            confidence=float(research_state.get("confidence") or 0),
            data_quality=100.0 if research.get("risk", {}).get("eligible") else 0.0,
            risk_eligible=bool(research.get("risk", {}).get("eligible")),
            market_state=market_state, sentiment_score=sentiment_score,
            expectation_score=expectation_score, valuation_score=valuation_score,
            valuation_upside=valuation_upside,
            theme_score=float((theme or {}).get("score") or 0),
            theme_verified=bool(theme and theme.get("promotable")),
            change_alarm_probability=float((market.get("change_alarm") or {}).get("p_change_1d") or 0),
            market_profile_multiplier=market_profile_multiplier,
        )
        item = {"symbol": symbol, "ts_code": _ts_code(symbol), "gate": gate, "theme": theme, "risk": research.get("risk") or {}}
        items.append(item)
        if gate.get("admitted"):
            admitted.append(symbol)
        else:
            blocked.append({
                "symbol": symbol, "ts_code": _ts_code(symbol), "side": "BUY",
                "code": "dynamic_strategy_gate_failed",
                "message": "；".join(str(x) for x in gate.get("blockers") or ["动态策略门控未通过"]),
                "gate": gate,
            })
    multipliers = [float(item["gate"].get("risk_budget_multiplier") or 0) for item in items if item["gate"].get("admitted")]
    equity_target_weight = min(multipliers) if multipliers else 0.0
    summary = {
        "as_of_time": as_of_time, "market_context": market, "items": items,
        "risk_budget_multiplier": equity_target_weight,
        "cash_target": max((float(item["gate"].get("cash_target") or 0) for item in items), default=1.0),
        "defensive_allocation": defensive_allocation_for_state(
            market_state, as_of_date=str(as_of_time)[:10], equity_target_weight=equity_target_weight,
        ),
    }
    return admitted, blocked, summary


def _frozen_research_evidence(symbol: str, *, as_of_time: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build the replay-safe research and valuation payload frozen with a trade decision."""
    from app.services.research_engine import build_stock_research, build_valuation_state
    from app.services.thesis_store import list_theses

    code = _ts_code(symbol)
    research = build_stock_research(code, persist=True, as_of_time=as_of_time)
    valuation = build_valuation_state(code, persist=True, as_of_time=as_of_time)
    theses = [item for item in list_theses(ts_code=code, limit=20) if not item.get("created_at") or str(item.get("created_at")) <= as_of_time]
    thesis = next((item for item in theses if item.get("derived_status") not in {"invalidated", "failed"}), theses[0] if theses else None)
    if thesis:
        thesis = dict(thesis)
        thesis["hypotheses"] = [
            {**hypothesis, "evidence": [evidence for evidence in (hypothesis.get("evidence") or []) if not evidence.get("checked_at") or str(evidence.get("checked_at")) <= as_of_time]}
            for hypothesis in thesis.get("hypotheses") or []
            if not hypothesis.get("created_at") or str(hypothesis.get("created_at")) <= as_of_time
        ]
    research_payload = {
        "ts_code": code,
        "as_of_time": as_of_time,
        "profile": research.get("profile"),
        "classification": research.get("classification"),
        "risk": research.get("risk"),
        "dimensions": research.get("dimensions"),
        "research_state": research.get("research_state"),
        "raw_metrics": research.get("raw_metrics"),
        "evidence": research.get("evidence"),
        "thesis": None if not thesis else {
            "id": thesis.get("id"), "title": thesis.get("title"), "thesis_type": thesis.get("thesis_type"),
            "derived_status": thesis.get("derived_status"), "derived_confidence": thesis.get("derived_confidence"),
            "hypotheses": thesis.get("hypotheses"),
        },
    }
    valuation_payload = {
        key: valuation.get(key) for key in (
            "ts_code", "status", "method", "trade_date", "current_price", "current_multiple",
            "history_percentile", "industry_percentile", "scenarios", "dimension", "evidence", "limitations",
        )
    }
    valuation_payload["as_of_time"] = as_of_time
    return research_payload, valuation_payload


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def list_consolidated_trade_dates() -> List[str]:
    out: List[str] = []
    for ymd in list_available_trade_dates():
        if len(ymd) == 8 and ymd.isdigit():
            out.append(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}")
    return sorted(set(out))


def require_consolidated_trade_dates(min_count: int = 2) -> List[str]:
    dates = list_consolidated_trade_dates()
    if len(dates) < max(1, int(min_count or 2)):
        raise HistoricalDataMissingError("可用交易日不足，无法执行任务/补跑")
    return dates


def resolve_execute_date(signal_date: str) -> tuple[str, bool]:
    """根据信号日解析执行日。返回 (execute_date, same_day_fallback)。"""
    dates = list_consolidated_trade_dates()
    sig = str(signal_date or "").strip()
    if sig not in dates:
        raise ValueError(f"signal_date 不在可用交易日列表: {sig}")
    idx = dates.index(sig)
    if idx + 1 < len(dates):
        return dates[idx + 1], False
    return sig, True


def _row_trade_price(row: Any, *, prefer_open: bool = True) -> float:
    if row is None:
        return 0.0
    open_px = float(getattr(row, "open", 0) or 0)
    close_px = float(getattr(row, "close", 0) or 0)
    if prefer_open and open_px > 0:
        return open_px
    if close_px > 0:
        return close_px
    return open_px


def _parse_risk_budget(risk_budget: Dict[str, Any] | None) -> tuple[float, float]:
    rb = risk_budget if isinstance(risk_budget, dict) else {}
    try:
        target_pct = float(rb.get("target_position_pct", 1.0) or 1.0)
    except Exception:
        target_pct = 1.0
    try:
        max_single = float(rb.get("max_single_position_pct", 0.15) or 0.15)
    except Exception:
        max_single = 0.15
    return max(0.0, min(1.0, target_pct)), max(0.0, min(1.0, max_single))


def _portfolio_state(ds: BacktestDataSource, valuation_date: str) -> Dict[str, Any]:
    cash = float(get_account_balance() or 0)
    positions = get_positions()
    position_value_by_symbol: Dict[str, float] = {}
    market_value = 0.0
    for pos in positions:
        sym = str(pos.get("symbol") or "").strip()
        qty = int(pos.get("quantity") or 0)
        if not sym or qty <= 0:
            continue
        px = float(ds.get_price_on_date(sym, valuation_date, field="close") or 0)
        if px <= 0 and is_exchange_etf(sym):
            try:
                from app.services.defensive_assets import defensive_asset_bar
                bar = defensive_asset_bar(_ts_code(sym), valuation_date)
                px = float((bar or {}).get("close") or 0)
            except Exception:  # noqa: BLE001
                px = 0.0
        if px <= 0:
            px = float(pos.get("price") or 0)
        val = round(px * qty, 2)
        position_value_by_symbol[sym] = val
        market_value += val
    market_value = round(market_value, 2)
    total_equity = round(cash + market_value, 2)
    return {
        "cash_balance": round(cash, 2),
        "position_market_value": market_value,
        "total_equity": total_equity,
        "position_value_by_symbol": position_value_by_symbol,
        "positions": positions,
        "valuation_date": valuation_date,
    }


def _sell_position_for_rebalance(
    *,
    pos: Dict[str, Any],
    quantity: int,
    price_row: Any,
    execute_date: str,
    executed_at: str,
    reason: str,
    detail: str,
    turnover_remaining: float,
    hard_risk_exit: bool,
    blocked_events: List[Dict[str, Any]],
    sell_triggers: List[Dict[str, Any]],
    sold_ops_payload: List[Dict[str, Any]],
) -> tuple[bool, float, str | None]:
    sym = str(pos.get("symbol") or "").strip()
    if not sym or quantity <= 0:
        return False, turnover_remaining, None
    px = _row_trade_price(price_row, prefer_open=True)
    if px <= 0:
        return False, turnover_remaining, "price_missing"
    tradable = check_open_trade_constraints(
        side="SELL",
        symbol=sym,
        name=str(pos.get("name") or ""),
        open_price=px,
        close_price=float(getattr(price_row, "close", 0) or 0) if price_row else 0.0,
        change_pct=float(getattr(price_row, "change_pct", 0) or 0) if price_row else 0.0,
        volume=float(getattr(price_row, "volume", 0) or 0) if price_row else 0.0,
    )
    if not tradable.ok:
        blocked_events.append({"date": execute_date, "symbol": sym, "side": "SELL", "code": tradable.code, "message": tradable.message})
        add_log("WARN", f"QuantRun 卖出被交易约束拦截: {tradable.message}")
        return False, turnover_remaining, str(tradable.code or "trade_constraint")
    proposed_turnover = px * quantity
    if proposed_turnover > turnover_remaining + 1e-6 and not hard_risk_exit:
        blocked_events.append({"date": execute_date, "symbol": sym, "side": "SELL", "code": "monthly_turnover_budget", "message": "月度双边换手预算30%已用尽，非硬风险退出延后"})
        return False, turnover_remaining, "monthly_turnover_budget"
    try:
        fee = sell_cost_breakdown(px, quantity, sym)
        exec_price = float(fee["executed_price"])
        sell_position(
            int(pos["id"]),
            quantity=quantity,
            price=exec_price,
            trade_date=execute_date,
            record_operation=True,
            operation_at=executed_at,
            operation_remark="量化任务卖出（关联执行）",
            fee_breakdown=fee,
        )
    except ValueError:
        return False, turnover_remaining, "t_plus_one_or_position_constraint"
    turnover_after = max(0.0, turnover_remaining - proposed_turnover)
    sell_triggers.append({
        "symbol": sym,
        "name": str(pos.get("name") or ""),
        "reason": reason,
        "detail": detail,
        "quantity": quantity,
        "price": exec_price,
        "requested_price": round(px, 4),
    })
    sold_ops_payload.append({
        "symbol": sym,
        "side": "SELL",
        "price": exec_price,
        "quantity": quantity,
        "at": executed_at,
        "remark": "量化任务卖出（关联执行）",
        "name": str(pos.get("name") or "").strip(),
        **{k: fee[k] for k in ("commission", "transfer_fee", "stamp_tax", "slippage_bps", "fee_schedule_version")},
    })
    return True, turnover_after, None


def _industry_exposure_inputs(
    symbols: List[str], position_value_by_symbol: Dict[str, float],
) -> tuple[Dict[str, str], Dict[str, float]]:
    from app.data_platform.db import _conn as data_conn

    normalized = {_ts_code(symbol): str(symbol).split(".")[0] for symbol in symbols if str(symbol).strip()}
    industry_by_symbol: Dict[str, str] = {}
    if normalized:
        placeholders = ",".join("?" for _ in normalized)
        with data_conn() as c:
            rows = c.execute(
                f"SELECT ts_code,industry FROM dc_stock_master WHERE ts_code IN ({placeholders})",
                tuple(normalized),
            ).fetchall()
        for row in rows:
            item = dict(row)
            raw_symbol = normalized.get(str(item.get("ts_code") or ""))
            if raw_symbol:
                industry_by_symbol[raw_symbol] = str(item.get("industry") or "unclassified")
    existing_industry_value: Dict[str, float] = {}
    for symbol, value in position_value_by_symbol.items():
        industry = industry_by_symbol.get(str(symbol).split(".")[0], "unclassified")
        existing_industry_value[industry] = existing_industry_value.get(industry, 0.0) + float(value or 0)
    return industry_by_symbol, existing_industry_value


def _compute_buy_budget(
    *,
    total_equity: float,
    position_market_value: float,
    cash_balance: float,
    target_position_pct: float,
    budget_ratio: float = 1.0,
) -> float:
    target_invested = float(total_equity) * max(0.0, min(1.0, float(target_position_pct)))
    need_to_buy = max(0.0, target_invested - float(position_market_value))
    ratio = max(0.0, min(1.0, float(budget_ratio)))
    return max(0.0, round(min(float(cash_balance), need_to_buy) * ratio, 2))


def _monthly_turnover_budget(execute_date: str, total_equity: float) -> Dict[str, float]:
    month = str(execute_date)[:7]
    used = sum(
        float(item.get("price") or 0) * int(item.get("quantity") or 0)
        for item in list_operations(execution_id=None, limit=200000)
        if str(item.get("at") or "")[:7] == month
    )
    limit = max(0.0, float(total_equity)) * .30
    return {"limit": round(limit, 2), "used_before": round(used, 2), "remaining": round(max(0.0, limit - used), 2)}


def _pretrade_symbol_limits(symbols: List[str], *, cutoff: str, max_single: float) -> Dict[str, float]:
    from app.services.thesis_store import list_theses
    theses = {
        str(item.get("ts_code") or "").split(".")[0]: item
        for item in list_theses(limit=500)
        if (not item.get("created_at") or str(item.get("created_at")) <= cutoff)
        and (not item.get("updated_at") or str(item.get("updated_at")) <= cutoff)
    }
    limits: Dict[str, float] = {}
    for symbol in symbols:
        if is_exchange_etf(symbol):
            limits[symbol] = min(max_single, .25)
            continue
        thesis = theses.get(str(symbol).split(".")[0]) or {}
        core = float(thesis.get("derived_confidence") or 0) >= 75 and str(thesis.get("derived_status") or "") in {"active", "confirmed", "valid"}
        limits[symbol] = min(max_single, .25 if core else .15)
    return limits


def _selection_score_map(detail: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in detail.get("selections") or []:
        sym = str((row or {}).get("symbol") or "").strip()
        if sym:
            out[sym] = float((row or {}).get("score") or 0)
    return out


def _calc_total_buy_cost(orders: List[Any]) -> float:
    total = 0.0
    for o in orders:
        amount = float(getattr(o, "price", 0) or 0) * int(getattr(o, "quantity", 0) or 0)
        total += amount + float(getattr(o, "commission", 0) or 0) + float(getattr(o, "transfer_fee", 0) or 0)
    return round(total, 2)


def _filled_orders(orders: List[Any]) -> List[Any]:
    return [o for o in orders if int(getattr(o, "quantity", 0) or 0) > 0]


def _selection_explanation(
    ranked: List[tuple[Any, float]],
    *,
    weights: Dict[str, float],
    factor_values: Dict[str, Dict[str, float]],
    top_n: int,
) -> List[Dict[str, Any]]:
    selections: List[Dict[str, Any]] = []
    for row, score in ranked[: max(0, int(top_n))]:
        symbol = str(getattr(row, "symbol", "") or "").strip()
        vals = factor_values.get(symbol) or {}
        contribs: List[Dict[str, Any]] = []
        for factor, weight in weights.items():
            raw = float(vals.get(factor, 0) or 0)
            contribs.append(
                {
                    "factor": factor,
                    "weight": round(float(weight or 0), 4),
                    "raw_value": round(raw, 6),
                    "contribution": round(float(weight or 0) * raw, 6),
                }
            )
        contribs.sort(key=lambda x: abs(float(x.get("contribution") or 0)), reverse=True)
        selections.append(
            {
                "symbol": symbol,
                "name": str(getattr(row, "name", "") or ""),
                "score": round(float(score or 0), 6),
                "factor_contributions": contribs[:6],
            }
        )
    return selections


def _build_target_symbols(
    *,
    ds: BacktestDataSource,
    strategy_id: int,
    signal_date: str,
    top_n: int,
    exchange_group: str = "HS",
    progress_callback: Optional[Callable[[str], None]] = None,
    scoring_timeout_sec: Optional[int] = None,
) -> tuple[List[str], Dict[str, Any]]:
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy_id={strategy_id} 不存在")
    strategy_content = strategy.get("content") if isinstance(strategy.get("content"), dict) else {}
    strategy_type = strategy.get("type", "formula")
    config = parse_strategy_content(strategy_content, strategy_type)
    top_n = max(1, min(int(top_n), int(config.rebalance.get("top_n") or top_n)))
    exchange_group = normalize_exchange_group(exchange_group)
    trend_analysis = analyze_trend(ds=ds, signal_date=signal_date, exchange_group=exchange_group)
    strategy_template = recommend_strategy_template(trend_analysis, requested_top_n=top_n)
    # The frozen strategy bundle is the authority for selection. Market trend
    # templates provide risk context only; overriding the stored factor formula
    # here made replayed Challengers impossible to reproduce in daily plans.
    from app.services.market_context import market_context_as_of

    market_context = market_context_as_of(signal_available_time(signal_date))
    transparent_market_state = str(((market_context.get("market_state") or {}).get("state") or "")) if market_context.get("ok") else ""
    market_state = transparent_market_state
    score_weights = get_strategy_score_for_market_state(strategy_content, strategy_type, market_state)
    filters = list(config.filters)
    empty_detail = {
        "signal_date": signal_date,
        "exchange_group": exchange_group,
        "trend_analysis": trend_analysis,
        "strategy_template": strategy_template,
        "selected_factors": list(score_weights.keys()),
        "selection_market_state": market_state,
        "selection_market_context": market_context,
        "risk_budget": strategy_template.get("risk_budget") or {},
        "selections": [],
        "blocked_events": [],
        "sell_triggers": [],
    }
    if progress_callback is not None:
        progress_callback("正在加载快照")
    snapshot = ds.load_snapshot(signal_date.replace("-", ""))
    # 数据容错：过滤掉晚于 signal_date 的脏行，避免未来数据污染评分。
    snapshot = [r for r in snapshot if str(getattr(r, "date", "")) <= str(signal_date)]
    if exchange_group != "ALL":
        snapshot = [r for r in snapshot if matches_exchange_group(r.symbol, exchange_group)]
    if not snapshot:
        return [], empty_detail
    required_factors = get_strategy_factor_names(strategy_content, strategy_type)
    if float(config.universe.get("exclude_smallest_float_mv_pct") or 0.0) > 0:
        required_factors.add("float_mv")
    # Production plan-model families consume these registered factors even
    # when the selected formula does not explicitly weight them.
    required_factors.update({"momentum_20", "momentum_60", "volatility_20"})
    for f in filters:
        fn = str(f.get("factor", "")).strip()
        if fn:
            required_factors.add(fn)
    daily_basic_map = ds.get_daily_basic_map(signal_date.replace("-", ""))
    max_lookback = max(int(config.lookback_days or 60), 20)
    factor_values: Dict[str, Dict[str, float]] = {}
    if progress_callback is not None:
        progress_callback("正在计算打分")
    ds.preload_histories([row.symbol for row in snapshot], end_date=signal_date, lookback=max_lookback)
    benchmark_returns = ds.get_index_returns("000300.SH", signal_date, lookback=60) if required_factors & {"beta_60", "relative_strength_60"} else []
    benchmark_return = (
        math.prod(1.0 + value for value in benchmark_returns) - 1.0
        if benchmark_returns else None
    )
    selection_short_return = (
        ds.get_index_return("000300.SH", signal_date, lookback=20)
        if isinstance(strategy_content.get("selection_regime"), dict) and hasattr(ds, "get_index_return")
        else None
    )
    market_state = resolve_selection_market_state(
        strategy_content,
        transparent_state=transparent_market_state,
        short_benchmark_return=selection_short_return,
        long_benchmark_return=benchmark_return,
    )
    score_weights = get_strategy_score_for_market_state(strategy_content, strategy_type, market_state)
    empty_detail.update({
        "selected_factors": list(score_weights.keys()),
        "selection_market_state": market_state,
        "selection_transparent_market_state": transparent_market_state,
        "selection_short_benchmark_return": selection_short_return,
        "selection_long_benchmark_return": benchmark_return,
    })
    t0 = time.monotonic()
    total = len(snapshot)
    timeout_sec = int(scoring_timeout_sec) if scoring_timeout_sec else _SCORING_TIMEOUT_SEC
    for idx, row in enumerate(snapshot, start=1):
        if (time.monotonic() - t0) > timeout_sec:
            raise ReplayRiskGateError(
                f"评分阶段超时：已处理 {idx-1}/{total}，超时阈值 {timeout_sec}s",
                code="paused_scoring_timeout",
            )
        if progress_callback is not None and (idx == 1 or idx % _SCORING_HEARTBEAT_EVERY == 0 or idx == total):
            progress_callback(f"正在计算打分 {idx}/{total}")
        hist = ds.get_hist_window(row.symbol, signal_date, max_lookback)
        vals: Dict[str, float] = {}
        for f in required_factors:
            vals[f] = _factor_value(
                f,
                row,
                hist,
                daily_basic_map,
                benchmark_return=benchmark_return,
                benchmark_returns=benchmark_returns,
            )
        factor_values[row.symbol] = vals
    universe_rows = _apply_universe_controls(snapshot, factor_values, config.universe)
    filtered = _apply_filters(universe_rows, factor_values, filters, skip_factors=set())
    if not filtered:
        return [], {**empty_detail, "filter_summary": {"passed": 0, "universe": len(snapshot)}}
    # Formal replay validates the frozen formula in the strategy bundle. An
    # unrelated globally active ranker (including one marked production-
    # ineligible) must not silently replace that formula in daily execution.
    # Production model families are fused below through their explicit bundle
    # bindings and remain fully auditable.
    ranked = _score_rows(
        filtered,
        score_weights,
        factor_values,
        normalization=config.score_normalization,
    )
    # Candidate recall is industry-first: only confirmed, non-fading narrative
    # industries enter the final stock ranking. This prevents a globally high
    # stock score from bypassing the current market story.
    from app.services.theme_engine import list_themes, materialize_theme_snapshots
    materialize_theme_snapshots(as_of_time=signal_available_time(signal_date))
    themes = list_themes(as_of_time=signal_available_time(signal_date), promotable_only=True, limit=100).get("items") or []
    narrative_codes = {
        str(company.get("ts_code") or "").split(".")[0]
        for theme in themes if str(theme.get("lifecycle") or "") not in {"退潮", "沉寂"}
        for company in (theme.get("companies") or [])
    }
    narrative_ranked = [item for item in ranked if str(item[0].symbol).split(".")[0] in narrative_codes]
    if narrative_ranked:
        ranked = narrative_ranked
    from app.services.model_signal_fusion import apply_production_model_signals
    histories = {
        str(row.symbol): ds.get_hist_window(row.symbol, signal_date, max_lookback)
        for row, _score in ranked
    }
    ranked, model_evidence, model_blocks, model_fusion = apply_production_model_signals(
        ranked=ranked,
        factor_values=factor_values,
        histories=histories,
        themes=themes,
    )
    recall_n = max(top_n, min(80, top_n * 10))
    selections = _selection_explanation(
        ranked,
        weights=score_weights,
        factor_values=factor_values,
        top_n=recall_n,
    )
    for selection in selections:
        symbol = str(selection.get("symbol") or "")
        selection["model_signals"] = model_evidence.get(symbol) or {}
    detail = {
        **empty_detail,
        "filter_summary": {
            "passed": len(filtered),
            "universe": len(snapshot),
            "template": strategy_template.get("id"),
        },
        "selections": selections,
        "blocked_events": model_blocks,
        "model_signal_fusion": model_fusion,
        "narrative_recall": {
            "method": "industry_story_first_v1",
            "themes": [{"theme": item.get("theme_name"), "score": item.get("score"), "lifecycle": item.get("lifecycle")} for item in themes],
            "eligible_symbols": len(narrative_codes),
        },
    }
    return [x[0].symbol for x in ranked[:recall_n]], detail


def run_for_trade_date(
    *,
    strategy_id: int,
    signal_date: str,
    execute_date: str,
    top_n: int,
    budget_ratio: float,
    exchange_group: str = "HS",
    progress_callback: Optional[Callable[[str], None]] = None,
    strict_no_future: bool = True,
    scoring_timeout_sec: Optional[int] = None,
    authoritative_plan: Dict[str, Any] | None = None,
) -> QuantRunExecResult:
    def _report(phase: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(phase)
            except Exception:
                pass

    ds = BacktestDataSource()
    exchange_group = normalize_exchange_group(exchange_group)
    if strict_no_future and str(signal_date) >= str(execute_date):
        raise ReplayRiskGateError(
            f"未来函数防护：signal_date({signal_date}) 必须早于 execute_date({execute_date})",
            code="future_guard_date_order",
        )
    _require_consistent_simulation_ledger()
    execution_guard = champion_execution_gate()
    champion_quarantined = str(execution_guard.get("status") or "") == "quarantined"
    _report("正在加载快照")
    if authoritative_plan:
        target_symbols = [str(x).strip() for x in authoritative_plan.get("target_symbols") or [] if str(x).strip()]
        selection_detail = {
            "signal_date": signal_date,
            "exchange_group": exchange_group,
            "strategy_template": {"id": "authoritative_daily_plan", "name": "统一日调仓计划"},
            "risk_budget": authoritative_plan.get("risk_budget") or {},
            "selections": [
                {"symbol": str(item.get("symbol") or ""), "name": str(item.get("name") or ""),
                 "score": float(item.get("expected_20d_return") or 0) * 100}
                for item in authoritative_plan.get("proposal") or []
                if str(item.get("symbol") or "") in target_symbols
            ],
            "blocked_events": [], "sell_triggers": [],
            "plan_provenance": {
                "source": "daily_rebalance_plans",
                "plan_id": authoritative_plan.get("plan_id"),
                "plan_version": authoritative_plan.get("plan_version"),
                "input_hash": authoritative_plan.get("input_hash"),
            },
            "decision_version_manifest": authoritative_plan.get("decision_version_manifest") or {},
        }
    else:
        target_symbols, selection_detail = _build_target_symbols(
            ds=ds,
            strategy_id=int(strategy_id),
            signal_date=signal_date,
            top_n=int(top_n),
            exchange_group=exchange_group,
            progress_callback=_report,
            scoring_timeout_sec=scoring_timeout_sec,
        )
    target_symbols, research_blocks = _apply_research_admission(target_symbols, as_of_time=signal_available_time(signal_date))
    target_symbols, dynamic_blocks, dynamic_gate = _apply_dynamic_strategy_gate(
        target_symbols, as_of_time=signal_available_time(signal_date)
    )
    if champion_quarantined:
        target_symbols = []
        dynamic_gate["risk_budget_multiplier"] = 0.0
        dynamic_blocks.append({
            "date": execute_date,
            "side": "BUY",
            "code": "champion_formal_oos_quarantined",
            "message": "当前 Champion 正式 OOS 失败；已隔离新开仓和加仓，仅允许减仓、卖出与账本审计",
            "execution_guard": execution_guard,
        })
    target_symbols = target_symbols[:max(1, int(top_n))]
    equity_target_symbols = list(target_symbols)
    score_map = _selection_score_map(selection_detail)
    market_state = str(((dynamic_gate.get("market_context") or {}).get("market_state") or {}).get("state") or "unknown")
    industry_allocation = build_industry_allocation(
        symbols=equity_target_symbols,
        symbol_scores=score_map,
        equity_budget=float(dynamic_gate.get("risk_budget_multiplier") or 0),
        market_state=market_state,
        as_of_time=signal_available_time(signal_date),
    )
    admitted_industries = set((industry_allocation.get("industry_targets") or {}).keys())
    symbol_industries = industry_allocation.get("symbol_industries") or {}
    industry_rejected = [symbol for symbol in equity_target_symbols if symbol_industries.get(symbol) not in admitted_industries]
    for symbol in industry_rejected:
        dynamic_blocks.append({"symbol": symbol, "ts_code": _ts_code(symbol), "side": "BUY", "code": "industry_budget_gate_failed", "message": "所属行业未获得当日股票风险预算"})
    equity_target_symbols = [symbol for symbol in equity_target_symbols if symbol not in industry_rejected]
    allocated_equity_budget = float(industry_allocation.get("allocated_equity_budget") or 0)
    from app.services.defensive_assets import defensive_allocation_for_state
    dynamic_gate["risk_budget_multiplier"] = allocated_equity_budget
    dynamic_gate["defensive_allocation"] = defensive_allocation_for_state(
        market_state, as_of_date=signal_date, equity_target_weight=allocated_equity_budget,
    )
    defensive_policy = dynamic_gate.get("defensive_allocation") or {}
    defensive_weights = {str(code).split(".")[0]: float(weight) for code, weight in (defensive_policy.get("target_weights") or {}).items()}
    if champion_quarantined:
        # A defensive ETF is still a new/add order.  Formal OOS isolation is
        # intentionally stricter: cash is acceptable while the failed bundle
        # is only retained as a research reference.
        defensive_weights = {}
    high_risk_symbols = {
        str(item.get("symbol") or "") for item in (dynamic_gate.get("items") or [])
        if str((item.get("risk") or {}).get("risk_level") or "") == "high"
    }
    target_symbols = list(dict.fromkeys(equity_target_symbols + list(defensive_weights)))
    selection_detail["research_admission"] = {"admitted": equity_target_symbols, "blocked": research_blocks}
    selection_detail["dynamic_strategy_gate"] = dynamic_gate
    selection_detail["industry_allocation"] = industry_allocation
    blocked_events: List[Dict[str, Any]] = list(selection_detail.get("blocked_events") or []) + research_blocks + dynamic_blocks
    sell_triggers: List[Dict[str, Any]] = []
    strategy = get_strategy(strategy_id)
    config = parse_strategy_content(strategy.get("content"), strategy.get("type", "formula")) if strategy else None
    hold_days = int(config.hold_days) if config and config.hold_days else None
    if champion_quarantined:
        hold_days = None
    signal_rows = ds.load_snapshot(signal_date.replace("-", ""))
    if strict_no_future:
        if not signal_rows:
            raise ReplayRiskGateError(f"数据门：信号日 {signal_date} 快照缺失", code="paused_data_not_ready")
        # 容错：过滤掉脏快照中的“未来日期行”，避免单条异常数据导致整任务长时间暂停。
        signal_rows = [x for x in signal_rows if str(getattr(x, "date", "")) <= str(signal_date)]
        if not signal_rows:
            _report("信号日快照全部为未来日期，已降级为空仓信号继续执行")
    exec_rows = ds.load_snapshot(execute_date.replace("-", ""))
    if strict_no_future:
        if not exec_rows:
            raise ReplayRiskGateError(f"数据门：执行日 {execute_date} 快照缺失", code="paused_data_not_ready")
        exec_rows = [x for x in exec_rows if str(getattr(x, "date", "")) <= str(execute_date)]
        if not exec_rows:
            raise ReplayRiskGateError(
                f"未来函数防护：执行日快照全部为未来日期，execute_date={execute_date}",
                code="future_guard_execute_snapshot",
            )
    exec_map = {x.symbol: x for x in exec_rows}
    if defensive_weights:
        from app.services.defensive_assets import DEFENSIVE_ASSETS, defensive_asset_bar
        defensive_meta = {item["ts_code"].split(".")[0]: item for item in DEFENSIVE_ASSETS}
        for symbol in defensive_weights:
            meta = defensive_meta.get(symbol) or {}
            bar = defensive_asset_bar(str(meta.get("ts_code") or _ts_code(symbol)), execute_date)
            if not bar:
                blocked_events.append({"date": execute_date, "symbol": symbol, "side": "BUY", "code": "defensive_asset_price_missing", "message": "防御ETF执行日行情缺失"})
                continue
            exec_map[symbol] = SimpleNamespace(
                symbol=symbol, name=meta.get("name") or symbol, date=execute_date,
                open=float(bar.get("open") or 0), close=float(bar.get("close") or 0),
                change_pct=float(bar.get("pct_chg") or 0), volume=float(bar.get("vol") or 0),
            )
    executed_at = f"{execute_date}T09:30:00"
    pretrade_portfolio = _portfolio_state(ds, execute_date)
    turnover_budget = _monthly_turnover_budget(execute_date, pretrade_portfolio["total_equity"])
    turnover_remaining = float(turnover_budget["remaining"])
    from app.services.thesis_store import list_theses as _list_theses_for_turnover
    thesis_status_by_symbol = {
        str(item.get("ts_code") or "").split(".")[0]: str(item.get("derived_status") or "")
        for item in _list_theses_for_turnover(limit=500)
    }

    # 先卖出（仅对不在目标池且满足 T+1 / hold_days 的仓位）
    sold_count = 0
    sold_ops_payload: List[Dict[str, Any]] = []
    blocked_count = 0
    rows = get_positions()
    for pos in rows:
        sym = str(pos.get("symbol") or "").strip()
        if not sym or sym in target_symbols:
            continue
        if hold_days and hold_days > 0:
            bought_at = str(pos.get("bought_at") or "")[:10]
            if bought_at:
                try:
                    held = (datetime.fromisoformat(execute_date) - datetime.fromisoformat(bought_at)).days
                    if held < hold_days:
                        continue
                except ValueError:
                    continue
        sold, turnover_remaining, block_code = _sell_position_for_rebalance(
            pos=pos,
            quantity=int(pos.get("quantity") or 0),
            price_row=exec_map.get(sym),
            execute_date=execute_date,
            executed_at=executed_at,
            reason="调仓卖出",
            detail="不在目标池且满足持有期/T+1",
            turnover_remaining=turnover_remaining,
            hard_risk_exit=thesis_status_by_symbol.get(sym) in {"invalidated", "failed"},
            blocked_events=blocked_events,
            sell_triggers=sell_triggers,
            sold_ops_payload=sold_ops_payload,
        )
        if not sold:
            blocked_count += 1
            if block_code == "price_missing":
                blocked_events.append({"date": execute_date, "symbol": sym, "side": "SELL", "code": "price_missing", "message": "执行日卖出价格缺失"})
            continue
        sold_count += 1

    # 再买入（等权资金，按总权益目标仓位与单票上限）
    held_after_sell = {str(p.get("symbol") or "").strip() for p in get_positions()}
    # Existing targets may still be below today's optimizer weight. Include
    # them so idle cash is deployed instead of treating every held name as a
    # permanent no-op.
    buy_list = list(target_symbols)
    risk_budget = selection_detail.get("risk_budget") if isinstance(selection_detail.get("risk_budget"), dict) else {}
    _strategy_target_position_pct, max_single_position_pct = _parse_risk_budget(risk_budget)
    target_position_pct = 1.0
    portfolio = _portfolio_state(ds, execute_date)
    defensive_target_weight = sum(defensive_weights.values())
    effective_budget_ratio = max(0.0, min(1.0, float(budget_ratio))) * min(1.0, float(dynamic_gate.get("risk_budget_multiplier") or 0) + defensive_target_weight)
    available_balance = _compute_buy_budget(
        total_equity=portfolio["total_equity"],
        position_market_value=portfolio["position_market_value"],
        cash_balance=portfolio["cash_balance"],
        target_position_pct=target_position_pct,
        budget_ratio=effective_budget_ratio,
    )
    residual_cash_limit = min(portfolio["total_equity"] * .005, 500.0)
    full_investment_gap = portfolio["cash_balance"] if portfolio["cash_balance"] > residual_cash_limit else 0.0
    turnover_capacity = max(turnover_remaining, full_investment_gap) if defensive_weights else turnover_remaining
    available_balance = min(available_balance, turnover_capacity)
    order_requests: List[OrderRequest] = []
    narrative_name_by_symbol = {
        str(candidate.get("symbol") or ""): str(candidate.get("name") or "")
        for industry in (industry_allocation.get("items") or [])
        for candidate in (industry.get("candidates") or [])
    }
    returns_by_symbol: Dict[str, List[float]] = {}
    liquidity_caps: Dict[str, float] = {}
    for sym in buy_list:
        px_row = exec_map.get(sym)
        px = _row_trade_price(px_row, prefer_open=True)
        if px <= 0:
            continue
        if sym in defensive_weights:
            from app.services.defensive_assets import defensive_asset_returns
            returns = defensive_asset_returns(next(code for code in (defensive_policy.get("target_weights") or {}) if str(code).split(".")[0] == sym), signal_date)
            average_amount_thousand = float((defensive_asset_bar(next(code for code in (defensive_policy.get("target_weights") or {}) if str(code).split(".")[0] == sym), signal_date) or {}).get("amount") or 0)
        else:
            hist = ds.get_hist_window(sym, execute_date, 60)
            closes = [float(getattr(x, "close", 0) or 0) for x in hist]
            returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
            amounts = [float(getattr(item, "turnover", 0) or 0) for item in hist[-20:] if float(getattr(item, "turnover", 0) or 0) > 0]
            average_amount_thousand = sum(amounts) / len(amounts) if amounts else 0.0
        if average_amount_thousand > 0:
            liquidity_caps[sym] = average_amount_thousand * 1000.0 * .01
        returns_by_symbol[sym] = returns
        score_map[sym] = float(score_map.get(sym, 0.0)) * cvar_position_multiplier(returns)
        order_requests.append(
            OrderRequest(
                symbol=sym,
                side="BUY",
                price=px,
                quantity=0,
                name=str(getattr(px_row, "name", "") or narrative_name_by_symbol.get(sym) or ""),
                change_pct=float(getattr(px_row, "change_pct", 0) or 0),
                volume=float(getattr(px_row, "volume", 0) or 0),
                close=float(getattr(px_row, "close", 0) or 0),
            )
        )

    # Portfolio-level risk budget: covariance is shrunk before the constrained
    # CVaR weights are applied, preventing a single volatile name from
    # consuming the whole buy budget. The covariance snapshot is retained in
    # the execution detail below for auditability.
    covariance_snapshot = ledoit_wolf_covariance(returns_by_symbol)
    cvar_weights = constrained_cvar_weights(returns_by_symbol, max_single=max_single_position_pct)
    bl_result: Dict[str, Any] = {}
    optimizer_name = "ledoit_wolf_shrinkage_plus_constrained_cvar_v1"
    if not cvar_weights:
        cvar_weights = hrp_fallback_weights(returns_by_symbol, max_single=max_single_position_pct)
        optimizer_name = "hrp_inverse_volatility_fallback_v1"
    if cvar_weights:
        score_values = [float(score_map.get(symbol, 0) or 0) for symbol in cvar_weights]
        score_mean = sum(score_values) / len(score_values) if score_values else 0.0
        views = {symbol: (float(score_map.get(symbol, 0) or 0) - score_mean) / 100.0 for symbol in cvar_weights}
        confidences = {symbol: min(1.0, len(returns_by_symbol.get(symbol, [])) / 60.0) for symbol in cvar_weights}
        bl_result = black_litterman_weights(cvar_weights, views, confidences, max_weight=max_single_position_pct)
        cvar_weights = dict(bl_result.get("weights") or cvar_weights)
        optimizer_name = f"{optimizer_name}+{bl_result.get('method', 'black_litterman_diagonal_v1')}"
    equity_weights = {symbol: weight for symbol, weight in cvar_weights.items() if symbol not in defensive_weights}
    equity_returns = {symbol: returns_by_symbol[symbol] for symbol in equity_weights if returns_by_symbol.get(symbol)}
    adaptive_marginal_limit = max(.12, min(.35, 1.0 / max(1, len(equity_returns)) + .02))
    marginal_cvar = enforce_marginal_cvar_limit(
        equity_weights, equity_returns, max_contribution=adaptive_marginal_limit,
    ) if equity_weights else {"status": "not_applicable", "weights": {}, "contributions": {}, "reason": "无股票风险袖套"}
    if marginal_cvar.get("status") in {"ready", "not_applicable"}:
        equity_weights = dict(marginal_cvar.get("weights") or {})
        equity_weights = apply_industry_targets(
            equity_weights, industry_allocation, equity_budget=allocated_equity_budget,
        )
        equity_total = sum(equity_weights.values()) or 1.0
        equity_sleeve = max(0.0, 1.0 - defensive_target_weight)
        cvar_weights = {symbol: weight / equity_total * equity_sleeve for symbol, weight in equity_weights.items()}
        cvar_weights.update(defensive_weights)
        optimizer_name += "+marginal_cvar_12pct_v1"
        if defensive_weights:
            optimizer_name += "+fixed_defensive_sleeve_v1"
    else:
        cvar_weights = dict(defensive_weights)
        if defensive_weights:
            order_requests = [order for order in order_requests if str(order.symbol) in defensive_weights]
        else:
            available_balance = 0.0
        blocked_events.append({"date": execute_date, "side": "BUY", "code": "marginal_cvar_infeasible", "message": marginal_cvar.get("reason") or "单股边际CVaR贡献12%硬约束不可行"})
    if authoritative_plan:
        planned_weights = {
            str(symbol): max(0.0, float(weight or 0))
            for symbol, weight in (authoritative_plan.get("target_weights") or {}).items()
            if str(symbol) in target_symbols and float(weight or 0) > 0
        }
        cvar_weights = planned_weights
        optimizer_name = "authoritative_daily_plan_weights_v1"
    if cvar_weights:
        scale = max(1, len(cvar_weights))
        for sym, weight in cvar_weights.items():
            score_map[sym] = float(score_map.get(sym, 0.0)) * float(weight * scale)

    # Positions that cannot be sold today still consume capital. Scale today's
    # target sleeve around them so the optimizer does not request more than
    # 100% of account equity.
    non_target_value = sum(
        float(value or 0) for symbol, value in portfolio["position_value_by_symbol"].items()
        if symbol not in cvar_weights
    )
    target_sleeve = max(0.0, 1.0 - non_target_value / max(float(portfolio["total_equity"]), 1.0))
    weight_sum = sum(cvar_weights.values())
    if weight_sum > 0 and not authoritative_plan:
        cvar_weights = {symbol: weight / weight_sum * target_sleeve for symbol, weight in cvar_weights.items()}

    # 风险收缩不是“禁止调仓”：目标池内的超配持仓也必须先降到目标权重。
    reduced_to_target = False
    for pos in list(get_positions()):
        sym = str(pos.get("symbol") or "").strip()
        if not sym or sym not in cvar_weights:
            continue
        px_row = exec_map.get(sym)
        px = _row_trade_price(px_row, prefer_open=True)
        quantity = int(pos.get("quantity") or 0)
        if px <= 0 or quantity <= 0 or portfolio["total_equity"] <= 0:
            continue
        current_value = px * quantity
        target_value = float(portfolio["total_equity"]) * float(cvar_weights.get(sym) or 0)
        excess_value = current_value - target_value
        if excess_value < max(100.0, px * 100):
            continue
        sell_qty = min(quantity, int(excess_value // (px * 100)) * 100)
        if sell_qty <= 0:
            continue
        current_weight = current_value / max(float(portfolio["total_equity"]), 1.0)
        hard_risk_exit = (
            thesis_status_by_symbol.get(sym) in {"invalidated", "failed"}
            or current_weight > max_single_position_pct + 1e-6
            or current_weight > float(cvar_weights.get(sym) or 0) + 0.03
        )
        sold, turnover_remaining, block_code = _sell_position_for_rebalance(
            pos=pos,
            quantity=sell_qty,
            price_row=px_row,
            execute_date=execute_date,
            executed_at=executed_at,
            reason="目标权重降风险",
            detail=f"当前约 {current_weight * 100:.1f}%，目标约 {float(cvar_weights.get(sym) or 0) * 100:.1f}%",
            turnover_remaining=turnover_remaining,
            hard_risk_exit=hard_risk_exit,
            blocked_events=blocked_events,
            sell_triggers=sell_triggers,
            sold_ops_payload=sold_ops_payload,
        )
        if sold:
            sold_count += 1
            reduced_to_target = True
        else:
            blocked_count += 1
            if block_code == "price_missing":
                blocked_events.append({"date": execute_date, "symbol": sym, "side": "SELL", "code": "price_missing", "message": "执行日降权卖出价格缺失"})
    if reduced_to_target:
        portfolio = _portfolio_state(ds, execute_date)
        available_balance = _compute_buy_budget(
            total_equity=portfolio["total_equity"],
            position_market_value=portfolio["position_market_value"],
            cash_balance=portfolio["cash_balance"],
            target_position_pct=target_position_pct,
            budget_ratio=effective_budget_ratio,
        )
        turnover_capacity = max(turnover_remaining, full_investment_gap) if defensive_weights else turnover_remaining
        available_balance = min(available_balance, turnover_capacity)

    _report("正在生成买卖单")
    industry_by_symbol, existing_industry_value = _industry_exposure_inputs(
        list(portfolio["position_value_by_symbol"]) + [str(order.symbol) for order in order_requests],
        portfolio["position_value_by_symbol"],
    )
    if defensive_weights:
        from app.services.defensive_assets import DEFENSIVE_ASSETS
        defensive_classes = {item["ts_code"].split(".")[0]: f"defensive_{item['asset_class']}" for item in DEFENSIVE_ASSETS}
        for symbol in defensive_weights:
            industry_by_symbol[symbol] = defensive_classes.get(symbol, "defensive")
        existing_industry_value = {}
        for symbol, value in portfolio["position_value_by_symbol"].items():
            industry = industry_by_symbol.get(str(symbol).split(".")[0], "unclassified")
            existing_industry_value[industry] = existing_industry_value.get(industry, 0.0) + float(value or 0)
    exec_t0 = time.monotonic()
    position_limits = _pretrade_symbol_limits(
        [str(order.symbol) for order in order_requests], cutoff=signal_available_time(signal_date), max_single=max_single_position_pct,
    )
    for symbol in defensive_weights:
        position_limits[symbol] = .45
    defensive_only_target = bool(cvar_weights) and not any(symbol not in defensive_weights for symbol in cvar_weights)
    residual_sweep_symbol = None
    defensive_prices = {
        str(order.symbol): float(order.price or 0)
        for order in order_requests if str(order.symbol) in defensive_weights and float(order.price or 0) > 0
    }
    if defensive_prices:
        residual_sweep_symbol = min(defensive_prices, key=defensive_prices.get)
    result = simulate_open_execution(
        order_requests,
        budget=available_balance,
        executed_at=executed_at,
        total_equity=portfolio["total_equity"],
        max_single_position_pct=max_single_position_pct,
        existing_position_value=portfolio["position_value_by_symbol"],
        symbol_scores=score_map,
        industry_by_symbol=industry_by_symbol,
        existing_industry_value=existing_industry_value,
        max_industry_position_pct=.45 if defensive_only_target else .25,
        max_correlated_industry_pct=.45 if defensive_only_target else .35,
        target_weights=cvar_weights,
        symbol_position_limits=position_limits,
        high_risk_symbols=high_risk_symbols,
        max_order_value_by_symbol=liquidity_caps,
        initial_entry_fraction=1.0,
        residual_sweep_symbol=residual_sweep_symbol,
    )
    exec_latency_ms = max(1, int((time.monotonic() - exec_t0) * 1000))
    filled = _filled_orders(result.orders)
    blocked_count += len(result.orders) - len(filled)
    for o in result.orders:
        if int(getattr(o, "quantity", 0) or 0) > 0:
            continue
        blocked_events.append(
            {
                "date": execute_date,
                "symbol": str(getattr(o, "symbol", "") or ""),
                "side": str(getattr(o, "side", "") or "BUY").upper(),
                "code": str(getattr(o, "block_code", "") or "blocked"),
                "message": str(getattr(o, "message", "") or "买入被拦截"),
            }
        )
    total_buy_cost = _calc_total_buy_cost(filled)
    turnover_remaining = max(0.0, turnover_remaining - sum(float(o.price) * int(o.quantity) for o in filled))
    req_price_map = {str(x.symbol): float(x.price) for x in order_requests}
    req_name_map = {str(x.symbol): str(x.name or "") for x in order_requests}
    orders_payload = [
        {
            "symbol": o.symbol,
            "name": req_name_map.get(str(o.symbol), ""),
            "side": o.side,
            "price": o.price,
            "quantity": o.quantity,
            "commission": getattr(o, "commission", 0),
            "transfer_fee": getattr(o, "transfer_fee", 0),
            "slippage_bps": round(
                (float(o.price) / req_price_map[str(o.symbol)] - 1.0) * 10000.0, 4
            ) if req_price_map.get(str(o.symbol)) else 0.0,
            "fee_schedule_version": ETF_FEE_SCHEDULE_VERSION if is_exchange_etf(o.symbol) else FEE_SCHEDULE_VERSION,
        }
        for o in result.orders
        if int(getattr(o, "quantity", 0) or 0) > 0
    ]
    execution_id: int | None = None
    if sold_count == 0 and not filled:
        raise ReplayRiskGateError(
            "正常交易日未形成任何可执行订单；全仓策略禁止将 no_trade 标记为成功",
            code="blocked_zero_order_full_investment",
        )
    if orders_payload:
        execution_id = create_and_apply_buy_execution(
            strategy_id=int(strategy_id),
            executed_at=executed_at,
            budget=total_buy_cost,
            top_n=int(top_n),
            orders=orders_payload,
        )
    elif sold_ops_payload:
        execution_id = create_execution(
            strategy_id=int(strategy_id),
            executed_at=executed_at,
            budget=0.0,
            top_n=int(top_n),
            orders=[],
        )
    if execution_id is not None and sold_ops_payload:
        link_recorded_operations_to_execution(execution_id, sold_ops_payload)

    planned_buy_count = len(order_requests)
    bought_count = len(filled)
    fill_rate_pct = round((bought_count / planned_buy_count) * 100.0, 2) if planned_buy_count > 0 else 0.0
    slippage_bps: list[float] = []
    for o in filled:
        qty = int(getattr(o, "quantity", 0) or 0)
        px_exec = float(getattr(o, "price", 0) or 0)
        px_req = float(req_price_map.get(str(getattr(o, "symbol", "")), 0) or 0)
        if qty <= 0 or px_exec <= 0 or px_req <= 0:
            continue
        slippage_bps.append((px_exec / px_req - 1.0) * 10000.0)
    avg_slippage_bps = round(sum(slippage_bps) / len(slippage_bps), 2) if slippage_bps else 0.0
    trend_analysis = selection_detail.get("trend_analysis") if isinstance(selection_detail.get("trend_analysis"), dict) else {}
    strategy_template = selection_detail.get("strategy_template") if isinstance(selection_detail.get("strategy_template"), dict) else {}
    trend_text = str(trend_analysis.get("trend") or "unknown")
    template_name = str(strategy_template.get("name") or strategy_template.get("id") or "策略模板")
    msg = (
        f"signal={signal_date} execute={execute_date} "
        f"趋势={trend_text} 模板={template_name} 交易所={exchange_group} "
        f"卖出{sold_count} 买入{bought_count} 约束拦截{blocked_count} 买入成本={total_buy_cost:.2f} "
        f"偏差(fill={fill_rate_pct:.2f}% slip={avg_slippage_bps:.2f}bp latency={exec_latency_ms}ms)"
    )
    add_log("INFO", f"QuantRun 执行完成: {msg}")
    buy_actions = [
        {
            "symbol": str(getattr(o, "symbol", "") or ""),
            "name": req_name_map.get(str(getattr(o, "symbol", "") or ""), ""),
            "quantity": int(getattr(o, "quantity", 0) or 0),
            "price": round(float(getattr(o, "price", 0) or 0), 4),
        }
        for o in filled
    ]
    current_positions = [
        {
            "symbol": str(p.get("symbol") or ""),
            "name": str(p.get("name") or ""),
            "quantity": int(p.get("quantity") or 0),
            "cost_price": round(float(p.get("price") or 0), 4),
        }
        for p in get_positions()
    ]
    from app.services.defensive_assets import DEFENSIVE_ASSETS as _DEFENSIVE_ASSETS_FOR_AUDIT
    defensive_asset_classes = {
        str(item["ts_code"]).split(".")[0]: str(item["asset_class"])
        for item in _DEFENSIVE_ASSETS_FOR_AUDIT
    }
    asset_function_by_symbol = {}
    for symbol in target_symbols:
        asset_class = defensive_asset_classes.get(symbol)
        if asset_class == "money":
            asset_function_by_symbol[symbol] = "liquidity"
        elif asset_class in {"bond", "gold"}:
            asset_function_by_symbol[symbol] = "diversifier"
        elif asset_class in {"low_volatility", "dividend"}:
            asset_function_by_symbol[symbol] = "core"
        else:
            asset_function_by_symbol[symbol] = "tactical" if symbol in high_risk_symbols else "core"
    current_weights_for_strategy = {
        symbol: float(value or 0.0) / max(float(portfolio["total_equity"] or 0.0), 1.0)
        for symbol, value in portfolio["position_value_by_symbol"].items()
    }
    price_by_symbol_for_strategy = {
        symbol: round(_row_trade_price(row, prefer_open=True), 4)
        for symbol, row in exec_map.items()
        if _row_trade_price(row, prefer_open=True) > 0
    }
    selection_map_for_strategy = {
        str(item.get("symbol") or ""): item
        for item in (selection_detail.get("selections") or [])
        if str(item.get("symbol") or "")
    }
    target_weight_explanations = build_target_weight_explanations(
        target_weights=cvar_weights,
        current_weights=current_weights_for_strategy,
        score_map=score_map,
        returns_by_symbol=returns_by_symbol,
        liquidity_caps=liquidity_caps,
        defensive_weights=defensive_weights,
        optimizer=optimizer_name,
        risk_budget=risk_budget,
        constraints={
            "max_single_position_pct": max_single_position_pct,
            "max_industry_position_pct": .25,
            "max_correlated_industry_pct": .35,
            "max_marginal_cvar_contribution": round(adaptive_marginal_limit, 4),
            "monthly_turnover_remaining": round(turnover_remaining, 2),
        },
    )
    final_strategy_decision = build_strategy_rebalance_actions(
        target_symbols=target_symbols,
        held_positions={str(pos.get("symbol") or ""): pos for pos in get_positions() if str(pos.get("symbol") or "")},
        selection_map=selection_map_for_strategy,
        market_rows=exec_map,
        same_day_fallback=same_day_fallback,
        mode="full_rebalance",
        blocked_actions=blocked_events,
        current_weights=current_weights_for_strategy,
        target_weights=cvar_weights,
        thesis_status_by_symbol=thesis_status_by_symbol,
        context={
            **champion_strategy_policy_context(),
            "signal_date": signal_date,
            "execute_date": execute_date,
            "data_status": "frozen",
            "strategy_template": strategy_template,
            "risk_budget": risk_budget,
            "target_position_pct": target_position_pct,
            "target_weights": cvar_weights,
            "defensive_weights": defensive_weights,
            "optimizer": optimizer_name,
            "price_by_symbol": price_by_symbol_for_strategy,
        },
    )
    strategy_controller = final_strategy_decision["strategy_controller"]
    detail = {
        **selection_detail,
        "execute_date": execute_date,
        "sell_triggers": sell_triggers,
        "blocked_events": blocked_events,
        "target_positions": [
            {
                "symbol": str(x.get("symbol") or ""),
                "name": str(x.get("name") or ""),
                "score": x.get("score"),
            }
            for x in (selection_detail.get("selections") or [])
        ],
        "rebalance_actions": {
            "buy": buy_actions,
            "sell": sell_triggers,
            "hold": sorted(list(held_after_sell.intersection(set(target_symbols)))),
            "blocked": blocked_events,
        },
        "strategy_controller": strategy_controller,
        "strategy_action_contracts": final_strategy_decision["strategy_action_contracts"],
        "rebalance_decision_trace": final_strategy_decision["rebalance_decision_trace"],
        "strategy_rebalance_actions": {
            "buy": final_strategy_decision["buy"],
            "sell": final_strategy_decision["sell"],
            "hold": final_strategy_decision["hold"],
            "blocked": final_strategy_decision["blocked"],
        },
        "target_weight_explanations": target_weight_explanations,
        "current_positions": current_positions,
        "summary": (
            f"今日趋势判断为 {trend_text}，采用{template_name}；"
            f"本次卖出 {sold_count} 只、买入 {bought_count} 只、拦截 {blocked_count} 笔。"
        ),
        "buy_summary": {
            "planned": planned_buy_count,
            "filled": bought_count,
            "fill_rate_pct": fill_rate_pct,
            "avg_slippage_bps": avg_slippage_bps,
            "effective_budget_ratio": round(effective_budget_ratio, 4),
        },
        "portfolio_risk": {
            "optimizer": optimizer_name,
            "role_contract": {
                "strategy_controller": "负责买入/卖出/持有动作、退出诊断、策略参数版本与人工上线门",
                "portfolio_optimizer": "负责在策略动作约束下分配目标权重、控制单票/行业/相关性/换手/尾部风险",
                "execution_layer": "负责模拟订单、价格可得性、T+1/持有期/费用约束；不连接券商",
            },
            "weights": cvar_weights,
            "weight_explanations": target_weight_explanations,
            "black_litterman": bl_result,
            "covariance": covariance_snapshot,
            "hard_constraints": {"max_single_position_pct": max_single_position_pct, "max_industry_position_pct": .25, "max_correlated_industry_pct": .35, "max_marginal_cvar_contribution": round(adaptive_marginal_limit, 4)},
            "monthly_turnover_budget": {**turnover_budget, "remaining_after": round(turnover_remaining, 2)},
            "marginal_cvar": marginal_cvar,
            "industry_allocation": industry_allocation,
            "asset_function_by_symbol": asset_function_by_symbol,
            "full_investment": {
                "target_weight": 1.0,
                "minimum_invested_ratio": .995,
                "maximum_residual_cash": 500.0,
                "defensive_target_weight": round(defensive_target_weight, 6),
            },
            "decision_version_manifest": selection_detail.get("decision_version_manifest") or {},
        },
    }
    transaction_cost = round(sum(
        sum(float(order.get(key, 0) or 0) for key in ("commission", "transfer_fee", "stamp_tax"))
        for order in (orders_payload + sold_ops_payload)
    ), 2)
    detail["daily_report"] = {
        "schema_version": "daily_rebalance_report_v1",
        "signal_date": signal_date,
        "execute_date": execute_date,
        "market_environment": {
            "current": selection_detail.get("trend_analysis") or {},
            "change_from_previous": (selection_detail.get("trend_analysis") or {}).get("change_from_previous"),
        },
        "portfolio": {
            "equity": portfolio.get("total_equity"),
            "cash": portfolio.get("cash_balance"),
            "risk_budget": risk_budget,
            "optimizer": optimizer_name,
        },
        "simulated_trades": {"buys": buy_actions, "sells": sell_triggers, "transaction_cost": transaction_cost},
        "trade_reasons": {
            "new_facts_and_scores": selection_detail.get("selections") or [],
            "valuation_and_risk": selection_detail.get("filter_summary") or {},
            "strategy_action_contracts": strategy_controller["contracts"],
        },
        "unchanged_holdings": [
            {"symbol": symbol, "reason": "仍在目标组合且未触发逻辑/风险退出条件", "next_trigger": "目标池、逻辑或风险状态变化"}
            for symbol in sorted(held_after_sell.intersection(set(target_symbols)))
        ],
        "rejected_signals": blocked_events,
        "next_observations": [
            {"symbol": str(item.get("symbol") or ""), "conditions": ["新公告与财务证据", "估值安全边际", "投资逻辑失效条件"]}
            for item in (selection_detail.get("selections") or [])[:20]
        ],
        "learning_and_challenger": {
            "strategy_template": selection_detail.get("strategy_template") or {},
            "model_version": (selection_detail.get("strategy_template") or {}).get("id") or "rule_fallback_v1",
            "experiments": selection_detail.get("challenger_experiments") or [],
        },
    }
    try:
        selection_by_symbol = {str(x.get("symbol") or ""): x for x in (selection_detail.get("selections") or [])}
        template = selection_detail.get("strategy_template") or {}
        model_version = str(template.get("id") or "rule_fallback_v1")
        market_state = selection_detail.get("trend_analysis") or {}
        decision_time = f"{execute_date}T09:30:00"
        data_as_of = signal_available_time(signal_date)
        for action in buy_actions:
            symbol = str(action.get("symbol") or "")
            evidence = selection_by_symbol.get(symbol) or {}
            frozen_research, frozen_valuation = _frozen_research_evidence(symbol, as_of_time=data_as_of)
            frozen_research["selection"] = evidence
            create_decision_snapshot({
                "ts_code": _ts_code(symbol), "decision_type": "buy", "decision_time": decision_time,
                "execution_id": execution_id,
                "price": action.get("price"), "data_as_of_time": data_as_of, "model_version": model_version,
                "market_state": market_state, "research_state": frozen_research, "valuation": frozen_valuation,
                "portfolio_context": detail.get("portfolio_risk") or {},
                "reason": f"入选目标组合；{template.get('reason') or detail.get('summary')}",
                "invalidation_conditions": ["跌出目标组合", "硬风险门控触发", "组合风险约束要求减仓"],
            })
        for action in sell_triggers:
            symbol = str(action.get("symbol") or "")
            frozen_research, frozen_valuation = _frozen_research_evidence(symbol, as_of_time=data_as_of)
            create_decision_snapshot({
                "ts_code": _ts_code(symbol), "decision_type": "sell", "decision_time": decision_time,
                "execution_id": execution_id,
                "price": action.get("price"), "data_as_of_time": data_as_of, "model_version": model_version,
                "market_state": market_state, "research_state": frozen_research, "valuation": frozen_valuation,
                "portfolio_context": detail.get("portfolio_risk") or {},
                "reason": str(action.get("reason") or "不再满足目标组合条件"),
                "invalidation_conditions": [],
            })
    except Exception as exc:  # decision evidence failure must be visible but cannot corrupt the execution ledger
        add_log("ERROR", f"决策快照写入失败：{str(exc)[:240]}")
        detail["decision_snapshot_error"] = str(exc)[:240]
    return QuantRunExecResult(
        signal_date=signal_date,
        execute_date=execute_date,
        sold_count=sold_count,
        bought_count=bought_count,
        total_buy_cost=total_buy_cost,
        execution_id=execution_id,
        message=msg,
        planned_buy_count=planned_buy_count,
        executed_buy_count=bought_count,
        fill_rate_pct=fill_rate_pct,
        avg_slippage_bps=avg_slippage_bps,
        exec_latency_ms=exec_latency_ms,
        detail=detail,
    )


def analyze_market_plan(
    *,
    strategy_id: int | None = None,
    signal_date: str | None = None,
    top_n: int = 10,
    exchange_group: str = "HS",
    scoring_timeout_sec: int | None = None,
) -> Dict[str, Any]:
    """
    仅分析：趋势理解 -> 策略模板 -> 选股目标池 -> 调仓建议。
    不执行真实买卖，供前端一键分析展示。
    """
    execution_guard = champion_execution_gate()
    champion_quarantined = str(execution_guard.get("status") or "") == "quarantined"
    dates = list_consolidated_trade_dates()
    if not dates:
        raise HistoricalDataMissingError("可用交易日不足，无法分析")
    sig = str(signal_date or dates[-1]).strip()
    if sig not in dates:
        raise ValueError(f"signal_date 不在可用交易日列表: {sig}")
    execute_date, same_day_fallback = resolve_execute_date(sig)

    sid = int(strategy_id or 0)
    if sid <= 0:
        sid = int(resolve_production_strategy_champion()["strategy_id"])

    ds = BacktestDataSource()
    exchange_group = normalize_exchange_group(exchange_group)
    targets, detail = _build_target_symbols(
        ds=ds,
        strategy_id=sid,
        signal_date=sig,
        top_n=int(top_n),
        exchange_group=exchange_group,
        scoring_timeout_sec=scoring_timeout_sec,
    )
    targets, research_blocks = _apply_research_admission(targets, as_of_time=signal_available_time(sig))
    targets, dynamic_blocks, dynamic_gate = _apply_dynamic_strategy_gate(
        targets, as_of_time=signal_available_time(sig)
    )
    if champion_quarantined:
        targets = []
        dynamic_gate["risk_budget_multiplier"] = 0.0
        dynamic_blocks.append({
            "date": execute_date,
            "side": "BUY",
            "code": "champion_formal_oos_quarantined",
            "message": "当前 Champion 正式 OOS 失败；分析仅保留减仓/卖出建议",
            "execution_guard": execution_guard,
        })
    detail["research_admission"] = {"admitted": list(targets), "blocked": research_blocks}
    detail["dynamic_strategy_gate"] = dynamic_gate
    target_set = set(targets)
    positions = get_positions()
    held: Dict[str, Dict[str, Any]] = {}
    for pos in positions:
        sym = str(pos.get("symbol") or "").strip()
        if sym:
            held[sym] = pos

    selection_map = {
        str(x.get("symbol") or "").strip(): x
        for x in (detail.get("selections") or [])
        if str(x.get("symbol") or "").strip()
    }
    exec_rows = ds.load_snapshot(execute_date.replace("-", ""))
    exec_map = {
        str(getattr(r, "symbol", "") or "").strip(): r
        for r in exec_rows
        if str(getattr(r, "symbol", "") or "").strip()
    }

    price_by_symbol = {
        sym: round(_row_trade_price(row, prefer_open=True), 4)
        for sym, row in exec_map.items()
        if _row_trade_price(row, prefer_open=True) > 0
    }
    strategy_decision = build_strategy_rebalance_actions(
        target_symbols=targets,
        held_positions=held,
        selection_map=selection_map,
        market_rows=exec_map,
        same_day_fallback=same_day_fallback,
        mode="full_rebalance",
        blocked_actions=list(detail.get("blocked_events") or []) + research_blocks + dynamic_blocks,
        context={
            **champion_strategy_policy_context(),
            "signal_date": sig,
            "execute_date": execute_date,
            "data_status": "frozen",
            "strategy_template": detail.get("strategy_template") or {},
            "risk_budget": detail.get("risk_budget") or {},
            "target_position_pct": (detail.get("risk_budget") or {}).get("target_position_pct") if isinstance(detail.get("risk_budget"), dict) else None,
            "optimizer": "analysis_target_pool_v1",
            "price_by_symbol": price_by_symbol,
        },
    )
    buy_actions = strategy_decision["buy"]
    sell_actions = strategy_decision["sell"]
    hold_symbols = strategy_decision["hold"]
    trend = (detail.get("trend_analysis") or {}).get("trend")
    template_name = (detail.get("strategy_template") or {}).get("name") or (detail.get("strategy_template") or {}).get("id")
    summary = (
        f"分析日 {sig}，执行日 {execute_date}：趋势判断为 {trend or '未知'}，采用{template_name or '策略模板'}；"
        f"建议买入 {len(buy_actions)} 只、卖出 {len(sell_actions)} 只、继续持有 {len(hold_symbols)} 只。"
    )
    execution_note = (
        "按执行日开盘价模拟成交（与量化任务口径一致）"
        if not same_day_fallback
        else "当前为最新交易日，暂用当日开盘价口径执行"
    )
    strategy_controller = strategy_decision["strategy_controller"]

    current_positions = [
        {
            "symbol": sym,
            "name": str(pos.get("name") or ""),
            "quantity": int(pos.get("quantity") or 0),
            "cost_price": round(float(pos.get("price") or 0), 4),
        }
        for sym, pos in sorted(held.items())
    ]

    return {
        "ok": True,
        "signal_date": sig,
        "execute_date": execute_date,
        "execution_note": execution_note,
        "same_day_fallback": same_day_fallback,
        "analyzed_at": datetime.now().astimezone().isoformat(),
        "strategy_id": sid,
        "exchange_group": exchange_group,
        "champion_execution_gate": execution_guard,
        "target_symbols": targets,
        **detail,
        "rebalance_actions": {
            "buy": buy_actions,
            "sell": sell_actions,
            "hold": hold_symbols,
            "blocked": research_blocks + dynamic_blocks,
        },
        "strategy_controller": strategy_controller,
        "strategy_action_contracts": strategy_decision["strategy_action_contracts"],
        "rebalance_decision_trace": strategy_decision["rebalance_decision_trace"],
        "current_positions": current_positions,
        "summary": summary,
    }


def plan_analyze_rebalance(
    *,
    signal_date: str,
    symbols: List[str],
    strategy_id: int | None = None,
    mode: str = "full_rebalance",
    target_position_pct: float | None = None,
    max_single_position_pct: float | None = None,
    target_symbols: List[str] | None = None,
    selection_scores: Dict[str, float] | None = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """预览或执行分析调仓：次日开盘价口径、总权益仓位、单票上限、先卖后买。"""
    sig = str(signal_date or "").strip()
    if not dry_run:
        _require_consistent_simulation_ledger()
    execution_guard = champion_execution_gate()
    champion_quarantined = str(execution_guard.get("status") or "") == "quarantined"
    dates = list_consolidated_trade_dates()
    if sig not in dates:
        raise ValueError(f"signal_date 不在可用交易日列表: {sig}")

    execute_date, same_day_fallback = resolve_execute_date(sig)
    sid = int(strategy_id or 0)
    if sid <= 0:
        sid = int(resolve_production_strategy_champion()["strategy_id"])

    strategy = get_strategy(sid)
    config = parse_strategy_content(strategy.get("content"), strategy.get("type", "formula")) if strategy else None
    hold_days = int(config.hold_days) if config and config.hold_days else None
    if champion_quarantined:
        hold_days = None

    ds = BacktestDataSource()
    exec_rows = ds.load_snapshot(execute_date.replace("-", ""))
    if not exec_rows:
        raise ReplayRiskGateError(f"执行日 {execute_date} 快照缺失", code="paused_data_not_ready")
    exec_map = {str(getattr(r, "symbol", "") or "").strip(): r for r in exec_rows}

    requested_targets = [str(s).strip() for s in (target_symbols or symbols or []) if str(s).strip()]
    research_targets, research_blocks = _apply_research_admission(requested_targets, as_of_time=signal_available_time(sig))
    gated_targets, dynamic_blocks, dynamic_gate = _apply_dynamic_strategy_gate(
        research_targets, as_of_time=signal_available_time(sig)
    )
    if champion_quarantined:
        gated_targets = []
        dynamic_gate["risk_budget_multiplier"] = 0.0
        dynamic_blocks.append({
            "date": execute_date,
            "side": "BUY",
            "code": "champion_formal_oos_quarantined",
            "message": "当前 Champion 正式 OOS 失败；调仓隔离为仅减仓/卖出",
            "execution_guard": execution_guard,
        })
    high_risk_symbols = {
        str(item.get("symbol") or "") for item in (dynamic_gate.get("items") or [])
        if str((item.get("risk") or {}).get("risk_level") or "") == "high"
    }
    pool = set(gated_targets)
    buy_symbols = [str(s).strip() for s in symbols if str(s).strip() in pool]
    scores = dict(selection_scores or {})
    for sym in buy_symbols:
        scores.setdefault(sym, 0.0)
    held_before_strategy: Dict[str, Dict[str, Any]] = {}
    for pos in get_positions():
        sym = str(pos.get("symbol") or "").strip()
        if sym:
            held_before_strategy[sym] = pos
    selection_map_for_strategy = {
        sym: {"symbol": sym, "score": scores.get(sym)}
        for sym in buy_symbols
    }
    price_by_symbol_for_strategy = {
        sym: round(_row_trade_price(row, prefer_open=True), 4)
        for sym, row in exec_map.items()
        if _row_trade_price(row, prefer_open=True) > 0
    }
    initial_strategy_decision = build_strategy_rebalance_actions(
        target_symbols=buy_symbols,
        held_positions=held_before_strategy,
        selection_map=selection_map_for_strategy,
        market_rows=exec_map,
        same_day_fallback=same_day_fallback,
        mode=mode,
        blocked_actions=research_blocks + dynamic_blocks,
        context={
            **champion_strategy_policy_context(),
            "signal_date": sig,
            "execute_date": execute_date,
            "data_status": "frozen",
            "risk_budget": {
                "target_position_pct": target_position_pct if target_position_pct is not None else dynamic_gate.get("risk_budget_multiplier"),
                "max_single_position_pct": max_single_position_pct if max_single_position_pct is not None else .25,
            },
            "target_position_pct": target_position_pct if target_position_pct is not None else dynamic_gate.get("risk_budget_multiplier"),
            "optimizer": "strategy_controller_pretrade_v1",
            "price_by_symbol": price_by_symbol_for_strategy,
        },
    )
    buy_symbols = [str(item.get("symbol") or "") for item in initial_strategy_decision["buy"] if str(item.get("symbol") or "")]

    rebalance_mode = str(mode or "full_rebalance").strip().lower()
    sell_preview: List[Dict[str, Any]] = []
    sell_blocked: List[Dict[str, Any]] = list(research_blocks) + list(dynamic_blocks)
    sold_count = 0
    sold_ops_payload: List[Dict[str, Any]] = []
    executed_at = f"{execute_date}T09:30:00"

    if rebalance_mode == "full_rebalance":
        for pos in get_positions():
            sym = str(pos.get("symbol") or "").strip()
            if not sym or sym in pool:
                continue
            if hold_days and hold_days > 0:
                bought_at = str(pos.get("bought_at") or "")[:10]
                if bought_at:
                    try:
                        held_days = (datetime.fromisoformat(execute_date) - datetime.fromisoformat(bought_at)).days
                        if held_days < hold_days:
                            sell_blocked.append(
                                {
                                    "symbol": sym,
                                    "side": "SELL",
                                    "code": "hold_days_not_met",
                                    "message": f"{sym} 未满足最短持有期 {hold_days} 天",
                                }
                            )
                            continue
                    except ValueError:
                        pass
            px_row = exec_map.get(sym)
            px = _row_trade_price(px_row, prefer_open=True)
            if px <= 0:
                sell_blocked.append(
                    {"symbol": sym, "side": "SELL", "code": "no_price", "message": f"{sym} 无有效执行价"}
                )
                continue
            tradable = check_open_trade_constraints(
                side="SELL",
                symbol=sym,
                name=str(pos.get("name") or ""),
                open_price=px,
                close_price=float(getattr(px_row, "close", 0) or 0) if px_row else 0.0,
                change_pct=float(getattr(px_row, "change_pct", 0) or 0) if px_row else 0.0,
                volume=float(getattr(px_row, "volume", 0) or 0) if px_row else 0.0,
            )
            qty = int(pos.get("quantity") or 0)
            fee = sell_cost_breakdown(px, qty, sym)
            exec_price = float(fee["executed_price"])
            item = {
                "symbol": sym,
                "name": str(pos.get("name") or ""),
                "quantity": qty,
                "price": exec_price,
                "requested_price": round(px, 4),
                **{k: fee[k] for k in ("commission", "transfer_fee", "stamp_tax", "slippage_bps", "fee_schedule_version")},
                "status": "simulated" if tradable.ok else "blocked",
                "message": tradable.message,
                "block_code": tradable.code,
            }
            if not tradable.ok:
                sell_blocked.append({"symbol": sym, "side": "SELL", "code": tradable.code, "message": tradable.message})
                sell_preview.append(item)
                continue
            sell_preview.append(item)
            if dry_run:
                continue
            try:
                sell_position(
                    int(pos["id"]),
                    quantity=qty,
                    price=exec_price,
                    trade_date=execute_date,
                    record_operation=True,
                    operation_at=executed_at,
                    operation_remark="量化分析调仓卖出",
                    fee_breakdown=fee,
                )
                sold_count += 1
                sold_ops_payload.append(
                    {
                        "symbol": sym,
                        "side": "SELL",
                        "price": exec_price,
                        "quantity": qty,
                        "at": executed_at,
                        "remark": "量化分析调仓卖出",
                        "name": str(pos.get("name") or "").strip(),
                        **{k: fee[k] for k in ("commission", "transfer_fee", "stamp_tax", "slippage_bps", "fee_schedule_version")},
                    }
                )
            except ValueError as e:
                sell_blocked.append({"symbol": sym, "side": "SELL", "code": "t1_blocked", "message": str(e)})

    portfolio = _portfolio_state(ds, execute_date)
    cash_balance = float(portfolio["cash_balance"])
    if cash_balance < 100 and not dry_run:
        raise ReplayRiskGateError(
            f"账户余额不足（当前 {cash_balance:.2f} 元），最低需 100 元",
            code="paused_insufficient_balance",
        )

    risk_budget = {
        "target_position_pct": min(
            target_position_pct if target_position_pct is not None else 1.0,
            float(dynamic_gate.get("risk_budget_multiplier") or 0),
        ),
        "max_single_position_pct": min(max_single_position_pct if max_single_position_pct is not None else .25, .25),
    }
    target_pct, max_single = _parse_risk_budget(risk_budget)
    buy_budget = _compute_buy_budget(
        total_equity=portfolio["total_equity"],
        position_market_value=portfolio["position_market_value"],
        cash_balance=cash_balance,
        target_position_pct=target_pct,
    )

    held = {str(p.get("symbol") or "").strip() for p in get_positions()}
    pending_buys = [sym for sym in buy_symbols if sym not in held]
    order_requests: List[OrderRequest] = []
    name_by_symbol: Dict[str, str] = {}
    for sym in pending_buys:
        row = exec_map.get(sym)
        px = _row_trade_price(row, prefer_open=True)
        if px <= 0:
            continue
        name = str(getattr(row, "name", "") or "")
        name_by_symbol[sym] = name
        order_requests.append(
            OrderRequest(
                symbol=sym,
                side="BUY",
                price=px,
                quantity=0,
                name=name,
                change_pct=float(getattr(row, "change_pct", 0) or 0),
                volume=float(getattr(row, "volume", 0) or 0),
                close=float(getattr(row, "close", 0) or 0),
            )
        )

    industry_by_symbol, existing_industry_value = _industry_exposure_inputs(
        list(portfolio["position_value_by_symbol"]) + [str(order.symbol) for order in order_requests],
        portfolio["position_value_by_symbol"],
    )
    plan_returns: Dict[str, List[float]] = {}
    plan_liquidity_caps: Dict[str, float] = {}
    for order in order_requests:
        hist = ds.get_hist_window(str(order.symbol), sig, 60)
        closes = [float(getattr(item, "close", 0) or 0) for item in hist]
        plan_returns[str(order.symbol)] = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(1, len(closes)) if closes[index - 1] > 0 and closes[index] > 0
        ]
        amounts = [float(getattr(item, "turnover", 0) or 0) for item in hist[-20:] if float(getattr(item, "turnover", 0) or 0) > 0]
        if amounts:
            plan_liquidity_caps[str(order.symbol)] = sum(amounts) / len(amounts) * 1000.0 * .01
    target_weights = constrained_cvar_weights(plan_returns, max_single=max_single)
    plan_optimizer = "ledoit_wolf_shrinkage_plus_constrained_cvar_v1"
    if not target_weights:
        target_weights = hrp_fallback_weights(plan_returns, max_single=max_single)
        plan_optimizer = "hrp_inverse_volatility_fallback_v1"
    plan_bl: Dict[str, Any] = {}
    if target_weights:
        values = [float(scores.get(symbol, 0) or 0) for symbol in target_weights]
        mean_score = sum(values) / len(values) if values else 0.0
        plan_bl = black_litterman_weights(
            target_weights,
            {symbol: (float(scores.get(symbol, 0) or 0) - mean_score) / 100.0 for symbol in target_weights},
            {symbol: min(1.0, len(plan_returns.get(symbol, [])) / 60.0) for symbol in target_weights},
            max_weight=max_single,
        )
        target_weights = dict(plan_bl.get("weights") or target_weights)
        plan_optimizer += "+black_litterman_diagonal_v1"
    symbol_position_limits = _pretrade_symbol_limits(
        [str(order.symbol) for order in order_requests], cutoff=signal_available_time(sig), max_single=max_single,
    )
    planned, buy_blocked, pruned = allocate_buy_orders(
        order_requests,
        budget=buy_budget,
        total_equity=portfolio["total_equity"],
        max_single_position_pct=max_single,
        existing_position_value=portfolio["position_value_by_symbol"],
        symbol_scores=scores,
        industry_by_symbol=industry_by_symbol,
        existing_industry_value=existing_industry_value,
        max_industry_position_pct=.25,
        max_correlated_industry_pct=.35,
        target_weights=target_weights,
        symbol_position_limits=symbol_position_limits,
        high_risk_symbols=high_risk_symbols,
        max_order_value_by_symbol=plan_liquidity_caps,
    )
    buy_preview = [
        {
            "symbol": x.symbol,
            "name": x.name,
            "quantity": x.quantity,
            "request_price": x.request_price,
            "exec_price": x.exec_price,
            "amount": x.amount,
            "commission": x.commission,
            "transfer_fee": x.transfer_fee,
            "total_cost": x.total_cost,
            "position_pct": x.position_pct,
            "score": x.score,
            "status": x.status,
            "message": x.message,
            "block_code": x.block_code,
        }
        for x in planned
    ]
    blocked_buy_orders = [
        {
            "symbol": str(getattr(o, "symbol", "") or ""),
            "side": "BUY",
            "price": round(float(getattr(o, "price", 0) or 0), 4),
            "quantity": 0,
            "status": str(getattr(o, "status", "") or "blocked"),
            "message": str(getattr(o, "message", "") or ""),
            "block_code": str(getattr(o, "block_code", "") or ""),
        }
        for o in buy_blocked
    ]
    current_weights_for_strategy = {
        symbol: float(value or 0.0) / max(float(portfolio["total_equity"] or 0.0), 1.0)
        for symbol, value in portfolio["position_value_by_symbol"].items()
    }
    target_weight_explanations = build_target_weight_explanations(
        target_weights=target_weights,
        current_weights=current_weights_for_strategy,
        score_map=scores,
        returns_by_symbol=plan_returns,
        liquidity_caps=plan_liquidity_caps,
        defensive_weights={},
        optimizer=plan_optimizer,
        risk_budget=risk_budget,
        constraints={
            "max_single_position_pct": max_single,
            "max_industry_position_pct": .25,
            "max_correlated_industry_pct": .35,
            "buy_budget": round(buy_budget, 2),
        },
    )
    final_strategy_decision = build_strategy_rebalance_actions(
        target_symbols=buy_symbols,
        held_positions={str(pos.get("symbol") or ""): pos for pos in get_positions() if str(pos.get("symbol") or "")},
        selection_map={symbol: {"symbol": symbol, "score": scores.get(symbol), "name": name_by_symbol.get(symbol, "")} for symbol in buy_symbols},
        market_rows=exec_map,
        same_day_fallback=same_day_fallback,
        mode=rebalance_mode,
        blocked_actions=sell_blocked + blocked_buy_orders,
        current_weights=current_weights_for_strategy,
        target_weights=target_weights,
        context={
            **champion_strategy_policy_context(),
            "signal_date": sig,
            "execute_date": execute_date,
            "data_status": "frozen",
            "risk_budget": risk_budget,
            "target_position_pct": target_pct,
            "target_weights": target_weights,
            "optimizer": plan_optimizer,
            "price_by_symbol": price_by_symbol_for_strategy,
        },
    )
    strategy_controller = final_strategy_decision["strategy_controller"]
    total_buy_cost = round(sum(float(x.get("total_cost") or 0) for x in buy_preview), 2)
    estimated_cash_after = round(cash_balance - total_buy_cost, 2)
    if not dry_run and total_buy_cost > cash_balance:
        raise ReplayRiskGateError(
            f"本次买入需 {total_buy_cost:.2f} 元，账户余额 {cash_balance:.2f} 元不足",
            code="paused_insufficient_balance",
        )

    execution_id: int | None = None
    bought_count = 0
    if not dry_run and (buy_preview or sold_ops_payload):
        filled_results = planned_to_order_results(planned)
        orders_payload = [
            {
                "symbol": o.symbol,
                "side": o.side,
                "price": o.price,
                "quantity": o.quantity,
                "commission": getattr(o, "commission", 0),
                "transfer_fee": getattr(o, "transfer_fee", 0),
                "slippage_bps": round(
                    (float(o.price) / float(next((x.request_price for x in planned if x.symbol == o.symbol), o.price)) - 1.0) * 10000.0,
                    4,
                ),
                "fee_schedule_version": ETF_FEE_SCHEDULE_VERSION if is_exchange_etf(o.symbol) else FEE_SCHEDULE_VERSION,
                "name": name_by_symbol.get(o.symbol, ""),
            }
            for o in filled_results
        ]
        if orders_payload or sold_ops_payload:
            if orders_payload:
                execution_id = create_and_apply_buy_execution(
                    strategy_id=int(sid),
                    executed_at=executed_at,
                    budget=total_buy_cost,
                    top_n=len(order_requests),
                    orders=orders_payload,
                    name_by_symbol=name_by_symbol,
                )
            else:
                execution_id = create_execution(
                    strategy_id=int(sid),
                    executed_at=executed_at,
                    budget=0.0,
                    top_n=len(order_requests),
                    orders=[],
                )
            if sold_ops_payload:
                link_recorded_operations_to_execution(execution_id, sold_ops_payload)
            bought_count = len(orders_payload)

    planned_buy_count = len(order_requests)
    fill_rate_pct = round((len(buy_preview) / planned_buy_count) * 100.0, 2) if planned_buy_count > 0 else 0.0
    execution_note = (
        "按执行日开盘价模拟成交（与量化任务口径一致）"
        if not same_day_fallback
        else "当前为最新交易日，暂用当日开盘价口径执行"
    )
    msg = (
        f"分析调仓{'预览' if dry_run else '执行'}：signal={sig} execute={execute_date} mode={rebalance_mode} "
        f"卖出{sold_count if not dry_run else len([x for x in sell_preview if x.get('status') == 'simulated'])} "
        f"买入{len(buy_preview) if dry_run else bought_count} 成本={total_buy_cost:.2f}"
    )
    if not dry_run:
        add_log("INFO", msg)

    decision_snapshot_ids: list[int] = []
    decision_snapshot_error = ""
    pool_sync: dict[str, Any] | None = None
    if not dry_run and (bought_count > 0 or sold_count > 0):
        try:
            decision_time = executed_at or datetime.now().isoformat(timespec="seconds")
            data_as_of_time = signal_available_time(sig)
            model_version = f"rebalance_strategy_{sid}" if sid else "analysis_rebalance_v1"
            shared_context = {
                "signal_date": sig,
                "execute_date": execute_date,
                "mode": rebalance_mode,
                "target_position_pct": target_pct,
                "max_single_position_pct": max_single,
                "selection_scores": scores,
            }
            for order in buy_preview:
                symbol = str(order.get("symbol") or "")
                frozen_research, frozen_valuation = _frozen_research_evidence(symbol, as_of_time=data_as_of_time)
                frozen_research["selection_score"] = order.get("score")
                frozen_research["name"] = order.get("name")
                snapshot = create_decision_snapshot({
                    "ts_code": _ts_code(symbol), "decision_type": "buy",
                    "execution_id": execution_id,
                    "decision_time": decision_time, "price": order.get("exec_price"),
                    "data_as_of_time": data_as_of_time, "model_version": model_version,
                    "market_state": shared_context,
                    "research_state": frozen_research,
                    "valuation": frozen_valuation, "portfolio_context": {"equity": portfolio["total_equity"], "cash_before": cash_balance, "buy_cost": total_buy_cost},
                    "reason": f"自动模拟调仓买入；评分={order.get('score')}; {execution_note}",
                    "invalidation_conditions": ["触发组合风险门控", "目标权重或选股评分失效", "交易约束禁止继续持有"],
                })
                decision_snapshot_ids.append(int(snapshot["id"]))
            for order in sell_preview:
                if str(order.get("status") or "") not in {"filled", "executed", "simulated"}:
                    continue
                symbol = str(order.get("symbol") or "")
                frozen_research, frozen_valuation = _frozen_research_evidence(symbol, as_of_time=data_as_of_time)
                snapshot = create_decision_snapshot({
                    "ts_code": _ts_code(symbol), "decision_type": "sell",
                    "execution_id": execution_id,
                    "decision_time": decision_time, "price": order.get("price"),
                    "data_as_of_time": data_as_of_time, "model_version": model_version,
                    "market_state": shared_context, "research_state": frozen_research, "valuation": frozen_valuation,
                    "portfolio_context": {"equity": portfolio["total_equity"], "cash_before": cash_balance},
                    "reason": str(order.get("message") or "自动模拟调仓卖出"),
                    "invalidation_conditions": [],
                })
                decision_snapshot_ids.append(int(snapshot["id"]))
        except Exception as exc:  # Decision logging must not roll back a completed simulated execution.
            decision_snapshot_error = str(exc)[:240]
            add_log("ERROR", f"决策快照写入失败：{decision_snapshot_error}")
        try:
            pool_sync = sync_pool_from_confirmed_execution(
                buy_orders=buy_preview, sell_orders=sell_preview, execution_id=execution_id, executed_at=decision_time,
            )
        except Exception as exc:  # Pool state is secondary to a completed execution but remains observable.
            detail = str(exc)[:240]
            add_log("ERROR", f"股票池执行状态同步失败：{detail}")
            pool_sync = {"ok": False, "error": detail}

    return {
        "ok": dry_run or bought_count > 0 or sold_count > 0,
        "dry_run": dry_run,
        "mode": rebalance_mode,
        "signal_date": sig,
        "execute_date": execute_date,
        "execution_note": execution_note,
        "same_day_fallback": same_day_fallback,
        "executed_at": executed_at if not dry_run else None,
        "execution_id": execution_id,
        "strategy_id": sid,
        "champion_execution_gate": execution_guard,
        "total_equity": portfolio["total_equity"],
        "cash_balance": portfolio["cash_balance"],
        "position_market_value": portfolio["position_market_value"],
        "buy_budget": buy_budget,
        "target_position_pct": target_pct,
        "max_single_position_pct": max_single,
        "total_buy_cost": total_buy_cost,
        "estimated_cash_after": estimated_cash_after,
        "planned_buy_count": planned_buy_count,
        "bought_count": len(buy_preview) if dry_run else bought_count,
        "sold_count": len([x for x in sell_preview if x.get("status") == "simulated"]) if dry_run else sold_count,
        "blocked_count": len(blocked_buy_orders) + len(sell_blocked),
        "pruned_symbols": pruned,
        "fill_rate_pct": fill_rate_pct,
        "account_balance": round(float(get_account_balance() or 0), 2) if not dry_run else portfolio["cash_balance"],
        "message": msg,
        "sell_orders": sell_preview,
        "buy_orders": buy_preview,
        "blocked_events": sell_blocked + blocked_buy_orders,
        "orders": buy_preview + blocked_buy_orders,
        "strategy_controller": strategy_controller,
        "strategy_action_contracts": final_strategy_decision["strategy_action_contracts"],
        "rebalance_decision_trace": final_strategy_decision["rebalance_decision_trace"],
        "strategy_rebalance_actions": {
            "buy": final_strategy_decision["buy"],
            "sell": final_strategy_decision["sell"],
            "hold": final_strategy_decision["hold"],
            "blocked": final_strategy_decision["blocked"],
        },
        "target_weight_explanations": target_weight_explanations,
        "decision_snapshot_ids": decision_snapshot_ids,
        "decision_snapshot_error": decision_snapshot_error or None,
        "pool_sync": pool_sync,
        "research_admission": {"admitted": research_targets, "blocked": research_blocks},
        "dynamic_strategy_gate": dynamic_gate,
        "portfolio_optimizer": {
            "method": plan_optimizer,
            "weights": target_weights,
            "weight_explanations": target_weight_explanations,
            "black_litterman": plan_bl,
            "hard_constraints": {"max_single_position_pct": max_single, "max_industry_position_pct": .25, "max_correlated_industry_pct": .35},
        },
    }


def preview_analyze_rebalance_plan(**kwargs: Any) -> Dict[str, Any]:
    return plan_analyze_rebalance(dry_run=True, **kwargs)


def execute_analyze_rebalance_plan(**kwargs: Any) -> Dict[str, Any]:
    result = plan_analyze_rebalance(dry_run=False, **kwargs)
    if not result.get("ok"):
        raise ValueError(result.get("message") or "调仓未成交")
    return result


def execute_analyze_buy_plan(
    *,
    signal_date: str,
    symbols: List[str],
    strategy_id: int | None = None,
    target_position_pct: float = 1.0,
    max_single_position_pct: float | None = None,
    mode: str = "buy_only",
) -> Dict[str, Any]:
    """兼容旧接口：默认仅买入。"""
    return execute_analyze_rebalance_plan(
        signal_date=signal_date,
        symbols=symbols,
        strategy_id=strategy_id,
        mode=mode,
        target_position_pct=target_position_pct,
        max_single_position_pct=max_single_position_pct,
    )
