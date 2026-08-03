from __future__ import annotations

from datetime import datetime, timezone
import math
import os
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
from app.services.factor_platform import compute_bound_factor
from app.services.rebalance_allocator import allocate_buy_orders, planned_to_order_results
from app.services.decision_store import create_decision_snapshot
from app.services.advanced_controls import black_litterman_weights
from app.services.stock_pool_sync import sync_pool_from_confirmed_execution
from app.services.strategy_recommender import recommend_strategy_template
from app.services.strategy_config import (
    get_strategy_factor_names,
    get_strategy_expert_route,
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
from app.services.quant_run_contracts import (
    HistoricalDataMissingError,
    QuantRunExecResult,
    ReplayRiskGateError,
    require_consistent_simulation_ledger as _require_consistent_simulation_ledger,
)
from app.services import quant_run_primitives as _primitives
from app.services import quant_run_portfolio_prep as _portfolio_prep
from app.services import quant_run_research_gates as _research_gates
from app.services.quant_run_plan_input import build_authoritative_selection_detail, normalize_authoritative_plan
from app.services.risk_limit_contract import (
    DEFAULT_CORRELATED_INDUSTRY_LIMIT,
    DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    DEFAULT_INDUSTRY_LIMIT,
)

_SCORING_HEARTBEAT_EVERY = max(100, min(int(os.getenv("QUANT_SCORING_HEARTBEAT_EVERY", "300")), 2000))
_SCORING_TIMEOUT_SEC = max(10, min(int(os.getenv("QUANT_SCORING_TIMEOUT_SEC", "90")), 1800))


def _ts_code(symbol: str) -> str:
    return _research_gates.normalize_ts_code(symbol)


def _production_market_context_as_of(
    as_of_time: str,
    *,
    market_state_binding: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return _research_gates.production_market_context_as_of(
        as_of_time,
        market_state_binding=market_state_binding,
    )


def _apply_research_admission(target_symbols: List[str], *, as_of_time: str | None = None) -> tuple[List[str], List[Dict[str, Any]]]:
    """Admit model selections only after current core-pool evidence gates pass."""
    return _research_gates.apply_research_admission(
        target_symbols,
        as_of_time=as_of_time,
    )


def _apply_dynamic_strategy_gate(
    target_symbols: List[str], *, as_of_time: str,
) -> tuple[List[str], List[Dict[str, Any]], Dict[str, Any]]:
    """Apply P2 environment/expectation/valuation gates to already research-admitted names."""
    return _research_gates.apply_dynamic_strategy_gate(
        target_symbols,
        as_of_time=as_of_time,
    )


def _frozen_research_evidence(symbol: str, *, as_of_time: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build the replay-safe research and valuation payload frozen with a trade decision."""
    return _research_gates.frozen_research_evidence(
        symbol,
        as_of_time=as_of_time,
    )


def list_consolidated_trade_dates() -> List[str]:
    return _primitives.list_consolidated_trade_dates()


def require_consolidated_trade_dates(min_count: int = 2) -> List[str]:
    return _primitives.require_consolidated_trade_dates(min_count, date_loader=list_consolidated_trade_dates)


def resolve_execute_date(signal_date: str) -> tuple[str, bool]:
    return _primitives.resolve_execute_date(signal_date, date_loader=list_consolidated_trade_dates)


def _row_trade_price(row: Any, *, prefer_open: bool = True) -> float:
    return _primitives.row_trade_price(row, prefer_open=prefer_open)


def _open_change_pct(row: Any) -> float:
    if row is None:
        return 0.0
    open_price = float(getattr(row, "open", 0) or 0)
    previous_close = float(getattr(row, "prev_close", 0) or 0)
    if open_price <= 0 or previous_close <= 0:
        return 0.0
    return (open_price / previous_close - 1.0) * 100.0


def _execution_volume(row: Any) -> float:
    try:
        return max(0.0, float(getattr(row, "volume", 0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _defensive_execution_row(
    symbol: str,
    *,
    ts_code: str,
    name: str,
    execute_date: str,
    require_exact_date: bool,
) -> Any | None:
    """Build an ETF execution row without relabeling a stale market record."""
    from app.services.defensive_assets import defensive_asset_bar

    bar = defensive_asset_bar(ts_code, execute_date, exact_date=require_exact_date)
    if not bar:
        return None
    actual_date = str(bar.get("trade_date") or "").replace("-", "")[:8]
    target_date = str(execute_date or "").replace("-", "")[:8]
    open_price = float(bar.get("open") or 0)
    previous_close = float(bar.get("pre_close") or 0)
    volume = float(bar.get("vol") or 0)
    if require_exact_date and (
        actual_date != target_date
        or open_price <= 0
        or previous_close <= 0
        or volume <= 0
    ):
        return None
    return SimpleNamespace(
        symbol=symbol,
        name=name or symbol,
        date=actual_date,
        open=open_price,
        close=float(bar.get("close") or open_price),
        prev_close=previous_close,
        change_pct=float(bar.get("pct_chg") or 0),
        volume=volume,
    )


def _load_validated_signal_rows(
    ds: BacktestDataSource,
    signal_date: str,
    *,
    strict_no_future: bool,
) -> List[Any]:
    rows = ds.load_snapshot(signal_date.replace("-", ""))
    if not strict_no_future:
        return rows
    if not rows:
        raise ReplayRiskGateError(
            f"数据门：信号日 {signal_date} 快照缺失",
            code="paused_data_not_ready",
        )
    valid = [row for row in rows if str(getattr(row, "date", "")) <= str(signal_date)]
    if not valid:
        raise ReplayRiskGateError(
            f"未来函数防护：信号日快照全部晚于 signal_date={signal_date}",
            code="future_guard_signal_snapshot",
        )
    return valid


def _parse_risk_budget(risk_budget: Dict[str, Any] | None) -> tuple[float, float]:
    return _primitives.parse_risk_budget(risk_budget)


def _portfolio_state(
    ds: BacktestDataSource,
    valuation_date: str,
    *,
    execution_rows: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    cash = float(get_account_balance() or 0)
    positions = get_positions()
    position_value_by_symbol: Dict[str, float] = {}
    market_value = 0.0
    for pos in positions:
        sym = str(pos.get("symbol") or "").strip()
        qty = int(pos.get("quantity") or 0)
        if not sym or qty <= 0:
            continue
        px = _row_trade_price((execution_rows or {}).get(sym), prefer_open=True)
        if px <= 0:
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
    if quantity > int(pos.get("quantity") or 0):
        blocked_events.append({
            "date": execute_date,
            "symbol": sym,
            "side": "SELL",
            "code": "no_oversell",
            "message": "冻结卖出数量超过执行时实际持仓",
        })
        return False, turnover_remaining, "no_oversell"
    px = _row_trade_price(price_row, prefer_open=True)
    if px <= 0:
        return False, turnover_remaining, "price_missing"
    tradable = check_open_trade_constraints(
        side="SELL",
        symbol=sym,
        name=str(pos.get("name") or ""),
        open_price=px,
        close_price=float(getattr(price_row, "prev_close", 0) or 0) if price_row else 0.0,
        change_pct=_open_change_pct(price_row),
        volume=_execution_volume(price_row),
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
        "slippage_cost": round(max(0.0, px - exec_price) * quantity, 2),
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
    return _portfolio_prep.compute_buy_budget(
        total_equity=total_equity,
        position_market_value=position_market_value,
        cash_balance=cash_balance,
        target_position_pct=target_position_pct,
        budget_ratio=budget_ratio,
    )


def _monthly_turnover_budget(execute_date: str, total_equity: float) -> Dict[str, float]:
    return _portfolio_prep.monthly_turnover_budget(
        execute_date,
        total_equity,
        list_operations(execution_id=None, limit=200000),
    )


def _pretrade_symbol_limits(symbols: List[str], *, cutoff: str, max_single: float) -> Dict[str, float]:
    from app.services.thesis_store import list_theses
    theses = [
        item
        for item in list_theses(limit=500)
        if (not item.get("created_at") or str(item.get("created_at")) <= cutoff)
        and (not item.get("updated_at") or str(item.get("updated_at")) <= cutoff)
    ]
    return _portfolio_prep.pretrade_symbol_limits(
        symbols,
        max_single=max_single,
        theses=theses,
        is_etf=is_exchange_etf,
    )


def _selection_score_map(detail: Dict[str, Any]) -> Dict[str, float]:
    return _portfolio_prep.selection_score_map(detail)


def _strategy_portfolio_method(strategy_id: int) -> str:
    return _portfolio_prep.strategy_portfolio_method(get_strategy(int(strategy_id)))


def _base_portfolio_weights(
    returns_by_symbol: Dict[str, List[float]],
    *,
    max_single: float,
    method: str,
) -> tuple[Dict[str, float], str]:
    return _portfolio_prep.base_portfolio_weights(
        returns_by_symbol,
        max_single=max_single,
        method=method,
        constrained_cvar=constrained_cvar_weights,
        hrp_fallback=hrp_fallback_weights,
    )


def _calc_total_buy_cost(orders: List[Any]) -> float:
    return _portfolio_prep.calc_total_buy_cost(orders)


def _filled_orders(orders: List[Any]) -> List[Any]:
    return _portfolio_prep.filled_orders(orders)


def _selection_explanation(
    ranked: List[tuple[Any, float]],
    *,
    weights: Dict[str, float],
    factor_values: Dict[str, Dict[str, float]],
    top_n: int,
) -> List[Dict[str, Any]]:
    return _portfolio_prep.selection_explanation(
        ranked,
        weights=weights,
        factor_values=factor_values,
        top_n=top_n,
    )


def _build_target_symbols(
    *,
    ds: BacktestDataSource,
    strategy_id: int,
    signal_date: str,
    top_n: int,
    exchange_group: str = "HS",
    progress_callback: Optional[Callable[[str], None]] = None,
    scoring_timeout_sec: Optional[int] = None,
    explain_symbols: List[str] | None = None,
    model_inference_mode: str = "frozen_production_bundle",
) -> tuple[List[str], Dict[str, Any]]:
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy_id={strategy_id} 不存在")
    strategy_content = strategy.get("content") if isinstance(strategy.get("content"), dict) else {}
    strategy_type = strategy.get("type", "formula")
    champion_bundle = resolve_production_strategy_champion()
    frozen_factor_bindings = (
        champion_bundle.get("factor_bindings")
        if int(champion_bundle.get("strategy_id") or 0) == int(strategy_id)
        and isinstance(champion_bundle.get("factor_bindings"), dict)
        else {}
    )
    config = parse_strategy_content(strategy_content, strategy_type)
    top_n = max(1, min(int(top_n), int(config.rebalance.get("top_n") or top_n)))
    exchange_group = normalize_exchange_group(exchange_group)
    trend_analysis = analyze_trend(ds=ds, signal_date=signal_date, exchange_group=exchange_group)
    strategy_template = recommend_strategy_template(trend_analysis, requested_top_n=top_n)
    # The frozen strategy bundle is the authority for selection. Market trend
    # templates provide risk context only; overriding the stored factor formula
    # here made replayed Challengers impossible to reproduce in daily plans.
    champion_market_binding = (
        champion_bundle.get("market_state_binding")
        if int(champion_bundle.get("strategy_id") or 0) == int(strategy_id)
        and isinstance(champion_bundle.get("market_state_binding"), dict)
        else {"production_model": "transparent_four_state_v1"}
    )
    market_context = _production_market_context_as_of(
        signal_available_time(signal_date),
        market_state_binding=champion_market_binding,
    )
    market_runtime = (
        market_context.get("market_state_runtime")
        if isinstance(market_context.get("market_state_runtime"), dict) else {}
    )
    transparent_market_state = str(market_runtime.get("transparent_state") or "")
    production_market_state = str(
        ((market_context.get("market_state") or {}).get("state") or "")
        if isinstance(market_context.get("market_state"), dict) else ""
    )
    market_state = production_market_state
    score_weights = get_strategy_score_for_market_state(strategy_content, strategy_type, market_state)
    expert_route = get_strategy_expert_route(strategy_content, market_state)
    filters = list(config.filters)
    empty_detail = {
        "signal_date": signal_date,
        "exchange_group": exchange_group,
        "trend_analysis": trend_analysis,
        "strategy_template": strategy_template,
        "selected_factors": list(score_weights.keys()),
        "selected_factor_weights": {
            str(key): round(float(value or 0.0), 8)
            for key, value in score_weights.items()
        },
        "selection_market_state": market_state,
        "selection_expert_route": expert_route,
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
        transparent_state=production_market_state,
        short_benchmark_return=selection_short_return,
        long_benchmark_return=benchmark_return,
    )
    score_weights = get_strategy_score_for_market_state(strategy_content, strategy_type, market_state)
    expert_route = get_strategy_expert_route(strategy_content, market_state)
    empty_detail.update({
        "selected_factors": list(score_weights.keys()),
        "selected_factor_weights": {
            str(key): round(float(value or 0.0), 8)
            for key, value in score_weights.items()
        },
        "selection_market_state": market_state,
        "selection_expert_route": expert_route,
        "selection_production_market_state": production_market_state,
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
            binding = frozen_factor_bindings.get(f)
            if isinstance(binding, dict):
                vals[f] = compute_bound_factor(
                    f,
                    binding,
                    row,
                    hist,
                    daily_basic_map,
                    benchmark_return=benchmark_return,
                    benchmark_returns=benchmark_returns,
                )
            else:
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
    model_disabled_baseline_ranked_symbols = [
        row.symbol for row, _score in ranked[:max(top_n, min(80, top_n * 10))]
    ]
    # Theme evidence is a bounded bonus or an explicit risk veto. A stock with
    # no theme may still enter research recall through fundamentals, capital
    # confirmation and price structure.
    from app.services.theme_engine import list_themes, materialize_theme_snapshots
    materialize_theme_snapshots(as_of_time=signal_available_time(signal_date))
    theme_result = list_themes(
        as_of_time=signal_available_time(signal_date),
        promotable_only=False,
        limit=500,
    )
    if not theme_result.get("ok", True):
        raise ReplayRiskGateError(
            f"叙事准入数据不可用：{theme_result.get('reason') or theme_result.get('status') or 'unknown'}",
            code="narrative_admission_data_unavailable",
        )
    themes = theme_result.get("items") or []
    theme_mapped_codes = {
        str(company.get("ts_code") or "").split(".")[0]
        for theme in themes
        for company in (theme.get("companies") or [])
    }
    from app.services.model_signal_fusion import MODEL_KEYS, apply_model_signals
    histories = {
        str(row.symbol): ds.get_hist_window(row.symbol, signal_date, max_lookback)
        for row, _score in ranked
    }
    from app.services.plan_model_control import active_production_plan_model_bindings
    online_limited_live = str(model_inference_mode or "") == "online_limited_live"
    frozen_model_bindings = None if online_limited_live else active_production_plan_model_bindings()
    ranked, model_evidence, model_blocks, model_fusion = apply_model_signals(
        ranked=ranked,
        factor_values=factor_values,
        histories=histories,
        themes=themes,
        model_bindings=frozen_model_bindings,
        use_live_health=True,
        as_of_time=signal_available_time(signal_date),
        market_state=market_state,
    )
    model_fusion["inference_mode"] = (
        "online_limited_live" if online_limited_live else "frozen_production_bundle"
    )
    model_fusion["formal_promotion_separate_from_online_inference"] = True
    recall_n = max(top_n, min(80, top_n * 10))
    selections = _selection_explanation(
        ranked,
        weights=score_weights,
        factor_values=factor_values,
        top_n=recall_n,
    )
    explained = {str(item.get("symbol") or "") for item in selections}
    requested_explanations = {
        str(symbol or "").split(".")[0]
        for symbol in (explain_symbols or [])
        if str(symbol or "").strip()
    }
    additional_ranked = [
        item for item in ranked
        if str(item[0].symbol) in requested_explanations
        and str(item[0].symbol) not in explained
    ]
    if additional_ranked:
        selections.extend(_selection_explanation(
            additional_ranked,
            weights=score_weights,
            factor_values=factor_values,
            top_n=len(additional_ranked),
        ))
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
        "model_disabled_baseline": {
            "schema_version": "alis_daily_model_disabled_baseline_v1",
            "ranked_symbols": model_disabled_baseline_ranked_symbols,
            "model_weights_zeroed": True,
            "purpose": "daily_virtual_ab_against_online_model_scored_selection",
        },
        "narrative_recall": {
            "method": "full_market_theme_bonus_and_risk_veto_v2",
            "themes": [
                {
                    "theme": item.get("theme_name"),
                    "score": item.get("score"),
                    "lifecycle": item.get("lifecycle"),
                    "promotable": bool(item.get("promotable")),
                }
                for item in themes
            ],
            "mapped_symbols": len(theme_mapped_codes),
            "unmapped_stock_allowed": True,
            "status": "ready" if themes else "no_theme_evidence_full_market_allowed",
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
    if strict_no_future and hasattr(ds, "set_as_of_time"):
        ds.set_as_of_time(signal_available_time(signal_date))
    portfolio_method = ""
    exchange_group = normalize_exchange_group(exchange_group)
    if strict_no_future and str(signal_date) >= str(execute_date):
        raise ReplayRiskGateError(
            f"未来函数防护：signal_date({signal_date}) 必须早于 execute_date({execute_date})",
            code="future_guard_date_order",
        )
    _require_consistent_simulation_ledger()
    execution_guard = champion_execution_gate()
    champion_quarantined = str(execution_guard.get("status") or "") == "quarantined"
    defensive_continuity = (
        execution_guard.get("defensive_continuity")
        if isinstance(execution_guard.get("defensive_continuity"), dict)
        else {}
    )
    _report("正在加载快照")
    frozen_unified_plan = False
    frozen_controller: Dict[str, Any] = {}
    frozen_decision_contract: Dict[str, Any] = {}
    frozen_exact_quantity_orders = False
    frozen_order_by_symbol: Dict[str, Dict[str, Any]] = {}
    if authoritative_plan:
        authoritative_plan = normalize_authoritative_plan(authoritative_plan)
        frozen_unified_plan = bool(authoritative_plan.get("is_unified_frozen_plan"))
        frozen_controller = (
            authoritative_plan.get("pretrade_controller")
            if isinstance(authoritative_plan.get("pretrade_controller"), dict) else {}
        )
        frozen_decision_contract = (
            authoritative_plan.get("decision_contract")
            if isinstance(authoritative_plan.get("decision_contract"), dict) else {}
        )
        frozen_exact_quantity_orders = (
            str(frozen_decision_contract.get("schema_version") or "")
            == "alis_unified_daily_decision_v2"
        )
        if frozen_exact_quantity_orders:
            frozen_order_by_symbol = {
                str(item.get("symbol") or ""): dict(item)
                for item in (frozen_decision_contract.get("execution_orders") or [])
                if isinstance(item, dict) and str(item.get("symbol") or "")
            }
        if frozen_unified_plan:
            selection_snapshot = (
                frozen_decision_contract.get("selection_snapshot")
                if isinstance(frozen_decision_contract.get("selection_snapshot"), dict) else {}
            )
            frozen_strategy_id = int(selection_snapshot.get("strategy_id") or 0)
            if int(strategy_id) != frozen_strategy_id:
                raise ReplayRiskGateError(
                    "统一正式计划与执行任务的策略身份不一致",
                    code="invalid_authoritative_plan",
                )
            if (
                str(frozen_decision_contract.get("signal_date") or "").replace("-", "")[:8]
                != str(signal_date).replace("-", "")[:8]
                or str(frozen_decision_contract.get("planned_execute_date") or "").replace("-", "")[:8]
                != str(execute_date).replace("-", "")[:8]
            ):
                raise ReplayRiskGateError(
                    "统一正式计划的信号日或执行日与任务不一致",
                    code="invalid_authoritative_plan",
                )
        target_symbols = list(authoritative_plan["target_symbols"])
        selection_detail = build_authoritative_selection_detail(
            authoritative_plan,
            signal_date=signal_date,
            exchange_group=exchange_group,
        )
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
    if not frozen_unified_plan:
        portfolio_method = _strategy_portfolio_method(strategy_id)
    if frozen_unified_plan:
        research_blocks = []
        dynamic_blocks = []
        industry_allocation = dict(frozen_decision_contract.get("industry_allocation") or {})
        expected_contract = frozen_decision_contract.get("expected_return_contract")
        market_state = str((expected_contract or {}).get("market_state") or "unknown")
        controller_actions = [
            item for item in (frozen_controller.get("actions") or []) if isinstance(item, dict)
        ]
        defensive_symbols = {
            str(item.get("symbol") or "")
            for item in controller_actions if item.get("is_defensive_asset")
        }
        defensive_weights = {
            symbol: float((authoritative_plan.get("target_weights") or {}).get(symbol) or 0.0)
            for symbol in defensive_symbols
            if float((authoritative_plan.get("target_weights") or {}).get(symbol) or 0.0) > 0
        }
        equity_target_symbols = [symbol for symbol in target_symbols if symbol not in defensive_symbols]
        allocated_equity_budget = sum(
            float((authoritative_plan.get("target_weights") or {}).get(symbol) or 0.0)
            for symbol in equity_target_symbols
        )
        dynamic_gate = {
            "schema_version": "frozen_unified_execution_gate_v1",
            "risk_budget_multiplier": allocated_equity_budget,
            "market_context": {"market_state": {"state": market_state}},
            "defensive_allocation": {
                "status": "frozen",
                "target_weights": defensive_weights,
                "target_weight": round(sum(defensive_weights.values()), 8),
            },
            "investment_opinion_source": "t_day_frozen_contract",
        }
        defensive_policy = dynamic_gate["defensive_allocation"]
        plan_guard = frozen_controller.get("champion_execution_guard")
        plan_quarantined = str((plan_guard or {}).get("mode") or "") == "research_only_sell_reduce"
        if champion_quarantined and not plan_quarantined:
            dynamic_blocks.append({
                "date": execute_date,
                "side": "BUY",
                "code": "overnight_champion_restriction",
                "message": "T+1 当前 Champion 隔离门比冻结计划更严格；只收紧个股买入/加仓权限，不重做投资判断",
                "execution_guard": execution_guard,
            })
        score_map = {
            str(item.get("symbol") or ""): float(item.get("expected_20d_return") or 0.0) * 100.0
            for item in (authoritative_plan.get("proposal") or [])
            if str(item.get("symbol") or "")
        }
    else:
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
                "message": "当前 Champion 正式 OOS 失败；已隔离个股新开仓和加仓，仅允许受限防御 ETF、减仓、卖出与账本审计",
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
    if champion_quarantined and not frozen_unified_plan:
        if bool(defensive_continuity.get("enabled")):
            maximum = max(
                0.0,
                min(
                    float(defensive_continuity.get("maximum_total_weight") or 0.0),
                    0.30,
                ),
            )
            total = sum(max(0.0, weight) for weight in defensive_weights.values())
            scale = (maximum / total) if total > maximum > 0 else 1.0
            defensive_weights = {
                symbol: max(0.0, weight) * scale
                for symbol, weight in defensive_weights.items()
                if weight > 0
            } if maximum > 0 else {}
            dynamic_gate["defensive_continuity"] = {
                **defensive_continuity,
                "applied_total_weight": round(sum(defensive_weights.values()), 8),
            }
        else:
            defensive_weights = {}
    high_risk_symbols = set() if frozen_unified_plan else {
        str(item.get("symbol") or "") for item in (dynamic_gate.get("items") or [])
        if str((item.get("risk") or {}).get("risk_level") or "") == "high"
    }
    target_symbols = list(dict.fromkeys(equity_target_symbols + list(defensive_weights)))
    selection_detail["research_admission"] = {"admitted": equity_target_symbols, "blocked": research_blocks}
    selection_detail["dynamic_strategy_gate"] = dynamic_gate
    selection_detail["industry_allocation"] = industry_allocation
    blocked_events: List[Dict[str, Any]] = list(selection_detail.get("blocked_events") or []) + research_blocks + dynamic_blocks
    sell_triggers: List[Dict[str, Any]] = []
    if frozen_unified_plan:
        hold_days = None
    else:
        strategy = get_strategy(strategy_id)
        config = parse_strategy_content(strategy.get("content"), strategy.get("type", "formula")) if strategy else None
        hold_days = int(config.hold_days) if config and config.hold_days else None
        if champion_quarantined:
            hold_days = None
    signal_rows = _load_validated_signal_rows(
        ds,
        signal_date,
        strict_no_future=strict_no_future,
    )
    # T+1 开盘行情只用于成交观察；风险历史仍显式截止到 signal_date。
    if hasattr(ds, "set_as_of_time"):
        ds.set_as_of_time("")
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
    frozen_action_by_symbol = {
        str(item.get("symbol") or ""): item
        for item in (frozen_controller.get("actions") or [])
        if isinstance(item, dict) and str(item.get("symbol") or "")
    }
    if defensive_weights or frozen_unified_plan:
        from app.services.defensive_assets import DEFENSIVE_ASSETS

        defensive_meta = {item["ts_code"].split(".")[0]: item for item in DEFENSIVE_ASSETS}
        execution_defensive_symbols = set(defensive_weights)
        if frozen_unified_plan:
            execution_defensive_symbols.update(
                symbol for symbol in frozen_action_by_symbol if symbol in defensive_meta
            )
            execution_defensive_symbols.update(
                str(position.get("symbol") or "").split(".")[0]
                for position in get_positions()
                if str(position.get("symbol") or "").split(".")[0] in defensive_meta
                and int(position.get("quantity") or 0) > 0
            )
        missing_defensive_execution_rows: list[str] = []
        for symbol in sorted(execution_defensive_symbols):
            meta = defensive_meta.get(symbol) or {}
            row = _defensive_execution_row(
                symbol,
                ts_code=str(meta.get("ts_code") or _ts_code(symbol)),
                name=str(meta.get("name") or symbol),
                execute_date=execute_date,
                require_exact_date=frozen_unified_plan,
            )
            if row is None:
                missing_defensive_execution_rows.append(symbol)
                action = str((frozen_action_by_symbol.get(symbol) or {}).get("action") or "")
                blocked_events.append({
                    "date": execute_date,
                    "symbol": symbol,
                    "side": "SELL" if action in {"reduce_position", "sell_exit"} else "BUY",
                    "code": "defensive_asset_price_missing",
                    "message": "防御ETF执行日精确行情缺失" if frozen_unified_plan else "防御ETF执行日行情缺失",
                })
                continue
            exec_map[symbol] = row
        if frozen_unified_plan and missing_defensive_execution_rows:
            raise ReplayRiskGateError(
                "冻结计划所需防御ETF缺少执行日精确行情：" + ",".join(missing_defensive_execution_rows),
                code="paused_frozen_execution_data_not_ready",
            )
    executed_at = f"{execute_date}T09:30:00"
    pretrade_portfolio = _portfolio_state(ds, execute_date, execution_rows=exec_map)
    if frozen_unified_plan:
        # Turnover was already priced into the T-day method competition.  The
        # legacy monthly cap is an investment-policy decision, not a T+1 trade
        # safety veto, so it cannot reshape an immutable unified plan.
        turnover_remaining = max(0.0, float(pretrade_portfolio["total_equity"])) * 2.0
        turnover_budget = {
            "enforced": False,
            "policy": "t_day_frozen_plan_no_t1_turnover_redecision",
            "limit": None,
            "used_before": None,
            "remaining": round(turnover_remaining, 2),
        }
    else:
        turnover_budget = _monthly_turnover_budget(execute_date, pretrade_portfolio["total_equity"])
        turnover_remaining = float(turnover_budget["remaining"])
    if frozen_unified_plan:
        thesis_status_by_symbol = {}
    else:
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
        frozen_sell_order = frozen_order_by_symbol.get(sym) or {}
        if frozen_exact_quantity_orders:
            if str(frozen_sell_order.get("side") or "").upper() != "SELL":
                continue
        elif not sym or sym in target_symbols:
            continue
        if not frozen_exact_quantity_orders and hold_days and hold_days > 0:
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
            quantity=(
                int(frozen_sell_order.get("planned_quantity") or 0)
                if frozen_exact_quantity_orders else int(pos.get("quantity") or 0)
            ),
            price_row=exec_map.get(sym),
            execute_date=execute_date,
            executed_at=executed_at,
            reason="T日冻结数量卖出" if frozen_exact_quantity_orders else "调仓卖出",
            detail=(
                "T+1仅按冻结数量执行，未重新计算目标"
                if frozen_exact_quantity_orders else "不在目标池且满足持有期/T+1"
            ),
            turnover_remaining=turnover_remaining,
            hard_risk_exit=(
                str((frozen_action_by_symbol.get(sym) or {}).get("action") or "") == "sell_exit"
                if frozen_unified_plan else thesis_status_by_symbol.get(sym) in {"invalidated", "failed"}
            ),
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
        if frozen_exact_quantity_orders and sell_triggers:
            frozen_cost = frozen_sell_order.get("cost") if isinstance(frozen_sell_order.get("cost"), dict) else {}
            sell_triggers[-1].update({
                "planned_quantity": int(frozen_sell_order.get("planned_quantity") or 0),
                "planned_reference_close": frozen_sell_order.get("reference_close"),
                "planned_cost": frozen_cost,
                "actual_cost": {
                    key: sold_ops_payload[-1].get(key)
                    for key in ("commission", "transfer_fee", "stamp_tax", "slippage_bps")
                } if sold_ops_payload else {},
            })

    # 再买入（等权资金，按总权益目标仓位与单票上限）
    held_after_sell = {str(p.get("symbol") or "").strip() for p in get_positions()}
    # Existing targets may still be below today's optimizer weight. Include
    # them so idle cash is deployed instead of treating every held name as a
    # permanent no-op.
    if frozen_unified_plan:
        if frozen_exact_quantity_orders:
            buy_list = [
                symbol for symbol, order in frozen_order_by_symbol.items()
                if str(order.get("side") or "").upper() == "BUY"
                and int(order.get("planned_quantity") or 0) > 0
                and not (
                    champion_quarantined
                    and not bool((frozen_action_by_symbol.get(symbol) or {}).get("is_defensive_asset"))
                )
            ]
        else:
            buy_list = [
                symbol for symbol in target_symbols
                if (
                    str((frozen_action_by_symbol.get(symbol) or {}).get("action") or "")
                    in {"buy_candidate", "add_position", "defensive_switch"}
                    and bool((frozen_action_by_symbol.get(symbol) or {}).get("permission_allowed", True))
                    and not (
                        champion_quarantined
                        and not bool((frozen_action_by_symbol.get(symbol) or {}).get("is_defensive_asset"))
                    )
                )
            ]
    else:
        buy_list = list(target_symbols)
    risk_budget = selection_detail.get("risk_budget") if isinstance(selection_detail.get("risk_budget"), dict) else {}
    _strategy_target_position_pct, max_single_position_pct = _parse_risk_budget(risk_budget)
    target_position_pct = 1.0
    portfolio = _portfolio_state(ds, execute_date, execution_rows=exec_map)
    defensive_target_weight = sum(defensive_weights.values())
    effective_budget_ratio = (
        1.0
        if frozen_unified_plan
        else max(0.0, min(1.0, float(budget_ratio)))
        * min(1.0, float(dynamic_gate.get("risk_budget_multiplier") or 0) + defensive_target_weight)
    )
    available_balance = _compute_buy_budget(
        total_equity=portfolio["total_equity"],
        position_market_value=portfolio["position_market_value"],
        cash_balance=portfolio["cash_balance"],
        target_position_pct=target_position_pct,
        budget_ratio=effective_budget_ratio,
    )
    residual_cash_limit = min(portfolio["total_equity"] * .005, 500.0)
    full_investment_gap = portfolio["cash_balance"] if portfolio["cash_balance"] > residual_cash_limit else 0.0
    if not frozen_unified_plan:
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
            if frozen_exact_quantity_orders:
                blocked_events.append({
                    "date": execute_date,
                    "symbol": sym,
                    "side": "BUY",
                    "code": "missing_open_price",
                    "message": "执行日开盘价缺失，冻结买单被安全否决",
                })
            continue
        if sym in defensive_weights:
            from app.services.defensive_assets import defensive_asset_returns
            returns = defensive_asset_returns(next(code for code in (defensive_policy.get("target_weights") or {}) if str(code).split(".")[0] == sym), signal_date)
            average_amount_thousand = float((defensive_asset_bar(next(code for code in (defensive_policy.get("target_weights") or {}) if str(code).split(".")[0] == sym), signal_date) or {}).get("amount") or 0)
        else:
            hist = ds.get_hist_window(sym, signal_date, 60)
            closes = [float(getattr(x, "close", 0) or 0) for x in hist]
            returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
            amounts = [float(getattr(item, "turnover", 0) or 0) for item in hist[-20:] if float(getattr(item, "turnover", 0) or 0) > 0]
            average_amount_thousand = sum(amounts) / len(amounts) if amounts else 0.0
        if average_amount_thousand > 0:
            liquidity_caps[sym] = average_amount_thousand * 1000.0 * .01
        returns_by_symbol[sym] = returns
        if not frozen_unified_plan:
            score_map[sym] = float(score_map.get(sym, 0.0)) * cvar_position_multiplier(returns)
        order_requests.append(
            OrderRequest(
                symbol=sym,
                side="BUY",
                price=px,
                quantity=0,
                name=str(getattr(px_row, "name", "") or narrative_name_by_symbol.get(sym) or ""),
                change_pct=_open_change_pct(px_row),
                volume=_execution_volume(px_row),
                close=float(getattr(px_row, "prev_close", 0) or 0),
            )
        )

    if frozen_unified_plan:
        covariance_snapshot = {"status": "not_recomputed", "source": "t_day_frozen_plan"}
        bl_result = {"status": "not_recomputed", "source": "t_day_frozen_plan"}
        marginal_cvar = {"status": "frozen", "source": "t_day_portfolio_method_competition"}
        adaptive_marginal_limit = .12
        planned_weights = {
            str(symbol): float(weight)
            for symbol, weight in (authoritative_plan.get("target_weights") or {}).items()
            if str(symbol) in target_symbols and float(weight) > 0
        }
        cvar_weights = planned_weights
        optimizer_name = "authoritative_unified_daily_plan_weights_v1"
    else:
        # Legacy and research replay retain their compatibility optimization.
        covariance_snapshot = ledoit_wolf_covariance(returns_by_symbol)
        cvar_weights, optimizer_name = _base_portfolio_weights(
            returns_by_symbol,
            max_single=max_single_position_pct,
            method=portfolio_method,
        )
        bl_result: Dict[str, Any] = {}
        if cvar_weights and portfolio_method == "cvar":
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
                str(symbol): float(weight)
                for symbol, weight in (authoritative_plan.get("target_weights") or {}).items()
                if str(symbol) in target_symbols and float(weight) > 0
            }
            cvar_weights = planned_weights
            optimizer_name = "authoritative_legacy_daily_plan_weights_v1"
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
    for pos in ([] if frozen_exact_quantity_orders else list(get_positions())):
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
            str((frozen_action_by_symbol.get(sym) or {}).get("action") or "") == "sell_exit"
            if frozen_unified_plan
            else (
                thesis_status_by_symbol.get(sym) in {"invalidated", "failed"}
                or current_weight > max_single_position_pct + 1e-6
                or current_weight > float(cvar_weights.get(sym) or 0) + 0.03
            )
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
        portfolio = _portfolio_state(ds, execute_date, execution_rows=exec_map)
        available_balance = _compute_buy_budget(
            total_equity=portfolio["total_equity"],
            position_market_value=portfolio["position_market_value"],
            cash_balance=portfolio["cash_balance"],
            target_position_pct=target_position_pct,
            budget_ratio=effective_budget_ratio,
        )
        if not frozen_unified_plan:
            turnover_capacity = max(turnover_remaining, full_investment_gap) if defensive_weights else turnover_remaining
            available_balance = min(available_balance, turnover_capacity)

    _report("正在生成买卖单")
    if frozen_unified_plan:
        frozen_industries = industry_allocation.get("symbol_industries") if isinstance(industry_allocation.get("symbol_industries"), dict) else {}
        industry_by_symbol = {
            str(symbol).split(".")[0]: str(frozen_industries.get(str(symbol).split(".")[0]) or "frozen_unclassified")
            for symbol in list(portfolio["position_value_by_symbol"]) + [str(order.symbol) for order in order_requests]
        }
        existing_industry_value: Dict[str, float] = {}
        for symbol, value in portfolio["position_value_by_symbol"].items():
            industry = industry_by_symbol.get(str(symbol).split(".")[0], "frozen_unclassified")
            existing_industry_value[industry] = existing_industry_value.get(industry, 0.0) + float(value or 0)
    else:
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
    if frozen_unified_plan:
        frozen_bounds = frozen_controller.get("weight_bounds") if isinstance(frozen_controller.get("weight_bounds"), dict) else {}
        position_limits = {
            str(order.symbol): max(
                0.0,
                float((frozen_bounds.get(str(order.symbol)) or {}).get("max_weight") or 0.0),
            )
            for order in order_requests
        }
    else:
        position_limits = _pretrade_symbol_limits(
            [str(order.symbol) for order in order_requests], cutoff=signal_available_time(signal_date), max_single=max_single_position_pct,
        )
        for symbol in defensive_weights:
            position_limits[symbol] = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT
    defensive_only_target = bool(cvar_weights) and not any(symbol not in defensive_weights for symbol in cvar_weights)
    residual_sweep_symbol = None
    defensive_prices = {
        str(order.symbol): float(order.price or 0)
        for order in order_requests if str(order.symbol) in defensive_weights and float(order.price or 0) > 0
    }
    if defensive_prices and not frozen_unified_plan:
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
        max_industry_position_pct=DEFAULT_INDUSTRY_LIMIT,
        max_correlated_industry_pct=DEFAULT_CORRELATED_INDUSTRY_LIMIT,
        target_weights=cvar_weights,
        symbol_position_limits=position_limits,
        high_risk_symbols=high_risk_symbols,
        max_order_value_by_symbol=liquidity_caps,
        initial_entry_fraction=1.0,
        residual_sweep_symbol=residual_sweep_symbol,
        fixed_quantities=(
            {
                symbol: int(order.get("planned_quantity") or 0)
                for symbol, order in frozen_order_by_symbol.items()
                if str(order.get("side") or "").upper() == "BUY"
            }
            if frozen_exact_quantity_orders else None
        ),
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
            "slippage_cost": round(
                max(0.0, float(o.price) - req_price_map[str(o.symbol)]) * int(o.quantity), 2
            ) if req_price_map.get(str(o.symbol)) else 0.0,
            "fee_schedule_version": ETF_FEE_SCHEDULE_VERSION if is_exchange_etf(o.symbol) else FEE_SCHEDULE_VERSION,
            "planned_quantity": int((frozen_order_by_symbol.get(str(o.symbol)) or {}).get("planned_quantity") or 0)
            if frozen_exact_quantity_orders else None,
            "planned_reference_close": (frozen_order_by_symbol.get(str(o.symbol)) or {}).get("reference_close")
            if frozen_exact_quantity_orders else None,
            "planned_cost": (frozen_order_by_symbol.get(str(o.symbol)) or {}).get("cost")
            if frozen_exact_quantity_orders else None,
        }
        for o in result.orders
        if int(getattr(o, "quantity", 0) or 0) > 0
    ]
    execution_id: int | None = None
    frozen_plan_actionable = (
        bool(frozen_order_by_symbol)
        if frozen_exact_quantity_orders else any(
        abs(float(item.get("weight_change") or 0.0)) > 0.002
        and (
            str((frozen_action_by_symbol.get(str(item.get("symbol") or "")) or {}).get("action") or "")
            in {"sell_exit", "reduce_position"}
            or (
                str((frozen_action_by_symbol.get(str(item.get("symbol") or "")) or {}).get("action") or "")
                in {"buy_candidate", "add_position", "defensive_switch"}
                and bool((frozen_action_by_symbol.get(str(item.get("symbol") or "")) or {}).get("permission_allowed", True))
            )
        )
        for item in ((authoritative_plan or {}).get("proposal") or [])
        if isinstance(item, dict)
        )
    ) if frozen_unified_plan else False
    if sold_count == 0 and not filled and (not frozen_unified_plan or frozen_plan_actionable):
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
            "max_industry_position_pct": DEFAULT_INDUSTRY_LIMIT,
            "max_correlated_industry_pct": DEFAULT_CORRELATED_INDUSTRY_LIMIT,
            "max_marginal_cvar_contribution": round(adaptive_marginal_limit, 4),
            "monthly_turnover_remaining": round(turnover_remaining, 2),
        },
    )
    if frozen_unified_plan:
        frozen_actions = [
            dict(item) for item in (frozen_controller.get("actions") or []) if isinstance(item, dict)
        ]
        frozen_trace = [
            {"step": "T日Controller", "status": "frozen", "detail": "执行器只消费冻结动作与权重边界"},
            {"step": "T+1执行安全", "status": "checked", "detail": "仅应用价格、停牌涨跌停、整手、流动性、现金、持仓和账本安全否决"},
        ]
        strategy_controller = {
            **frozen_controller,
            "contracts": frozen_actions,
            "trace": frozen_trace,
            "execution_outcomes": {
                "buy": buy_actions,
                "sell": sell_triggers,
                "blocked": blocked_events,
            },
            "regenerated_on_t1": False,
        }
        final_strategy_decision = {
            "buy": buy_actions,
            "sell": sell_triggers,
            "hold": sorted(list(held_after_sell.intersection(set(target_symbols)))),
            "blocked": blocked_events,
            "strategy_controller": strategy_controller,
            "strategy_action_contracts": frozen_actions,
            "rebalance_decision_trace": frozen_trace,
        }
    else:
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
            "hard_constraints": {
                "max_single_position_pct": max_single_position_pct,
                "max_industry_position_pct": DEFAULT_INDUSTRY_LIMIT,
                "max_correlated_industry_pct": DEFAULT_CORRELATED_INDUSTRY_LIMIT,
                "max_marginal_cvar_contribution": round(adaptive_marginal_limit, 4),
                "t1_portfolio_redecision_disabled": frozen_unified_plan,
            },
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
    cash_fees = round(sum(
        sum(float(order.get(key, 0) or 0) for key in ("commission", "transfer_fee", "stamp_tax"))
        for order in (orders_payload + sold_ops_payload)
    ), 2)
    realized_slippage_cost = round(sum(
        float(order.get("slippage_cost") or 0.0)
        for order in (orders_payload + sold_ops_payload)
    ), 2)
    transaction_cost = round(cash_fees + realized_slippage_cost, 2)
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
        "simulated_trades": {
            "buys": buy_actions,
            "sells": sell_triggers,
            "cash_fees": cash_fees,
            "slippage_cost": realized_slippage_cost,
            "transaction_cost": transaction_cost,
            "cost_policy": "cash fees are deducted once; slippage is realized through execution price and is not deducted twice",
        },
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
    portfolio_method = _strategy_portfolio_method(sid)
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
                "max_single_position_pct": max_single_position_pct if max_single_position_pct is not None else DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
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
                close_price=float(getattr(px_row, "prev_close", 0) or 0) if px_row else 0.0,
                change_pct=_open_change_pct(px_row),
                volume=1.0 if px > 0 else 0.0,
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

    portfolio = _portfolio_state(ds, execute_date, execution_rows=exec_map)
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
        "max_single_position_pct": min(max_single_position_pct if max_single_position_pct is not None else DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT, DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT),
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
    target_weights, plan_optimizer = _base_portfolio_weights(
        plan_returns,
        max_single=max_single,
        method=portfolio_method,
    )
    plan_bl: Dict[str, Any] = {}
    if target_weights and portfolio_method == "cvar":
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
        max_industry_position_pct=DEFAULT_INDUSTRY_LIMIT,
        max_correlated_industry_pct=DEFAULT_CORRELATED_INDUSTRY_LIMIT,
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
            "max_industry_position_pct": DEFAULT_INDUSTRY_LIMIT,
            "max_correlated_industry_pct": DEFAULT_CORRELATED_INDUSTRY_LIMIT,
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
            "hard_constraints": {"max_single_position_pct": max_single, "max_industry_position_pct": DEFAULT_INDUSTRY_LIMIT, "max_correlated_industry_pct": DEFAULT_CORRELATED_INDUSTRY_LIMIT},
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
