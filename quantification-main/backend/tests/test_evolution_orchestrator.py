from app.services import evolution_orchestrator as evolution


def test_strategy_diagnosis_prioritizes_market_exposure():
    result = evolution.diagnose_strategy_results([
        {"role": "challenger", "summary": {"aggregate_cost_after_return": .02, "aggregate_benchmark_return": .42}},
        {"role": "baseline", "summary": {"aggregate_cost_after_return": .01}},
    ])
    assert result["primary_gap"] == "market_exposure"
    assert result["recommended_next_candidate"] == "dynamic_market_exposure_with_etf_fallback_v1"


def test_cycle_uses_checkpoint_and_reports_overall_progress(monkeypatch):
    monkeypatch.setattr(evolution, "audit_oos_hard_data", lambda **kwargs: {
        "status": "ready", "window_start": "20200101", "window_end": "20261231",
    })
    monkeypatch.setattr(evolution, "_strategy_targets", lambda: [
        {"role": "champion", "strategy_id": 1, "model_version_id": 42, "name": "Champion"},
        {"role": "challenger", "strategy_id": 2, "model_version_id": 43, "name": "Challenger"},
    ])
    monkeypatch.setattr(evolution, "_persist_candidate_result", lambda *args: None)
    monkeypatch.setattr(evolution, "_ensure_task_active", lambda *args: None)
    monkeypatch.setattr(evolution, "advance_formal_candidate_lifecycle", lambda *args, **kwargs: [])
    heartbeats: list[dict] = []
    monkeypatch.setattr(evolution, "_heartbeat", lambda task_id, **fields: heartbeats.append(fields))
    received_checkpoints: list[list[dict]] = []

    def runner(**kwargs):
        received_checkpoints.append(kwargs["checkpoint_results"])
        windows = list(kwargs["checkpoint_results"])
        while len(windows) < 2:
            index = len(windows) + 1
            windows.append({
                "index": index,
                "metrics": {"total_return": .01, "benchmark_return": 0, "excess_return": .01, "max_drawdown": -.01},
                "summary": {"total_fees": 1},
                "data_complete": True,
                "point_in_time_passed": True,
                "production_strategy_version": "champion-v1",
                "market_state_attribution": {},
            })
            kwargs["progress_callback"]({
                "checkpoint_results": list(windows),
                "message": f"{len(windows)}/2",
            })
        return {
            "status": "failed", "windows": windows,
            "summary": {"aggregate_cost_after_return": .01, "aggregate_benchmark_return": .42},
            "promotion_gates": {}, "blocking_reasons": ["aggregate_cost_after_excess_positive"],
        }

    result = evolution._execute_strategy_cycle(
        {"id": 7, "payload": {"max_windows": 2}, "checkpoint": {
            "completed": [{"role": "champion", "strategy_id": 1, "summary": {}}],
            "current": {"strategy_id": 2, "windows": [{"index": 1}]},
        }},
        validation_runner=runner,
    )
    assert received_checkpoints == [[{"index": 1}]]
    assert len(result["strategies"]) == 2
    assert heartbeats[-1]["progress"] == 1.0
