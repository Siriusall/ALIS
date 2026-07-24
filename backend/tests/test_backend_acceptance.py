from __future__ import annotations

from app.services import backend_acceptance


def test_backend_acceptance_marks_hsmm_partial_until_live_shadow_ready(monkeypatch):
    class _Cursor:
        def execute(self, sql: str):
            self.sql = sql
            return self

        def fetchone(self):
            sql = self.sql
            if "dc_theme_snapshot" in sql:
                return {"count": 1, "promotable": 1}
            if "dc_event_followup" in sql:
                return {"count": 1, "missing_date": 0}
            if "COUNT(DISTINCT ts_code)" in sql:
                return {"count": 5}
            return {"count": 1, "total": 1, "missing": False, "unique_count": 0}

    class _Conn:
        def __enter__(self):
            return _Cursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(backend_acceptance, "data_conn", lambda: _Conn())
    monkeypatch.setattr(backend_acceptance, "business_conn", lambda: _Conn())
    monkeypatch.setattr(backend_acceptance, "get_account_baseline", lambda: 200000.0)
    monkeypatch.setattr(backend_acceptance, "list_model_versions", lambda model_key, limit=10: [{"version": "hsmm-shadow-test"}])
    monkeypatch.setattr(
        backend_acceptance,
        "market_state_model_governance",
        lambda: {"hsmm": {"status": "shadow_live_observation", "latest_shadow_version": "hsmm-shadow-test", "validation": {"status": "passed"}, "live_observation_weeks": 3, "minimum_live_observation_weeks": 8}},
    )

    result = backend_acceptance.backend_acceptance_matrix()
    hsmm = next(item for item in result["checks"] if item["key"] == "P4_HSMM")

    assert hsmm["status"] == "partial"
    assert hsmm["evidence"]["live_observation_weeks"] == 3
