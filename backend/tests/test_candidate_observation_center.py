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
                "validation_json": '{"blocked_reasons":["formal_oos"]}',
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
    assert result["summary"]["production_authority"] == 0
    assert item["account"]["return"] == 0.01
    assert item["account"]["position_count"] == 1
    assert item["account"]["operation_count"] == 3
    assert item["account"]["total_fees"] == 18.5
    assert item["blocked_reasons"] == ["formal_oos"]
