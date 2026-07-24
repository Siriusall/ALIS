from app.services import review_data_quality_governance as governance


def _context() -> dict:
    return {
        "period": "2026-07",
        "error_breakdown": {"review_error_types": {"数据质量": 2}},
    }


def test_data_quality_review_audits_without_repair_when_hard_contract_ready(monkeypatch) -> None:
    monkeypatch.setattr(governance, "latest_performance_learning_context", _context)
    monkeypatch.setattr(governance, "audit_oos_hard_data", lambda **_: {"status": "ready", "window_end": "20260723"})
    monkeypatch.setattr(governance, "run_data_quality_checks", lambda trade_date=None: {"checks": [{"check_name": "snapshot_coverage", "status": "ok"}]})
    monkeypatch.setattr(governance, "repair_oos_hard_data_batch", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not repair")))

    result = governance.run_review_data_quality_governance()

    assert result["ok"] is True
    assert result["status"] == "audited_no_actionable_hard_gap"
    assert result["review_data_quality_error_count"] == 2
    assert result["repair"]["attempted"] is False


def test_data_quality_review_repairs_only_bounded_explicit_hard_gaps(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(governance, "latest_performance_learning_context", _context)
    audits = iter([
        {"status": "blocked", "window_end": "20260723"},
        {"status": "blocked", "window_end": "20260723", "dates": [
            {"trade_date": "20260701", "eligible": False, "missing_apis": ["adj_factor"]},
            {"trade_date": "20260702", "eligible": False, "missing_apis": ["daily"]},
            {"trade_date": "20260703", "eligible": True},
        ]},
        {"status": "ready", "window_end": "20260723"},
    ])
    monkeypatch.setattr(governance, "audit_oos_hard_data", lambda **_: next(audits))
    monkeypatch.setattr(governance, "run_data_quality_checks", lambda trade_date=None: {"checks": []})
    monkeypatch.setattr(governance, "repair_oos_hard_data_batch", lambda dates, **kwargs: calls.append({"dates": dates, **kwargs}) or {"ok": True})

    result = governance.run_review_data_quality_governance(max_repair_dates=1)

    assert result["status"] == "repaired"
    assert result["hard_data_after"]["status"] == "ready"
    assert calls == [{"dates": ["20260701"], "date_gaps": {"20260701": {"trade_date": "20260701", "eligible": False, "missing_apis": ["adj_factor"]}, "20260702": {"trade_date": "20260702", "eligible": False, "missing_apis": ["daily"]}}}]
