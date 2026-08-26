import pytest
from types import SimpleNamespace

from app.services.backtest_data_source import SnapshotRow
from app.services import quant_run_contracts as contracts
from app.services import quant_run_engine


def test_quant_run_engine_preserves_public_contract_imports():
    assert quant_run_engine.HistoricalDataMissingError is contracts.HistoricalDataMissingError
    assert quant_run_engine.ReplayRiskGateError is contracts.ReplayRiskGateError
    assert quant_run_engine.QuantRunExecResult is contracts.QuantRunExecResult


def test_ledger_contract_blocks_mutation_with_a_stable_error(monkeypatch):
    monkeypatch.setattr(
        "app.services.simulation_ledger_audit.audit_simulation_ledger",
        lambda **_kwargs: {"ok": False, "issues": [{"message": "cash drift"}]},
    )
    with pytest.raises(contracts.ReplayRiskGateError) as exc:
        contracts.require_consistent_simulation_ledger()
    assert exc.value.code == "paused_simulation_ledger_drift"
    assert "cash drift" in str(exc.value)


def test_future_only_signal_snapshot_is_a_hard_block():
    future = SnapshotRow(
        symbol="000001",
        date="2026-07-23",
        open=10,
        high=10,
        low=10,
        close=10,
        prev_close=10,
        change_pct=0,
        volume=1,
        turnover=1,
    )

    class DataSource:
        def load_snapshot(self, _date):
            return [future]

    with pytest.raises(contracts.ReplayRiskGateError) as exc:
        quant_run_engine._load_validated_signal_rows(
            DataSource(),
            "2026-07-22",
            strict_no_future=True,
        )

    assert exc.value.code == "future_guard_signal_snapshot"


def test_formal_selection_requires_a_passed_point_in_time_source_audit():
    source = quant_run_engine.BacktestDataSource()
    source._record_pit_failure(  # noqa: SLF001 - hard-gate contract
        "snapshot.daily_structured", "normalized_snapshot_version_missing",
    )
    with pytest.raises(contracts.ReplayRiskGateError) as exc:
        quant_run_engine._require_formal_pit_source(
            source, "snapshot.daily_structured", signal_date="20260722",
        )
    assert exc.value.code == "point_in_time_source_unavailable"


def test_open_change_uses_open_and_previous_close_only():
    row = SnapshotRow(
        symbol="000001",
        date="2026-07-23",
        open=11,
        high=20,
        low=5,
        close=20,
        prev_close=10,
        change_pct=100,
        volume=999999,
        turnover=1,
    )

    assert quant_run_engine._open_change_pct(row) == pytest.approx(10.0)


def test_execution_suspension_check_consumes_real_volume():
    suspended = SnapshotRow(
        symbol="000001",
        date="2026-07-23",
        open=10,
        high=10,
        low=10,
        close=10,
        prev_close=10,
        change_pct=0,
        volume=0,
        turnover=0,
    )

    assert quant_run_engine._execution_volume(suspended) == 0.0


def test_portfolio_state_blocks_cost_price_when_execution_market_data_is_missing(monkeypatch):
    monkeypatch.setattr(quant_run_engine, "get_account_balance", lambda: 100_000.0)
    monkeypatch.setattr(
        quant_run_engine,
        "get_positions",
        lambda: [{"symbol": "000001", "quantity": 100, "price": 9.8}],
    )
    monkeypatch.setattr(
        quant_run_engine,
        "point_in_time_position_market_series",
        lambda **_kwargs: {
            "price": 9.8,
            "price_source": "position_cost_fallback",
            "observation_error": "strict_snapshot_trade_date_missing",
        },
    )

    with pytest.raises(contracts.ReplayRiskGateError) as exc:
        quant_run_engine._portfolio_state(object(), "20260815", execution_rows={})

    assert exc.value.code == "paused_position_market_data_not_ready"


def test_defensive_execution_row_reads_exact_fallback_bar_available_now(monkeypatch):
    captured = {}

    def fake_bar(*args, **kwargs):
        captured.update(kwargs)
        return {"trade_date": "20260813", "open": 1.0, "pre_close": 0.99, "vol": 100}

    monkeypatch.setattr("app.services.defensive_assets.defensive_asset_bar", fake_bar)
    row = quant_run_engine._defensive_execution_row(
        "510880", ts_code="510880.SH", name="ETF", execute_date="20260813",
        require_exact_date=True,
        observation_cutoff="2026-08-13T16:00:00+08:00",
    )
    assert row is not None
    assert captured["exact_date"] is True
    assert captured["available_as_of_time"] == "2026-08-13T16:00:00+08:00"


@pytest.mark.parametrize(
    ("frozen_strategy_id", "frozen_signal_date", "frozen_execute_date"),
    [
        (2, "20260724", "20260727"),
        (1, "20260723", "20260727"),
        (1, "20260724", "20260728"),
    ],
)
def test_unified_plan_identity_mismatch_rejects_before_market_or_execution(
    monkeypatch,
    frozen_strategy_id,
    frozen_signal_date,
    frozen_execute_date,
):
    class DataSource:
        def set_as_of_time(self, _value):
            return None

        def load_snapshot(self, _value):
            raise AssertionError("identity mismatch must reject before reading market data")

    normalized_plan = {
        "is_unified_frozen_plan": True,
        "target_symbols": [],
        "target_weights": {},
        "pretrade_controller": {},
        "decision_contract": {
            "schema_version": "alis_unified_daily_decision_v2",
            "data_as_of_time": "2026-07-24T15:00:00+08:00",
            "selection_snapshot": {"strategy_id": frozen_strategy_id},
            "signal_date": frozen_signal_date,
            "planned_execute_date": frozen_execute_date,
        },
    }
    monkeypatch.setattr(quant_run_engine, "BacktestDataSource", DataSource)
    monkeypatch.setattr(quant_run_engine, "_require_consistent_simulation_ledger", lambda: None)
    monkeypatch.setattr(
        quant_run_engine,
        "champion_execution_gate",
        lambda: {"status": "active", "mode": "normal"},
    )
    monkeypatch.setattr(
        quant_run_engine,
        "normalize_authoritative_plan",
        lambda _plan: normalized_plan,
    )
    monkeypatch.setattr(
        quant_run_engine,
        "simulate_open_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity mismatch must reject before execution")
        ),
    )

    with pytest.raises(contracts.ReplayRiskGateError) as exc:
        quant_run_engine.run_for_trade_date(
            strategy_id=1,
            signal_date="20260724",
            execute_date="20260727",
            top_n=20,
            budget_ratio=1.0,
            authoritative_plan={"raw": True},
        )

    assert exc.value.code == "invalid_authoritative_plan"


def test_unified_t1_execution_does_not_rerun_investment_opinion(monkeypatch):
    loaded_dates = []
    signal_row = SnapshotRow(
        symbol="000001", date="20260724", open=10, high=10, low=10,
        close=10, prev_close=9.9, change_pct=1, volume=1000, turnover=10000,
    )
    execute_row = SnapshotRow(
        symbol="000001", date="20260727", open=10.1, high=10.2, low=10,
        close=10.1, prev_close=10, change_pct=1, volume=1000, turnover=10000,
    )

    class DataSource:
        def set_as_of_time(self, _value):
            return None

        def load_snapshot(self, value):
            loaded_dates.append(str(value).replace("-", ""))
            return [signal_row] if str(value).replace("-", "") == "20260724" else [execute_row]

    frozen_controller = {
        "schema_version": "alis_strategy_controller_pretrade_v1",
        "champion_execution_guard": {
            "status": "quarantined", "mode": "research_only_sell_reduce",
        },
        "actions": [{
            "symbol": "000001",
            "action": "buy_candidate",
            "permission_allowed": False,
            "min_weight": 0.0,
            "max_weight": 0.0,
            "is_defensive_asset": False,
        }],
    }
    normalized_plan = {
        "is_unified_frozen_plan": True,
        "target_symbols": [],
        "target_weights": {},
        "proposal": [{"symbol": "000001", "weight_change": 0.0, "expected_20d_return": 0.03}],
        "risk_budget": {},
        "pretrade_controller": frozen_controller,
        "decision_contract": {
            "schema_version": "alis_unified_daily_decision_v2",
            "data_as_of_time": "2026-07-24T15:00:00+08:00",
            "selection_snapshot": {
                "strategy_id": 1,
                "signal_date": "20260724",
                "research_admitted_symbols": ["000001"],
                "research_blocks": [],
            },
            "signal_date": "20260724",
            "planned_execute_date": "20260727",
            "industry_allocation": {"symbol_industries": {"000001": "银行"}},
            "expected_return_contract": {"market_state": "S1"},
            "pretrade_controller": frozen_controller,
        },
        "decision_version_manifest": {},
        "plan_provenance": {"plan_id": 1},
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("T+1 unified execution must not rerun investment opinion")

    monkeypatch.setattr(quant_run_engine, "BacktestDataSource", DataSource)
    monkeypatch.setattr(quant_run_engine, "normalize_authoritative_plan", lambda _plan: normalized_plan)
    monkeypatch.setattr(quant_run_engine, "_apply_research_admission", forbidden)
    monkeypatch.setattr(quant_run_engine, "_apply_dynamic_strategy_gate", forbidden)
    monkeypatch.setattr(quant_run_engine, "build_industry_allocation", forbidden)
    monkeypatch.setattr(quant_run_engine, "build_strategy_rebalance_actions", forbidden)
    monkeypatch.setattr(quant_run_engine, "_base_portfolio_weights", forbidden)
    monkeypatch.setattr(quant_run_engine, "_strategy_portfolio_method", lambda _strategy_id: "cvar")
    monkeypatch.setattr(quant_run_engine, "_require_consistent_simulation_ledger", lambda: None)
    monkeypatch.setattr(
        quant_run_engine,
        "champion_execution_gate",
        lambda: {
            "status": "quarantined", "mode": "research_only_sell_reduce",
            "defensive_continuity": {"enabled": True, "maximum_total_weight": 0.3},
        },
    )
    monkeypatch.setattr(quant_run_engine, "get_strategy", lambda _strategy_id: None)
    monkeypatch.setattr(quant_run_engine, "get_positions", lambda: [])
    monkeypatch.setattr(
        quant_run_engine,
        "_portfolio_state",
        lambda *_args, **_kwargs: {
            "total_equity": 200000.0,
            "position_market_value": 0.0,
            "cash_balance": 200000.0,
            "position_value_by_symbol": {},
        },
    )
    monkeypatch.setattr(quant_run_engine, "_monthly_turnover_budget", forbidden)
    monkeypatch.setattr(quant_run_engine, "_pretrade_symbol_limits", forbidden)
    execution_kwargs = {}

    def capture_execution(*_args, **kwargs):
        execution_kwargs.update(kwargs)
        return SimpleNamespace(orders=[])

    monkeypatch.setattr(quant_run_engine, "simulate_open_execution", capture_execution)
    monkeypatch.setattr(quant_run_engine, "add_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.services.profit_loop.audit_frozen_execution_orders",
        lambda *_args, **_kwargs: {
            "ok": True,
            "checked_at": "2026-07-27T09:30:00+08:00",
            "orders": [],
            "missing": [],
        },
    )

    result = quant_run_engine.run_for_trade_date(
        strategy_id=1,
        signal_date="20260724",
        execute_date="20260727",
        top_n=20,
        budget_ratio=0.01,
        authoritative_plan={"raw": True},
    )

    assert result.bought_count == 0
    assert result.sold_count == 0
    assert result.detail["strategy_controller"]["regenerated_on_t1"] is False
    assert result.detail["portfolio_risk"]["optimizer"] == "authoritative_unified_daily_plan_weights_v1"
    assert result.detail["buy_summary"]["effective_budget_ratio"] == 1.0
    assert result.detail["portfolio_risk"]["monthly_turnover_budget"]["enforced"] is False
    assert execution_kwargs["max_industry_position_pct"] == 0.25
    assert execution_kwargs["max_correlated_industry_pct"] == 0.35
    assert execution_kwargs["high_risk_symbols"] == set()
    assert execution_kwargs["residual_sweep_symbol"] is None
    assert "20260724" not in loaded_dates
    assert "20260727" in loaded_dates


def test_defensive_execution_row_never_relabels_stale_bar(monkeypatch):
    captured = {}

    def stale_bar(ts_code, trade_date, *, exact_date=False, available_as_of_time=None):
        captured.update(
            ts_code=ts_code,
            trade_date=trade_date,
            exact_date=exact_date,
            available_as_of_time=available_as_of_time,
        )
        return {
            "trade_date": "20260730",
            "open": 3.2,
            "pre_close": 3.1,
            "close": 3.2,
            "vol": 1000,
        }

    monkeypatch.setattr("app.services.defensive_assets.defensive_asset_bar", stale_bar)

    row = quant_run_engine._defensive_execution_row(
        "510880",
        ts_code="510880.SH",
        name="红利ETF",
        execute_date="20260731",
        require_exact_date=True,
        observation_cutoff="2026-07-31T16:00:00+08:00",
    )

    assert row is None
    assert captured == {
        "ts_code": "510880.SH",
        "trade_date": "20260731",
        "exact_date": True,
        "available_as_of_time": "2026-07-31T16:00:00+08:00",
    }
