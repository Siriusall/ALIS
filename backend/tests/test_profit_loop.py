from contextlib import contextmanager
import hashlib
import json

import pytest

from app.services import profit_loop
from app.services import decision_version_manifest
from app.services.quant_run_contracts import ReplayRiskGateError


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_missing_live_metrics_do_not_degrade_production_model(monkeypatch):
    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "model_family_status", lambda: {"items": [{
        "model_key": "m1", "name": "M1", "status": "production",
        "active_version": {"version": "v1", "metrics": {}},
    }]})
    monkeypatch.setattr(profit_loop, "model_feedback_gate", lambda *_: {
        "weighted_accuracy": None, "samples": 0, "should_degrade": False,
    })

    @contextmanager
    def fake_conn():
        class Conn:
            def execute(self, *_args, **_kwargs):
                return _Rows([])
        yield Conn()

    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)
    result = profit_loop.build_model_live_monitor()
    assert result["degraded_count"] == 0
    assert result["items"][0]["status"] == "observing"
    assert result["items"][0]["weight_multiplier"] == 1.0
    assert result["items"][0]["evidence_maturity"]["status"] == "insufficient"


def test_model_contribution_refuses_to_invent_portfolio_pnl(monkeypatch):
    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "model_family_status", lambda: {"items": [{
        "model_key": "m1", "name": "M1", "active_version": {"version": "v1"},
    }]})

    @contextmanager
    def fake_conn():
        class Conn:
            def execute(self, *_args, **_kwargs):
                return _Rows([{
                    "model_version": "v1", "horizon_sessions": 20, "samples": 8,
                    "avg_directional_return": .03, "avg_benchmark_return": .01,
                    "avg_excess_return": .02, "hit_rate": .625,
                }])
        yield Conn()

    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)
    result = profit_loop.build_model_return_contribution()
    item = result["items"][0]
    assert item["preferred_20d_evidence"]["avg_excess_return_pct"] == 2.0
    assert item["portfolio_return_contribution_pct"] is None
    assert item["contribution_status"] == "insufficient_samples"
    assert result["methodology"]["no_fabrication"] is True


def test_planned_execution_time_uses_operational_scheduler_clock(monkeypatch):
    monkeypatch.setenv("QUANT_AUTO_SIMULATION_CLOSE_HOUR", "16")
    monkeypatch.setenv("QUANT_AUTO_SIMULATION_CLOSE_MINUTE", "5")
    monkeypatch.setattr(profit_loop, "remembered_first_rebalance_clock", lambda: None)

    assert profit_loop._planned_execute_at("20260722") == "2026-07-22T16:05:00+08:00"


def test_planned_execution_time_remembers_first_actual_rebalance(monkeypatch):
    monkeypatch.setattr(profit_loop, "remembered_first_rebalance_clock", lambda: profit_loop.dtime(16, 37))

    assert profit_loop._planned_execute_at("20260723") == "2026-07-23T16:37:00+08:00"


def test_unified_contract_keeps_small_positive_frozen_weight_as_execution_target(monkeypatch):
    monkeypatch.setattr(
        decision_version_manifest,
        "build_decision_version_manifest",
        lambda **_kwargs: {"schema_version": "alis_decision_version_manifest_v1"},
    )
    contract = profit_loop.build_daily_decision_contract(
        portfolio={
            "selection_source": "unified_full_market_selection",
            "final_target_weights": {"000001": 0.001, "600000": 0.0},
            "decision_data_gate": {},
            "rebalance_proposal": [],
        },
        trade_date="20260724",
        planned_execute_date="20260727",
    )

    assert contract["target_symbols"] == ["000001"]
    assert contract["target_weights"] == {"000001": 0.001}


def test_expected_return_items_are_unique_when_defensive_asset_is_already_held():
    result = profit_loop._merge_expected_return_items(
        [
            {"symbol": "000001", "source": "equity"},
            {"symbol": "511880", "source": "holding"},
        ],
        [{"symbol": "511880", "source": "defensive_override"}],
        symbol_order=["000001", "511880"],
    )

    assert result == [
        {"symbol": "000001", "source": "equity"},
        {"symbol": "511880", "source": "defensive_override"},
    ]


def test_unified_contract_recomputes_execution_authorization_from_frozen_checks(monkeypatch):
    monkeypatch.setattr(
        decision_version_manifest,
        "build_decision_version_manifest",
        lambda **_kwargs: {"schema_version": "alis_decision_version_manifest_v1"},
    )
    contract = profit_loop.build_daily_decision_contract(
        portfolio={
            "selection_source": "unified_full_market_selection",
            "execution_authorized": True,
            "final_target_weights": {},
            "decision_data_gate": {"decision_ready": True},
            "execution_checks": [{"key": "risk_gate", "status": "block"}],
            "rebalance_proposal": [],
        },
        trade_date="20260724",
        planned_execute_date="20260727",
    )

    assert contract["execution_authorized"] is False


def test_unified_path_does_not_consume_legacy_pools(monkeypatch):
    monkeypatch.setattr(
        profit_loop,
        "list_pool_members",
        lambda _pool: (_ for _ in ()).throw(AssertionError("legacy pool must not be read")),
    )

    core, candidates, watch, status = profit_loop._legacy_pool_preview_inputs(
        unified_available=True,
    )

    assert (core, candidates, watch) == ([], [], [])
    assert status == "not_consumed_non_authoritative"


def test_legacy_portfolio_is_preview_only_and_never_opens_formal_plan_store(monkeypatch):
    monkeypatch.setattr(
        profit_loop,
        "_ensure_tables",
        lambda: (_ for _ in ()).throw(AssertionError("formal tables must not be touched")),
    )
    monkeypatch.setattr(
        profit_loop,
        "business_conn",
        lambda: (_ for _ in ()).throw(AssertionError("formal plan store must not be opened")),
    )

    result = profit_loop.materialize_daily_rebalance_plan({
        "selection_source": "legacy_pool_preview_blocked",
        "decision_data_gate": {"target_trade_date": "20260724"},
        "rebalance_proposal": [{"symbol": "000001", "target_weight": 0.1}],
        "execution_authorized": True,
    })

    assert result["status"] == "compatibility_preview_only"
    assert result["persisted"] is False
    assert result["execution_authorized"] is False


def test_spoofed_unified_source_without_complete_contract_is_rejected_before_write(monkeypatch):
    @contextmanager
    def fake_data_conn():
        class Conn:
            def execute(self, *_args, **_kwargs):
                return _Rows([{"value": "20260727"}])
        yield Conn()

    monkeypatch.setattr(profit_loop, "data_conn", fake_data_conn)
    monkeypatch.setattr(profit_loop, "_planned_execute_at", lambda _date: "")
    monkeypatch.setattr(
        profit_loop,
        "build_daily_decision_contract",
        lambda **_kwargs: {
            "schema_version": "alis_unified_daily_decision_v1",
            "selection_snapshot": {},
        },
    )
    monkeypatch.setattr(
        profit_loop,
        "_ensure_tables",
        lambda: (_ for _ in ()).throw(AssertionError("formal tables must not be touched")),
    )

    with pytest.raises(ReplayRiskGateError):
        profit_loop.materialize_daily_rebalance_plan({
            "selection_source": "unified_full_market_selection",
            "decision_data_gate": {"target_trade_date": "20260724"},
        })


def test_formal_plan_can_only_be_claimed_once(monkeypatch):
    state = {"status": "planned", "token": ""}
    contract = {"proposal": [], "risk_budget": {}, "decision_version_manifest": {}}
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan = {
        "id": 7, "trade_date": "20260724", "planned_execute_date": "20260727", "version": 1,
        "input_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "proposal_json": json.dumps(contract, ensure_ascii=False),
    }

    class Result(_Rows):
        rowcount = 1

    @contextmanager
    def fake_conn():
        class Conn:
            _is_postgres = False
            _is_mysql = False

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("SELECT * FROM daily_rebalance_plans"):
                    return Result([{**plan, "status": state["status"]}] if state["status"] == "planned" else [])
                if "SET status='executing'" in sql and state["status"] == "planned":
                    state.update(status="executing", token=str(params[0]))
                    return Result([])
                return Result([])
        yield Conn()

    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)

    first = profit_loop.claim_daily_rebalance_execution_input("20260724", "20260727", claim_token="task-1")
    second = profit_loop.claim_daily_rebalance_execution_input("20260724", "20260727", claim_token="task-2")

    assert first and first["execution_claim_token"] == "task-1"
    assert second is None
    assert state == {"status": "executing", "token": "task-1"}


def test_pending_plan_lists_defensive_assets_needed_by_plan_and_holdings(monkeypatch):
    contract = {
        "target_symbols": ["000001", "510880"],
        "proposal": [
            {"symbol": "000001", "action": "hold"},
            {"symbol": "511010", "action": "reduce_position"},
        ],
    }

    @contextmanager
    def fake_conn():
        class Conn:
            def execute(self, _sql, _params=()):
                return _Rows([{
                    "id": 21,
                    "trade_date": "20260730",
                    "planned_execute_date": "20260731",
                    "version": 2,
                    "proposal_json": json.dumps(contract),
                }])

        yield Conn()

    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)
    monkeypatch.setattr(
        profit_loop,
        "get_positions",
        lambda: [{"symbol": "511880", "quantity": 100}],
    )

    pending = profit_loop.list_pending_daily_rebalance_execution_inputs()

    assert pending == [{
        "plan_id": 21,
        "signal_date": "20260730",
        "execute_date": "20260731",
        "required_defensive_ts_codes": ["510880.SH", "511010.SH", "511880.SH"],
    }]


def test_missed_formal_plans_leave_pending_state_and_cannot_execute_late(monkeypatch):
    captured = {}
    state = {"status": "planned"}

    class Result(_Rows):
        rowcount = 1

    @contextmanager
    def fake_conn():
        class Conn:
            def execute(self, sql, params=()):
                captured["sql"] = sql
                captured["params"] = params
                if "SET status='missed_execution_window'" in sql:
                    state["status"] = "missed_execution_window"
                if sql.lstrip().startswith("SELECT * FROM daily_rebalance_plans"):
                    return Result([] if state["status"] != "planned" else [{"id": 1}])
                return Result([])

        yield Conn()

    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)

    changed = profit_loop.mark_missed_daily_rebalance_execution_inputs("2026-07-30")
    claimed = profit_loop.claim_daily_rebalance_execution_input(
        "20260728",
        "20260729",
        claim_token="late-task",
    )

    assert changed == 1
    assert state["status"] == "missed_execution_window"
    assert claimed is None


def test_invalid_unified_contract_never_transitions_plan_to_executing(monkeypatch):
    state = {"status": "planned", "update_attempted": False}
    contract = {
        "schema_version": "alis_unified_daily_decision_v1",
        "proposal": [],
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan = {
        "id": 8,
        "trade_date": "20260724",
        "planned_execute_date": "20260727",
        "version": 1,
        "status": "planned",
        "input_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "proposal_json": json.dumps(contract, ensure_ascii=False),
    }

    class Result(_Rows):
        rowcount = 1

    @contextmanager
    def fake_conn():
        class Conn:
            _is_postgres = False
            _is_mysql = False

            def execute(self, sql, _params=()):
                if sql.lstrip().startswith("SELECT * FROM daily_rebalance_plans"):
                    return Result([plan])
                if "SET status='executing'" in sql:
                    state["status"] = "executing"
                    state["update_attempted"] = True
                return Result([])

        yield Conn()

    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)

    with pytest.raises(ReplayRiskGateError):
        profit_loop.claim_daily_rebalance_execution_input(
            "20260724", "20260727", claim_token="task-invalid",
        )

    assert state == {"status": "planned", "update_attempted": False}


def test_unified_strategy_identity_mismatch_never_claims_plan(monkeypatch):
    state = {"status": "planned", "update_attempted": False}
    contract = {
        "schema_version": "alis_unified_daily_decision_v1",
        "signal_date": "20260724",
        "planned_execute_date": "20260727",
        "selection_snapshot": {"strategy_id": 3},
        "proposal": [],
    }
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan = {
        "id": 9,
        "trade_date": "20260724",
        "planned_execute_date": "20260727",
        "version": 1,
        "status": "planned",
        "input_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "proposal_json": json.dumps(contract, ensure_ascii=False),
    }

    class Result(_Rows):
        rowcount = 1

    @contextmanager
    def fake_conn():
        class Conn:
            _is_postgres = False
            _is_mysql = False

            def execute(self, sql, _params=()):
                if sql.lstrip().startswith("SELECT * FROM daily_rebalance_plans"):
                    return Result([plan])
                if "SET status='executing'" in sql:
                    state["status"] = "executing"
                    state["update_attempted"] = True
                return Result([])

        yield Conn()

    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)
    monkeypatch.setattr(
        "app.services.quant_run_plan_input.validate_unified_decision_contract",
        lambda _contract: None,
    )

    with pytest.raises(ReplayRiskGateError):
        profit_loop.claim_daily_rebalance_execution_input(
            "20260724",
            "20260727",
            claim_token="task-wrong-strategy",
            expected_strategy_id=4,
        )

    assert state == {"status": "planned", "update_attempted": False}


def _slo_plan(index: int, *, actionable: bool = True, status: str = "executed", execution_id: int | None = None) -> dict:
    signal_day = index
    execute_day = index + 1
    proposal = [{"action": "buy", "weight_change": .1}] if actionable else [{"action": "hold", "weight_change": 0}]
    return {
        "id": index, "trade_date": f"202607{signal_day:02d}", "planned_execute_date": f"202607{execute_day:02d}",
        "version": 1, "status": status, "execution_id": execution_id,
        "executed_at": f"2026-07-{execute_day:02d}T16:05:00+08:00" if status == "executed" else None,
        "proposal_json": json.dumps({"proposal": proposal, "execution_authorized": True}),
    }


def test_execution_chain_slo_observes_then_passes_linked_and_zero_order_plans(monkeypatch):
    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    plans = [_slo_plan(index, execution_id=index) for index in range(1, 20)]
    plans.append(_slo_plan(20, actionable=False, execution_id=None))
    operations = [
        {"execution_id": index, "operation_uid": f"op-{index}"}
        for index in range(1, 20)
    ]
    observing = profit_loop.build_execution_chain_slo(plans[:19], operations, as_of_date="20260731")
    passed = profit_loop.build_execution_chain_slo(plans, operations, as_of_date="20260731")
    assert observing["status"] == "observing"
    assert passed["status"] == "passed"
    assert passed["metrics"]["execution_binding_integrity_rate"] == 1.0


def test_execution_chain_slo_fails_duplicates_stranded_and_late_execution(monkeypatch):
    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    plans = [_slo_plan(index, execution_id=index) for index in range(1, 21)]
    plans[0]["execution_id"] = 2
    plans[1]["executed_at"] = "2026-07-05T16:05:00+08:00"
    plans[2].update(status="blocked_by_execution_recovery", executed_at=None, execution_id=None)
    operations = [{"execution_id": index, "operation_uid": f"op-{index}"} for index in range(1, 21)]
    operations.append({"execution_id": 2, "operation_uid": "op-2"})
    result = profit_loop.build_execution_chain_slo(plans, operations, as_of_date="20260731")
    assert result["status"] == "failed"
    assert result["metrics"]["duplicate_plan_execution_id_count"] == 1
    assert result["metrics"]["duplicate_operation_uid_count"] == 1
    assert result["metrics"]["stranded_execution_count"] == 1
    assert result["gates"]["on_schedule_execution_rate"] is False
