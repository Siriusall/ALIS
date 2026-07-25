from app.services import incident_supervisor


def test_internal_incidents_stay_system_owned(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([
        {"name": "sealed_holdout_data_gate", "ok": True, "health": {"status": "degraded", "reason": "blocked"}},
        {"name": "decision_data_gate", "ok": False, "health": {"status": "failed", "reason": "module_returned_not_ok"}},
        {"name": "periodic_performance_review", "ok": True, "health": {"status": "degraded", "reason": "attention"}},
    ])

    assert result["status"] == "self_managed"
    assert result["auto_managed_count"] == 3
    assert result["waiting_condition_count"] == 1
    assert result["internal_fault_count"] == 2
    assert result["owner_action_required_count"] == 0
    assert all(case["owner"] == "system" for case in result["cases"])
    assert result["llm"]["status"] == "deterministic_fallback"


def test_only_external_authority_is_escalated(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([{
        "name": "llm_data_operations",
        "ok": True,
        "result": {"execution": {"escalations": [{"action": "escalate_external_dependency", "categories": ["permission"]}]}},
    }])

    assert result["status"] == "owner_action_required"
    assert result["owner_action_required_count"] == 1
    assert result["cases"][0]["owner"] == "owner"


def test_llm_can_explain_but_cannot_change_ownership(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "configured")
    monkeypatch.setattr(incident_supervisor, "llm_token_budget_status", lambda: {"status": "available"})
    monkeypatch.setattr(incident_supervisor, "_call_deepseek_json", lambda **_kwargs: ({
        "cases": [{
            "module": "decision_data_gate",
            "summary": "收盘数据尚未完整",
            "system_action": "系统刷新数据",
            "next_check": "下个周期复核",
            "owner_action_required": True,
        }]
    }, "test-model"))

    result = incident_supervisor.run_incident_supervisor([
        {"name": "decision_data_gate", "ok": False, "health": {"status": "failed", "reason": "blocked"}},
    ])

    assert result["cases"][0]["owner"] == "system"
    assert result["cases"][0]["owner_action_required"] is False
    assert result["cases"][0]["summary"] == "收盘数据尚未完整"


def test_llm_invalid_json_retries_once(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "configured")
    monkeypatch.setattr(incident_supervisor, "llm_token_budget_status", lambda: {"status": "available"})
    calls = []

    def call(**kwargs):
        calls.append(kwargs["purpose"])
        if len(calls) == 1:
            raise ValueError("llm_response_not_json")
        return {"cases": [{"module": "decision_data_gate", "summary": "数据等待刷新"}]}, "test-model"

    monkeypatch.setattr(incident_supervisor, "_call_deepseek_json", call)
    result = incident_supervisor.run_incident_supervisor([
        {"name": "decision_data_gate", "ok": False, "health": {"status": "failed", "reason": "blocked"}},
    ])

    assert calls == ["incident_supervision", "incident_supervision_json_retry"]
    assert result["llm"]["status"] == "recovered_after_json_retry"
    assert result["cases"][0]["summary"] == "数据等待刷新"


def test_self_healing_summary_is_not_counted_as_a_separate_incident(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([
        {"name": "research_data_assets", "ok": True, "health": {"status": "degraded", "reason": "partial"},
         "result": {"gaps": ["industry:capacity_utilization"]}},
        {"name": "autonomous_self_healing", "ok": False, "health": {"status": "failed", "reason": "blocked"}},
    ])

    assert result["auto_managed_count"] == 1
    assert [case["module"] for case in result["cases"]] == ["research_data_assets"]
    assert result["cases"][0]["resolution_class"] == "internal_fault"


def test_sealed_holdout_no_progress_becomes_internal_fault(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([
        {"name": "sealed_holdout_data_gate", "ok": True, "health": {"status": "failed", "reason": "blocked"},
         "result": {"eligible_sessions": 1, "required_sessions": 126}},
        {"name": "autonomous_self_healing", "ok": False, "result": {"actions": [{
            "kind": "sealed_strategy_holdout", "verification": {"eligible_sessions": 1},
        }]}},
    ])

    case = result["cases"][0]
    assert case["resolution_class"] == "internal_fault"
    assert case["failure_signature"] == "sealed_holdout_repair_no_progress"
    assert case["progress"] == {"before": 1, "after": 1, "delta": 0, "required": 126, "unit": "交易日"}
    assert case["action"] == "diagnose_backfill_contract"


def test_sealed_holdout_with_progress_waits_for_next_bounded_cycle(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([
        {"name": "sealed_holdout_data_gate", "ok": True, "health": {"status": "failed", "reason": "blocked"},
         "result": {"eligible_sessions": 1, "required_sessions": 126}},
        {"name": "autonomous_self_healing", "ok": False, "result": {"actions": [{
            "kind": "sealed_strategy_holdout", "verification": {"eligible_sessions": 6},
        }]}},
    ])

    case = result["cases"][0]
    assert case["resolution_class"] == "waiting_condition"
    assert case["progress"]["delta"] == 5


def test_empty_pending_data_operations_is_waiting_condition(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([{
        "name": "llm_data_operations", "ok": True, "health": {"status": "degraded", "reason": "degraded"},
        "result": {"execution": {"actions": [{"result": {"results": [
            {"category": "empty_pending"}, {"category": "empty_pending"},
        ]}}]}},
    }])

    assert result["cases"][0]["resolution_class"] == "waiting_condition"


def test_decision_data_is_waiting_only_when_refresh_is_active(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([
        {"name": "decision_data_gate", "ok": False, "health": {"status": "failed", "reason": "blocked"}},
        {"name": "autonomous_self_healing", "ok": False, "result": {"actions": [{
            "kind": "latest_decision_data", "status": "refresh_already_running",
        }]}},
    ])

    assert result["cases"][0]["resolution_class"] == "waiting_condition"


def test_decision_data_upstream_cooldown_remains_waiting(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([
        {"name": "decision_data_gate", "ok": False, "health": {"status": "failed", "reason": "blocked"}},
        {"name": "autonomous_self_healing", "ok": False, "result": {"actions": [{
            "kind": "latest_decision_data", "status": "refresh_cooling_down",
            "start": {"category": "upstream_not_ready", "retry_after_seconds": 600},
        }]}},
    ])

    assert result["cases"][0]["resolution_class"] == "waiting_condition"
    assert result["cases"][0]["evidence"]["retry_after_seconds"] == 600


def test_decision_data_internal_slice_cooldown_is_internal_fault(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([
        {"name": "decision_data_gate", "ok": False, "health": {"status": "failed", "reason": "blocked"}},
        {"name": "autonomous_self_healing", "ok": False, "result": {"actions": [{
            "kind": "latest_decision_data", "status": "refresh_cooling_down",
            "start": {"category": "internal_fault_truncated_slice", "retry_after_seconds": 1800},
        }]}},
    ])

    assert result["cases"][0]["resolution_class"] == "internal_fault"
    assert result["cases"][0]["failure_signature"] == "internal_fault_truncated_slice"


def test_official_source_parser_failure_is_internal_fault(monkeypatch):
    monkeypatch.setattr(incident_supervisor, "get_env_value", lambda *_args: "")
    result = incident_supervisor.run_incident_supervisor([{
        "name": "official_policy_documents", "ok": True, "health": {"status": "degraded", "reason": "partial"},
        "result": {
            "failed_sources": ["official.test"],
            "sources": [{"source": "official.test", "status": "failed", "error": "unexpected html schema"}],
        },
    }])

    assert result["cases"][0]["resolution_class"] == "internal_fault"
