from __future__ import annotations

from app.services import strategy_replay
from app.services.backtest_data_source import BacktestDataSource, HistBar, SnapshotRow


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

    result = strategy_replay.run_strategy_replay(
        strategy_id=1,
        start_date="2026-07-01",
        end_date="2026-07-03",
        initial_cash=200_000,
        data_source=FakeReplayDataSource(),
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
