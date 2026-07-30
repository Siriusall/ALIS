"""Closed compatibility boundary for the retired generic ``main`` model.

The former module trained and promoted one generic cross-sectional return
model with lift and hit-rate gates.  That protocol has no authority after the
task-native five-model evaluation migration.  The reusable point-in-time data
builders remain here only because factor registration and streaming-data
contract tests share them.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from app.core.exchange_groups import matches_exchange_group
from app.services.backtest_data_source import BacktestDataSource, SnapshotRow
from app.services.backtest_engine import _factor_value
from app.services.model_evaluation_contracts import SCHEMA_VERSION
from app.services.streaming_training import (
    build_training_slices,
    consume_api_training_slice,
)


FEATURES = [
    "momentum_5",
    "momentum_20",
    "volatility_20",
    "volume_ratio_5",
    "turnover",
    "float_mv",
    "total_mv",
    "pe_ttm",
    "pb",
    "change_pct",
]


def _require_compact_api_training_history(
    refresh: dict[str, Any],
) -> dict[str, Any]:
    """Validate persisted full-market history and broad-index anchors."""

    sources = list(refresh.get("sources") or [])
    source_ok = bool(sources) and all(
        bool(item.get("ok"))
        for item in sources
        if isinstance(item, dict)
    )
    materialized = refresh.get("materialized") or {}
    written = int(materialized.get("written") or 0)
    if not bool(refresh.get("ok")) or not source_ok or written <= 0:
        raise RuntimeError(
            "训练所需的6-10年紧凑市场特征API刷新失败；"
            "未使用本地旧数据替代训练"
        )
    return {
        "contract": str(
            refresh.get("contract")
            or "full_market_daily_aggregates_plus_broad_index_anchors_v2"
        ),
        "range": list(refresh.get("range") or []),
        "sources": [
            {
                "ts_code": item.get("ts_code"),
                "rows_written": item.get("rows_written"),
            }
            for item in sources
            if isinstance(item, dict)
        ],
        "features_written": written,
        "full_market_history_persisted": True,
    }


def _safe_float(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _future_label(
    row: SnapshotRow,
    future_row: SnapshotRow | None,
) -> float | None:
    if row.close <= 0:
        return None
    future_close = _safe_float(
        getattr(future_row, "close", 0) if future_row else 0
    )
    if future_close <= 0:
        return None
    raw_return = future_close / row.close - 1.0
    risk_penalty = min(
        abs(float(row.change_pct or 0)) / 100.0,
        0.2,
    ) * 0.15
    return raw_return - risk_penalty


def _feature_row(
    data_source: BacktestDataSource,
    row: SnapshotRow,
    date: str,
    daily_basic: dict[str, dict[str, float]],
) -> list[float]:
    history = data_source.get_hist_window(row.symbol, date, 60)
    return [
        _safe_float(_factor_value(factor, row, history, daily_basic))
        for factor in FEATURES
    ]


def _build_dataset(
    *,
    ds: BacktestDataSource,
    dates: list[str],
    horizon: int,
    exchange_group: str,
    progress: Callable[[str, float, str], None],
) -> tuple[list[list[float]], list[float], list[dict[str, Any]]]:
    """Build a point-in-time labeled slice without training a model."""

    x_rows: list[list[float]] = []
    y_rows: list[float] = []
    metadata: list[dict[str, Any]] = []
    usable = dates[:-horizon] if horizon > 0 else dates
    total = max(1, len(usable))
    for index, date in enumerate(usable):
        progress(
            "building_dataset",
            0.05 + 0.45 * (index / total),
            f"构建样本 {index + 1}/{total} {date}",
        )
        future_date = dates[index + horizon]
        future_map = {
            item.symbol: item
            for item in ds.load_snapshot(future_date.replace("-", ""))
        }
        snapshot = ds.load_snapshot(date.replace("-", ""))
        if exchange_group != "ALL":
            snapshot = [
                item
                for item in snapshot
                if matches_exchange_group(item.symbol, exchange_group)
            ]
        daily_basic = ds.get_daily_basic_map(date.replace("-", ""))
        for row in snapshot:
            if row.volume <= 0 or row.close <= 0:
                continue
            label = _future_label(row, future_map.get(row.symbol))
            if label is None:
                continue
            x_rows.append(_feature_row(ds, row, date, daily_basic))
            y_rows.append(label)
            metadata.append({
                "date": date,
                "symbol": row.symbol,
                "future_date": future_date,
            })
    return x_rows, y_rows, metadata


def _build_multi_horizon_dataset(
    *,
    ds: BacktestDataSource,
    dates: list[str],
    horizons: list[int],
    exchange_group: str,
    progress: Callable[[float, str], None],
) -> dict[
    int,
    tuple[list[list[float]], list[float], list[dict[str, Any]]],
]:
    """Build all label horizons once from the same point-in-time features."""

    result = {horizon: ([], [], []) for horizon in horizons}
    if not dates or not horizons:
        return result
    total = max(1, len(dates) - min(horizons))
    for index, date in enumerate(dates):
        valid_horizons = [
            horizon
            for horizon in horizons
            if index + horizon < len(dates)
        ]
        if not valid_horizons:
            break
        progress(index / total, f"处理交易日 {index + 1}/{total} · {date}")
        snapshot = ds.load_snapshot(date.replace("-", ""))
        if exchange_group != "ALL":
            snapshot = [
                row
                for row in snapshot
                if matches_exchange_group(row.symbol, exchange_group)
            ]
        usable_rows = [
            row
            for row in snapshot
            if row.volume > 0 and row.close > 0
        ]
        if not usable_rows:
            continue
        daily_basic = ds.get_daily_basic_map(date.replace("-", ""))
        feature_by_symbol = {
            row.symbol: _feature_row(ds, row, date, daily_basic)
            for row in usable_rows
        }
        for horizon in valid_horizons:
            future_date = dates[index + horizon]
            future_map = {
                row.symbol: row
                for row in ds.load_snapshot(
                    future_date.replace("-", "")
                )
            }
            x_rows, y_rows, metadata = result[horizon]
            for row in usable_rows:
                label = _future_label(row, future_map.get(row.symbol))
                if label is None:
                    continue
                x_rows.append(feature_by_symbol[row.symbol])
                y_rows.append(label)
                metadata.append({
                    "date": date,
                    "symbol": row.symbol,
                    "future_date": future_date,
                })
    progress(1.0, f"本地样本构建完成 · {len(dates)} 个交易日")
    return result


def _stream_historical_datasets(
    *,
    historical_dates: list[str],
    label_bridge_dates: list[str],
    online_window_start: str,
    horizons: list[int],
    exchange_group: str,
    progress: Callable[[str, float, str], None],
) -> dict[
    int,
    tuple[list[list[float]], list[float], list[dict[str, Any]]],
]:
    """Stream persisted data slices without retaining old market rows."""

    result = {horizon: ([], [], []) for horizon in horizons}
    if not historical_dates:
        return result
    slices = build_training_slices(
        historical_dates,
        chunk_sessions=60,
        lookback_sessions=80,
        label_horizon=max(horizons),
    )
    if slices and label_bridge_dates:
        last = slices[-1]
        slices[-1] = type(last)(
            core_dates=last.core_dates,
            fetch_dates=tuple(sorted(set(
                last.fetch_dates
                + tuple(label_bridge_dates[:max(horizons)])
            ))),
        )
    for index, training_slice in enumerate(slices):
        progress(
            "local_training_data",
            0.12 + 0.30 * (index / max(1, len(slices))),
            f"本地训练切片 {index + 1}/{len(slices)} · 准备数据",
        )

        def consume(core_dates: tuple[str, ...]) -> None:
            core = set(core_dates)
            slice_dates = [
                f"{date[:4]}-{date[4:6]}-{date[6:8]}"
                for date in training_slice.fetch_dates
            ]
            datasets = _build_multi_horizon_dataset(
                ds=BacktestDataSource(),
                dates=slice_dates,
                horizons=horizons,
                exchange_group=exchange_group,
                progress=lambda fraction, message: progress(
                    "local_training_data",
                    0.12 + 0.30 * (
                        (index + fraction) / max(1, len(slices))
                    ),
                    (
                        f"本地训练切片 {index + 1}/{len(slices)}"
                        f" · {message}"
                    ),
                ),
            )
            for horizon in horizons:
                x_rows, y_rows, metadata = datasets[horizon]
                kept = [
                    (x_values, y_value, item)
                    for x_values, y_value, item in zip(
                        x_rows,
                        y_rows,
                        metadata,
                    )
                    if item["date"].replace("-", "") in core
                ]
                if kept:
                    xs, ys, items = zip(*kept)
                    result[horizon][0].extend(xs)
                    result[horizon][1].extend(ys)
                    result[horizon][2].extend(items)

        consume_api_training_slice(
            training_slice,
            online_window_start=online_window_start,
            consume=consume,
            retain_history=True,
        )
    return result


def run_model_training(
    *,
    run_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    exchange_group: str = "HS",
) -> dict[str, Any]:
    """Reject the removed generic model protocol without creating a run."""

    del run_id, start_date, end_date, exchange_group
    return {
        "ok": False,
        "started": False,
        "status": "retired_legacy_generic_model_protocol",
        "reason": "replaced_by_task_native_five_model_governance",
        "replacement_contract": SCHEMA_VERSION,
        "production_change_forbidden": True,
    }
