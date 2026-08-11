from app.services.decision_store import _decision_evidence_payload, _evidence_hash, _structured_diff
from app.services import decision_store
from contextlib import contextmanager


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_decision_evidence_hash_binds_resolved_execution_operation() -> None:
    base = {
        "ts_code": "000001.SZ", "decision_type": "buy", "price": 10.2,
        "thesis_id": 3, "execution_id": 9, "data_as_of_time": "2026-07-16T23:59:59",
        "model_version": "strategy-v1", "market_state": {"state": "S1"},
        "research_state": {"confidence": .8}, "valuation": {"current_price": 10.2},
        "portfolio_context": {"cash": 1000}, "reason": "test", "invalidation_conditions": ["risk"],
        "target_weight_contract": {"function_bucket": "leader", "target_weight": .08},
    }
    frozen = _decision_evidence_payload(base, decision_time="2026-07-17T09:30:00", operation_id=42)

    assert frozen["operation_id"] == 42
    assert _evidence_hash(frozen) != _evidence_hash({**frozen, "operation_id": None})
    assert _evidence_hash(frozen) != _evidence_hash({**frozen, "target_weight_contract": {}})


def test_snapshot_resolves_operation_before_writing_v2_hash(monkeypatch) -> None:
    payload = {
        "ts_code": "000001.SZ", "decision_type": "buy", "decision_time": "2026-07-17T09:30:00", "price": 10.2,
        "execution_id": 9, "data_as_of_time": "2026-07-16T23:59:59", "model_version": "strategy-v1",
        "market_state": {}, "research_state": {}, "valuation": {}, "portfolio_context": {}, "target_weight_contract": {"function_bucket": "leader"}, "reason": "test",
    }
    update_params = []
    returned = {
        "id": 1, **payload, "thesis_id": None, "operation_id": 42,
        "invalidation_conditions_json": "[]", "market_state_json": "{}", "research_state_json": "{}",
        "valuation_json": "{}", "portfolio_context_json": "{}", "created_at": "2026-07-17T09:30:00",
        "target_weight_contract_json": "{\"function_bucket\":\"leader\"}",
        "evidence_hash": "", "evidence_schema_version": "",
    }

    class _Conn:
        def execute(self, sql, params=()):
            if "SELECT id FROM operations" in sql:
                return _Cursor([{"id": 42}])
            if "SELECT release_id FROM executions" in sql:
                return _Cursor([{"release_id": 7}])
            if "INSERT INTO decision_snapshots" in sql:
                return _Cursor([{"id": 1}])
            if "UPDATE decision_snapshots" in sql:
                update_params.append(params)
                return _Cursor([])
            if "SELECT * FROM decision_snapshots" in sql:
                return _Cursor([returned])
            raise AssertionError(sql)

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(decision_store, "_conn", fake_conn)
    decision_store.create_decision_snapshot(payload)

    expected = _evidence_hash(_decision_evidence_payload(
        {**payload, "release_id": 7}, decision_time=payload["decision_time"], operation_id=42,
    ))
    assert update_params[0][0] == expected
    assert update_params[0][1] == "evidence_freeze_v3"


def test_structured_diff_reports_only_changed_frozen_paths() -> None:
    changes = _structured_diff(
        {"model": "v1", "research": {"confidence": .6, "risk": ["a"]}},
        {"model": "v2", "research": {"confidence": .8, "risk": ["a"]}},
    )

    assert changes == [
        {"path": "model", "left": "v1", "right": "v2"},
        {"path": "research.confidence", "left": .6, "right": .8},
    ]
