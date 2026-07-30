from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class EquityPoint:
    date: str
    total_value: float


def _safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b


def _daily_returns(series: List[EquityPoint]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(series)):
        prev = series[i - 1].total_value
        cur = series[i].total_value
        out.append(_safe_div(cur - prev, prev))
    return out


def _max_drawdown(series: List[EquityPoint]) -> float:
    if not series:
        return 0.0
    peak = series[0].total_value
    mdd = 0.0
    for p in series:
        v = p.total_value
        if v > peak:
            peak = v
        dd = _safe_div(v - peak, peak)
        if dd < mdd:
            mdd = dd
    return mdd


def _sharpe(returns: List[float], annual_factor: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return 0.0
    return (mean / (var ** 0.5)) * (annual_factor ** 0.5)


def build_metrics(
    equity_series: List[Dict[str, Any]],
    benchmark_series: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
) -> Dict[str, float]:
    eq = [EquityPoint(date=x["date"], total_value=float(x["total_value"])) for x in equity_series]
    bench = [EquityPoint(date=x["date"], total_value=float(x["total_value"])) for x in benchmark_series]
    if len(eq) < 2:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "win_rate": 0.0,
            "turnover_rate": 0.0,
            "benchmark_return": 0.0,
            "excess_return": 0.0,
        }

    start_val = eq[0].total_value
    end_val = eq[-1].total_value
    total_return = _safe_div(end_val - start_val, start_val)
    periods = max(1, len(eq) - 1)
    annual_return = ((end_val / start_val) ** (252.0 / periods) - 1.0) if start_val > 0 else 0.0
    returns = _daily_returns(eq)
    sharpe = _sharpe(returns)
    mdd = _max_drawdown(eq)

    bench_return = 0.0
    if len(bench) >= 2 and bench[0].total_value > 0:
        bench_return = _safe_div(bench[-1].total_value - bench[0].total_value, bench[0].total_value)

    # 胜率按平仓交易统计（SELL 且含 pnl 字段）
    sells = [t for t in trades if (t.get("side") == "SELL" and isinstance(t.get("pnl"), (int, float)))]
    wins = [t for t in sells if float(t.get("pnl", 0)) > 0]
    win_rate = _safe_div(len(wins), len(sells)) if sells else 0.0

    traded_amount = sum(float(t.get("gross_amount", 0) or 0) for t in trades)
    avg_equity = sum(p.total_value for p in eq) / len(eq) if eq else 0.0
    turnover_rate = _safe_div(traded_amount, avg_equity) if avg_equity > 0 else 0.0

    return {
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "max_drawdown": round(mdd, 6),
        "sharpe": round(sharpe, 6),
        "win_rate": round(win_rate, 6),
        "turnover_rate": round(turnover_rate, 6),
        "benchmark_return": round(bench_return, 6),
        "excess_return": round(total_return - bench_return, 6),
    }


def build_market_state_metrics(
    equity_series: List[Dict[str, Any]],
    benchmark_series: List[Dict[str, Any]],
    market_state_series: List[Dict[str, Any]],
    *,
    trades: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Attribute point-in-time daily returns to the state known before the return."""
    equity = {str(item.get("date") or ""): float(item.get("total_value") or 0.0) for item in equity_series}
    benchmark = {str(item.get("date") or ""): float(item.get("total_value") or 0.0) for item in benchmark_series}
    states = {
        str(item.get("date") or ""): {
            "state": str(item.get("state") or "unknown"),
            "confidence": float(item.get("confidence") or 0.0),
            "uncertain": bool(item.get("uncertain")),
            "change_probability": float(item.get("change_probability") or 0.0),
        }
        for item in market_state_series
    }
    dates = [str(item.get("date") or "") for item in equity_series if str(item.get("date") or "") in benchmark]
    intervals: list[dict[str, Any]] = []
    buckets: dict[str, list[dict[str, Any]]] = {
        state: [] for state in ("S0", "S1", "S2", "S3", "unknown")
    }
    for index in range(1, len(dates)):
        prior_date, current_date = dates[index - 1], dates[index]
        prior_equity = equity.get(prior_date, 0.0)
        current_equity = equity.get(current_date, 0.0)
        prior_benchmark = benchmark.get(prior_date, 0.0)
        current_benchmark = benchmark.get(current_date, 0.0)
        if prior_equity <= 0 or prior_benchmark <= 0:
            continue
        strategy_return = current_equity / prior_equity - 1.0
        benchmark_return = current_benchmark / prior_benchmark - 1.0
        state = str((states.get(prior_date) or {}).get("state") or "unknown")
        if state not in buckets:
            state = "unknown"
        current_state = str((states.get(current_date) or {}).get("state") or "unknown")
        interval = {
            "prior_date": prior_date,
            "current_date": current_date,
            "state": state,
            "current_state": current_state,
            "strategy_return": strategy_return,
            "benchmark_return": benchmark_return,
            "uncertain": bool((states.get(prior_date) or {}).get("uncertain")),
        }
        intervals.append(interval)
        buckets[state].append(interval)

    trades_by_date: dict[str, list[dict[str, Any]]] = {}
    for trade in trades or []:
        trades_by_date.setdefault(str(trade.get("date") or ""), []).append(trade)

    def summarize(values: list[dict[str, Any]]) -> dict[str, Any]:
        strategy_returns = [float(item["strategy_return"]) for item in values]
        benchmark_returns = [float(item["benchmark_return"]) for item in values]
        attributed_trades = [
            trade
            for item in values
            for trade in trades_by_date.get(str(item["current_date"]), [])
        ]
        traded_amount = sum(float(item.get("gross_amount") or 0.0) for item in attributed_trades)
        total_fees = sum(float(item.get("fees") or 0.0) for item in attributed_trades)
        average_equity = (
            sum(float(equity.get(str(item["prior_date"])) or 0.0) for item in values)
            / len(values)
            if values else 0.0
        )
        strategy_value = 1.0
        benchmark_value = 1.0
        peak = 1.0
        drawdown = 0.0
        for strategy_return, benchmark_return in zip(strategy_returns, benchmark_returns):
            strategy_value *= 1.0 + strategy_return
            benchmark_value *= 1.0 + benchmark_return
            peak = max(peak, strategy_value)
            drawdown = min(drawdown, strategy_value / peak - 1.0)
        strategy_total = strategy_value - 1.0
        benchmark_total = benchmark_value - 1.0
        return {
            "observations": len(values),
            "total_return": round(strategy_total, 8),
            "benchmark_return": round(benchmark_total, 8),
            "excess_return": round(strategy_total - benchmark_total, 8),
            "max_drawdown": round(drawdown, 8),
            "trade_count": len(attributed_trades),
            "traded_amount": round(traded_amount, 2),
            "total_fees": round(total_fees, 2),
            "turnover_rate": round(traded_amount / average_equity, 8) if average_equity > 0 else 0.0,
            "hit_rate": round(
                sum(value > 0 for value in strategy_returns) / len(strategy_returns), 8
            ) if strategy_returns else 0.0,
            "positive_day_ratio": round(
                sum(value > 0 for value in strategy_returns) / len(strategy_returns), 8
            ) if strategy_returns else 0.0,
        }

    transition_indexes = [
        index for index, item in enumerate(intervals)
        if str(item["current_state"]) != str(item["state"])
    ]
    transition_values = [intervals[index] for index in transition_indexes]
    pre_transition_values = [
        item
        for index in transition_indexes
        for item in intervals[max(0, index - 2):index + 1]
    ]
    post_transition_values = [
        item
        for index in transition_indexes
        for item in intervals[index + 1:index + 4]
    ]
    uncertain_values = [item for item in intervals if bool(item.get("uncertain"))]

    return {
        "schema_version": "alis_replay_market_state_matrix_v1",
        "states": {state: summarize(buckets[state]) for state in ("S0", "S1", "S2", "S3")},
        "unknown": summarize(buckets["unknown"]),
        "transitions": {
            "count": len(transition_indexes),
            **summarize(transition_values),
            "pre_3_sessions": summarize(pre_transition_values),
            "post_3_sessions": summarize(post_transition_values),
        },
        "uncertain": summarize(uncertain_values),
        "attribution_rule": "daily_return_attributed_to_prior_close_point_in_time_state",
    }
