from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Callable

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
from app.services.backtest_engine import _apply_filters, _apply_universe_controls, _score_rows
from app.services.backtest_metrics import build_market_state_metrics, build_metrics
from app.services.model_signal_fusion import MODEL_KEYS, apply_model_signals
from app.services.hsmm_shadow import infer_hsmm_state_as_of
from app.services.factor_platform import compute_bound_factor
from app.services.strategy_config import (
    get_strategy_factor_names,
    get_strategy_expert_route,
    get_strategy_score_for_market_state,
    parse_strategy_content,
    resolve_selection_market_state,
)
from app.services.strategy_controller import build_strategy_rebalance_actions
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.point_in_time import point_in_time_contract, signal_available_time
from app.services.two_group_optimizer import two_group_cvar_optimizer
from app.services.quant_risk import hrp_fallback_weights
from app.services.defensive_assets import DEFENSIVE_ASSETS, defensive_allocation_for_state
from app.services.market_context import (
    _transparent_regime,
    bocpd_change_alarm,
    classify_market_profile,
    score_market_sentiment,
)


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


def _annualized_volatility(returns: list[float], *, lookback: int = 20) -> float | None:
    values = [float(value) for value in returns[-max(2, int(lookback)):] if math.isfinite(float(value))]
    if len(values) < 2:
        return None
    center = sum(values) / len(values)
    variance = sum((value - center) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance)) * math.sqrt(252.0)


def _market_risk_context(
    snapshot: list[SnapshotRow],
    *,
    amount_history: list[float],
    signal_history: list[float],
    previous_state: str,
) -> dict[str, Any]:
    """Build the same explainable risk-state inputs for every replay day."""
    total = len(snapshot)
    amount = sum(max(0.0, float(row.turnover or 0.0)) for row in snapshot)
    prior_amounts = [value for value in amount_history[-20:] if value > 0]
    baseline = sum(prior_amounts) / len(prior_amounts) if prior_amounts else amount
    observations = {
        "breadth": sum(float(row.change_pct or 0.0) > 0 for row in snapshot) / max(1, total),
        "average_return": sum(float(row.change_pct or 0.0) / 100.0 for row in snapshot) / max(1, total),
        "volume_ratio": amount / baseline if baseline > 0 else 1.0,
        "limit_up_ratio": sum(float(row.change_pct or 0.0) >= 9.7 for row in snapshot) / max(1, total),
        "limit_down_ratio": sum(float(row.change_pct or 0.0) <= -9.7 for row in snapshot) / max(1, total),
        "northbound_flow_z": 0.0,
        "leader_persistence": 0.0,
        "price_fundamental_divergence": 0.0,
        "crowding": 0.0,
    }
    sentiment = score_market_sentiment(**observations)
    score = float(sentiment["score"])
    state = _transparent_regime(score, float(observations["average_return"]), previous_state)
    profile = classify_market_profile(state, observations)
    normalized_signal = (score - 50.0) / 50.0
    change_alarm = bocpd_change_alarm([*signal_history, normalized_signal])
    change_alarm["composite_signal"] = round(normalized_signal, 6)
    return {
        "market_state": state,
        "market_profile": profile,
        "change_alarm": change_alarm,
        "sentiment": sentiment,
        "observations": observations,
        "amount": amount,
        "signal": normalized_signal,
    }


def _apply_market_exposure_policy(
    *,
    base_equity_target: float,
    exposure_policy: dict[str, Any],
    market_profile: dict[str, Any],
    change_alarm: dict[str, Any],
    benchmark_returns: list[float],
) -> tuple[float, dict[str, Any]]:
    """Apply optional risk ceilings without inventing an implicit neutral cap."""
    target = max(0.0, min(1.0, float(base_equity_target)))
    trace: dict[str, Any] = {"base_equity_target": round(target, 8)}
    if bool(exposure_policy.get("use_profile_risk_multiplier")):
        multiplier = max(0.0, min(1.0, float(market_profile.get("risk_budget_multiplier") or 1.0)))
        target *= multiplier
        trace["market_profile_multiplier"] = multiplier
    if bool(exposure_policy.get("use_change_alarm")):
        multiplier = max(0.0, min(1.0, float(change_alarm.get("risk_multiplier") or 1.0)))
        target *= multiplier
        trace["change_alarm_multiplier"] = multiplier
    volatility_overlay = exposure_policy.get("volatility_overlay")
    if isinstance(volatility_overlay, dict):
        lookback = max(5, int(volatility_overlay.get("lookback_sessions") or 20))
        realized = _annualized_volatility(benchmark_returns, lookback=lookback)
        target_volatility = max(0.01, float(volatility_overlay.get("target_annualized") or 0.18))
        if realized and realized > 0:
            minimum_scale = max(0.0, min(1.0, float(volatility_overlay.get("minimum_scale") or 0.50)))
            maximum_scale = max(minimum_scale, min(1.0, float(volatility_overlay.get("maximum_scale") or 1.0)))
            scale = max(minimum_scale, min(maximum_scale, target_volatility / realized))
            target *= scale
            trace.update({
                "volatility_scale": round(scale, 8),
                "realized_annualized_volatility": round(realized, 8),
                "target_annualized_volatility": round(target_volatility, 8),
            })
    if "minimum_equity_weight" in exposure_policy:
        target = max(target, float(exposure_policy.get("minimum_equity_weight") or 0.0))
    if "maximum_equity_weight" in exposure_policy:
        target = min(target, float(exposure_policy.get("maximum_equity_weight") or 1.0))
    target = max(0.0, min(1.0, target))
    trace["final_equity_target"] = round(target, 8)
    return target, trace


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
    # Turnover blending and hysteresis must never reintroduce an overweight
    # position after the initial concentration cap was applied.
    weights = {symbol: min(max_single, weight) for symbol, weight in weights.items()}
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


def _select_rank_buffer_candidates(
    ranked: list[tuple[SnapshotRow, float]],
    *,
    held_symbols: set[str],
    top_n: int,
    controls: dict[str, Any],
    histories: dict[str, list[HistBar]],
) -> list[tuple[SnapshotRow, float]]:
    """Use a stricter buy rank than hold rank while keeping position count bounded."""
    limit = max(1, int(top_n))
    buy_rank = max(1, int(controls.get("buy_rank_max") or limit))
    hold_rank = max(buy_rank, int(controls.get("hold_rank_max") or buy_rank))
    use_buffer = "buy_rank_max" in controls or "hold_rank_max" in controls
    if not use_buffer:
        candidates = ranked[:limit]
    else:
        retained = [item for item in ranked[:hold_rank] if item[0].symbol in held_symbols][:limit]
        retained_symbols = {item[0].symbol for item in retained}
        slots = max(0, limit - len(retained))
        additions = [
            item for item in ranked[:buy_rank]
            if item[0].symbol not in held_symbols and item[0].symbol not in retained_symbols
        ][:slots]
        selected = retained + additions
        selected_symbols = {item[0].symbol for item in selected}
        candidates = [item for item in ranked if item[0].symbol in selected_symbols]

    max_1d = float(controls.get("max_entry_1d_return") or 1.0)
    max_5d = float(controls.get("max_entry_5d_return") or 10.0)
    min_1d = float(controls.get("min_entry_1d_return") or -1.0)
    min_5d = float(controls.get("min_entry_5d_return") or -10.0)

    def entry_allowed(item: tuple[SnapshotRow, float]) -> bool:
        row = item[0]
        one_day = float(row.change_pct or 0.0) / 100.0
        history = histories[row.symbol]
        five_day = (
            history[-1].close / max(history[-6].close, 1e-12) - 1.0
            if len(history) >= 6 else 0.0
        )
        return min_1d <= one_day <= max_1d and min_5d <= five_day <= max_5d

    return [
        item for item in candidates
        if item[0].symbol in held_symbols
        or entry_allowed(item)
    ]


def _expected_returns_for_candidates(
    candidates: list[tuple[SnapshotRow, float]],
    *,
    histories: dict[str, list[HistBar]],
    factor_values: dict[str, dict[str, float]],
    source: str,
    scale: float,
) -> dict[str, float]:
    """Align optimizer views with the candidate ranking instead of hard-coding 20-day momentum."""
    source = str(source or "momentum_20").strip()
    if source == "ranking_score":
        count = len(candidates)
        if count == 1:
            return {candidates[0][0].symbol: float(scale)}
        floor = max(0.0025, float(scale) * 0.10)
        return {
            row.symbol: float(scale) - (float(scale) - floor) * index / max(1, count - 1)
            for index, (row, _score) in enumerate(candidates)
        }
    if candidates and source in factor_values.get(candidates[0][0].symbol, {}):
        raw = {
            row.symbol: float((factor_values.get(row.symbol) or {}).get(source) or 0.0)
            for row, _score in candidates
        }
        ordered = sorted(raw, key=lambda symbol: (raw[symbol], symbol))
        if len(ordered) == 1:
            return {ordered[0]: float(scale)}
        floor = max(0.0025, float(scale) * 0.10)
        return {
            symbol: floor + (float(scale) - floor) * rank / max(1, len(ordered) - 1)
            for rank, symbol in enumerate(ordered)
        }
    return {
        row.symbol: (
            histories[row.symbol][-1].close / histories[row.symbol][-21].close - 1.0
            if len(histories[row.symbol]) >= 21 and histories[row.symbol][-21].close > 0
            else float(score) / 100.0
        )
        for row, score in candidates
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
    factor_bindings: dict[str, Any] | None = None,
    model_bindings: dict[str, Any] | None = None,
    market_state_binding: dict[str, Any] | None = None,
    strategy_controller_version_id: int | None = None,
    strategy_content: dict[str, Any] | None = None,
    strategy_name: str | None = None,
    data_source: BacktestDataSource | None = None,
    themes: list[dict[str, Any]] | None = None,
    consume_point_in_time_themes: bool = True,
    research_package_hash: str | None = None,
    evaluate_market_states: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    champion = resolve_production_strategy_champion()
    strategy_id = int(strategy_id or champion["strategy_id"])
    strategy = (
        {
            "id": strategy_id,
            "name": strategy_name or "内存研究候选",
            "type": "formula",
            "content": strategy_content,
        }
        if isinstance(strategy_content, dict)
        else get_strategy(strategy_id)
    )
    if not strategy:
        raise ValueError(f"strategy_id={strategy_id} 不存在")
    model_version = get_model_version(int(model_version_id)) if model_version_id else None
    raw_content = strategy.get("content") if isinstance(strategy.get("content"), dict) else {}
    controls = raw_content.get("challenger_controls") if isinstance(raw_content.get("challenger_controls"), dict) else {}
    exposure_policy = raw_content.get("dynamic_market_exposure") if isinstance(raw_content.get("dynamic_market_exposure"), dict) else {}
    regime_scores = raw_content.get("regime_scores") if isinstance(raw_content.get("regime_scores"), dict) else {}
    uses_market_state = bool(exposure_policy or regime_scores or evaluate_market_states)
    allocation_method = str(raw_content.get("portfolio_method") or "cvar").strip().lower()
    if allocation_method not in {"cvar", "equal_weight", "hrp"}:
        raise ValueError(f"策略重放不支持组合方法：{allocation_method}")
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
    market_state_series: list[dict[str, Any]] = []
    model_fusion_history: list[dict[str, Any]] = []
    previous_market_state = ""
    previous_selection_market_state = ""
    market_amount_history: list[float] = []
    market_signal_history: list[float] = []
    latest_market_risk: dict[str, Any] = {}
    controller_version = None
    policy = champion["strategy_policy"]
    if strategy_controller_version_id is not None:
        controller_version = get_model_version(int(strategy_controller_version_id))
        if not controller_version or str(controller_version.get("model_key") or "") != "strategy_controller":
            raise ValueError("strategy_controller_version_id 不存在或不是策略控制器候选")
        config_payload = controller_version.get("config") if isinstance(controller_version.get("config"), dict) else {}
        candidate_policy = config_payload.get("policy") if isinstance(config_payload.get("policy"), dict) else None
        if not candidate_policy:
            raise ValueError("策略控制器候选缺少 policy 配置")
        policy = candidate_policy
    controller_version_id = int((controller_version or {}).get("id") or champion["strategy_controller_version_id"])
    controller_version_name = str((controller_version or {}).get("version") or champion["strategy_controller_version"])
    explicit_factor_bindings = factor_bindings is not None
    replay_factor_bindings = factor_bindings
    if replay_factor_bindings is None and isinstance(champion.get("factor_bindings"), dict):
        replay_factor_bindings = champion.get("factor_bindings") or {}
    explicit_model_bindings = model_bindings is not None
    replay_model_bindings = model_bindings
    if replay_model_bindings is None and isinstance(champion.get("model_bindings"), dict):
        frozen = {
            key: champion["model_bindings"].get(key)
            for key in MODEL_KEYS
        }
        if any(value is not None for value in frozen.values()):
            replay_model_bindings = frozen
            explicit_model_bindings = True
    replay_market_state_binding = (
        dict(market_state_binding)
        if isinstance(market_state_binding, dict)
        else dict(champion.get("market_state_binding") or {})
    )
    if not replay_market_state_binding:
        replay_market_state_binding = {
            "production_model": "transparent_four_state_v1",
            "change_model": "bocpd_gaussian_v1",
        }
    production_market_state_model = str(
        replay_market_state_binding.get("production_model") or "transparent_four_state_v1"
    )
    if production_market_state_model not in {
        "transparent_four_state_v1",
        "market_regime_hsmm_v1",
    }:
        raise ValueError(f"策略重放暂不支持市场状态生产模型：{production_market_state_model}")
    hsmm_binding = replay_market_state_binding.get("hsmm_shadow")
    if production_market_state_model == "market_regime_hsmm_v1" and not isinstance(hsmm_binding, dict):
        raise ValueError("HSMM 市场状态候选缺少冻结版本绑定")
    required_factors = get_strategy_factor_names(raw_content, strategy.get("type", "formula"))
    required_factors.update({"momentum_20", "momentum_60", "volatility_20"})
    if float(config.universe.get("exclude_smallest_float_mv_pct") or 0.0) > 0:
        required_factors.add("float_mv")
    for item in config.filters:
        if item.get("factor"):
            required_factors.add(str(item["factor"]))
    if explicit_factor_bindings:
        missing_bindings = sorted(
            factor for factor in required_factors
            if not isinstance((replay_factor_bindings or {}).get(factor), dict)
        )
        if missing_bindings:
            raise ValueError(f"冻结策略包缺少因子绑定：{','.join(missing_bindings)}")
    need_daily_basic = bool(required_factors & {"float_mv", "total_mv", "pe_ttm", "pb"})
    theme_history: list[dict[str, Any]] = []
    theme_cache: dict[str, list[dict[str, Any]]] = {}

    def themes_as_of(cutoff: str) -> list[dict[str, Any]]:
        if themes is not None:
            return list(themes)
        if not consume_point_in_time_themes:
            return []
        cache_key = str(cutoff)[:10]
        if cache_key in theme_cache:
            return theme_cache[cache_key]
        try:
            from app.services.theme_engine import list_themes

            loaded = list_themes(
                as_of_time=cutoff,
                promotable_only=False,
                limit=300,
            ).get("items") or []
            theme_cache[cache_key] = list(loaded)
            theme_history.append({
                "date": cache_key,
                "status": "ready" if loaded else "valid_empty",
                "count": len(loaded),
            })
        except Exception as exc:  # Theme fallback must not invalidate price-only replay.
            theme_cache[cache_key] = []
            theme_history.append({
                "date": cache_key,
                "status": "unavailable_fallback",
                "count": 0,
                "error": str(exc)[:300],
            })
        return theme_cache[cache_key]

    def report_progress(*, phase: str, completed_days: int, message: str) -> None:
        if progress_callback:
            progress_callback({
                "phase": phase,
                "completed_days": completed_days,
                "total_days": len(dates),
                "progress": round(completed_days / max(1, len(dates)), 6),
                "message": message,
            })

    report_progress(phase="strategy_replay", completed_days=0, message="正式 OOS 窗口开始逐日重放")

    for index, date in enumerate(dates):
        cutoff = signal_available_time(date)
        if hasattr(ds, "set_as_of_time"):
            ds.set_as_of_time(cutoff)
        snapshot = ds.load_snapshot(date.replace("-", ""))
        snapshot = [row for row in snapshot if exchange_group == "ALL" or matches_exchange_group(row.symbol, exchange_group)]
        rows = {row.symbol: row for row in snapshot}
        if bool(exposure_policy.get("etf_fallback")) and hasattr(ds, "load_fund_snapshot"):
            fund_rows = ds.load_fund_snapshot(date.replace("-", ""), [item["ts_code"] for item in DEFENSIVE_ASSETS])
            rows.update({row.symbol: row for row in fund_rows})
        if not rows:
            blocked.append({"date": date, "side": "SYSTEM", "code": "empty_snapshot", "message": "当日行情截面为空"})
            continue
        if uses_market_state:
            latest_market_risk = _market_risk_context(
                snapshot,
                amount_history=market_amount_history,
                signal_history=market_signal_history,
                previous_state=previous_market_state,
            )
            transparent_state = str(latest_market_risk["market_state"])
            effective_market_state_model = "transparent_four_state_v1"
            market_state_fallback_reason = ""
            state_confidence: float | None = None
            hsmm_evidence: dict[str, Any] | None = None
            if production_market_state_model == "market_regime_hsmm_v1":
                hsmm_evidence = infer_hsmm_state_as_of(
                    hsmm_binding,
                    as_of_time=cutoff,
                )
                hsmm_state = str(hsmm_evidence.get("state") or "")
                if bool(hsmm_evidence.get("ok")) and hsmm_state in {"S0", "S1", "S2", "S3"}:
                    latest_market_risk["market_state"] = hsmm_state
                    latest_market_risk["market_profile"] = classify_market_profile(
                        hsmm_state,
                        latest_market_risk.get("observations") or {},
                    )
                    effective_market_state_model = "market_regime_hsmm_v1"
                    state_confidence = float(hsmm_evidence.get("confidence") or 0.0)
                else:
                    market_state_fallback_reason = str(
                        hsmm_evidence.get("reason") or "hsmm_point_in_time_state_unavailable"
                    )
            previous_market_state = str(latest_market_risk["market_state"])
            market_amount_history.append(float(latest_market_risk["amount"]))
            market_signal_history.append(float(latest_market_risk["signal"]))
            if state_confidence is None:
                sentiment_score = float((latest_market_risk.get("sentiment") or {}).get("score") or 50.0)
                state_confidence = min(0.85, 0.45 + abs(sentiment_score - 50.0) / 100.0)
            market_state_series.append({
                "date": date,
                "state": str(latest_market_risk.get("market_state") or "S1"),
                "confidence": round(state_confidence, 6),
                "uncertain": state_confidence < 0.55,
                "change_probability": float(
                    (latest_market_risk.get("change_alarm") or {}).get("p_change_1d") or 0.0
                ),
                "model": effective_market_state_model,
                "requested_model": production_market_state_model,
                "effective_model": effective_market_state_model,
                "fallback_reason": market_state_fallback_reason,
                "transparent_state": transparent_state,
                "hsmm_evidence": hsmm_evidence or {},
            })
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
        if (index + 1) % 10 == 0 or index + 1 == len(dates):
            report_progress(
                phase="strategy_replay",
                completed_days=index + 1,
                message=f"正式 OOS 窗口重放 {index + 1}/{len(dates)} 个交易日",
            )

        if index >= len(dates) - 1 or not ds.should_rebalance(frequency, date, previous_rebalance):
            continue
        previous_rebalance = date
        daily_basic = ds.get_daily_basic_map(date.replace("-", "")) if need_daily_basic else {}
        lookback = max(int(config.lookback_days or 60), 60)
        stock_rows = {row.symbol: row for row in snapshot}
        ds.preload_histories(list(stock_rows), end_date=date, lookback=lookback)
        benchmark_returns = (
            ds.get_index_returns("000300.SH", date, lookback=60)
            if uses_market_state or required_factors & {"beta_60", "relative_strength_60"}
            else []
        )
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
                factor: compute_bound_factor(
                    factor,
                    (
                        (replay_factor_bindings or {}).get(factor)
                        if isinstance((replay_factor_bindings or {}).get(factor), dict)
                        else None
                    ),
                    row,
                    history,
                    daily_basic,
                    benchmark_return=benchmark_return,
                    benchmark_returns=benchmark_returns,
                )
                for factor in required_factors
            }
        universe_rows = _apply_universe_controls(list(stock_rows.values()), factor_values, config.universe)
        filtered = _apply_filters(universe_rows, factor_values, config.filters, skip_factors={"float_mv", "total_mv", "pe_ttm", "pb"} if need_daily_basic and not daily_basic else set())
        if not filtered:
            decisions.append({"signal_date": date, "execute_date": dates[index + 1], "data_cutoff": cutoff, "status": "no_candidates", "target_weights": {}, "strategy_action_contracts": []})
            source_audit = ds.point_in_time_sources() if hasattr(ds, "point_in_time_sources") else []
            pit_audits.append(point_in_time_contract(cutoff=cutoff, sources=source_audit))
            pending = {"target_weights": {}}
            continue
        transparent_state = str(latest_market_risk.get("market_state") or "") if uses_market_state else ""
        selection_short_return = (
            ds.get_index_return("000300.SH", date, lookback=20)
            if regime_scores and hasattr(ds, "get_index_return") else None
        )
        market_state_for_score = resolve_selection_market_state(
            raw_content,
            transparent_state=transparent_state,
            short_benchmark_return=selection_short_return,
            long_benchmark_return=benchmark_return,
            previous_state=previous_selection_market_state,
        )
        previous_selection_market_state = market_state_for_score
        score_weights = get_strategy_score_for_market_state(
            raw_content,
            strategy.get("type", "formula"),
            market_state_for_score,
        )
        expert_route = get_strategy_expert_route(
            raw_content,
            market_state_for_score,
        )
        ranked = _score_rows(
            filtered,
            score_weights,
            factor_values,
            normalization=config.score_normalization,
        )
        ranked, model_signal_map, model_blocked, model_fusion = apply_model_signals(
            ranked=ranked,
            factor_values=factor_values,
            histories=histories,
            themes=themes_as_of(cutoff),
            model_bindings=replay_model_bindings,
            use_live_health=False if explicit_model_bindings else None,
            as_of_time=cutoff,
            market_state=market_state_for_score,
        )
        model_fusion_history.append({"trade_date": date, **model_fusion})
        blocked.extend({"date": date, **item} for item in model_blocked)
        candidates = _select_rank_buffer_candidates(
            ranked,
            held_symbols=set(positions),
            top_n=top_n,
            controls=controls,
            histories=histories,
        )
        returns_by_symbol = {row.symbol: _returns(histories[row.symbol]) for row, _score in candidates}
        returns_by_symbol = {symbol: values for symbol, values in returns_by_symbol.items() if len(values) >= 2}
        candidates = [item for item in candidates if item[0].symbol in returns_by_symbol]
        expected_returns = _expected_returns_for_candidates(
            candidates,
            histories=histories,
            factor_values=factor_values,
            source=config.expected_return_source,
            scale=config.expected_return_scale,
        )
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
        elif allocation_method == "hrp":
            target_weights = hrp_fallback_weights(
                returns_by_symbol,
                max_single=max(0.01, min(1.0, float(controls.get("max_single_weight") or 1.0))),
            )
            optimized = {
                "ok": bool(target_weights),
                "status": "hrp" if target_weights else "keep_current",
                "optimizer": "hrp_inverse_volatility_v1",
                "weights": target_weights,
            }
            status = "hrp" if target_weights else "keep_current"
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
        market_state = market_state_for_score
        market_profile: dict[str, Any] = {}
        change_alarm: dict[str, Any] = {}
        exposure_trace: dict[str, Any] = {}
        benchmark_trend: float | None = None
        benchmark_confirmation_trend: float | None = None
        if exposure_policy:
            market_state = str(latest_market_risk.get("market_state") or "S1")
            market_profile = latest_market_risk.get("market_profile") if isinstance(latest_market_risk.get("market_profile"), dict) else {}
            change_alarm = latest_market_risk.get("change_alarm") if isinstance(latest_market_risk.get("change_alarm"), dict) else {}
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
                        if "positive_equity_floor" in trend_overlay:
                            equity_target = max(equity_target, float(trend_overlay.get("positive_equity_floor") or 0.0))
                    elif negative_confirmed:
                        if "negative_equity_cap" in trend_overlay:
                            equity_target = min(equity_target, float(trend_overlay.get("negative_equity_cap") or 0.0))
                    elif "neutral_equity_cap" in trend_overlay:
                        equity_target = min(equity_target, float(trend_overlay.get("neutral_equity_cap") or 0.0))
            equity_target, exposure_trace = _apply_market_exposure_policy(
                base_equity_target=equity_target,
                exposure_policy=exposure_policy,
                market_profile=market_profile,
                change_alarm=change_alarm,
                benchmark_returns=benchmark_returns,
            )
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
        selection_map = {
            row.symbol: {
                "symbol": row.symbol,
                "name": row.name,
                "score": float(score),
                "model_signals": model_signal_map.get(row.symbol) or {},
            }
            for row, score in candidates
        }
        strategy_decision = build_strategy_rebalance_actions(
            target_symbols=list(target_weights),
            held_positions=_position_payload(positions),
            selection_map=selection_map,
            market_rows=rows,
            current_weights=current_weights,
            target_weights=target_weights,
            context={"strategy_policy": policy, "strategy_policy_version": controller_version_name, "production_strategy_version": champion["champion_version"], "target_weights": target_weights, "defensive_weights": defensive_weights, "market_state": market_state, "optimizer": optimized.get("optimizer"), "data_status": "trade_date_frozen_v1", "price_by_symbol": prices},
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
            "selection_market_state": market_state_for_score,
            "selection_short_benchmark_return": selection_short_return,
            "selection_long_benchmark_return": benchmark_return,
            "selection_score_weights": score_weights,
            "selection_expert_route": expert_route,
            "market_profile": market_profile,
            "change_alarm": change_alarm,
            "exposure_trace": exposure_trace,
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
    metrics["market_state_matrix"] = build_market_state_metrics(
        equity_series,
        benchmark_series,
        market_state_series,
        trades=trades,
    )
    total_fees = round(sum(float(item.get("fees") or 0.0) for item in trades), 2)
    return {
        "schema_version": "strategy_replay_v1",
        "status": "completed",
        "strategy_id": strategy_id,
        "strategy_name": strategy.get("name") or "",
        "production_strategy_version_id": champion["champion_id"],
        "production_strategy_version": champion["champion_version"],
        "strategy_controller_version_id": controller_version_id,
        "strategy_controller_version": controller_version_name,
        "model_version_id": model_version_id,
        "model_version": (model_version or {}).get("version"),
        "research_package_hash": research_package_hash,
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
            "factor_bindings": replay_factor_bindings or {},
            "factor_binding_mode": "frozen" if explicit_factor_bindings else "champion_or_runtime",
            "model_bindings": replay_model_bindings or {},
            "model_binding_mode": "frozen" if explicit_model_bindings else "active_runtime",
            "market_state_binding": replay_market_state_binding,
            "market_state_model": production_market_state_model,
            "market_state_runtime": {
                "requested_model": production_market_state_model,
                "effective_days": {
                    model: sum(
                        str(item.get("effective_model") or item.get("model") or "") == model
                        for item in market_state_series
                    )
                    for model in ("transparent_four_state_v1", "market_regime_hsmm_v1")
                },
                "fallback_days": sum(bool(item.get("fallback_reason")) for item in market_state_series),
            },
            "theme_contract": {
                "mode": "explicit" if themes is not None else "point_in_time_snapshot" if consume_point_in_time_themes else "disabled",
                "history": theme_history,
            },
            "universe": config.universe,
            "score_normalization": config.score_normalization,
            "expected_return": {
                "source": config.expected_return_source,
                "scale": config.expected_return_scale,
            },
            "challenger_controls": controls,
            "dynamic_market_exposure": exposure_policy,
            "strategy": "StrategyController",
            "allocation_method": allocation_method,
            "optimizer": (
                "equal_weight_baseline_v1"
                if allocation_method == "equal_weight"
                else "hrp_inverse_volatility_v1"
                if allocation_method == "hrp"
                else "two_group_cvar_optimizer_v2"
            ),
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
        "market_state_series": market_state_series,
        "model_fusion_history": model_fusion_history,
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
                    "consumed": ["daily", "daily_basic", "adj_factor", "stock_daily_snapshot"] + (["theme"] if themes is not None or consume_point_in_time_themes else []) + (["fund_daily"] if exposure_policy else []),
                    "upstream_as_of_contract": ["financial", "announcement", "event", "news", "theme", "expectation", "valuation", "industry"],
                    "not_consumed_by_replay_v1": ["financial", "announcement", "event", "news", "expectation", "valuation", "industry"],
                },
            },
            "limitations": ["生产重放固定 Champion；历史版本自动切换仅保留为可选审计模式"],
        },
        "generated_at": datetime.now().isoformat(),
    }
