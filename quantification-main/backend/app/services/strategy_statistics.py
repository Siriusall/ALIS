"""Small-sample-safe statistics for formal strategy OOS comparisons."""

from __future__ import annotations

import hashlib
import math
import random
from typing import Any


BOOTSTRAP_REPLICATIONS = 2_000


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _two_sided_sign_pvalue(wins: int, trials: int) -> float | None:
    if trials <= 0:
        return None
    lower = min(wins, trials - wins)
    probability = sum(math.comb(trials, k) for k in range(lower + 1)) / (2 ** trials)
    return round(min(1.0, 2.0 * probability), 8)


def compare_strategy_windows(windows: list[dict[str, Any]], *, replications: int = BOOTSTRAP_REPLICATIONS) -> dict[str, Any]:
    """Compare strategy and benchmark returns without claiming small-N proof."""
    rows = [item for item in windows if bool(item.get("data_complete", True)) and bool(item.get("point_in_time_passed", True))]
    excess = [_number((item.get("metrics") or {}).get("excess_return")) for item in rows]
    drawdown_delta = [
        _number((item.get("metrics") or {}).get("max_drawdown")) - _number((item.get("metrics") or {}).get("benchmark_max_drawdown"))
        for item in rows
    ]
    n = len(excess)
    mean = sum(excess) / n if n else None
    positive = sum(value > 0 for value in excess)
    seed_source = ",".join(f"{value:.10f}" for value in excess)
    seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    samples = [sum(rng.choice(excess) for _ in range(n)) / n for _ in range(max(1, int(replications)))] if n else []
    samples.sort()
    low_index = int((len(samples) - 1) * .025) if samples else 0
    high_index = int((len(samples) - 1) * .975) if samples else 0
    stability = positive / n if n else 0.0
    gates = {
        "minimum_windows_for_descriptive_comparison": n >= 5,
        "positive_mean_excess_return": bool(mean is not None and mean > 0),
        "majority_windows_outperform": stability >= .60,
        "bootstrap_ci_lower_positive": bool(samples and samples[low_index] > 0),
    }
    ordered = sorted(excess)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2 if n else None
    return {
        "schema_version": "alis_strategy_oos_statistics_v1",
        "window_count": n,
        "mean_excess_return": round(mean, 8) if mean is not None else None,
        "median_excess_return": round(median, 8) if median is not None else None,
        "positive_window_ratio": round(stability, 8),
        "sign_test_two_sided_pvalue": _two_sided_sign_pvalue(positive, n),
        "bootstrap_mean_excess_ci_95": [round(samples[low_index], 8), round(samples[high_index], 8)] if samples else [None, None],
        "mean_drawdown_delta": round(sum(drawdown_delta) / n, 8) if n else None,
        "gates": gates,
        "statistical_support": all(gates.values()),
        "limitation": "多窗口统计仅提供可解释证据；不替代正式OOS、未来影子、成本和行为硬门。",
    }
