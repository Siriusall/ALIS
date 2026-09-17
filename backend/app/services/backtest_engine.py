from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.exchange_groups import matches_exchange_group, normalize_exchange_group
from app.core.a_share_fees import (
    apply_slippage_buy,
    apply_slippage_sell,
    commission,
    stamp_tax,
    transfer_fee,
)
from app.core.trading_constraints import check_open_trade_constraints, normalize_lot_quantity
from app.db.store import get_model_version, get_strategy
from app.services.production_strategy import resolve_production_strategy_champion
from app.services.backtest_data_source import BacktestDataSource, HistBar, SnapshotRow
from app.services.backtest_metrics import build_metrics
from app.services.strategy_config import (
    get_strategy_factor_names,
    get_strategy_score_for_market_state,
    parse_strategy_content,
    resolve_selection_market_state,
)
from app.services.factor_platform import compute_registered_factor

logger = logging.getLogger("quant.backtest_engine")


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_cost: float
    buy_date: str


def _norm(values: List[float]) -> List[float]:
    if not values:
        return []
    vmin = min(values)
    vmax = max(values)
    if vmax == vmin:
        return [0.0 for _ in values]
    return [(v - vmin) / (vmax - vmin) for v in values]


def _rank_percentiles(values: List[float]) -> List[float]:
    """Return deterministic average-tie percentile ranks in [0, 1]."""
    if not values:
        return []
    if len(values) == 1:
        return [0.5]
    ordered = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0 for _ in values]
    start = 0
    while start < len(ordered):
        end = start + 1
        value = values[ordered[start]]
        while end < len(ordered) and values[ordered[end]] == value:
            end += 1
        average_rank = (start + end - 1) / 2.0
        percentile = average_rank / (len(values) - 1)
        for offset in range(start, end):
            ranks[ordered[offset]] = percentile
        start = end
    return ranks


def _apply_universe_controls(
    rows: List[SnapshotRow],
    factor_values: Dict[str, Dict[str, float]],
    universe: Dict[str, Any],
) -> List[SnapshotRow]:
    """Apply point-in-time cross-sectional universe rules shared by replay paths."""
    excluded_pct = max(0.0, min(0.90, float((universe or {}).get("exclude_smallest_float_mv_pct") or 0.0)))
    if excluded_pct <= 0 or not rows:
        return rows
    positive = sorted(
        float((factor_values.get(row.symbol) or {}).get("float_mv") or 0.0)
        for row in rows
        if float((factor_values.get(row.symbol) or {}).get("float_mv") or 0.0) > 0
    )
    if not positive:
        return []
    cutoff_index = min(len(positive) - 1, max(0, math.ceil(len(positive) * excluded_pct) - 1))
    cutoff = positive[cutoff_index]
    return [
        row for row in rows
        if float((factor_values.get(row.symbol) or {}).get("float_mv") or 0.0) > cutoff
    ]


def _factor_value(
    factor: str,
    row: SnapshotRow,
    hist: List[HistBar],
    daily_basic: Dict[str, Dict[str, float]],
    *,
    benchmark_return: float | None = None,
    benchmark_returns: List[float] | None = None,
) -> float:
    return compute_registered_factor(
        factor,
        row,
        hist,
        daily_basic,
        benchmark_return=benchmark_return,
        benchmark_returns=benchmark_returns,
    )


def _apply_filters(
    rows: List[SnapshotRow],
    factor_values: Dict[str, Dict[str, float]],
    filters: List[Dict[str, Any]],
    skip_factors: Optional[set[str]] = None,
) -> List[SnapshotRow]:
    if not filters:
        return rows
    skip_factors = skip_factors or set()
    out: List[SnapshotRow] = []
    for r in rows:
        vals = factor_values.get(r.symbol) or {}
        ok = True
        for f in filters:
            fn = str(f.get("factor", "")).strip()
            if fn in skip_factors:
                continue
            op = str(f.get("op", ">"))
            threshold = float(f.get("value", 0) or 0)
            v = float(vals.get(fn, 0) or 0)
            if fn in ("float_mv", "total_mv") and 0 < threshold < 10000:
                threshold = threshold * 10000.0
            if op == ">" and not (v > threshold):
                ok = False
                break
            if op == ">=" and not (v >= threshold):
                ok = False
                break
            if op == "<" and not (v < threshold):
                ok = False
                break
            if op == "<=" and not (v <= threshold):
                ok = False
                break
            if op == "==" and not (v == threshold):
                ok = False
                break
        if ok:
            out.append(r)
    return out


def _score_rows(
    rows: List[SnapshotRow],
    weights: Dict[str, float],
    factor_values: Dict[str, Dict[str, float]],
    *,
    normalization: str = "minmax",
) -> List[Tuple[SnapshotRow, float]]:
    if not rows:
        return []
    factors = [f for f in weights.keys()]
    if not factors:
        return [(r, 0.0) for r in rows]

    try:
        import numpy as np
        n_rows = len(rows)
        n_factors = len(factors)
        mat = np.empty((n_rows, n_factors), dtype=np.float64)
        for i, r in enumerate(rows):
            vals = factor_values.get(r.symbol) or {}
            for j, f in enumerate(factors):
                mat[i, j] = float(vals.get(f, 0) or 0)

        weight_vec = np.array([float(weights.get(f, 0) or 0) for f in factors], dtype=np.float64)

        if str(normalization or "").lower() == "rank_percentile":
            if n_rows > 1:
                # Rank percentiles along columns in [0, 1]
                # Tie-aware ranking matching _rank_percentiles
                norm_mat = np.empty_like(mat)
                for j in range(n_factors):
                    norm_mat[:, j] = _rank_percentiles(mat[:, j].tolist())
            else:
                norm_mat = np.full((n_rows, n_factors), 0.5, dtype=np.float64)
        else:
            # MinMax normalization along columns
            vmin = mat.min(axis=0)
            vmax = mat.max(axis=0)
            diff = vmax - vmin
            diff[diff == 0] = 1.0
            norm_mat = (mat - vmin) / diff

        scores = norm_mat @ weight_vec
        out = [(rows[i], float(scores[i])) for i in range(n_rows)]
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception:
        # Fallback to deterministic scalar implementation if numpy fails
        values_per_factor: Dict[str, List[float]] = {f: [] for f in factors}
        for r in rows:
            vals = factor_values.get(r.symbol) or {}
            for f in factors:
                values_per_factor[f].append(float(vals.get(f, 0) or 0))
        normalize = _rank_percentiles if str(normalization or "").lower() == "rank_percentile" else _norm
        norm_per_factor: Dict[str, List[float]] = {f: normalize(values_per_factor[f]) for f in factors}
        out: List[Tuple[SnapshotRow, float]] = []
        for i, r in enumerate(rows):
            s = 0.0
            for f in factors:
                s += float(weights.get(f, 0) or 0) * norm_per_factor[f][i]
            out.append((r, s))
        out.sort(key=lambda x: x[1], reverse=True)
        return out


def run_backtest(
    strategy_id: int | None,
    start_date: str,
    end_date: str,
    initial_cash: float,
    rebalance_freq: Optional[str] = None,
    benchmark_symbol: str = "000300",
    exchange_group: str = "HS",
    model_version_id: int | None = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    champion = resolve_production_strategy_champion()
    strategy_id = int(strategy_id or champion["strategy_id"])
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy_id={strategy_id} 不存在")

    config = parse_strategy_content(strategy.get("content"), strategy.get("type", "formula"))
    model_version = get_model_version(int(model_version_id)) if model_version_id else None
    exchange_group = normalize_exchange_group(exchange_group)
    freq = rebalance_freq or str(config.rebalance.get("freq", "daily"))
    model_config = (model_version or {}).get("config") or {}
    top_n = int(model_config.get("top_n") or config.rebalance.get("top_n", 20))
    hold_days = config.hold_days
    lookback = int(config.lookback_days or 60)

    ds = BacktestDataSource()
    trade_dates = ds.get_available_trade_dates(start_date, end_date)
    if len(trade_dates) < 2:
        raise ValueError("回测区间内交易日不足，请检查日期范围或数据覆盖")

    cash = float(initial_cash)
    holdings: Dict[str, Position] = {}
    trades: List[Dict[str, Any]] = []
    equity_series: List[Dict[str, Any]] = []
    benchmark_series: List[Dict[str, Any]] = []
    warnings: List[str] = []
    constraint_events: List[Dict[str, Any]] = []
    position_market_missing_dates: Dict[str, List[str]] = {}

    prev_rebalance_date: Optional[str] = None
    pending_target: Optional[List[str]] = None
    previous_market_state = ""
    previous_selection_market_state = ""

    strategy_content = strategy.get("content") if isinstance(strategy.get("content"), dict) else {}
    strategy_type = strategy.get("type", "formula")
    required_factors = get_strategy_factor_names(strategy_content, strategy_type)
    if float(config.universe.get("exclude_smallest_float_mv_pct") or 0.0) > 0:
        required_factors.add("float_mv")
    for f in config.filters:
        fn = str(f.get("factor", "")).strip()
        if fn:
            required_factors.add(fn)
    need_daily_basic = any(
        f in {"float_mv", "total_mv", "pe_ttm", "pb"} for f in required_factors
    )
    logger.info(
        "backtest start strategy_id=%s model_version_id=%s name=%s range=%s~%s freq=%s top_n=%s factors=%s filters=%s",
        strategy_id,
        model_version_id,
        strategy.get("name", ""),
        start_date,
        end_date,
        freq,
        top_n,
        sorted(list(required_factors)),
        config.filters,
    )

    benchmark_base_close: Optional[float] = None
    benchmark_nav: float = initial_cash
    prev_market_avg_close: Optional[float] = None
    market_factors = {"float_mv", "total_mv", "pe_ttm", "pb"}

    def _emit_progress(payload: Dict[str, Any]) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(payload)
        except Exception:
            pass

    for idx, d in enumerate(trade_dates):
        _emit_progress(
            {
                "date": d,
                "phase": "loading_snapshot",
                "processed_days": idx,
                "total_days": len(trade_dates),
                "universe_processed": 0,
                "universe_total": 0,
            }
        )
        snapshot_all = ds.load_snapshot(d.replace("-", ""))
        if not snapshot_all:
            logger.warning("backtest day=%s snapshot empty, skip", d)
            pending_target = None
            continue
        snapshot_map = {x.symbol: x for x in snapshot_all}
        snapshot = snapshot_all
        if exchange_group != "ALL":
            snapshot = [x for x in snapshot_all if matches_exchange_group(x.symbol, exchange_group)]
        if not snapshot:
            logger.warning("backtest day=%s exchange_group=%s 后无可用样本", d, exchange_group)
            pending_target = []
            continue
        missing_holdings = sorted(
            symbol for symbol in holdings
            if symbol not in snapshot_map or float(snapshot_map[symbol].close or 0.0) <= 0.0
        )
        if missing_holdings:
            position_market_missing_dates[d] = missing_holdings
            pending_target = None
            warnings.append(
                f"{d} 持仓缺少精确收盘价：{','.join(missing_holdings)}；"
                "已停止当日估值和冻结 T+1 执行，不使用前收或成本价。"
            )
            constraint_events.extend({
                "date": d,
                "symbol": symbol,
                "side": "SYSTEM",
                "code": "position_close_missing",
                "message": "持仓缺少当日精确收盘价，禁止沿用前收、估值或执行 T+1 订单",
            } for symbol in missing_holdings)
            continue

        # 执行前一日调仓信号（T+1 开盘成交）
        if pending_target is not None:
            target_set = set(pending_target)
            # 先卖出
            for sym in list(holdings.keys()):
                pos = holdings[sym]
                should_sell = sym not in target_set
                if hold_days and hold_days > 0:
                    try:
                        days_held = (
                            datetime.fromisoformat(d[:10]) - datetime.fromisoformat(pos.buy_date[:10])
                        ).days
                        # 持有天数约束按“至少持有 N 天”执行：
                        # 未满 N 天时，即使不在新目标池里也不卖出；
                        # 满 N 天后，是否卖出由调仓目标决定。
                        if days_held < hold_days:
                            should_sell = False
                    except ValueError:
                        pass
                if not should_sell:
                    continue
                row = snapshot_map.get(sym)
                if row is None:
                    continue
                tradable = check_open_trade_constraints(
                    side="SELL",
                    symbol=sym,
                    name=row.name,
                    open_price=row.open,
                    close_price=row.close,
                    change_pct=row.change_pct,
                    volume=row.volume,
                )
                if not tradable.ok:
                    constraint_events.append({"date": d, "symbol": sym, "side": "SELL", "code": tradable.code, "message": tradable.message})
                    continue
                px = apply_slippage_sell(row.open, sym)
                gross = px * pos.quantity
                fee_comm = round(commission(gross), 2)
                fee_trans = round(transfer_fee(gross), 2)
                fee_stamp = round(stamp_tax(gross), 2)
                net = gross - fee_comm - fee_trans - fee_stamp
                pnl = net - pos.avg_cost * pos.quantity
                cash += net
                trades.append(
                    {
                        "date": d,
                        "symbol": sym,
                        "side": "SELL",
                        "price": round(px, 2),
                        "quantity": pos.quantity,
                        "gross_amount": round(gross, 2),
                        "fees": round(fee_comm + fee_trans + fee_stamp, 2),
                        "pnl": round(pnl, 2),
                        "reason": "rebalance_or_hold_days",
                    }
                )
                del holdings[sym]

            # 再买入
            buy_list = [s for s in pending_target if s not in holdings]
            if buy_list:
                per_budget = cash / len(buy_list)
                for sym in buy_list:
                    row = snapshot_map.get(sym)
                    if row is None:
                        continue
                    tradable = check_open_trade_constraints(
                        side="BUY",
                        symbol=sym,
                        name=row.name,
                        open_price=row.open,
                        close_price=row.close,
                        change_pct=row.change_pct,
                        volume=row.volume,
                    )
                    if not tradable.ok:
                        constraint_events.append({"date": d, "symbol": sym, "side": "BUY", "code": tradable.code, "message": tradable.message})
                        continue
                    px = apply_slippage_buy(row.open, sym)
                    qty = int(per_budget / px)
                    qty = normalize_lot_quantity(qty)
                    if qty <= 0:
                        continue
                    gross = px * qty
                    fee_comm = round(commission(gross), 2)
                    fee_trans = round(transfer_fee(gross), 2)
                    total_cost = gross + fee_comm + fee_trans
                    if total_cost > cash:
                        continue
                    cash -= total_cost
                    holdings[sym] = Position(
                        symbol=sym,
                        quantity=qty,
                        avg_cost=round(total_cost / qty, 4),
                        buy_date=d,
                    )
                    trades.append(
                        {
                            "date": d,
                            "symbol": sym,
                            "side": "BUY",
                            "price": round(px, 2),
                            "quantity": qty,
                            "gross_amount": round(gross, 2),
                            "fees": round(fee_comm + fee_trans, 2),
                            "pnl": None,
                            "reason": "rebalance",
                        }
                    )
            pending_target = None

        # 更新市值
        stock_value = 0.0
        by_symbol: Dict[str, float] = {}
        for sym, pos in holdings.items():
            mv = float(snapshot_map[sym].close) * pos.quantity
            stock_value += mv
            by_symbol[sym] = round(mv, 2)
        total_value = cash + stock_value
        equity_series.append(
            {
                "date": d,
                "total_value": round(total_value, 2),
                "cash": round(cash, 2),
                "stock_value": round(stock_value, 2),
                "by_symbol": by_symbol,
            }
        )

        # 更新基准净值（优先指定 symbol，其次全市场等权）
        bm_close = ds.get_price_on_date(benchmark_symbol, d, field="close")
        if bm_close is not None and bm_close > 0:
            if benchmark_base_close is None:
                benchmark_base_close = bm_close
            benchmark_nav = initial_cash * (bm_close / benchmark_base_close)
        else:
            market_avg_close = 0.0
            if snapshot_all:
                market_avg_close = sum(r.close for r in snapshot_all if r.close > 0) / max(
                    1, len([r for r in snapshot_all if r.close > 0])
                )
            if market_avg_close > 0:
                if prev_market_avg_close is not None and prev_market_avg_close > 0:
                    benchmark_nav = benchmark_nav * (market_avg_close / prev_market_avg_close)
                prev_market_avg_close = market_avg_close
            else:
                warnings.append(f"{d} 无法计算基准收益，沿用上一日基准净值")
        benchmark_series.append({"date": d, "total_value": round(benchmark_nav, 2)})
        _emit_progress(
            {
                "date": d,
                "phase": "day_completed",
                "processed_days": idx + 1,
                "total_days": len(trade_dates),
                "universe_processed": len(snapshot),
                "universe_total": len(snapshot),
                "total_value": round(total_value, 2),
                "benchmark_value": round(benchmark_nav, 2),
                "trades_count": len(trades),
                "append_daily_record": True,
            }
        )

        # 生成新的调仓目标（T 日信号）
        if idx >= len(trade_dates) - 1:
            continue
        if not ds.should_rebalance(freq, d, prev_rebalance_date):
            continue
        prev_rebalance_date = d

        daily_basic_map: Dict[str, Dict[str, float]] = {}
        if need_daily_basic:
            _emit_progress(
                {
                    "date": d,
                    "phase": "loading_daily_basic",
                    "processed_days": idx,
                    "total_days": len(trade_dates),
                    "universe_processed": 0,
                    "universe_total": len(snapshot),
                }
            )
            daily_basic_map = ds.get_daily_basic_map(d.replace("-", ""))
            if not daily_basic_map:
                warnings.append(f"{d} 的 TuShare daily_basic 为空，市值/估值因子按 0 处理")
            logger.info(
                "backtest day=%s daily_basic rows=%d (need_mv_factor=%s)",
                d,
                len(daily_basic_map),
                need_daily_basic,
            )

        # 每个股票计算因子值
        factor_values: Dict[str, Dict[str, float]] = {}
        max_lookback = max(lookback, 20)
        total_universe = len(snapshot)
        ds.preload_histories([row.symbol for row in snapshot], end_date=d, lookback=max_lookback)
        benchmark_returns = ds.get_index_returns("000300.SH", d, lookback=60) if required_factors & {"beta_60", "relative_strength_60"} else []
        benchmark_return = (
            math.prod(1.0 + value for value in benchmark_returns) - 1.0
            if benchmark_returns else None
        )
        _emit_progress(
            {
                "date": d,
                "phase": "calculating_factors",
                "processed_days": idx,
                "total_days": len(trade_dates),
                "universe_processed": 0,
                "universe_total": total_universe,
            }
        )
        for i, row in enumerate(snapshot, start=1):
            hist = ds.get_hist_window(row.symbol, d, max_lookback)
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
            if i % 50 == 0 or i == total_universe:
                _emit_progress(
                    {
                        "date": d,
                        "phase": "calculating_factors",
                        "processed_days": idx,
                        "total_days": len(trade_dates),
                        "universe_processed": i,
                        "universe_total": total_universe,
                    }
                )

        # 过滤 + 打分 + 取 top_n
        skip_filters: set[str] = set()
        if need_daily_basic and not daily_basic_map:
            # TuShare 服务异常时，避免“市值过滤全灭”导致整段回测空跑。
            skip_filters = market_factors
            warnings.append(f"{d} 市值/估值数据不可用，已跳过相关过滤条件")
        universe_rows = _apply_universe_controls(snapshot, factor_values, config.universe)
        filtered = _apply_filters(universe_rows, factor_values, config.filters, skip_factors=skip_filters)
        if config.filters:
            logger.info(
                "backtest rebalance day=%s snapshot=%d filtered=%d skip_filters=%s",
                d,
                len(snapshot),
                len(filtered),
                sorted(list(skip_filters)) if skip_filters else [],
            )
        _emit_progress(
            {
                "date": d,
                "phase": "ranking",
                "processed_days": idx,
                "total_days": len(trade_dates),
                "universe_processed": len(filtered),
                "universe_total": len(snapshot),
            }
        )
        if not filtered:
            pending_target = []
            continue
        if model_version_id:
            from app.services.model_scorer import score_snapshot_rows

            ranked = score_snapshot_rows(ds=ds, rows=filtered, signal_date=d, model_version_id=int(model_version_id))
        else:
            score_weights = dict(config.score)
            if isinstance(strategy_content.get("regime_scores"), dict):
                from app.services.market_context import _transparent_regime, score_market_sentiment

                breadth = sum(float(row.change_pct or 0.0) > 0 for row in snapshot) / max(1, len(snapshot))
                average_return = sum(float(row.change_pct or 0.0) / 100.0 for row in snapshot) / max(1, len(snapshot))
                sentiment = score_market_sentiment(breadth=breadth, average_return=average_return)
                previous_market_state = _transparent_regime(
                    float(sentiment["score"]), average_return, previous_market_state,
                )
                benchmark_long = ds.get_index_return("000300.SH", d, lookback=60) if hasattr(ds, "get_index_return") else None
                benchmark_short = ds.get_index_return("000300.SH", d, lookback=20) if hasattr(ds, "get_index_return") else None
                previous_selection_market_state = resolve_selection_market_state(
                    strategy_content,
                    transparent_state=previous_market_state,
                    short_benchmark_return=benchmark_short,
                    long_benchmark_return=benchmark_long,
                    previous_state=previous_selection_market_state,
                )
                score_weights = get_strategy_score_for_market_state(
                    strategy_content, strategy_type, previous_selection_market_state,
                )
            ranked = _score_rows(
                filtered,
                score_weights,
                factor_values,
                normalization=config.score_normalization,
            )
        pending_target = [x[0].symbol for x in ranked[:top_n]]

    metrics = build_metrics(equity_series, benchmark_series, trades)
    logger.info(
        "backtest done strategy_id=%s days=%d trades=%d total_return=%.4f",
        strategy_id,
        len(equity_series),
        len(trades),
        float(metrics.get("total_return", 0) or 0),
    )
    combined_warnings = sorted(set([*warnings, *getattr(ds, "data_warnings", [])]))[:50]
    fallback_events = list(getattr(ds, "fallback_events", []))
    if fallback_events:
        combined_warnings.insert(0, f"回测使用了 {len(fallback_events)} 次 fallback 数据，请先核对 data_quality.fallback_events。")
    if constraint_events:
        combined_warnings.insert(0, f"回测有 {len(constraint_events)} 次交易被 A 股约束拦截，请核对 trade_constraints.blocked_events。")

    return {
        "strategy_id": strategy_id,
        "strategy_name": f"模型策略 {model_version.get('version')}" if model_version else strategy.get("name", ""),
        "model_version_id": int(model_version_id) if model_version_id else None,
        "model_version": model_version.get("version") if model_version else None,
        "production_strategy_version_id": champion["champion_id"],
        "production_strategy_version": champion["champion_version"],
        "factor_contract": {
            "schema_version": "alis_factor_registry_v1",
            "bindings": (
                (model_config.get("factor_bindings") if isinstance(model_config, dict) else None)
                or champion.get("factor_bindings")
                or {}
            ),
        },
        "start_date": trade_dates[0] if trade_dates else start_date,
        "end_date": trade_dates[-1] if trade_dates else end_date,
        "initial_cash": round(float(initial_cash), 2),
        "rebalance_freq": freq,
        "top_n": top_n,
        "benchmark_symbol": benchmark_symbol,
        "exchange_group": exchange_group,
        "metrics": metrics,
        "equity_series": equity_series,
        "benchmark_series": benchmark_series,
        "trades": trades,
        "warnings": combined_warnings,
        "data_quality": {
            "used_fallback": bool(fallback_events),
            "fallback_events": fallback_events[:100],
            "warnings": list(getattr(ds, "data_warnings", []))[:100],
            "held_position_market_data": {
                "passed": not position_market_missing_dates,
                "missing_dates": position_market_missing_dates,
                "policy": "持仓必须有当日精确收盘价；缺口不得用前收、成本价或后续交易日替代。",
            },
        },
        "trade_constraints": {
            "policy": {
                "lot_size": 100,
                "buy_blocks": ["suspended_or_no_open_price", "st_buy_blocked", "limit_up_buy_blocked"],
                "sell_blocks": ["suspended_or_no_open_price", "limit_down_sell_blocked"],
            },
            "blocked_count": len(constraint_events),
            "blocked_events": constraint_events[:200],
        },
    }
