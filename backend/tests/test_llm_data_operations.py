import json
import sqlite3
from contextlib import contextmanager

from app.data_platform.services import data_center
from app.services import llm_data_operations as data_ops


def _evidence() -> dict:
    return {
        "safe_retry_candidates": [
            {"id": 11, "api_name": "daily", "category": "transient", "attempts": 1},
            {"id": 12, "api_name": "moneyflow", "category": "empty_pending", "attempts": 2},
        ],
        "external_dependency_failures": [
            {"id": 13, "api_name": "fina_indicator", "category": "permission", "attempts": 3},
        ],
        "superseded_api_candidates": [
            {"id": 14, "api_name": "etf_sh_cons", "category": "unknown", "replacement": "exchange_official_pcf"},
        ],
        "oos_hard_data": {"status": "ready"},
        "sealed_holdout": {"status": "blocked"},
        "decision_data": {"status": "ready"},
    }


def test_normalise_proposal_drops_unregistered_actions() -> None:
    proposal = data_ops._normalise_proposal({
        "summary": "diagnosis",
        "actions": [
            {"action": "retry_transient_requests", "confidence": .9},
            {"action": "run_shell", "confidence": 1.0},
            {"action": "change_api_key", "confidence": 1.0},
        ],
    })

    assert [item["action"] for item in proposal["actions"]] == ["retry_transient_requests"]


def test_normalise_proposal_keeps_string_limitations_as_one_item() -> None:
    proposal = data_ops._normalise_proposal({"limitations": "最多重试5条，不扩大权限"})
    assert proposal["limitations"] == ["最多重试5条，不扩大权限"]


def test_error_evidence_redacts_credentials() -> None:
    value = data_ops._redact_error("timeout token=super-secret api_key:abc123")
    assert "super-secret" not in value
    assert "abc123" not in value
    assert value.count("[REDACTED]") == 2


def test_governance_revalidates_safe_retry_and_external_escalation() -> None:
    proposal = data_ops._normalise_proposal({
        "actions": [
            {"action": "retry_transient_requests", "confidence": .9},
            {"action": "delegate_hard_data_repair", "confidence": .8},
            {"action": "resolve_superseded_requests", "confidence": .9},
            {"action": "escalate_external_dependency", "confidence": .9},
        ],
    })

    governed = data_ops.govern_data_operations_proposal(proposal, _evidence())

    assert governed["accepted"][0]["request_ids"] == [11, 12]
    assert governed["accepted"][1]["execution"] == "existing_autonomous_healing"
    assert governed["accepted"][2]["request_ids"] == [14]
    assert governed["accepted"][3]["categories"] == ["permission"]
    assert governed["accepted"][3]["affected_apis"] == ["fina_indicator"]


def test_low_confidence_retry_cannot_cross_governance_gate() -> None:
    proposal = data_ops._normalise_proposal({
        "actions": [{"action": "retry_transient_requests", "confidence": .4}],
    })

    governed = data_ops.govern_data_operations_proposal(proposal, _evidence())

    assert governed["accepted"] == []
    assert governed["rejected"][0]["reason"] == "insufficient_confidence_or_no_safe_requests"


def test_executor_uses_deterministic_ids_and_never_executes_escalation() -> None:
    calls = []
    governance = {
        "accepted": [
            {"action": "retry_transient_requests", "request_ids": [11, 12]},
            {"action": "delegate_hard_data_repair", "execution": "existing_autonomous_healing"},
            {"action": "resolve_superseded_requests", "request_ids": [14]},
            {"action": "escalate_external_dependency", "categories": ["permission"], "affected_apis": ["fina_indicator"]},
        ]
    }

    result = data_ops.execute_governed_data_operations(
        governance,
        retry_runner=lambda ids, **kwargs: calls.append((ids, kwargs)) or {"ok": True, "attempted": 2},
        superseded_runner=lambda ids, **kwargs: {"ok": True, "resolved": len(ids)},
    )

    assert calls == [([11, 12], {"limit": data_ops.MAX_RETRY_REQUESTS})]
    assert result["status"] == "escalated"
    assert result["actions"][1]["status"] == "delegated"
    assert result["actions"][2]["result"]["resolved"] == 1
    assert result["escalations"][0]["required_human_authority"]


def test_governance_rejects_retry_when_only_permission_failures_exist() -> None:
    evidence = _evidence()
    evidence["safe_retry_candidates"] = []
    proposal = data_ops._normalise_proposal({
        "actions": [{"action": "retry_transient_requests", "confidence": .99}],
    })

    governed = data_ops.govern_data_operations_proposal(proposal, evidence)

    assert governed["accepted"] == []


def test_deterministic_fallback_preserves_same_safety_routes() -> None:
    proposal = data_ops.deterministic_data_operations_fallback(_evidence(), reason="llm_response_not_json")

    assert [item["action"] for item in proposal["actions"]] == [
        "retry_transient_requests",
        "resolve_superseded_requests",
        "delegate_hard_data_repair",
        "escalate_external_dependency",
    ]
    assert proposal["limitations"] == ["llm_response_not_json"]


def test_production_data_operations_never_calls_llm(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE llm_data_operation_cases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,case_key TEXT UNIQUE,status TEXT,
        evidence_json TEXT,proposal_json TEXT,governance_json TEXT,result_json TEXT,
        model TEXT,created_at TEXT,updated_at TEXT
    )""")

    class Wrapper:
        def execute(self, sql, params=()):
            return connection.execute(sql, params)

    @contextmanager
    def open_connection():
        yield Wrapper()
        connection.commit()

    evidence = _evidence()
    calls = []
    monkeypatch.setattr(data_ops, "_conn", open_connection)
    monkeypatch.setattr(data_ops, "_ensure_table", lambda: None)
    monkeypatch.setattr(data_center, "resolve_covered_failed_tushare_requests", lambda **_kwargs: {"ok": True, "resolved": 0})
    monkeypatch.setattr(data_center, "reconcile_terminal_ingestion_runs", lambda **_kwargs: {"ok": True, "reconciled": 0})
    monkeypatch.setattr(data_center, "resolve_legacy_daily_index_weight_requests", lambda **_kwargs: {"ok": True, "resolved": 0})
    monkeypatch.setattr(data_center, "resolve_legacy_period_only_financial_requests", lambda **_kwargs: {"ok": True, "resolved": 0})
    monkeypatch.setattr(data_center, "resolve_mature_fund_nav_empty_requests", lambda **_kwargs: {"ok": True, "resolved": 0})
    monkeypatch.setattr(data_center, "resolve_mature_optional_daily_empty_requests", lambda **_kwargs: {"ok": True, "resolved": 0})
    monkeypatch.setattr(data_ops, "build_data_operations_evidence", lambda **_kwargs: evidence)
    monkeypatch.setattr(data_ops, "_call_deepseek_json", lambda **_kwargs: calls.append(_kwargs))
    monkeypatch.setattr(
        data_ops,
        "execute_governed_data_operations",
        lambda _governance: {"ok": True, "status": "completed", "actions": [], "escalations": []},
    )

    result = data_ops.run_deterministic_data_operations_cycle(cycle_steps=[])

    assert calls == []
    assert result["execution_mode"] == "deterministic"
    assert result["llm_advisory"]["status"] == "detached_from_production_cycle"
    assert result["covered_failure_cleanup"]["resolved"] == 0
    assert result["ingestion_run_cleanup"]["reconciled"] == 0
    assert result["index_weight_cleanup"]["resolved"] == 0
    assert result["financial_request_cleanup"]["resolved"] == 0
    assert result["fund_nav_cleanup"]["resolved"] == 0
    assert result["optional_daily_cleanup"]["resolved"] == 0
    assert connection.execute("SELECT model FROM llm_data_operation_cases").fetchone()[0] == "deterministic_governance"


def test_covered_failure_reconciliation_requires_the_exact_request_key(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_failed_requests(
            id INTEGER PRIMARY KEY,request_key TEXT,status TEXT,resolved_at TEXT
        );
        CREATE TABLE dc_tushare_request_coverage(
            request_key TEXT PRIMARY KEY,result_status TEXT
        );
        INSERT INTO dc_tushare_failed_requests VALUES(1,'covered','pending',NULL);
        INSERT INTO dc_tushare_failed_requests VALUES(2,'different','pending',NULL);
        INSERT INTO dc_tushare_request_coverage VALUES('covered','success');
        INSERT INTO dc_tushare_request_coverage VALUES('different-params','success');
    """)

    class Wrapper:
        def execute(self, sql, params=()):
            return connection.execute(sql, params)

        raw = connection

    @contextmanager
    def open_connection():
        yield Wrapper()
        connection.commit()

    monkeypatch.setattr(data_center, "_conn", open_connection)
    monkeypatch.setattr(data_center, "redis_set_remove", lambda *_args: None)

    result = data_center.resolve_covered_failed_tushare_requests(limit=10)

    assert result["resolved"] == 1
    assert connection.execute("SELECT status FROM dc_tushare_failed_requests WHERE id=1").fetchone()[0] == "resolved"
    assert connection.execute("SELECT status FROM dc_tushare_failed_requests WHERE id=2").fetchone()[0] == "pending"


def test_targeted_retry_enforces_category_attempt_and_batch_boundaries(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE dc_tushare_failed_requests(
        id INTEGER PRIMARY KEY,api_name TEXT,params_json TEXT,limit_rows INTEGER,error_category TEXT,
        last_error TEXT,status TEXT,attempts INTEGER,resumed_at TEXT,last_failed_at TEXT
    )""")
    rows = [
        (1, "daily", json.dumps({"trade_date": "20260723"}), None, "transient", "timeout", "pending", 1, None, "2026-07-23T15:00:00"),
        (2, "daily_basic", "{}", None, "permission", "forbidden", "pending", 1, None, "2026-07-23T15:00:00"),
        (3, "moneyflow", "{}", None, "empty_pending", "empty", "pending", 5, None, "2026-07-23T15:00:00"),
        (4, "fund_nav", "{}", None, "empty_pending", "empty", "pending", 4, None, "2026-07-23T15:00:00"),
    ]
    connection.executemany("INSERT INTO dc_tushare_failed_requests VALUES(?,?,?,?,?,?,?,?,?,?)", rows)

    class Wrapper:
        def execute(self, sql, params=()):
            return connection.execute(sql, params)

    @contextmanager
    def open_connection():
        yield Wrapper()
        connection.commit()

    calls = []
    monkeypatch.setattr(data_center, "_conn", open_connection)
    def sync(api_name, **kwargs):
        calls.append((api_name, kwargs))
        if api_name == "fund_nav":
            raise RuntimeError("返回为空")
        return {"ok": True}

    monkeypatch.setattr(data_center, "sync_tushare_api_raw", sync)
    monkeypatch.setattr(data_center, "_defer_empty_request_if_exhausted", lambda *_args, **_kwargs: True)

    result = data_center.resume_safe_failed_tushare_requests_by_id([1, 2, 3, 4], limit=5)

    assert [item[0] for item in calls] == ["daily", "fund_nav"]
    assert result["attempted"] == 2
    assert result["results"][1]["status"] == "deferred_empty"
    assert {item["reason"] for item in result["skipped"]} == {
        "category_not_safe_for_autonomous_retry", "maximum_attempts_reached",
    }


def test_superseded_resolver_only_closes_registered_api(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE dc_tushare_failed_requests(
            id INTEGER PRIMARY KEY,request_key TEXT,api_name TEXT,params_json TEXT,limit_rows INTEGER,
            status TEXT,resolved_at TEXT
        );
        CREATE TABLE dc_tushare_request_coverage(
            request_key TEXT PRIMARY KEY,api_name TEXT,ts_code TEXT,trade_date TEXT,period TEXT,
            params_json TEXT,rows_read INTEGER,result_status TEXT,result_reason TEXT,checked_at TEXT
        );
    """)
    params = json.dumps({"trade_date": "20200102"}, sort_keys=True, ensure_ascii=False)
    connection.execute("INSERT INTO dc_tushare_failed_requests VALUES(1,'k1','etf_sh_cons',?,NULL,'pending',NULL)", (params,))
    connection.execute("INSERT INTO dc_tushare_failed_requests VALUES(2,'k2','unknown_api','{}',NULL,'pending',NULL)")

    class Wrapper:
        def execute(self, sql, values=()):
            return connection.execute(sql, values)

    @contextmanager
    def open_connection():
        yield Wrapper()
        connection.commit()

    monkeypatch.setattr(data_center, "_conn", open_connection)
    monkeypatch.setattr(data_center, "redis_set_remove", lambda *_args: None)
    monkeypatch.setattr(data_center, "_request_key", lambda api, *_args: "k1" if api == "etf_sh_cons" else "k2")

    result = data_center.resolve_superseded_failed_requests_by_id([1, 2])

    assert result["resolved"] == 1
    assert result["results"][0]["replacement"] == "exchange_official_pcf"
    assert result["skipped"][0]["reason"] == "api_not_registered_as_superseded"
    assert connection.execute("SELECT status FROM dc_tushare_failed_requests WHERE id=1").fetchone()[0] == "resolved"
