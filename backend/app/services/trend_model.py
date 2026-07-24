from __future__ import annotations

from statistics import mean, pstdev
from typing import Any, Dict, List, Protocol

from app.core.exchange_groups import matches_exchange_group, normalize_exchange_group
from app.services.backtest_data_source import BacktestDataSource, HistBar, SnapshotRow


class TrendAnalyzer(Protocol):
    """趋势理解器接口；后续可替换为训练模型实现。"""

    def analyze(
        self,
        *,
        ds: BacktestDataSource,
        signal_date: str,
        exchange_group: str = "HS",
        sample_limit: int = 500,
    ) -> Dict[str, Any]:
        ...


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _pct_return(bars: List[HistBar], lookback: int) -> float | None:
    if len(bars) < lookback:
        return None
    start = float(bars[-lookback].close or 0)
    end = float(bars[-1].close or 0)
    if start <= 0 or end <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _volatility_pct(bars: List[HistBar], lookback: int = 20) -> float | None:
    if len(bars) < lookback:
        return None
    closes = [float(x.close or 0) for x in bars[-lookback:]]
    rets: List[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0 and cur > 0:
            rets.append((cur / prev - 1.0) * 100.0)
    if len(rets) < 5:
        return None
    return pstdev(rets)


def _sample_rows(rows: List[SnapshotRow], limit: int = 500) -> List[SnapshotRow]:
    liquid = [x for x in rows if _safe_float(getattr(x, "turnover", 0)) > 0 and _safe_float(getattr(x, "close", 0)) > 0]
    liquid.sort(key=lambda x: _safe_float(getattr(x, "turnover", 0)), reverse=True)
    return liquid[: max(50, int(limit))]


def analyze_trend(
    *,
    ds: BacktestDataSource,
    signal_date: str,
    exchange_group: str = "HS",
    sample_limit: int = 500,
) -> Dict[str, Any]:
    """
    简单趋势理解器。

    只使用 signal_date 当天及以前可见数据，不输出概率，直接给出趋势结论和原因。
    后续如果训练更强模型，应优先替换本函数，保持下游推荐/调仓接口稳定。
    """
    exchange_group = normalize_exchange_group(exchange_group)
    rows = ds.load_snapshot(signal_date.replace("-", ""))
    rows = [r for r in rows if str(getattr(r, "date", "")) <= str(signal_date)]
    if exchange_group != "ALL":
        rows = [r for r in rows if matches_exchange_group(r.symbol, exchange_group)]

    tradable_rows = [x for x in rows if _safe_float(getattr(x, "volume", 0)) > 0 and _safe_float(getattr(x, "close", 0)) > 0]
    if len(tradable_rows) < 50:
        return {
            "trade_date": signal_date,
            "trend": "high_risk",
            "risk_level": "high",
            "reason": "可用行情样本不足，趋势判断降级为高风险。",
            "features": {
                "universe": len(rows),
                "tradable": len(tradable_rows),
            },
        }

    changes = [_safe_float(getattr(x, "change_pct", 0)) for x in tradable_rows]
    up_ratio = sum(1 for x in changes if x > 0) / len(changes)
    avg_change_pct = mean(changes) if changes else 0.0

    momentum_values: List[float] = []
    volatility_values: List[float] = []
    sampled_rows = _sample_rows(tradable_rows, limit=sample_limit)
    ds.preload_histories([row.symbol for row in sampled_rows], end_date=signal_date, lookback=21)
    for row in sampled_rows:
        hist = ds.get_hist_window(row.symbol, signal_date, 21)
        momentum = _pct_return(hist, 20)
        volatility = _volatility_pct(hist, 20)
        if momentum is not None:
            momentum_values.append(momentum)
        if volatility is not None:
            volatility_values.append(volatility)

    avg_momentum_20 = mean(momentum_values) if momentum_values else 0.0
    avg_volatility_20 = mean(volatility_values) if volatility_values else 0.0
    coverage = len(momentum_values) / max(1, len(sampled_rows))

    trend = "sideways"
    risk_level = "normal"
    reason_parts: List[str] = []

    if coverage < 0.35:
        trend = "high_risk"
        risk_level = "high"
        reason_parts.append("20日历史样本覆盖不足，优先降低仓位。")
    elif avg_volatility_20 >= 4.5 and avg_change_pct <= -1.0:
        trend = "high_risk"
        risk_level = "high"
        reason_parts.append("截面波动放大且当日均跌幅偏大，进入高风险模式。")
    elif avg_momentum_20 >= 3.0 and up_ratio >= 0.52 and avg_change_pct >= -0.5:
        trend = "uptrend"
        risk_level = "normal"
        reason_parts.append("20日动量为正且上涨家数占优，趋势偏强。")
    elif avg_momentum_20 <= -3.0 or up_ratio <= 0.42 or avg_change_pct <= -1.5:
        trend = "downtrend"
        risk_level = "elevated"
        reason_parts.append("20日动量或市场宽度偏弱，趋势偏弱。")
    else:
        reason_parts.append("动量和市场宽度没有明显方向，按震荡处理。")

    if avg_volatility_20 >= 3.5 and risk_level == "normal":
        risk_level = "elevated"
        reason_parts.append("波动水平偏高，仓位不宜打满。")

    return {
        "trade_date": signal_date,
        "trend": trend,
        "risk_level": risk_level,
        "reason": "".join(reason_parts),
        "features": {
            "universe": len(rows),
            "tradable": len(tradable_rows),
            "sampled": min(sample_limit, len(tradable_rows)),
            "history_coverage": round(coverage, 4),
            "up_ratio": round(up_ratio, 4),
            "avg_change_pct": round(avg_change_pct, 4),
            "avg_momentum_20_pct": round(avg_momentum_20, 4),
            "avg_volatility_20_pct": round(avg_volatility_20, 4),
        },
    }
