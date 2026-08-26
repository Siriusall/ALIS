from __future__ import annotations

import inspect
import json
from contextlib import contextmanager

from app.services import (
    caam_ocr_data,
    continuous_strategy_research,
    decision_store,
    factor_strategy_evolution,
    llm_research_governance,
    manual_review_queue,
    performance_reports,
    theme_engine,
    thesis_store,
)


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql = []
        self.params = []

    def execute(self, sql, params=()):
        self.sql.append(str(sql))
        self.params.append(tuple(params) if params else ())
        return _Cursor(self.rows)


def test_market_state_history_uses_requested_date_window(monkeypatch):
    conn = _Conn([{"trade_date": "2026-08-01", "market_state": "risk_on", "state_confidence": 0.8, "model_version": "v1"}])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(performance_reports, "data_conn", fake_conn)
    result = performance_reports.load_market_state_history(start_date="2026-08-01", end_date="2026-08-05")

    assert "SELECT *" not in conn.sql[0].upper()
    assert "trade_date >= ?" in conn.sql[0]
    assert "trade_date <= ?" in conn.sql[0]
    assert conn.params[0] == ("20260801", "20260805")
    assert result["20260801"]["state"] == "risk_on"


def test_performance_validation_reads_complete_history(monkeypatch):
    conn = _Conn([])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(performance_reports, "business_conn", fake_conn)
    result = performance_reports.validate_performance_reports()

    assert result["ok"] is False
    sql = conn.sql[0].upper()
    assert "SELECT *" not in sql
    assert "LIMIT" not in sql


def test_manual_review_triage_does_not_silently_drop_old_state(monkeypatch):
    conn = _Conn([])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(manual_review_queue, "_TRIAGE_TABLE_READY", True)
    monkeypatch.setattr(manual_review_queue, "_conn", fake_conn)
    assert manual_review_queue.list_manual_review_triage() == {}

    sql = conn.sql[0].upper()
    assert "SELECT *" not in sql
    assert "ORDER BY UPDATED_AT DESC" in sql
    assert "LIMIT" not in sql


def test_pool_members_default_read_preserves_complete_worker_semantics(monkeypatch):
    conn = _Conn([])

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(decision_store, "_conn", fake_conn)
    assert decision_store.list_pool_members("candidate") == []
    assert "LIMIT" not in conn.sql[0].upper()
    assert conn.params[0] == ("candidate",)


def test_performance_validation_catches_anomaly_in_any_period_type(monkeypatch):
    rows = [
        {"id": 1, "period_type": "weekly", "payload_json": json.dumps({
            "return_pct": 1, "max_drawdown_pct": -1, "turnover_pct": 2, "total_cost": 3,
        })},
        {"id": 2, "period_type": "monthly", "payload_json": json.dumps({
            "return_pct": 1, "max_drawdown_pct": -1, "turnover_pct": 2, "total_cost": 3,
        })},
        {"id": 3, "period_type": "yearly", "payload_json": json.dumps({
            "return_pct": 1, "max_drawdown_pct": -1, "turnover_pct": 2,
        })},
    ]
    conn = _Conn(rows)

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(performance_reports, "business_conn", fake_conn)
    result = performance_reports.validate_performance_reports()

    assert result["ok"] is False
    yearly = next(item for item in result["checks"] if item["period_type"] == "yearly")
    assert yearly["status"] == "partial"
    assert yearly["field_coverage"]["total_cost"] == 0


class _RoutingConn:
    def __init__(self, handler):
        self.handler = handler
        self.sql = []
        self.params = []

    def execute(self, sql, params=()):
        self.sql.append(str(sql))
        self.params.append(tuple(params))
        return _Cursor(self.handler(str(sql), tuple(params)))


def test_fifo_buy_outcome_uses_exit_after_old_5000_boundary(monkeypatch):
    target = {
        "id": 6001, "symbol": "600000", "side": "BUY", "quantity": 10,
        "price": 10.0, "at": "2026-01-01T10:00:00", "execution_id": 1,
        "commission": 0, "transfer_fee": 0, "stamp_tax": 0,
    }
    exit_row = {
        "id": 11002, "symbol": "600000", "side": "SELL", "quantity": 10,
        "price": 12.0, "at": "2026-02-01T10:00:00",
        "commission": 0, "transfer_fee": 0, "stamp_tax": 0,
    }

    def handler(sql, params):
        if "WHERE id=?" in sql:
            return [target]
        if "WHERE symbol=?" in sql:
            return [target, exit_row]
        return []

    conn = _RoutingConn(handler)

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(decision_store, "_conn", fake_conn)
    outcome = decision_store._matched_execution_outcome({"operation_id": 6001})

    assert outcome["status"] == "realized"
    assert outcome["matched_quantity"] == 10
    assert outcome["realized_pnl"] == 20.0
    ledger_sql = next(sql for sql in conn.sql if "WHERE symbol=?" in sql)
    assert "LIMIT" not in ledger_sql.upper()


def test_completed_v10_candidates_keep_snapshot_and_backtests_on_same_keys(monkeypatch):
    candidate = {
        "candidate_id": "candidate-a", "snapshot_id": "snapshot-a",
        "factor_weights": {"quality": 1.0},
    }

    def handler(sql, params):
        if "FROM strategy_research_waves" in sql:
            return [{"candidate_ids_json": json.dumps(["candidate-a"])}]
        if "FROM strategy_research_events" in sql:
            return [{
                "event_id": "event-a", "source_id": "candidate-a",
                "payload_json": json.dumps(candidate),
            }]
        if "FROM strategy_environment_backtests" in sql:
            return [{
                "candidate_id": "candidate-a", "environment_id": "env-a",
                "result_json": json.dumps({"environment_id": "env-a", "return_pct": 3}),
            }]
        return []

    conn = _RoutingConn(handler)

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(continuous_strategy_research, "_conn", fake_conn)
    monkeypatch.setattr(continuous_strategy_research, "ensure_strategy_research_tables", lambda: None)
    result = continuous_strategy_research._load_completed_v10_candidates("snapshot-a")

    assert [item["candidate_id"] for item in result] == ["candidate-a"]
    assert result[0]["environment_results"] == [{"environment_id": "env-a", "return_pct": 3}]
    assert all("candidate-a" in params for sql, params in zip(conn.sql, conn.params)
               if "IN (" in sql)


def test_candidate_event_uses_exact_business_key_before_legacy_null_history(monkeypatch):
    target = {
        "event_id": "target", "source_id": "candidate-new", "status": "completed",
        "payload_json": json.dumps({"candidate_id": "candidate-new"}),
    }

    def handler(sql, params):
        if "source_id=?" in sql:
            return [target]
        raise AssertionError("exact candidate lookup must not scan NULL legacy history")

    conn = _RoutingConn(handler)

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(continuous_strategy_research, "_conn", fake_conn)
    monkeypatch.setattr(continuous_strategy_research, "ensure_strategy_research_tables", lambda: None)

    assert continuous_strategy_research._candidate_event("candidate-new")["event_id"] == "target"


def test_factor_lineage_reads_members_and_links_for_same_combination_keys(monkeypatch):
    def handler(sql, params):
        if "FROM factor_combinations" in sql:
            return [{
                "combination_key": "combo-a", "metrics_json": "{}", "lineage_json": "{}",
            }]
        if "FROM factor_combination_members" in sql:
            assert params == ("combo-a",)
            return [{
                "combination_key": "combo-a", "factor_key": "quality",
                "evidence_json": "{}",
            }]
        if "FROM factor_strategy_links" in sql:
            assert params == ("combo-a",)
            return [{
                "combination_key": "combo-a", "model_version_id": 7,
                "evidence_json": "{}",
            }]
        return []

    conn = _RoutingConn(handler)

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(factor_strategy_evolution, "_conn", fake_conn)
    monkeypatch.setattr(factor_strategy_evolution, "_ensure_tables", lambda: None)
    result = factor_strategy_evolution.factor_strategy_lineage_status()

    assert result["combinations"][0]["members"][0]["factor_key"] == "quality"
    assert result["strategy_links"][0]["model_version_id"] == 7
    assert result["possibly_truncated"] is False


def test_ocr_audit_reconciles_all_same_field_peers_without_global_cap(monkeypatch):
    rows = [
        {
            "source_code": caam_ocr_data.CAAM_OCR_SOURCE, "document_id": "doc-a",
            "field_code": "vehicle_sales_10k", "observation_date": "2026-07-31",
            "value": 200.0, "confidence": .9, "validation_status": "pending",
            "evidence_json": "{}",
        },
        {
            "source_code": caam_ocr_data.CAAM_OCR_SOURCE, "document_id": "doc-b",
            "field_code": "vehicle_sales_10k", "observation_date": "2026-07-31",
            "value": 200.02, "confidence": .8, "validation_status": "pending",
            "evidence_json": "{}",
        },
    ]
    conn = _Conn(rows)

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(caam_ocr_data, "_conn", fake_conn)
    monkeypatch.setattr(caam_ocr_data, "_read_conn", fake_conn)
    result = caam_ocr_data.audit_caam_ocr_candidates()

    assert result["observations"] == 2
    assert result["group_status_counts"] == {
        "cross_document_consistent_pending_structured": 1,
    }
    assert "LIMIT" not in conn.sql[0].upper()


def test_ocr_audit_preindexes_peers_instead_of_rescanning_all_rows():
    source = inspect.getsource(caam_ocr_data.audit_caam_ocr_candidates)

    assert "peers_by_key" in source
    assert "for peer in rows" not in source


def test_theme_reads_use_autocommit_and_exhaustive_bounded_fallback():
    source = inspect.getsource(theme_engine.list_themes)
    candidate_source = inspect.getsource(theme_engine._theme_snapshot_candidates)

    assert "with _read_conn()" in source
    assert "theme_code=?" in candidate_source
    assert "as_of_date<=?" in candidate_source
    assert "as_of_date<?" in candidate_source
    assert "LIMIT ?" in candidate_source
    assert "while True" in candidate_source


def test_quality_report_batches_history_and_related_ids():
    source = inspect.getsource(
        llm_research_governance.llm_research_proposal_quality_report
    )

    assert "with _read_conn()" in source
    assert "QUALITY_REPORT_BATCH_SIZE" in source
    assert "while remaining is None or remaining > 0" in source
    assert "SELECT * FROM llm_research_proposals" not in source


def test_thesis_review_timestamp_normalizes_naive_input_to_shanghai():
    normalized = thesis_store._business_timestamp("2026-08-26T12:30:00")

    assert normalized == "2026-08-26T12:30:00+08:00"
