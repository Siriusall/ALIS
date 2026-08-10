from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from app.data_platform import db
from app.services import acceptance_evidence


def test_slo_evaluation_is_persisted_in_data_platform_store():
    tempdir = tempfile.TemporaryDirectory()
    original_db_path = db.DB_PATH
    db.DB_PATH = Path(tempdir.name) / "evidence.db"

    try:
        result = acceptance_evidence.evaluate_and_record_slo(
            [{"status": "ok", "latency_ms": 12, "freshness_seconds": 10}],
            scope="unit-test",
        )
        history = acceptance_evidence.list_slo_evaluations(scope="unit-test")
    finally:
        db.DB_PATH = original_db_path
        tempdir.cleanup()

    assert result["persisted"] is True
    assert result["status"] == "ready"
    assert history["items"][0]["scope"] == "unit-test"
    assert history["items"][0]["result"]["status"] == "ready"


def test_backend_acceptance_run_is_persisted(monkeypatch):
    tempdir = tempfile.TemporaryDirectory()
    original_db_path = db.DB_PATH
    db.DB_PATH = Path(tempdir.name) / "acceptance.db"

    monkeypatch.setattr(
        acceptance_evidence,
        "backend_acceptance_matrix",
        lambda: {
            "ok": False,
            "status": "partial",
            "counts": {"passed": 1, "partial": 1, "failed": 0},
            "checks": [],
        },
    )

    try:
        result = acceptance_evidence.run_and_record_backend_acceptance(suite="unit-suite")
        with sqlite3.connect(db.DB_PATH) as conn:
            row = conn.execute("SELECT suite,status,passed_count,partial_count,failed_count FROM dc_acceptance_runs").fetchone()
    finally:
        db.DB_PATH = original_db_path
        tempdir.cleanup()

    assert result["persisted"] is True
    assert row == ("unit-suite", "partial", 1, 1, 0)
