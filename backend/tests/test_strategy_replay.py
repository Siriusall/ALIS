from __future__ import annotations

from app.services import strategy_replay
from app.services.backtest_engine import _apply_universe_controls, _score_rows
from app.services.backtest_data_source import BacktestDataSource, HistBar, SnapshotRow
from app.services.strategy_config import parse_strategy_content


class FakeReplayDataSource:
    def __init__(self) -> None:
        self.data_warnings = []
        self.fallback_events = []
        self._dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
        self.cutoffs = []

    def set_as_of_time(self, cutoff: str) -> None:
        self.cutoffs.append(cutoff)

    def point_in_time_sources(self) -> list[dict]:
        return [{"source": "fake", "cutoff": self.cutoffs[-1], "read_count": 2, "passed": True}]

    def get_available_trade_dates(self, start: str, end: str) -> list[str]:
        return self._dates

    def load_snapshot(self, date_ymd: str) -> list[SnapshotRow]:
        date = f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:8]}"
        day = self._dates.index(date)
        return [
            SnapshotRow("000001", date, 10 + day, 11 + day, 9 + day, 10.5 + day, 10 + day, 1.0, 1_000_000, 20_000_000, "平安银行"),
            SnapshotRow("000002", date, 8 + day, 9 + day, 7 + day, 8.4 + day, 8 + day, 0.8, 900_000, 16_000_000, "万科A"),
        ]

    def get_price_on_date(self, symbol: str, date: str, field: str = "close") -> float:
        return 100.0 + self._dates.index(date)

    def get_index_returns(self, symbol: str, date: str, lookback: int = 60) -> list[float]:
        return [0.001 for _ in range(min(lookback, 20))]

    def get_index_return(self, symbol: str, date: str, lookback: int = 20) -> float:
        return 0.0

    def should_rebalance(self, frequency: str, date: str, previous: str | None) -> bool:
        return True

    def get_daily_basic_map(self, date: str) -> dict:
        return {}

    def preload_histories(self, symbols: list[str], *, end_date: str, lookback: int) -> None:
        return None

    def get_hist_window(self, symbol: str, date: str, lookback: int) -> list[HistBar]:
        base = 10.0 if symbol == "000001" else 8.0
        return [HistBar(f"2026-06-{index:02d}", base + index * 0.05, base + index * 0.05, base + index * 0.05, base + index * 0.05, 1000, 10000) for index in range(1, 31)]


def test_strategy_replay_uses_t_plus_one_and_isolated_ledger(monkeypatch):
    monkeypatch.setattr(strategy_replay, "resolve_production_strategy_champion", lambda: {
        "champion_id": 99, "champion_version": "alis-champion-test", "strategy_id": 1,
        "strategy_controller_version_id": 31, "strategy_controller_version": "controller-test", "strategy_policy": {},
    })
    monkeypatch.setattr(strategy_replay, "get_strategy", lambda strategy_id: {
        "id": strategy_id,
        "name": "重放测试策略",
        "type": "formula",
        "content": {"score": {"factors": {"momentum_20": 1.0}}, "rebalance": {"freq": "daily", "top_n": 2}, "lookback_days": 30},
    })

    events: list[dict] = []
    result = strategy_replay.run_strategy_replay(
        strategy_id=1,
        start_date="2026-07-01",
        end_date="2026-07-03",
        initial_cash=200_000,
        data_source=FakeReplayDataSource(),
        progress_callback=events.append,
    )

    assert result["schema_version"] == "strategy_replay_v1"
    assert result["component_contract"]["strategy"] == "StrategyController"
    assert result["production_strategy_version"] == "alis-champion-test"
    assert result["component_contract"]["signal_execution"] == "T close signal / T+1 open execution"
    assert result["decisions"][0]["signal_date"] == "2026-07-01"
    assert result["decisions"][0]["execute_date"] == "2026-07-02"
    assert result["decisions"][0]["strategy_action_contracts"]
    assert result["trades"]
    assert all(trade["date"] != "2026-07-01" for trade in result["trades"])
    assert result["summary"]["ending_equity"] > 0
    assert result["summary"]["total_fees"] > 0
    assert result["decisions"][0]["data_cutoff"] == "2026-07-01T15:00:00"
    assert result["data_quality"]["point_in_time"]["passed"] is True
    assert result["data_quality"]["point_in_time"]["audits"][0]["decision_cutoff"] == "2026-07-01T15:00:00"
    assert events[0]["completed_days"] == 0
    assert events[-1]["completed_days"] == 3
    assert events[-1]["progress"] == 1.0


def test_strategy_replay_rejects_single_day_range(monkeypatch):
    source = FakeReplayDataSource()
    source._dates = ["2026-07-01"]
    monkeypatch.setattr(strategy_replay, "resolve_production_strategy_champion", lambda: {
        "champion_id": 99, "champion_version": "alis-champion-test", "strategy_id": 1,
        "strategy_controller_version_id": 31, "strategy_controller_version": "controller-test", "strategy_policy": {},
    })
    monkeypatch.setattr(strategy_replay, "get_strategy", lambda strategy_id: {"id": strategy_id, "name": "x", "type": "formula", "content": {}})

    try:
        strategy_replay.run_strategy_replay(strategy_id=1, start_date="2026-07-01", end_date="2026-07-01", data_source=source)
    except ValueError as exc:
        assert "至少需要两个交易日" in str(exc)
    else:
        raise AssertionError("single-day replay must fail")


def test_snapshot_prices_consume_adj_factor_without_future_anchor(monkeypatch):
    source = BacktestDataSource()
    payloads = {
        "20240102": [{
            "ts_code": "000001.SZ", "trade_date": "20240102", "name": "x",
            "close": 10.0, "change_pct": 0.0, "volume": 1.0, "turnover": 1.0,
            "available_time": "2024-01-02T15:00:00",
            "daily_payload_json": '{"open":10,"high":10,"low":10,"close":10,"pre_close":10}',
            "adj_payload_json": '{"adj_factor":2}', "adj_available_time": "2024-01-02T15:00:00",
        }],
        "20240103": [{
            "ts_code": "000001.SZ", "trade_date": "20240103", "name": "x",
            "close": 5.0, "change_pct": 0.0, "volume": 1.0, "turnover": 1.0,
            "available_time": "2024-01-03T15:00:00",
            "daily_payload_json": '{"open":5,"high":5,"low":5,"close":5,"pre_close":5}',
            "adj_payload_json": '{"adj_factor":4}', "adj_available_time": "2024-01-03T15:00:00",
        }],
    }
    monkeypatch.setattr(source, "_postgres_snapshot_rows", lambda ymd: payloads[ymd])

    first = source.load_snapshot("20240102")[0]
    second = source.load_snapshot("20240103")[0]

    assert first.close == 10.0
    assert second.close == 10.0
    assert second.adj_factor == 4.0


def test_challenger_controls_cap_weight_and_turnover():
    controlled = strategy_replay._apply_challenger_controls(
        {"A": 0.70, "B": 0.20, "C": 0.10},
        {"A": 0.10, "B": 0.10, "C": 0.10},
        {"max_single_weight": 0.40, "max_rebalance_turnover": 0.20, "min_weight_change": 0.0},
    )
    assert max(controlled.values()) <= 0.40
    turnover = sum(abs(controlled.get(symbol, 0) - 0.10) for symbol in ("A", "B", "C"))
    assert turnover <= 0.200001


def test_initial_deployment_can_bypass_turnover_ramp():
    controlled = strategy_replay._apply_challenger_controls(
        {"A": .5, "B": .5},
        {},
        {
            "max_single_weight": .5,
            "max_rebalance_turnover": .2,
            "initial_deployment_turnover_exempt": True,
        },
    )
    assert sum(controlled.values()) == 1.0


def test_turnover_blend_cannot_restore_an_overweight_position():
    controlled = strategy_replay._apply_challenger_controls(
        {"A": .05, "B": .95},
        {"A": .20, "B": .80},
        {
            "max_single_weight": .08,
            "max_rebalance_turnover": .02,
            "min_weight_change": 0.0,
        },
    )

    assert max(controlled.values()) <= .08


def test_equal_weight_baseline_bypasses_cvar_optimizer(monkeypatch):
    monkeypatch.setattr(strategy_replay, "resolve_production_strategy_champion", lambda: {
        "champion_id": 99, "champion_version": "alis-champion-test", "strategy_id": 1,
        "strategy_controller_version_id": 31, "strategy_controller_version": "controller-test", "strategy_policy": {},
    })
    monkeypatch.setattr(strategy_replay, "get_strategy", lambda strategy_id: {
        "id": strategy_id,
        "name": "等权基线",
        "type": "formula",
        "content": {
            "portfolio_method": "equal_weight",
            "score": {"factors": {"momentum_20": 1.0}},
            "rebalance": {"freq": "weekly", "top_n": 2},
            "lookback_days": 30,
        },
    })
    monkeypatch.setattr(strategy_replay, "two_group_cvar_optimizer", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CVaR should not run")))

    result = strategy_replay.run_strategy_replay(
        strategy_id=3, start_date="2026-07-01", end_date="2026-07-03",
        data_source=FakeReplayDataSource(),
    )

    weights = result["decisions"][0]["target_weights"]
    assert weights == {"000001": 0.5, "000002": 0.5}
    assert result["component_contract"]["allocation_method"] == "equal_weight"
    assert result["component_contract"]["optimizer"] == "equal_weight_baseline_v1"


def test_rank_percentile_scoring_and_small_cap_exclusion_are_deterministic():
    rows = [
        SnapshotRow("A", "2026-07-01", 1, 1, 1, 1, 1, 0, 1, 1, "A"),
        SnapshotRow("B", "2026-07-01", 1, 1, 1, 1, 1, 0, 1, 1, "B"),
        SnapshotRow("C", "2026-07-01", 1, 1, 1, 1, 1, 0, 1, 1, "C"),
        SnapshotRow("D", "2026-07-01", 1, 1, 1, 1, 1, 0, 1, 1, "D"),
    ]
    values = {
        "A": {"float_mv": 10, "earnings_yield": .10},
        "B": {"float_mv": 20, "earnings_yield": .20},
        "C": {"float_mv": 30, "earnings_yield": .30},
        "D": {"float_mv": 10_000_000, "earnings_yield": .40},
    }

    universe = _apply_universe_controls(
        rows, values, {"exclude_smallest_float_mv_pct": .25},
    )
    ranked = _score_rows(
        universe, {"earnings_yield": 1.0}, values, normalization="rank_percentile",
    )

    assert [row.symbol for row in universe] == ["B", "C", "D"]
    assert [row.symbol for row, _score in ranked] == ["D", "C", "B"]
    assert ranked[0][1] == 1.0


def test_buy_hold_rank_buffer_retains_existing_name_but_blocks_new_outside_buy_rank():
    rows = [
        SnapshotRow(symbol, "2026-07-01", 1, 1, 1, 1, 1, 0, 1, 1, symbol)
        for symbol in ("A", "B", "C", "D")
    ]
    ranked = [(row, 1.0 - index * .1) for index, row in enumerate(rows)]
    histories = {
        row.symbol: [HistBar("2026-06-01", 1, 1, 1, 1, 1, 1) for _ in range(10)]
        for row in rows
    }

    selected = strategy_replay._select_rank_buffer_candidates(
        ranked,
        held_symbols={"C"},
        top_n=2,
        controls={"buy_rank_max": 2, "hold_rank_max": 3},
        histories=histories,
    )

    assert [row.symbol for row, _score in selected] == ["A", "C"]


def test_entry_guard_blocks_extreme_five_day_fall_but_does_not_force_sell_existing_holding():
    rows = [
        SnapshotRow(symbol, "2026-07-01", 1, 1, 1, 1, 1, 0, 1, 1, symbol)
        for symbol in ("A", "B")
    ]
    ranked = [(rows[0], 1.0), (rows[1], .9)]
    falling = [10.0, 9.5, 9.0, 8.5, 8.0, 7.5]
    histories = {
        symbol: [HistBar(f"2026-06-0{index + 1}", price, price, price, price, 1, 1) for index, price in enumerate(falling)]
        for symbol in ("A", "B")
    }
    controls = {"buy_rank_max": 2, "hold_rank_max": 2, "min_entry_5d_return": -.10}

    new_selection = strategy_replay._select_rank_buffer_candidates(
        ranked, held_symbols=set(), top_n=2, controls=controls, histories=histories,
    )
    held_selection = strategy_replay._select_rank_buffer_candidates(
        ranked, held_symbols={"A"}, top_n=2, controls=controls, histories=histories,
    )

    assert new_selection == []
    assert [row.symbol for row, _score in held_selection] == ["A"]


def test_candidate_config_parses_robust_scoring_and_optimizer_view_source():
    config = parse_strategy_content(
        {
            "score": {
                "normalization": "rank_percentile",
                "factors": {"earnings_yield": .6, "downside_volatility_60": -.4},
            },
            "expected_return": {"source": "ranking_score", "scale": .06},
        },
        "formula",
    )

    assert config.score_normalization == "rank_percentile"
    assert config.expected_return_source == "ranking_score"
    assert config.expected_return_scale == .06


def test_market_exposure_policy_has_no_implicit_neutral_cap_and_can_scale_high_volatility():
    unchanged, trace = strategy_replay._apply_market_exposure_policy(
        base_equity_target=1.0,
        exposure_policy={"minimum_equity_weight": .5, "maximum_equity_weight": 1.0},
        market_profile={"risk_budget_multiplier": .9},
        change_alarm={"risk_multiplier": .7},
        benchmark_returns=[.01, -.01] * 10,
    )
    managed, managed_trace = strategy_replay._apply_market_exposure_policy(
        base_equity_target=1.0,
        exposure_policy={
            "volatility_overlay": {
                "lookback_sessions": 20,
                "target_annualized": .10,
                "minimum_scale": .50,
                "maximum_scale": 1.0,
            },
        },
        market_profile={},
        change_alarm={},
        benchmark_returns=[.02, -.02] * 10,
    )

    assert unchanged == 1.0
    assert trace["final_equity_target"] == 1.0
    assert .49 <= managed <= .51
    assert managed_trace["volatility_scale"] == .5


def test_market_risk_context_uses_volume_breadth_profiles_and_change_alarm():
    snapshot = [
        SnapshotRow("A", "2026-07-01", 1, 1, 1, 1, 1, 2.0, 1, 150, "A"),
        SnapshotRow("B", "2026-07-01", 1, 1, 1, 1, 1, -1.0, 1, 50, "B"),
    ]

    result = strategy_replay._market_risk_context(
        snapshot,
        amount_history=[100.0, 100.0],
        signal_history=[0.0, 0.0, 0.0],
        previous_state="S1",
    )

    assert result["observations"]["breadth"] == .5
    assert result["observations"]["volume_ratio"] == 2.0
    assert result["market_state"] in {"S0", "S1", "S2", "S3"}
    assert result["market_profile"]["parent_state"] == result["market_state"]
    assert result["change_alarm"]["method"] == "bocpd_gaussian_v1"


def test_dual_trend_selection_state_switches_factor_mix_without_changing_exposure():
    content = {
        "score": {"normalization": "rank_percentile", "factors": {"earnings_yield": 1.0}},
        "selection_regime": {
            "method": "benchmark_dual_trend_v1",
            "positive_short_threshold": .03,
            "positive_long_threshold": .06,
            "negative_short_threshold": -.03,
            "negative_long_threshold": -.06,
        },
        "regime_scores": {
            "S0": {"normalization": "rank_percentile", "factors": {"relative_strength_60": .7, "earnings_yield": .3}},
            "S2": {"normalization": "rank_percentile", "factors": {"downside_volatility_60": -1.0}},
        },
    }

    bull = strategy_replay.resolve_selection_market_state(
        content,
        transparent_state="S1",
        short_benchmark_return=.05,
        long_benchmark_return=.10,
    )
    bear = strategy_replay.resolve_selection_market_state(
        content,
        transparent_state="S1",
        short_benchmark_return=-.04,
        long_benchmark_return=.01,
    )

    assert bull == "S0"
    assert bear == "S2"
    assert strategy_replay.get_strategy_score_for_market_state(content, "formula", bull) == {
        "relative_strength_60": .7,
        "earnings_yield": .3,
    }
    assert set(strategy_replay.get_strategy_factor_names(content, "formula")) == {
        "earnings_yield", "relative_strength_60", "downside_volatility_60",
    }
