"""Small-sample-safe statistics for formal strategy OOS comparisons."""

from __future__ import annotations

import hashlib
import math
import random
from statistics import NormalDist
from typing import Any


BOOTSTRAP_REPLICATIONS = 2_000
DSR_CONFIDENCE_THRESHOLD = 0.95


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


def deflated_sharpe_ratio(
    windows: list[dict[str, Any]],
    *,
    trial_count: int,
) -> dict[str, Any]:
    """Deflate window-level excess Sharpe for the number of tried strategies."""
    rows = [item for item in windows if bool(item.get("data_complete", True)) and bool(item.get("point_in_time_passed", True))]
    excess = [_number((item.get("metrics") or {}).get("excess_return")) for item in rows]
    sample_count = len(excess)
    trials = max(1, int(trial_count))
    mean = sum(excess) / sample_count if sample_count else 0.0
    centered = [value - mean for value in excess]
    variance = sum(value * value for value in centered) / max(1, sample_count - 1)
    standard_deviation = math.sqrt(max(0.0, variance))
    observed_sharpe = mean / standard_deviation if standard_deviation > 0 else math.inf if mean > 0 else 0.0
    second_moment = sum(value * value for value in centered) / sample_count if sample_count else 0.0
    if second_moment > 0:
        skewness = (sum(value ** 3 for value in centered) / sample_count) / (second_moment ** 1.5)
        kurtosis = (sum(value ** 4 for value in centered) / sample_count) / (second_moment ** 2)
    else:
        skewness = 0.0
        kurtosis = 3.0
    if sample_count >= 2 and math.isfinite(observed_sharpe):
        sharpe_standard_error = math.sqrt(max(
            1e-12,
            (1.0 - skewness * observed_sharpe + ((kurtosis - 1.0) / 4.0) * observed_sharpe ** 2)
            / (sample_count - 1),
        ))
    else:
        sharpe_standard_error = 0.0
    if trials > 1 and sharpe_standard_error > 0:
        euler_gamma = 0.5772156649015329
        normal = NormalDist()
        expected_max_sharpe = sharpe_standard_error * (
            (1.0 - euler_gamma) * normal.inv_cdf(1.0 - 1.0 / trials)
            + euler_gamma * normal.inv_cdf(1.0 - 1.0 / (trials * math.e))
        )
    else:
        expected_max_sharpe = 0.0
    if sample_count < 5:
        probability = None
    elif math.isinf(observed_sharpe):
        probability = 1.0
    elif sharpe_standard_error > 0:
        probability = NormalDist().cdf((observed_sharpe - expected_max_sharpe) / sharpe_standard_error)
    else:
        probability = 0.0
    return {
        "schema_version": "alis_deflated_sharpe_ratio_v1",
        "sample_count": sample_count,
        "trial_count": trials,
        "observed_window_excess_sharpe": round(observed_sharpe, 8) if math.isfinite(observed_sharpe) else "infinity",
        "expected_max_sharpe_under_selection": round(expected_max_sharpe, 8),
        "sharpe_standard_error": round(sharpe_standard_error, 8),
        "skewness": round(skewness, 8),
        "kurtosis": round(kurtosis, 8),
        "probability_sharpe_exceeds_selection_bias": round(probability, 8) if probability is not None else None,
        "confidence_threshold": DSR_CONFIDENCE_THRESHOLD,
        "passed": bool(probability is not None and probability >= DSR_CONFIDENCE_THRESHOLD),
        "limitation": "仅使用正式OOS窗口级超额收益；小样本结果必须与未来影子和搜索预算共同使用。",
    }


def paired_block_bootstrap_compounded_excess_ci(
    observations: list[dict[str, Any]],
    *,
    replications: int = BOOTSTRAP_REPLICATIONS,
) -> dict[str, Any]:
    """Bootstrap paired daily returns in short blocks to preserve local dependence."""
    pairs = [
        (_number(item.get("candidate_return")), _number(item.get("champion_return")))
        for item in observations
    ]
    sample_count = len(pairs)
    block_length = max(1, int(math.sqrt(sample_count))) if sample_count else 1
    if not pairs:
        return {
            "schema_version": "alis_shadow_paired_block_bootstrap_v1",
            "sample_count": 0, "block_length": block_length,
            "compounded_excess_ci_95": [None, None],
        }
    seed_source = ",".join(f"{candidate:.10f}:{champion:.10f}" for candidate, champion in pairs)
    seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(max(1, int(replications))):
        sampled: list[tuple[float, float]] = []
        while len(sampled) < sample_count:
            start = rng.randrange(sample_count)
            sampled.extend(pairs[(start + offset) % sample_count] for offset in range(block_length))
        candidate_equity = 1.0
        champion_equity = 1.0
        for candidate_return, champion_return in sampled[:sample_count]:
            candidate_equity *= 1.0 + candidate_return
            champion_equity *= 1.0 + champion_return
        estimates.append(candidate_equity - champion_equity)
    estimates.sort()
    low_index = int((len(estimates) - 1) * .025)
    high_index = int((len(estimates) - 1) * .975)
    return {
        "schema_version": "alis_shadow_paired_block_bootstrap_v1",
        "sample_count": sample_count,
        "block_length": block_length,
        "replications": max(1, int(replications)),
        "compounded_excess_ci_95": [round(estimates[low_index], 8), round(estimates[high_index], 8)],
    }


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
