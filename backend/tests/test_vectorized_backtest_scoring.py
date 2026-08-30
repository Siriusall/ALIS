"""Unit tests for vectorized backtest scoring and parallel evaluation."""

from __future__ import annotations

import pytest
from app.services.backtest_data_source import SnapshotRow
from app.services.backtest_engine import _score_rows


def test_score_rows_vectorized_minmax():
    rows = [
        SnapshotRow("000001", "2026-08-28", 10.0, 10.5, 9.5, 10.0, 10.0, 0.0, 1000, 10000, "A", 1.0),
        SnapshotRow("000002", "2026-08-28", 20.0, 20.5, 19.5, 20.0, 20.0, 0.0, 1000, 10000, "B", 1.0),
        SnapshotRow("000003", "2026-08-28", 30.0, 30.5, 29.5, 30.0, 30.0, 0.0, 1000, 10000, "C", 1.0),
    ]
    weights = {"momentum": 0.6, "value": 0.4}
    factor_values = {
        "000001": {"momentum": 10.0, "value": 1.0},
        "000002": {"momentum": 20.0, "value": 2.0},
        "000003": {"momentum": 30.0, "value": 3.0},
    }

    scored = _score_rows(rows, weights, factor_values, normalization="minmax")
    assert len(scored) == 3
    # Highest values should have score 1.0
    assert scored[0][0].symbol == "000003"
    assert pytest.approx(scored[0][1], 0.001) == 1.0
    # Lowest values should have score 0.0
    assert scored[2][0].symbol == "000001"
    assert pytest.approx(scored[2][1], 0.001) == 0.0


def test_score_rows_vectorized_rank_percentile():
    rows = [
        SnapshotRow("000001", "2026-08-28", 10.0, 10.5, 9.5, 10.0, 10.0, 0.0, 1000, 10000, "A", 1.0),
        SnapshotRow("000002", "2026-08-28", 20.0, 20.5, 19.5, 20.0, 20.0, 0.0, 1000, 10000, "B", 1.0),
    ]
    weights = {"factor_a": 1.0}
    factor_values = {
        "000001": {"factor_a": 5.0},
        "000002": {"factor_a": 10.0},
    }

    scored = _score_rows(rows, weights, factor_values, normalization="rank_percentile")
    assert len(scored) == 2
    assert scored[0][0].symbol == "000002"
    assert scored[0][1] > scored[1][1]
