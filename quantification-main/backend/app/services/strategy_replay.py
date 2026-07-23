from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from app.core.a_share_fees import (
    apply_slippage_buy,
    apply_slippage_sell,
    commission,
    stamp_tax,
    transfer_fee,
)
from app.core.exchange_groups import matches_exchange_group, normalize_exchange_group
from app.core.trading_constraints import check_open_trade_constraints, normalize_lot_quantity
from app.db.store import get_model_version, get_strategy
from app.services.backtest_data_source import BacktestDataSource, HistBar, SnapshotRow
from app.services.backtest_engine import _apply_filters, _factor_value, _score_rows
from app.services.backtest_metrics import build_metrics
from app.services.strategy_config import parse_strategy_content
from app.services.strategy_controller import build_strategy_rebalance_actions
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.point_in_time import point_in_time_contract, signal_available_time
from app.services.two_group_optimizer import two_group_cvar_optimizer
from app.services.defensive_assets import DEFENSIVE_ASSETS, defensive_allocation_for_state
from app.services.market_context import _transparent_regime, score_market_sentiment


@dataclass
class ReplayPosition:
    symbol: str
    quantity: int
    avg_cost: float
    buy_date: str
    last_close: float
    name: str = ""


def _returns(bars: list[HistBar], limit: int = 60) -> list[float]:
    closes = [float(row.close) for row in bars[-max(2, limit + 1):] if float(row.close) > 0]
    return [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes)) if closes[index - 1] > 0]


def _apply_challenger_controls(
    target_weights: dict[str, float],
    current_weights: dict[str, float],
    controls: dict[str, Any],
) -> dict[str, float]:
    """Apply final-weight and turnover controls after the optimizer."""
    weights = {str(k): max(0.0, float(v or 0.0)) for k, v in target_weights.items()}
    max_single = max(0.01, min(1.0, float(controls.get("max_single_weight") or 1.0)))
    for _ in range(len(weights) + 1):
        excess = sum(max(0.0, value - max_single) for value in weights.values())
        weights = {symbol: min(value, max_single) for symbol, value in weights.items()}
        recipients = [symbol for symbol, value in weights.items() if value < max_single - 1e-12]
        if excess <= 1e-12 or not recipients:
            break
        room = sum(max_single - weights[symbol] for symbol in recipients)
        if room <= 1e-12:
            break
        for symbol in recipients:
            weights[symbol] += excess * (max_single - weights[symbol]) / room

    max_turnover = max(0.0, float(controls.get("max_rebalance_turnover") or 0.0))
    initial_deployment_exempt = bool(controls.get("initial_deployment_turnover_exempt")) and not current_weights
    if max_turnover > 0 and not initial_deployment_exempt:
        symbols = set(weights) | set(current_weights)
        turnover = sum(abs(weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0)) for symbol in symbols)
        if turnover > max_turnover:
            blend = max_turnover / turnover
            weights = {
                symbol: current_weights.get(symbol, 0.0) + blend * (weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0))
                for symbol in symbols
            }
    min_change = max(0.0, float(controls.get("min_weight_change") or 0.0))
    for symbol in set(weights) | set(current_weights):
        if abs(weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0)) < min_change:
            weights[symbol] = current_weights.get(symbol, 0.0)
    return {symbol: round(weight, 8) for symbol, weight in weights.items() if weight > 0.0001}


def _weights(positions: dict[str, ReplayPosition], prices: dict[str, float], cash: float) -> dict[str, float]:
    values = {
        symbol: position.quantity * max(float(prices.get(symbol) or position.last_close), 0.0)
        for symbol, position in positions.items()
    }
    total = cash + sum(values.values())
    if total <= 0:
        return {}
    return {symbol: value / total for symbol, value in values.items()}


def _position_payload(positions: dict[str, ReplayPosition]) -> dict[str, dict[str, Any]]:
    return {
        symbol: {
            "symbol": symbol,
            "name": position.name,
            "quantity": position.quantity,
            "cost_price": position.avg_cost,
            "buy_date": position.buy_date,
        }
        for symbol, position in positions.items()
    }


def _execute_target(
    *,
    date: str,
    target_weights: dict[str, float],
    positions: dict[str, ReplayPosition],
    cash: float,
    rows: dict[str, SnapshotRow],
    trades: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
) -> float:
    open_prices = {symbol: float(row.open or 0.0) for symbol, row in rows.items()}
    equity = cash + sum(
        position.quantity * max(open_prices.get(symbol, 0.0), position.last_close)
        for symbol, position in positions.items()
    )
    if equity <= 0:
        return cash

    # Sell first so purchases never rely on cash that does not exist.
    for symbol in sorted(set(positions) | set(target_weights)):
        position = positions.get(symbol)
        if position is None:
            continue
        row = rows.get(symbol)
        if row is None or row.open <= 0:
            blocked.append({"date": date, "symbol": symbol, "side": "SELL", "code": "missing_open_row", "message": "缺少次日开盘行情"})
            continue
        target_value = equity * max(0.0, float(target_weights.get(symbol) or 0.0))
        current_value = position.quantity * row.open
        quantity = normalize_lot_quantity(int(max(0.0, current_value - target_value) / row.open))
        quantity = min(quantity, position.quantity)
        if quantity <= 0:
            continue
        gate = check_open_trade_constraints(side="SELL", symbol=symbol, name=row.name, open_price=row.open, close_price=row.close, change_pct=row.change_pct, volume=row.volume)
        if not gate.ok:
            blocked.append({"date": date, "symbol": symbol, "side": "SELL", "code": gate.code, "message": gate.message})
            continue
        price = apply_slippage_sell(row.open, symbol)
        gross = price * quantity
        fees = round(commission(gross) + transfer_fee(gross) + stamp_tax(gross), 2)
        cash += gross - fees
        pnl = gross - fees - position.avg_cost * quantity
        position.quantity -= quantity
        trades.append({"date": date, "symbol": symbol, "side": "SELL", "price": round(price, 4), "quantity": quantity, "gross_amount": round(gross, 2), "fees": fees, "pnl": round(pnl, 2), "reason": "strategy_replay_target_delta"})
        if position.quantity <= 0:
            del positions[symbol]

    for symbol, target_weight in sorted(target_weights.items(), key=lambda item: (-float(item[1]), item[0])):
        row = rows.get(symbol)
        if row is None or row.open <= 0:
            blocked.append({"date": date, "symbol": symbol, "side": "BUY", "code": "missing_open_row", "message": "缺少次日开盘行情"})
            continue
        position = positions.get(symbol)
        current_quantity = position.quantity if position else 0
        current_value = current_quantity * row.open
        target_value = equity * max(0.0, float(target_weight or 0.0))
        quantity = normalize_lot_quantity(int(max(0.0, target_value - current_value) / row.open))
        if quantity <= 0:
            continue
        gate = check_open_trade_constraints(side="BUY", symbol=symbol, name=row.name, open_price=row.open, close_price=row.close, change_pct=row.change_pct, volume=row.volume)
        if not gate.ok:
            blocked.append({"date": date, "symbol": symbol, "side": "BUY", "code": gate.code, "message": gate.message})
            continue
        price = apply_slippage_buy(row.open, symbol)
        gross = price * quantity
        fees = round(commission(gross) + transfer_fee(gross), 2)
        total_cost = gross + fees
        if total_cost > cash:
            affordable = normalize_lot_quantity(int(max(0.0, cash - commission(max(cash, 0.0))) / price))
            quantity = min(quantity, affordable)
            gross = price * quantity
            fees = round(commission(gross) + transfer_fee(gross), 2) if quantity > 0 else 0.0
            total_cost = gross + fees
        if quantity <= 0 or total_cost > cash:
            blocked.append({"date": date, "symbol": symbol, "side": "BUY", "code": "insufficient_cash", "message": "现金不足以买入一手"})
            continue
        cash -= total_cost
        previous_cost = position.avg_cost * position.quantity if position else 0.0
        new_quantity = current_quantity + quantity
        positions[symbol] = ReplayPosition(symbol=symbol, quantity=new_quantity, avg_cost=(previous_cost + total_cost) / new_quantity, buy_date=position.buy_date if position else date, last_close=row.close, name=row.name)
        trades.append({"date": date, "symbol": symbol, "side": "BUY", "price": round(price, 4), "quantity": quantity, "gross_amount": round(gross, 2), "fees": fees, "pnl": None, "reason": "strategy_replay_target_delta"})
    return cash


def run_strategy_replay(
    *,
    strategy_id: int | None,
    start_date: str,
    end_date: str,
    initial_cash: float = 200_000.0,
    rebalance_freq: str | None = None,
    benchmark_symbol: str = "000300.SH",
    exchange_group: str = "HS",
    model_version_id: int | None = None,
    data_source: BacktestDataSource | None = None,
) -> dict[str, Any]:
    champion = resolve_production_strategy_champion()
    strategy_id = int(strategy_id or champion["strategy_id"])
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy_id={strategy_id} 不存在")
    model_version = get_model_version(int(model_version_id)) if model_version_id else None
    raw_content = strategy.get("content") if isinstance(strategy.get("content"), dict) else {}
    controls = raw_content.get("challenger_controls") if isinstance(raw_content.get("challenger_controls"), dict) else {}
    exposure_policy = raw_content.get("dynamic_market_exposure") if isinstance(raw_content.get("dynamic_market_exposure"), dict) else {}
    allocation_method = str(raw_content.get("portfolio_method") or "cvar").strip().lower()
    config = parse_strategy_content(strategy.get("content"), strategy.get("type", "formula"))
    frequency = rebalance_freq or str(config.rebalance.get("freq", "daily"))
    top_n = int(((model_version or {}).get("config") or {}).get("top_n") or config.rebalance.get("top_n", 20))
    exchange_group = normalize_exchange_group(exchange_group)
    ds = data_source or BacktestDataSource()
    dates = ds.get_available_trade_dates(start_date, end_date)
    if len(dates) < 2:
        raise ValueError("策略重放至少需要两个交易日")

    cash = float(initial_cash)
    positions: dict[str, ReplayPosition] = {}
    trades: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    equity_series: list[dict[str, Any]] = []
    benchmark_series: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    previous_rebalance: str | None = None
    benchmark_base: float | None = None
    pit_audits: list[dict[str, Any]] = []
    previous_market_state = ""
    policy = champion["strategy_policy"]
    required_factors = set(config.score)
    for item in config.filters:
        if item.get("factor"):
            required_factors.add(str(item["factor"]))
    need_daily_basic = bool(required_factors & {"float_mv", "total_mv", "pe_ttm", "pb"})

    for index, date in enumerate(dates):
        cutoff = signal_available_time(date)
        if hasattr(ds, "set_as_of_time"):
            ds.set_as_of_time(cutoff)
        snapshot = ds.load_snapshot(date.replace("-", ""))
        snapshot = [row for row in snapshot if exchange_group == "ALL" or matches_exchange_group(row.symbol, exchange_group)]
        rows = {row.symbol: row for row in snapshot}
        if exposure_policy and hasattr(ds, "load_fund_snapshot"):
            fund_rows = ds.load_fund_snapshot(date.replace("-", ""), [item["ts_code"] for item in DEFENSIVE_ASSETS])
            rows.update({row.symbol: row for row in fund_rows})
        if not rows:
            blocked.append({"date": date, "side": "SYSTEM", "code": "empty_snapshot", "message": "当日行情截面为空"})
            continue
        if pending:
            cash = _execute_target(date=date, target_weights=pending["target_weights"], positions=positions, cash=cash, rows=rows, trades=trades, blocked=blocked)
            pending = None

        prices = {symbol: float(row.close or 0.0) for symbol, row in rows.items()}
        stock_value = 0.0
        by_symbol: dict[str, float] = {}
        for symbol, position in positions.items():
            if symbol in rows and rows[symbol].close > 0:
                position.last_close = rows[symbol].close
            value = position.quantity * position.last_close
            stock_value += value
            by_symbol[symbol] = round(value, 2)
        total_value = cash + stock_value
        equity_series.append({"date": date, "total_value": round(total_value, 2), "cash": round(cash, 2), "stock_value": round(stock_value, 2), "by_symbol": by_symbol})
        benchmark_close = ds.get_price_on_date(benchmark_symbol, date, field="close")
        if benchmark_close and benchmark_close > 0:
            benchmark_base = benchmark_base or benchmark_close
            benchmark_value = initial_cash * benchmark_close / benchmark_base
        else:
            benchmark_value = benchmark_series[-1]["total_value"] if benchmark_series else initial_cash
        benchmark_series.append({"date": date, "total_value": round(float(benchmark_value), 2)})

        if index >= len(dates) - 1 or not ds.should_rebalance(frequency, date, previous_rebalance):
            continue
        previous_rebalance = date
        daily_basic = ds.get_daily_basic_map(date.replace("-", "")) if need_daily_basic else {}
        lookback = max(int(config.lookback_days or 60), 60)
        stock_rows = {row.symbol: row for row in snapshot}
        ds.preload_histories(list(stock_rows), end_date=date, lookback=lookback)
        benchmark_returns = ds.get_index_returns("000300.SH", date, lookback=60) if required_factors & {"beta_60", "relative_strength_60"} else []
        benchmark_return = (
            math.prod(1.0 + value for value in benchmark_returns) - 1.0
            if benchmark_returns else None
        )
        factor_values: dict[str, dict[str, float]] = {}
        histories: dict[str, list[HistBar]] = {}
        for symbol, row in stock_rows.items():
            history = ds.get_hist_window(symbol, date, lookback)
            histories[symbol] = history
            factor_values[symbol] = {
                factor: _factor_value(
                    factor,
                    row,
                    history,
                    daily_basic,
                    benchmark_return=benchmark_return,
                    benchmark_returns=benchmark_returns,
                )
                for factor in required_factors
            }
        filtered = _apply_filters(list(stock_rows.values()), factor_values, config.filters, skip_factors={"float_mv", "total_mv", "pe_ttm", "pb"} if need_daily_basic and not daily_basic else set())
        if not filtered:
            decisions.append({"signal_date": date, "execute_date": dates[index + 1], "data_cutoff": cutoff, "status": "no_candidates", "target_weights": {}, "strategy_action_contracts": []})
            source_audit = ds.point_in_time_sources() if hasattr(ds, "point_in_time_sources") else []
            pit_audits.append(point_in_time_contract(cutoff=cutoff, sources=source_audit))
            pending = {"target_weights": {}}
            continue
        ranked = _score_rows(filtered, config.score, factor_values)
        candidates = ranked[:top_n]
        if controls:
            max_1d = float(controls.get("max_entry_1d_return") or 1.0)
            max_5d = float(controls.get("max_entry_5d_return") or 10.0)
            candidates = [
                item for item in candidates
                if float(item[0].change_pct or 0.0) / 100.0 <= max_1d
                and (
                    len(histories[item[0].symbol]) < 6
                    or histories[item[0].symbol][-1].close / max(histories[item[0].symbol][-6].close, 1e-12) - 1.0 <= max_5d
                )
            ]
        returns_by_symbol = {row.symbol: _returns(histories[row.symbol]) for row, _score in candidates}
        returns_by_symbol = {symbol: values for symbol, values in returns_by_symbol.items() if len(values) >= 2}
        expected_returns = {
            row.symbol: (histories[row.symbol][-1].close / histories[row.symbol][-21].close - 1.0) if len(histories[row.symbol]) >= 21 and histories[row.symbol][-21].close > 0 else float(score) / 100.0
            for row, score in candidates
            if row.symbol in returns_by_symbol
        }
        current_weights = _weights(positions, prices, cash)
        defensive_symbols = {item["ts_code"].split(".", 1)[0] for item in DEFENSIVE_ASSETS}
        current_stock_weights = {symbol: weight for symbol, weight in current_weights.items() if symbol not in defensive_symbols}
        if allocation_method == "equal_weight":
            equal_weight = 1.0 / len(returns_by_symbol) if returns_by_symbol else 0.0
            target_weights = {symbol: equal_weight for symbol in returns_by_symbol}
            optimized = {
                "ok": bool(target_weights),
                "status": "equal_weight",
                "optimizer": "equal_weight_baseline_v1",
                "weights": target_weights,
            }
            status = "equal_weight" if target_weights else "keep_current"
        else:
            optimized = two_group_cvar_optimizer(returns_by_symbol, expected_returns=expected_returns, current_weights=current_weights, trading_costs={symbol: 0.0015 for symbol in returns_by_symbol})
            if not optimized.get("ok"):
                target_weights = current_weights
                status = "keep_current"
            else:
                target_weights = {str(symbol): float(weight) for symbol, weight in optimized.get("weights", {}).items() if float(weight) > 0.0001}
                status = str(optimized.get("status") or "optimal")
        if controls:
            target_weights = _apply_challenger_controls(target_weights, current_stock_weights, controls)
            status = f"{status}_challenger_controlled"
        defensive_weights: dict[str, float] = {}
        market_state = ""
        benchmark_trend: float | None = None
        benchmark_confirmation_trend: float | None = None
        if exposure_policy:
            breadth = sum(float(row.change_pct or 0) > 0 for row in snapshot) / max(1, len(snapshot))
            average_return = sum(float(row.change_pct or 0) / 100.0 for row in snapshot) / max(1, len(snapshot))
            sentiment = score_market_sentiment(breadth=breadth, average_return=average_return)
            market_state = _transparent_regime(float(sentiment["score"]), average_return, previous_market_state)
            previous_market_state = market_state
            state_weights = exposure_policy.get("state_equity_weights") if isinstance(exposure_policy.get("state_equity_weights"), dict) else {}
            equity_target = max(0.0, min(1.0, float(state_weights.get(market_state, exposure_policy.get("default_equity_weight", 0.70)) or 0.70)))
            trend_overlay = exposure_policy.get("benchmark_trend_overlay") if isinstance(exposure_policy.get("benchmark_trend_overlay"), dict) else {}
            if trend_overlay and hasattr(ds, "get_index_return"):
                benchmark_trend = ds.get_index_return(
                    benchmark_symbol, date,
                    lookback=int(trend_overlay.get("lookback_sessions") or 20),
                )
                confirmation_lookback = int(trend_overlay.get("confirmation_lookback_sessions") or 0)
                if confirmation_lookback > 0:
                    benchmark_confirmation_trend = ds.get_index_return(
                        benchmark_symbol, date, lookback=confirmation_lookback,
                    )
                if benchmark_trend is not None:
                    positive = float(trend_overlay.get("positive_threshold") or 0.02)
                    negative = float(trend_overlay.get("negative_threshold") or -0.02)
                    confirmation_positive = float(trend_overlay.get("confirmation_positive_threshold") or 0.03)
                    confirmation_negative = float(trend_overlay.get("confirmation_negative_threshold") or -0.03)
                    positive_confirmed = (
                        benchmark_trend >= positive
                        and (
                            confirmation_lookback <= 0
                            or (
                                benchmark_confirmation_trend is not None
                                and benchmark_confirmation_trend >= confirmation_positive
                            )
                        )
                    )
                    negative_confirmed = (
                        benchmark_trend <= negative
                        or (
                            benchmark_confirmation_trend is not None
                            and benchmark_confirmation_trend <= confirmation_negative
                        )
                    )
                    if positive_confirmed:
                        equity_target = max(equity_target, float(trend_overlay.get("positive_equity_floor") or 0.90))
                    elif negative_confirmed:
                        equity_target = min(equity_target, float(trend_overlay.get("negative_equity_cap") or 0.35))
                    else:
                        equity_target = min(equity_target, float(trend_overlay.get("neutral_equity_cap") or 0.65))
            stock_total = sum(target_weights.values())
            target_weights = {
                symbol: weight / stock_total * equity_target
                for symbol, weight in target_weights.items()
            } if stock_total > 0 else {}
            if bool(exposure_policy.get("etf_fallback", True)):
                defensive = defensive_allocation_for_state(
                    market_state, as_of_date=date, equity_target_weight=equity_target,
                )
                defensive_weights = {
                    str(code).split(".", 1)[0]: float(weight)
                    for code, weight in (defensive.get("target_weights") or {}).items()
                    if str(code).split(".", 1)[0] in rows
                }
                risk_off_classes = exposure_policy.get("risk_off_etf_classes")
                positive_threshold = float(trend_overlay.get("positive_threshold") or 0.02) if trend_overlay else 0.02
                confirmation_threshold = float(trend_overlay.get("confirmation_positive_threshold") or 0.03) if trend_overlay else 0.03
                trend_risk_on = (
                    benchmark_trend is not None
                    and benchmark_trend >= positive_threshold
                    and (
                        int(trend_overlay.get("confirmation_lookback_sessions") or 0) <= 0
                        or (
                            benchmark_confirmation_trend is not None
                            and benchmark_confirmation_trend >= confirmation_threshold
                        )
                    )
                )
                if (
                    isinstance(risk_off_classes, list)
                    and not trend_risk_on
                ):
                    allowed_codes = {
                        item["ts_code"].split(".", 1)[0]
                        for item in DEFENSIVE_ASSETS
                        if item["asset_class"] in {str(value) for value in risk_off_classes}
                    }
                    defensive_weights = {
                        code: weight for code, weight in defensive_weights.items()
                        if code in allowed_codes
                    }
                    defensive_total = sum(defensive_weights.values())
                    defensive_target = max(0.0, 1.0 - equity_target)
                    if defensive_total > 0:
                        defensive_weights = {
                            code: weight / defensive_total * defensive_target
                            for code, weight in defensive_weights.items()
                        }
                target_weights.update(defensive_weights)
            status = f"{status}_dynamic_exposure_{market_state}"
        selection_map = {row.symbol: {"symbol": row.symbol, "name": row.name, "score": float(score)} for row, score in candidates}
        strategy_decision = build_strategy_rebalance_actions(
            target_symbols=list(target_weights),
            held_positions=_position_payload(positions),
            selection_map=selection_map,
            market_rows=rows,
            current_weights=current_weights,
            target_weights=target_weights,
            context={"strategy_policy": policy, "strategy_policy_version": champion["strategy_controller_version"], "production_strategy_version": champion["champion_version"], "target_weights": target_weights, "defensive_weights": defensive_weights, "market_state": market_state, "optimizer": optimized.get("optimizer"), "data_status": "trade_date_frozen_v1", "price_by_symbol": prices},
        )
        decision = {
            "signal_date": date,
            "execute_date": dates[index + 1],
            "data_cutoff": cutoff,
            "status": status,
            "strategy_id": strategy_id,
            "model_version_id": model_version_id,
            "target_weights": target_weights,
            "market_state": market_state,
            "benchmark_trend": benchmark_trend,
            "benchmark_confirmation_trend": benchmark_confirmation_trend,
            "defensive_weights": defensive_weights,
            "optimizer": optimized,
            "strategy_action_contracts": strategy_decision["strategy_action_contracts"],
            "rebalance_decision_trace": strategy_decision["rebalance_decision_trace"],
        }
        decisions.append(decision)
        source_audit = ds.point_in_time_sources() if hasattr(ds, "point_in_time_sources") else []
        pit_audits.append(point_in_time_contract(cutoff=cutoff, sources=source_audit))
        pending = decision

    metrics = build_metrics(equity_series, benchmark_series, trades)
    total_fees = round(sum(float(item.get("fees") or 0.0) for item in trades), 2)
    return {
        "schema_version": "strategy_replay_v1",
        "status": "completed",
        "strategy_id": strategy_id,
        "strategy_name": strategy.get("name") or "",
        "production_strategy_version_id": champion["champion_id"],
        "production_strategy_version": champion["champion_version"],
        "strategy_controller_version_id": champion["strategy_controller_version_id"],
        "strategy_controller_version": champion["strategy_controller_version"],
        "model_version_id": model_version_id,
        "model_version": (model_version or {}).get("version"),
        "start_date": dates[0],
        "end_date": dates[-1],
        "initial_cash": round(initial_cash, 2),
        "rebalance_freq": frequency,
        "benchmark_symbol": benchmark_symbol,
        "exchange_group": exchange_group,
        "component_contract": {
            "production_strategy": champion["champion_version"],
            "selection_strategy_id": strategy_id,
            "factor_registry": "alis_factor_registry_v1",
            "factor_bindings": (
                ((model_version or {}).get("config") or {}).get("factor_bindings")
                or champion.get("factor_bindings")
                or {}
            ),
            "challenger_controls": controls,
            "dynamic_market_exposure": exposure_policy,
            "strategy": "StrategyController",
            "allocation_method": allocation_method,
            "optimizer": "equal_weight_baseline_v1" if allocation_method == "equal_weight" else "two_group_cvar_optimizer_v2",
            "execution": "A-share open constraints + shared fee/slippage rules",
            "ledger": "isolated_in_memory_replay_v1",
            "signal_execution": "T close signal / T+1 open execution",
            "data_cutoff": "trade_date_bounded_v1",
        },
        "metrics": metrics,
        "summary": {"trading_days": len(equity_series), "decision_count": len(decisions), "trade_count": len(trades), "blocked_count": len(blocked), "total_fees": total_fees, "ending_equity": equity_series[-1]["total_value"] if equity_series else round(initial_cash, 2)},
        "equity_series": equity_series,
        "benchmark_series": benchmark_series,
        "decisions": decisions,
        "trades": trades,
        "blocked_events": blocked,
        "data_quality": {
            "warnings": list(getattr(ds, "data_warnings", []))[:100],
            "fallback_events": list(getattr(ds, "fallback_events", []))[:100],
            "point_in_time": {
                "schema_version": "strategy_replay_point_in_time_audit_v1",
                "passed": all(item.get("passed") for item in pit_audits),
                "decision_count": len(pit_audits),
                "audits": pit_audits,
                "source_scope": {
                    "consumed": ["daily", "daily_basic", "adj_factor", "stock_daily_snapshot"] + (["fund_daily"] if exposure_policy else []),
                    "upstream_as_of_contract": ["financial", "announcement", "event", "news", "theme", "expectation", "valuation", "industry"],
                    "not_consumed_by_replay_v1": ["financial", "announcement", "event", "news", "theme", "expectation", "valuation", "industry"],
                },
            },
            "limitations": ["生产重放固定 Champion；历史版本自动切换仅保留为可选审计模式"],
        },
        "generated_at": datetime.now().isoformat(),
    }
