from app.services import model_governance_scheduler as scheduler


def test_retired_generic_model_scheduler_never_writes_or_promotes():
    decision = scheduler.automatic_retraining_decision(
        latest_run={"id": 7, "status": "completed"},
        latest_version={"id": 6},
        current_trade_date="20260728",
    )
    training = scheduler.request_model_training(force=True)
    tuning = scheduler.auto_tune_models_once()

    assert decision["due"] is False
    assert decision["status"] == scheduler.RETIRED_STATUS
    assert training["started"] is False
    assert tuning["decision"] == "no_action"
    assert all(
        item["production_change_forbidden"]
        for item in (decision, training, tuning)
    )


def test_retired_generic_bootstrap_gate_is_closed():
    version = {
        "id": 10,
        "validation": {"status": "passed", "production_eligible": True},
    }

    assert scheduler.bootstrap_candidate_eligible(version) is False
    result = scheduler.mark_bootstrap_promotion(version)
    assert result["status"] == scheduler.RETIRED_STATUS
    assert result["model_version_id"] == 10
    assert scheduler.start_model_governance_scheduler() is False
