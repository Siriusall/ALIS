from app.services import candidate_observation_center as center


def test_observation_center_separates_runtime_account_and_eligibility(monkeypatch):
    class Result:
        def fetchall(self):
            return [{
                "model_version_id": 8, "candidate_type": "model",
                "runtime_status": "observing", "eligibility_status": "ineligible",
                "change_type": "performance_tuning", "problem_family": "ranking",
                "production_change_forbidden": 1, "model_key": "ranker",
                "version": "v8", "version_status": "candidate",
                "stop_reason": "human_review_rejected",
                "validation_json": '{"blocked_reasons":["formal_oos"],"review":{"reviewer":"auto_optimizer","comment":"auto rejected"}}',
                "version_created_at": "2026-08-12T16:00:00+08:00",
                "account_id": 2, "account_status": "observing",
                "latest_trade_date": "20260812", "initial_cash": 200000,
                "latest_cash": 10000, "latest_equity": 202000,
                "stock_value": 192000, "positions_json": '{"000001.SZ":100}',
                "decision_count": 2, "operation_count": 3, "total_fees": 18.5,
                "blocked_event_count": 1,
                "account_updated_at": "2026-08-12T18:00:00+08:00",
            }]

    class Conn:
        def execute(self, *_args):
            return Result()

    class Context:
        def __enter__(self):
            return Conn()
        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(center, "_conn", Context)
    result = center.build_candidate_observation_center()
    item = result["items"][0]
    assert result["summary"]["observing"] == 1
    assert result["summary"]["with_isolated_account"] == 1
    assert result["summary"]["with_replay_evidence"] == 1
    assert result["summary"]["production_authority"] == 0
    assert item["account"]["return"] == 0.01
    assert item["account"]["position_count"] == 1
    assert item["account"]["operation_count"] == 3
    assert item["account"]["total_fees"] == 18.5
    assert item["blocked_reasons"] == ["formal_oos"]
    assert item["stop_reason"] == "human_review_rejected"
    assert item["reviewer"] == "auto_optimizer"
    assert item["review_comment"] == "auto rejected"


def test_observation_center_does_not_count_empty_trade_date_as_replay_evidence(monkeypatch):
    class Result:
        def fetchall(self):
            return [{
                "model_version_id": 9, "candidate_type": "model", "runtime_status": "observing",
                "eligibility_status": "ineligible", "change_type": "structural_change",
                "problem_family": "event", "production_change_forbidden": 1, "model_key": "event",
                "version": "v9", "version_status": "candidate", "stop_reason": "", "validation_json": "{}",
                "version_created_at": "2026-08-12T16:00:00+08:00", "account_id": 3,
                "account_status": "awaiting_first_replay", "latest_trade_date": "", "initial_cash": 200000,
                "latest_cash": 200000, "latest_equity": 200000, "stock_value": 0, "positions_json": "{}",
                "decision_count": 0, "operation_count": 0, "total_fees": 0, "blocked_event_count": 0,
                "account_updated_at": "2026-08-12T16:00:00+08:00",
            }]

    class Conn:
        def execute(self, *_args):
            return Result()

    class Context:
        def __enter__(self):
            return Conn()
        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(center, "_conn", Context)
    result = center.build_candidate_observation_center()
    assert result["summary"]["with_isolated_account"] == 1
    assert result["summary"]["with_replay_evidence"] == 0
