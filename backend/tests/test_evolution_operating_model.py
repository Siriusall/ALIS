from app.services import evolution_orchestrator


def test_evolution_schedule_declares_continuous_factor_strategy_research_model():
    status = evolution_orchestrator.evolution_schedule_status()

    assert status["factor_strategy_operating_model"] == "continuous_research"
    assert (
        status["factor_strategy_operating_model_version"]
        == "alis_factor_strategy_continuous_research_v1"
    )
    assert status["scheduler_tick_seconds"] == 300
    assert (
        status["factor_strategy_cadence_contract"]["runtime"]["cadence"]
        == "always_on_queue_worker"
    )
    assert (
        status["factor_strategy_cadence_contract"]["runtime"]["requires_market_close"]
        is False
    )
    assert (
        status["factor_strategy_cadence_contract"]["research"]["requires_market_close"]
        is False
    )
    assert (
        status["factor_strategy_cadence_contract"]["research"][
            "stops_on_same_snapshot_epoch_complete"
        ]
        is True
    )
    assert (
        status["factor_strategy_cadence_contract"]["formal_oos"][
            "requires_market_close"
        ]
        is False
    )
