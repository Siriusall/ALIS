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

