from app.services import strategy_lab


def test_strategy_lab_exposes_persisted_formal_oos(monkeypatch):
    monkeypatch.setattr(strategy_lab, "resolve_production_strategy_champion", lambda: {
        "champion_id": 42,
        "strategy_id": 1,
        "champion_version": "champion-v1",
        "strategy": {"name": "Champion"},
        "is_active": True,
    })
    monkeypatch.setattr(strategy_lab, "list_model_versions", lambda model_key, **kwargs: [{
        "id": 43,
        "version": "challenger-v1",
        "status": "candidate",
        "created_at": "2026-07-23",
        "updated_at": "2026-07-23",
        "config": {
            "strategy_id": 2,
            "champion_reference": "champion-v1",
            "selection_strategy": {
                "rebalance": {"freq": "weekly", "top_n": 20},
                "challenger_controls": {"max_single_weight": .08},
            },
        },
        "metrics": {"formal_oos": {
            "status": "failed",
            "valid_windows": 5,
            "aggregate_cost_after_return": .0258,
            "window_returns": [-.08, .01],
            "failure_attribution": {
                "status": "complete", "primary_cause": "transaction_costs", "confidence": "high",
                "evidence": {"fee_burden_return": .03}, "limitations": ["industry data unavailable"],
            },
        }},
        "validation": {
            "status": "formal_oos_failed",
            "production_eligible": False,
            "blocking_reasons": ["aggregate_cost_after_excess_positive"],
        },
    }] if model_key == strategy_lab.CHALLENGER_MODEL_KEY else [])

    result = strategy_lab.build_strategy_lab()

    assert result["champion"]["is_active"] is True
    assert result["challengers"][0]["strategy_id"] == 2
    assert result["challengers"][0]["formal_oos"]["valid_windows"] == 5
    assert result["challengers"][0]["formal_oos"]["windows"][0]["return"] == -.08
    assert result["challengers"][0]["formal_oos"]["failure_attribution"]["primary_cause"] == "transaction_costs"
    assert result["challengers"][0]["production_eligible"] is False
    assert result["baselines"] == []
