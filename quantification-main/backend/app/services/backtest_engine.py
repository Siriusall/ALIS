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
from app.db.store import get_default_strategy_id, get_model_version, get_strategy
from app.services.backtest_data_source import BacktestDataSource, HistBar, SnapshotRow
from app.services.backtest_metrics import build_metrics
from app.services.strategy_config import parse_strategy_content

logger = logging.getLogger("quant.backtest_engine")


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_cost: float
    buy_date: str
    last_close: float = 0.0


def _norm(values: List[float]) -> List[float]:
    if not values:
        return []
    vmin = min(values)
    vmax = max(values)
    if vmax == vmin:
        return [0.0 for _ in values]
    return [(v - vmin) / (vmax - vmin) for v in values]


def _factor_value(
    factor: str,
    row: SnapshotRow,
    hist: List[HistBar],
    daily_basic: Dict[str, Dict[str, float]],
) -> float:
    if factor == "volume":
        return row.volume
    if factor == "turnover":
        return row.turnover
    if factor == "change_pct":
        return abs(row.change_pct)
    if factor == "momentum_5":
        if len(hist) < 5 or hist[-5].close <= 0:
            return 0.0
        return row.close / hist[-5].close - 1.0
    if factor == "momentum_20":
        if len(hist) < 20 or hist[-20].close <= 0:
            return 0.0
        return row.close / hist[-20].close - 1.0
    if factor == "volatility_20":
        if len(hist) < 20:
            return 0.0
        closes = [x.close for x in hist[-20:]]
        rets: List[float] = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                rets.append(closes[i] / closes[i - 1] - 1.0)
        if not rets:
            return 0.0
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / len(rets)
        return var ** 0.5 if var > 0 else 0.0
    if factor == "volume_ratio_5":
        if len(hist) < 5:
            return 0.0
        avg = sum(x.volume for x in hist[-5:]) / 5.0
        return row.volume / avg if avg > 0 else 0.0

    db = daily_basic.get(row.symbol, {})
    def _safe_num(v: Any) -> float:
        try:
            x = float(v or 0)
            return x if math.isfinite(x) else 0.0
        except Exception:
            return 0.0
    if factor == "float_mv":
        return _safe_num(db.get("float_mv", 0))
    if factor == "total_mv":
        return _safe_num(db.get("total_mv", 0))
    if factor == "pe_ttm":
        return _safe_num(db.get("pe_ttm", 0))
    if factor == "pb":
        return _safe_num(db.get("pb", 0))
    if factor == "is_st":
        return 1.0 if row.name.upper().startswith(("ST", "*ST")) else 0.0
    if factor == "is_limit_up":
        return 1.0 if row.change_pct >= 9.7 else 0.0
    if factor == "is_limit_down":
        return 1.0 if row.change_pct <= -9.7 else 0.0
    if factor == "is_suspended":
        return 1.0 if row.volume <= 0 else 0.0
    return 0.0


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
) -> List[Tuple[SnapshotRow, float]]:
    factors = [f for f in weights.keys()]
    values_per_factor: Dict[str, List[float]] = {f: [] for f in factors}
    for r in rows:
        vals = factor_values.get(r.symbol) or {}
        for f in factors:
            values_per_factor[f].append(float(vals.get(f, 0) or 0))
    norm_per_factor: Dict[str, List[float]] = {f: _norm(values_per_factor[f]) for f in factors}
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
    strategy_id = int(strategy_id or get_default_strategy_id() or 1)
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

    prev_rebalance_date: Optional[str] = None
    pending_target: Optional[List[str]] = None

    required_factors = set(config.score.keys())
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
            continue
        snapshot_map = {x.symbol: x for x in snapshot_all}
        snapshot = snapshot_all
        if exchange_group != "ALL":
            snapshot = [x for x in snapshot_all if matches_exchange_group(x.symbol, exchange_group)]
        if not snapshot:
            logger.warning("backtest day=%s exchange_group=%s 后无可用样本", d, exchange_group)
            pending_target = []
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
                px = apply_slippage_sell(row.open)
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
                    px = apply_slippage_buy(row.open)
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
                        last_close=row.close,
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
            row = snapshot_map.get(sym)
            if row and row.close > 0:
                pos.last_close = row.close
            mv = pos.last_close * pos.quantity
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
                vals[f] = _factor_value(f, row, hist, daily_basic_map)
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
        filtered = _apply_filters(snapshot, factor_values, config.filters, skip_factors=skip_filters)
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
            ranked = _score_rows(filtered, config.score, factor_values)
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
