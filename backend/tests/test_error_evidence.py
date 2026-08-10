from app.core.error_evidence import ERROR_EVIDENCE_CONTRACT, classify_exception, error_evidence


class _Unavailable(RuntimeError):
    category = "upstream_not_ready"


def test_error_evidence_has_stable_category_and_no_zero_value_fallback():
    evidence = error_evidence(
        _Unavailable("close data not published"),
        operation="daily_update:market",
        source="tushare",
        deferred=True,
        context={"trade_date": "20260724"},
    )

    assert evidence["contract"] == ERROR_EVIDENCE_CONTRACT
    assert evidence["category"] == "data_unavailable"
    assert evidence["ok"] is False
    assert evidence["deferred"] is True
    assert evidence["context"]["trade_date"] == "20260724"
    assert "value" not in evidence


def test_contract_errors_are_not_reported_as_empty_data():
    assert classify_exception(ValueError("bad response shape")) == "contract_not_met"
    assert classify_exception(RuntimeError("unknown")) == "internal_fault"
