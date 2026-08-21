from __future__ import annotations

from copy import deepcopy

import pytest

from app.data_platform.services.decision_data_contract import (
    DECISION_DATA_CONTRACT_SCHEMA_VERSION,
    REQUIRED_DECISION_APIS,
    REQUIRED_DECISION_FIELDS,
    _normalize_observation_cutoff,
    _empty_evidence,
    decision_data_contract,
)


class _Result:
    def __init__(self, *, one=None, many=None):
        self._one = one
        self._many = many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


def _complete_evidence(stock_count: int = 5518) -> dict[str, int]:
    evidence = _empty_evidence()
    evidence.update({
        "snapshot_rows": stock_count,
        "snapshot_observation_rows": stock_count,
        "daily_execution_price_rows": stock_count,
    })
    for api in REQUIRED_DECISION_APIS:
        evidence[f"{api}_raw_rows"] = stock_count
        evidence[f"{api}_raw_observation_rows"] = stock_count
        evidence[f"{api}_snapshot_complete_rows"] = stock_count
        evidence[f"{api}_qualified_rows"] = stock_count
        for raw_field, _snapshot_field in REQUIRED_DECISION_FIELDS[api]:
            evidence[f"{api}_raw_{raw_field}_rows"] = stock_count
            evidence[f"{api}_snapshot_{raw_field}_rows"] = stock_count
    return evidence


class _ContractConn:
    def __init__(
        self,
        *,
        stock_count: int = 5518,
        evidence: dict[str, int] | None = None,
        api_counts: dict[str, int] | None = None,
        strict_error: Exception | None = None,
    ):
        self.stock_count = stock_count
        self.evidence = deepcopy(evidence if evidence is not None else _complete_evidence(stock_count))
        self.api_counts = dict(api_counts or {api: stock_count for api in REQUIRED_DECISION_APIS})
        self.strict_error = strict_error
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if sql in {
            "SAVEPOINT decision_base_counts",
            "ROLLBACK TO SAVEPOINT decision_base_counts",
            "RELEASE SAVEPOINT decision_base_counts",
            "SAVEPOINT decision_field_lineage",
            "ROLLBACK TO SAVEPOINT decision_field_lineage",
            "RELEASE SAVEPOINT decision_field_lineage",
            "SAVEPOINT decision_strict_evidence",
            "ROLLBACK TO SAVEPOINT decision_strict_evidence",
            "RELEASE SAVEPOINT decision_strict_evidence",
        }:
            return _Result()
        if "FROM dc_stock_master" in sql and "COUNT(*) AS count" in sql:
            return _Result(one={"count": self.stock_count})
        if "GROUP BY raw.api_name,raw.source_code" in sql:
            return _Result(many=[
                {"api_name": api, "source_code": "tushare", "rows": rows}
                for api, rows in self.api_counts.items()
            ])
        if "GROUP BY raw.api_name" in sql:
            return _Result(many=[
                {"api_name": api, "rows": rows}
                for api, rows in self.api_counts.items()
            ])
        if "jsonb_each_text" in sql:
            return _Result(many=[])
        if "qualified_evidence AS (" in sql:
            if self.strict_error:
                raise self.strict_error
            return _Result(one=self.evidence)
        raise AssertionError(sql)


def _contract(conn: _ContractConn) -> dict:
    return decision_data_contract(
        "20260724",
        conn=conn,
        as_of_time="2026-07-24T18:00:00+08:00",
    )


def test_decision_contract_rejects_present_but_incomplete_stk_limit():
    evidence = _complete_evidence()
    evidence["stk_limit_raw_rows"] = 5462
    evidence["stk_limit_raw_observation_rows"] = 5462
    evidence["stk_limit_snapshot_complete_rows"] = 5462
    evidence["stk_limit_qualified_rows"] = 5462
    for raw_field, _snapshot_field in REQUIRED_DECISION_FIELDS["stk_limit"]:
        evidence[f"stk_limit_raw_{raw_field}_rows"] = 5462
        evidence[f"stk_limit_snapshot_{raw_field}_rows"] = 5462

    result = _contract(_ContractConn(evidence=evidence))

    assert result["ok"] is False
    assert result["schema_version"] == DECISION_DATA_CONTRACT_SCHEMA_VERSION
    assert result["missing_apis"] == []
    assert result["incomplete_apis"] == ["stk_limit"]
    assert result["counts"]["stk_limit"] == 5462
    assert result["minimum_rows_by_api"]["stk_limit"] == 5463
    assert result["coverage"]["stk_limit"] == round(5462 / 5518, 6)
    assert result["blocking_reasons"] == ["insufficient_stock_coverage", "missing_required_fields", "unverifiable_observation_evidence"]


def test_contract_uses_per_api_coverage_not_legacy_eighty_percent_threshold():
    evidence = _complete_evidence()
    # 99.49% is far above the retired 80% gate but below daily's 99.5% rule.
    for key in ("daily_raw_rows", "daily_raw_observation_rows", "daily_snapshot_complete_rows", "daily_qualified_rows"):
        evidence[key] = 5490
    for raw_field, _snapshot_field in REQUIRED_DECISION_FIELDS["daily"]:
        evidence[f"daily_raw_{raw_field}_rows"] = 5490
        evidence[f"daily_snapshot_{raw_field}_rows"] = 5490

    result = _contract(_ContractConn(evidence=evidence))

    assert result["ok"] is False
    assert result["minimum_coverage_by_api"] == {
        "daily": 0.995, "daily_basic": 0.95, "stk_limit": 0.99,
    }
    assert result["minimum_rows_by_api"]["daily"] == 5491
    assert result["incomplete_apis"] == ["daily"]
    assert result["coverage"]["daily"] == round(5490 / 5518, 6)


def test_contract_rejects_required_field_gap_but_not_negative_earnings_pe_pb_nulls():
    evidence = _complete_evidence()
    # pe/pb are deliberately not decision-data hard fields: loss-making firms
    # legitimately have null PE and must not make the entire market unusable.
    # In contrast, the exact daily amount field is required for liquidity.
    evidence["daily_raw_amount_rows"] = 5400
    evidence["daily_snapshot_amount_rows"] = 5400

    result = _contract(_ContractConn(evidence=evidence))

    assert result["ok"] is False
    assert result["incomplete_apis"] == []
    assert result["field_incomplete_apis"] == ["daily"]
    assert result["field_coverage"]["daily"]["amount"]["ready"] is False
    assert "pe_ttm" not in result["required_fields"]["daily_basic"]
    assert "pb" not in result["required_fields"]["daily_basic"]
    assert result["blocking_reasons"] == ["missing_required_fields"]


def test_contract_fails_closed_when_observation_migration_is_unavailable():
    result = _contract(_ContractConn(strict_error=RuntimeError("column observed_at does not exist")))

    assert result["ok"] is False
    assert result["missing_apis"] == []
    assert result["observation_contract"]["strict_schema_ready"] is False
    assert result["observation_contract"]["query_error_type"] == "RuntimeError"
    assert result["blocking_reasons"] == [
        "pit_observation_schema_unavailable",
        "insufficient_stock_coverage",
        "missing_required_fields",
        "insufficient_snapshot_coverage",
    ]


def test_contract_requires_observation_evidence_before_the_frozen_cutoff():
    evidence = _complete_evidence()
    # Raw rows are present, but none is proven observed before plan freeze.
    for api in REQUIRED_DECISION_APIS:
        evidence[f"{api}_raw_observation_rows"] = 0
        evidence[f"{api}_qualified_rows"] = 0
    evidence["snapshot_observation_rows"] = 0
    for api in REQUIRED_DECISION_APIS:
        evidence[f"{api}_snapshot_complete_rows"] = 0
        for raw_field, _snapshot_field in REQUIRED_DECISION_FIELDS[api]:
            evidence[f"{api}_raw_{raw_field}_rows"] = 0
            evidence[f"{api}_snapshot_{raw_field}_rows"] = 0

    result = _contract(_ContractConn(evidence=evidence))

    assert result["ok"] is False
    assert result["raw_rows"]["daily"] == 5518
    assert result["raw_observation_rows"]["daily"] == 0
    assert result["observation_incomplete_apis"] == list(REQUIRED_DECISION_APIS)
    assert "unverifiable_observation_evidence" in result["blocking_reasons"]
    assert result["observation_contract"]["cutoff"] == "2026-07-24T18:00:00+08:00"


def test_contract_rejects_naive_explicitly_supplied_observation_cutoff():
    result = decision_data_contract(
        "20260724",
        conn=_ContractConn(),
        as_of_time="2026-07-24T18:00:00",
    )

    assert result["ok"] is False
    assert "invalid_observation_cutoff" in result["blocking_reasons"]


def test_default_contract_targets_exchange_calendar_not_latest_snapshot(monkeypatch):
    from app.data_platform.services import market_calendar

    monkeypatch.setattr(market_calendar, "target_trade_date", lambda _conn: "20260724")

    result = decision_data_contract(
        conn=_ContractConn(),
        as_of_time="2026-07-24T18:00:00+08:00",
    )

    assert result["latest_trade_date"] == "20260724"


def test_observation_cutoff_normalizes_offsets_but_never_guesses_one():
    assert _normalize_observation_cutoff("2026-07-24T10:00:00Z") == (
        "2026-07-24T18:00:00+08:00", True,
    )
    assert _normalize_observation_cutoff("2026-07-24T18:00:00") == ("", False)


def test_contract_query_guards_bad_persisted_timestamp_before_casting():
    conn = _ContractConn()
    result = _contract(conn)

    assert result["ok"] is True
    strict_sql, strict_params = next(
        (sql, params)
        for sql, params in conn.queries
        if "qualified_evidence AS (" in sql
    )
    assert "~ '(Z|[+-][0-9]{2}:[0-9]{2})$'" in strict_sql
    assert "CASE WHEN" in strict_sql
    assert "pg_input_is_valid" in strict_sql
    assert "created_at" in strict_sql
    # A version cannot be selected merely because its update timestamp happens
    # to predate the frozen plan. Its first observation and economic
    # availability must also be visible at that same instant.
    assert "raw.first_observed_at" in strict_sql
    assert "raw.economic_available_time" in strict_sql
    assert "snapshot.first_observed_at" in strict_sql
    assert "snapshot.economic_available_time" in strict_sql
    assert "FROM dc_tushare_raw_records accepted" in strict_sql
    assert "FROM dc_stock_daily_snapshot accepted" in strict_sql
    assert "ROW_NUMBER() OVER" not in strict_sql
    assert "COUNT(DISTINCT" not in strict_sql
    assert "raw_flags AS MATERIALIZED" in strict_sql
    assert "snapshot_flags AS MATERIALIZED" in strict_sql
    assert "COUNT(*) FILTER" in strict_sql
    assert "bool_or" in strict_sql
    assert "FILTER (WHERE api_name='daily')" in strict_sql
    # The source sets are reduced before their only cross-source join.  A
    # full join forces PostgreSQL to use hash/merge semantics even when the
    # strict timestamp predicates cause a very low CTE cardinality estimate;
    # null-extended rows cannot pass the qualified r/s conjunctions.
    assert "FULL OUTER JOIN snapshot_flags s ON s.ts_code=r.ts_code" in strict_sql
    assert "FROM raw_flags r\n              FULL OUTER JOIN snapshot_flags s" in strict_sql
    assert "qualified AS (" not in strict_sql
    assert "daily_execution_price_rows" in strict_sql
    assert "'double precision'" in strict_sql
    assert "< 'Infinity'::double precision" in strict_sql
    # The mutable accepted projection selects the content hash, but the
    # immutable evidence must still belong to that same market session.  A
    # provider correction must not let a retained old version be counted for
    # a rewritten projection date.
    all_contract_sql = "\n".join(sql for sql, _params in conn.queries)
    assert all_contract_sql.count("raw.trade_date=accepted.trade_date") == 4
    assert strict_params == (
        "20260724", "2026-07-24T18:00:00+08:00", "2026-07-24T18:00:00+08:00",
        "2026-07-24T18:00:00+08:00", "2026-07-24T18:00:00+08:00",
        "20260724", "2026-07-24T18:00:00+08:00", "2026-07-24T18:00:00+08:00",
        "2026-07-24T18:00:00+08:00", "2026-07-24T18:00:00+08:00",
    )


def test_contract_exposes_execution_only_price_coverage_from_same_strict_query():
    result = _contract(_ContractConn())

    assert result["execution_price_rows"] == 5518


def test_contract_rolls_back_base_diagnostic_timeout_before_reusing_caller_connection():
    """A bounded diagnostic timeout must not poison the scheduler's connection."""

    class TimeoutThenRecoveringConn(_ContractConn):
        def __init__(self):
            super().__init__()
            self.poisoned = False
            self.timed_out = False

        def execute(self, sql, params=()):
            self.queries.append((sql, params))
            if sql == "SAVEPOINT decision_base_counts":
                return _Result()
            if sql == "ROLLBACK TO SAVEPOINT decision_base_counts":
                self.poisoned = False
                return _Result()
            if sql == "RELEASE SAVEPOINT decision_base_counts":
                return _Result()
            if self.poisoned:
                raise RuntimeError("current transaction is aborted")
            if "GROUP BY raw.api_name" in sql and "raw.source_code" not in sql and not self.timed_out:
                self.timed_out = True
                self.poisoned = True
                raise RuntimeError("canceling statement due to statement timeout")
            if sql == "SELECT 1":
                return _Result(one={"?column?": 1})
            return super().execute(sql, params)

    conn = TimeoutThenRecoveringConn()

    result = _contract(conn)

    assert result["ok"] is False
    assert conn.poisoned is False
    assert [sql for sql, _params in conn.queries if "decision_base_counts" in sql] == [
        "SAVEPOINT decision_base_counts",
        "ROLLBACK TO SAVEPOINT decision_base_counts",
        "RELEASE SAVEPOINT decision_base_counts",
    ]
    assert not any(
        "qualified_evidence AS (" in sql
        for sql, _params in conn.queries
    )
    assert conn.execute("SELECT 1").fetchone() == {"?column?": 1}


def test_contract_does_not_hide_a_failed_base_diagnostic_savepoint_recovery():
    """If the recovery itself fails, the enclosing data connection must rollback."""

    class BrokenRecoveryConn(_ContractConn):
        def execute(self, sql, params=()):
            self.queries.append((sql, params))
            if sql == "SAVEPOINT decision_base_counts":
                return _Result()
            if "GROUP BY raw.api_name" in sql and "raw.source_code" not in sql:
                raise RuntimeError("canceling statement due to statement timeout")
            if sql == "ROLLBACK TO SAVEPOINT decision_base_counts":
                raise RuntimeError("savepoint recovery failed")
            return super().execute(sql, params)

    with pytest.raises(RuntimeError, match="savepoint recovery failed"):
        _contract(BrokenRecoveryConn())
