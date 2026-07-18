"""Bounded historical slices for cross-sectional model history completion.

This module plans older API training slices with the feature/label overlap
required by the ranking model. Callers may retain the completed history for
local reuse or use the original fail-closed cleanup mode; cleanup may never
delete dates inside the online window.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class TrainingSlice:
    core_dates: tuple[str, ...]
    fetch_dates: tuple[str, ...]


def build_training_slices(
    dates: Iterable[str], *, chunk_sessions: int = 60, lookback_sessions: int = 80, label_horizon: int = 20,
) -> list[TrainingSlice]:
    """Split chronological dates while retaining only necessary overlap."""
    ordered = sorted({str(x).replace("-", "")[:8] for x in dates if str(x).replace("-", "")[:8].isdigit()})
    size = max(20, min(int(chunk_sessions), 120))
    before = max(60, int(lookback_sessions))
    after = max(20, int(label_horizon))
    result: list[TrainingSlice] = []
    for start in range(0, len(ordered), size):
        core = ordered[start:start + size]
        if not core:
            continue
        fetch = ordered[max(0, start - before):min(len(ordered), start + len(core) + after)]
        result.append(TrainingSlice(tuple(core), tuple(fetch)))
    return result


def temporary_dates_for_cleanup(*, fetched_dates: Iterable[str], online_window_start: str) -> list[str]:
    """Return only old dates; current online data is never a cleanup target."""
    floor = str(online_window_start).replace("-", "")[:8]
    if len(floor) != 8 or not floor.isdigit():
        raise ValueError("online_window_start must be YYYYMMDD")
    dates = sorted({str(x).replace("-", "")[:8] for x in fetched_dates})
    if any(len(x) != 8 or not x.isdigit() for x in dates):
        raise ValueError("fetched_dates must contain YYYYMMDD values")
    return [value for value in dates if value < floor]


def consume_api_training_slice(
    training_slice: TrainingSlice, *, online_window_start: str,
    consume: Callable[[tuple[str, ...]], Any],
    retain_history: bool = False,
) -> Any:
    """Fetch one old slice, build snapshots, consume it, then always clean it.

    Imports are delayed so planning/tests do not initialise the data platform.
    The callback receives only core dates; overlap exists solely for rolling
    features and future labels and is never allowed to leak into duplicate
    training samples.
    """
    from app.data_platform.services.data_center import build_stock_daily_snapshot, purge_temporary_training_market_data, sync_training_market_date_vip, training_market_date_ready

    fetched = list(training_slice.fetch_dates)
    try:
        for trade_date in fetched:
            if not training_market_date_ready(trade_date):
                outcome = sync_training_market_date_vip(trade_date)
                if not isinstance(outcome, dict) or not bool(outcome.get("ok")):
                    raise RuntimeError(f"训练切片 {trade_date} 的 TuShare VIP 全市场批量拉取失败")
                snapshot = build_stock_daily_snapshot(trade_date)
                if not isinstance(snapshot, dict) or not bool(snapshot.get("ok")):
                    raise RuntimeError(f"训练切片 {trade_date} 快照构建失败")
        return consume(training_slice.core_dates)
    finally:
        # Cleanup is deliberately in finally: a rate-limit, feature failure,
        # or model exception must not turn into retained old market history.
        if not retain_history:
            purge_temporary_training_market_data(fetched_dates=fetched, online_window_start=online_window_start)
