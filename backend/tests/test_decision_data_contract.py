from app.data_platform.services.decision_data_contract import decision_data_contract


class _Result:
    def __init__(self, *, one=None, many=None):
        self._one = one
        self._many = many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _ContractConn:
    def execute(self, sql, _params=()):
        if "FROM dc_stock_master" in sql:
            return _Result(one={"count": 5518})
        if "GROUP BY api_name" in sql:
            return _Result(many=[
                {"api_name": "daily", "rows": 5518},
                {"api_name": "daily_basic", "rows": 5518},
                {"api_name": "stk_limit", "rows": 3864},
            ])
        if "jsonb_each_text" in sql:
            return _Result(many=[])
        if "FROM dc_stock_daily_snapshot WHERE trade_date" in sql:
            return _Result(one={
                "snapshot_rows": 5518,
                "close_rows": 5518,
                "valuation_rows": 5518,
                "limit_rows": 3864,
            })
        if "COUNT(DISTINCT daily.ts_code) AS count" in sql:
            return _Result(one={"count": 5518})
        if "COUNT(DISTINCT limits.ts_code) AS count" in sql:
            return _Result(one={"count": 3861})
        raise AssertionError(sql)


def test_decision_contract_rejects_present_but_incomplete_stk_limit():
    result = decision_data_contract("20260724", conn=_ContractConn())

    assert result["ok"] is False
    assert result["missing_apis"] == []
    assert result["incomplete_apis"] == ["stk_limit"]
    assert result["counts"]["stk_limit"] == 3861
    assert result["snapshot_limit_rows"] == 3864
    assert result["raw_limit_rows"] == 3861
    assert result["expected_rows"]["stk_limit"] == 5518
    assert result["coverage"]["stk_limit"] == round(3861 / 5518, 6)
    assert result["blocking_reasons"] == ["insufficient_stock_coverage"]
