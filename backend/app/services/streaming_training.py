"""Bounded historical slices for cross-sectional model history completion.

This module plans older API training slices with the feature/label overlap
required by the ranking model. Callers may retain the completed history for
local reuse.  The historical market data is shared with replay and decision
consumers, so the post-slice cleanup hook is audit-only and never deletes it.
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
    """Fetch one old slice, build snapshots, consume it, then audit retention.

    Imports are delayed so planning/tests do not initialise the data platform.
    The callback receives only core dates; overlap exists solely for rolling
    features and future labels and is never allowed to leak into duplicate
    training samples.
    """
    from app.data_platform.services.data_center import build_stock_daily_snapshot, purge_temporary_training_market_data, training_market_date_ready

    fetched = list(training_slice.fetch_dates)
    try:
        for trade_date in fetched:
            if not training_market_date_ready(trade_date):
                # Restored PostgreSQL history may already contain all three raw
                # datasets while the derived daily snapshot has not been built.
                # Materialize locally first; only genuine raw-data gaps may call
                # TuShare.  This avoids re-downloading complete historical days.
                local_snapshot = build_stock_daily_snapshot(trade_date)
                if isinstance(local_snapshot, dict) and bool(local_snapshot.get("ok")) and training_market_date_ready(trade_date):
                    continue
                raise RuntimeError(f"训练切片 {trade_date} 本地数据不完整；严格本地训练禁止调用 TuShare")
        return consume(training_slice.core_dates)
    finally:
        # The compatibility hook is deliberately in finally so failures leave
        # an explicit retention audit.  Shared authoritative market history is
        # never deleted; isolated scratch storage must be cleaned separately.
        if not retain_history:
            purge_temporary_training_market_data(fetched_dates=fetched, online_window_start=online_window_start)
