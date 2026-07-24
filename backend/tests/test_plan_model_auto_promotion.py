from app.services import plan_model_auto_optimizer as optimizer


def test_plan_model_auto_promotion_requires_cost_non_worse_and_real_shadow(monkeypatch):
    reviewed: list[dict] = []
    monkeypatch.setattr(
        optimizer,
        "review_plan_model_version",
        lambda model_version_id, **kwargs: reviewed.append({"id": model_version_id, **kwargs})
        or {"production_eligible": True},
    )
    family = {
        "items": [{
            "model_key": "leader_lambdarank_v1",
            "active_version": {
                "id": 1,
                "metrics": {
                    "net_lift": .02,
                    "walk_forward_lift": .02,
                    "worst_window_net_lift": .01,
                    "hit_rate": .55,
                },
            },
            "latest_version": {
                "id": 2,
                "version": "candidate-v2",
                "status": "candidate",
                "metrics": {
                    "net_lift": .03,
                    "walk_forward_lift": .025,
                    "worst_window_net_lift": .015,
                    "hit_rate": .57,
                },
                "validation": {
                    "production_eligibility": {
                        "gates": {
                            "artifact_exists": True,
                            "walk_forward_passed": True,
                            "live_observation_20_sessions": True,
                            "human_review": False,
                        }
                    }
                },
            },
        }]
    }
    result = optimizer._auto_promote_ready_candidates(family)
    assert len(result) == 1
    assert reviewed[0]["reviewer"] == "deterministic_auto_governance"
    assert reviewed[0]["waive_live_observation"] is False

    family["items"][0]["latest_version"]["validation"]["production_eligibility"]["gates"]["live_observation_20_sessions"] = False
    assert optimizer._auto_promote_ready_candidates(family) == []
