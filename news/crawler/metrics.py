from __future__ import annotations

import math
import statistics
from typing import Iterable


def safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def cagr(values: Iterable[float], years: int) -> float | None:
    seq = [v for v in values if v is not None and v > 0]
    if len(seq) < years + 1:
        return None
    start = seq[-(years + 1)]
    end = seq[-1]
    if start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def volatility_coefficient(values: Iterable[float]) -> float | None:
    seq = [v for v in values if v is not None]
    if len(seq) < 2:
        return None
    mean = statistics.mean(seq)
    if mean == 0:
        return None
    return statistics.pstdev(seq) / abs(mean)


def percentile_rank(current: float | None, history: Iterable[float]) -> float | None:
    if current is None:
        return None
    seq = sorted(v for v in history if v is not None and v > 0)
    if not seq:
        return None
    count_le = sum(1 for v in seq if v <= current)
    return count_le / len(seq)


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def infer_cyclicality(volatility: float | None) -> str | None:
    if volatility is None:
        return None
    if volatility >= 0.5:
        return "强周期"
    if volatility >= 0.25:
        return "弱周期"
    return "稳定型"
