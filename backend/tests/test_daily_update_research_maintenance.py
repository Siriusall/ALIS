import sys
from types import SimpleNamespace

from app.data_platform.services import daily_update


def test_daily_research_maintenance_enriches_only_incomplete_candidates(monkeypatch):
    calls = []
    monkeypatch.setenv("ALIS_DAILY_RESEARCH_LIMIT", "3")
    monkeypatch.setenv("ALIS_DAILY_CANDIDATE_ENRICH_LIMIT", "2")
    monkeypatch.setattr(
        daily_update,
        "financial_research_completeness",
        lambda code, **_kwargs: {"status": "complete" if code == "000001.SZ" else "partial"},
    )
    monkeypatch.setattr(
        daily_update,
        "enrich_financial_research_fields",
        lambda code, **_kwargs: calls.append(code) or {"requested": 7, "after": {"status": "complete"}},
    )
    monkeypatch.setitem(sys.modules, "app.services.stock_screener", SimpleNamespace(
        run_daily_screener=lambda **kwargs: {"ok": True, "trade_date": kwargs["trade_date"]},
    ))
    monkeypatch.setitem(sys.modules, "app.services.research_workflow", SimpleNamespace(
        stage_watch_pool_candidates=lambda **_kwargs: {"ok": True},
        process_watch_pool=lambda **_kwargs: {"ok": True},
    ))
    monkeypatch.setitem(sys.modules, "app.services.decision_store", SimpleNamespace(
        list_pool_members=lambda _pool: [
            {"ts_code": "000001.SZ"},
            {"ts_code": "000002.SZ"},
            {"ts_code": ""},
            {"ts_code": "000003.SZ"},
        ],
    ))

    result = daily_update._run_research_maintenance("20260811")

    assert calls == ["000002.SZ", "000003.SZ"]
    assert result["screen"]["trade_date"] == "20260811"
    assert result["candidate_enrichment"] == [
        {"ts_code": "000002.SZ", "before": "partial", "after": "complete", "requested": 7},
        {"ts_code": "000003.SZ", "before": "partial", "after": "complete", "requested": 7},
    ]
