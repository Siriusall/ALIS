from app.services.backtest_data_source import HistBar, SnapshotRow
from app.services import model_signal_fusion


def _row(symbol: str, change_pct: float) -> SnapshotRow:
    return SnapshotRow(
        symbol=symbol,
        date="2026-07-22",
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        prev_close=9.9,
        change_pct=change_pct,
        volume=1000,
        turnover=10000,
        name=symbol,
    )


def _history() -> list[HistBar]:
    return [
        HistBar(
            date=f"2026-06-{index + 1:02d}",
            open=10 + index / 10,
            high=10 + index / 10,
            low=10 + index / 10,
            close=10 + index / 10,
            volume=1000,
            turnover=10000 + index * 100,
        )
        for index in range(21)
    ]


def test_production_models_change_ranking_and_block_bottom_capital_signal(monkeypatch):
    rows = [_row("000001", -1.0), _row("000002", 0.0), _row("000003", 1.0)]
    monkeypatch.setattr(
        model_signal_fusion,
        "get_active_model_version",
        lambda key: {"id": 1, "version": f"{key}-active"},
    )
    monkeypatch.setattr(model_signal_fusion, "_artifact", lambda version: {"ready": True})

    def fake_score(key, features, artifact=None):
        multiplier = {
            "medium_horizon_lambdarank_v1": 1.0,
            "capital_persistence_multitask_v1": 2.0,
            "leader_lambdarank_v1": 3.0,
            "event_surprise_lgbm_v1": 4.0,
            "narrative_transition_multitask_v1": 5.0,
        }[key]
        return {"score": features["event_surprise"] * multiplier}

    monkeypatch.setattr(model_signal_fusion, "score_plan_model_features", fake_score)
    ranked, evidence, blocked, status = model_signal_fusion.apply_production_model_signals(
        ranked=[(rows[0], 90.0), (rows[1], 80.0), (rows[2], 70.0)],
        factor_values={
            row.symbol: {"momentum_20": 0.1, "momentum_60": 0.2, "volatility_20": 0.01}
            for row in rows
        },
        histories={row.symbol: _history() for row in rows},
        themes=[],
    )

    assert [row.symbol for row, _score in ranked] == ["000003", "000002"]
    assert blocked[0]["symbol"] == "000001"
    assert blocked[0]["code"] == "capital_persistence_gate_failed"
    assert evidence["000003"]["add_position_eligible"] is True
    assert len(evidence["000003"]["trigger_models"]) == 5
    assert status["input_count"] == 3
    assert status["output_count"] == 2


def test_equal_model_scores_receive_equal_percentiles():
    assert model_signal_fusion._percentiles({"000003": 0.0, "000001": 0.0, "000002": 1.0}) == {
        "000001": 0.25,
        "000003": 0.25,
        "000002": 1.0,
    }
