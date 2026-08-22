from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _read_conn
from app.data_platform.services.snapshot_health import candidate_snapshot_dates
from app.services.point_in_time import normalize_explicit_timestamp


DECISION_DATA_CONTRACT_SCHEMA_VERSION = "alis_decision_data_contract_v3"
REQUIRED_DECISION_APIS = ("daily", "daily_basic", "stk_limit")

# These are deliberately per-interface.  A single 80% universe threshold let
# a partly populated daily price slice pass as a formal decision input.
DECISION_MIN_STOCK_COVERAGE_BY_API = {
    "daily": 0.995,
    "daily_basic": 0.95,
    "stk_limit": 0.99,
}

# Compatibility export for callers which imported the old scalar.  New code
# must consume ``minimum_coverage_by_api`` from the returned contract.
DECISION_MIN_STOCK_COVERAGE = min(DECISION_MIN_STOCK_COVERAGE_BY_API.values())

REQUIRED_DECISION_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    # (raw payload field, normalized snapshot field)
    "daily": (
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "close"),
        ("pre_close", "pre_close"),
        ("vol", "volume"),
        ("amount", "turnover"),
    ),
    "daily_basic": (
        ("total_mv", "total_mv"),
        ("circ_mv", "circ_mv"),
        ("turnover_rate", "turnover_rate"),
    ),
    "stk_limit": (
        ("up_limit", "up_limit"),
        ("down_limit", "down_limit"),
    ),
}

_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row else {}


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _autocommit_read_only_connection(conn: Any) -> bool:
    """Whether a standalone contract owns an already-isolated read session."""

    return bool(getattr(conn, "_autocommit_read_only", False))


def _minimum_rows(expected_rows: int, coverage: float) -> int:
    return max(1, int(ceil(max(0, expected_rows) * coverage)))


def _normalize_observation_cutoff(value: str | None) -> tuple[str, bool]:
    """Return a timezone-aware observation cutoff, or a fail-closed marker."""

    if value is None:
        return datetime.now(_BUSINESS_TZ).isoformat(timespec="seconds"), True
    raw = str(value).strip()
    if not raw:
        return "", False
    normalized = normalize_explicit_timestamp(raw)
    return (normalized, bool(normalized))


def _explicit_timestamp_predicate(alias: str, column: str) -> str:
    """Require parseable timestamp text carrying an explicit timezone."""
    value = f"NULLIF(BTRIM(COALESCE({alias}.{column}, '')), '')"
    return (
        f"{value} IS NOT NULL AND {value} ~ '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$' "
        f"AND {value} ~ "
        f"'^(19|20)[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}"
        f"(\\.[0-9]+)?(Z|[+-][0-9]{{2}}:[0-9]{{2}})$' "
        f"AND pg_input_is_valid({value}, 'timestamptz')"
    )


def _safe_timestamp_value(alias: str, column: str) -> str:
    """Avoid casting malformed persisted strings before the regex guards."""
    value = f"NULLIF(BTRIM(COALESCE({alias}.{column}, '')), '')"
    return (
        f"CASE WHEN {value} ~ '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$' "
        f"AND {value} ~ "
        f"'^(19|20)[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}"
        f"(\\.[0-9]+)?(Z|[+-][0-9]{{2}}:[0-9]{{2}})$' "
        f"AND pg_input_is_valid({value}, 'timestamptz') "
        f"THEN {value}::timestamptz END"
    )


def latest_market_trade_date(conn: Any) -> str:
    row = conn.execute("SELECT MAX(trade_date) AS value FROM dc_available_trade_dates").fetchone()
    value = str(_row_dict(row).get("value") or "")
    if value:
        return value
    row = conn.execute("SELECT MAX(trade_date) AS value FROM dc_stock_daily_snapshot").fetchone()
    return str(_row_dict(row).get("value") or "")


def _version_observation_ready_predicate(alias: str) -> str:
    """Strict append-only metadata; the CTE owns frozen-cutoff filtering."""

    return f"""
        {_explicit_timestamp_predicate(alias, 'observed_at')}
        AND {_explicit_timestamp_predicate(alias, 'first_observed_at')}
        AND {_explicit_timestamp_predicate(alias, 'economic_available_time')}
        AND NULLIF(BTRIM(COALESCE({alias}.observation_provenance, '')), '') IS NOT NULL
        AND LOWER(COALESCE({alias}.observation_provenance, '')) NOT LIKE 'legacy_%'
        AND NULLIF(BTRIM(COALESCE({alias}.content_hash, '')), '') IS NOT NULL
        AND NULLIF(BTRIM(COALESCE({alias}.source_code, '')), '') IS NOT NULL
        AND {_explicit_timestamp_predicate(alias, 'created_at')}
    """


def _raw_field_ready_predicate(alias: str, raw_field: str) -> str:
    return f"NULLIF(BTRIM({alias}.payload_json::jsonb->>'{raw_field}'), '') IS NOT NULL"


def _finite_positive_json_field_predicate(alias: str, raw_field: str) -> str:
    """Accept only finite positive numeric JSON values without unsafe casts.

    This is narrower than the regular decision-field presence contract: it is
    used by the frozen T+1 execution safety count, where ``open`` and
    ``pre_close`` must be usable prices rather than merely present strings.
    """

    value = f"NULLIF(BTRIM({alias}.payload_json::jsonb->>'{raw_field}'), '')"
    return (
        f"{value} IS NOT NULL AND pg_input_is_valid({value}, 'double precision') "
        f"AND {value}::double precision > 0 "
        f"AND {value}::double precision < 'Infinity'::double precision"
    )


def _fields_ready_predicate(alias: str, api_name: str, *, raw: bool) -> str:
    if raw:
        return " AND ".join(
            _raw_field_ready_predicate(alias, raw_field)
            for raw_field, _snapshot_field in REQUIRED_DECISION_FIELDS[api_name]
        )
    return " AND ".join(
        f"{alias}.{snapshot_field} IS NOT NULL"
        for _raw_field, snapshot_field in REQUIRED_DECISION_FIELDS[api_name]
    )


def _strict_evidence_query() -> str:
    """Build a bounded date query for field and observation-qualified coverage.

    Current raw and normalized rows are overwrite projections.  This query
    selects the latest immutable version visible at the plan's frozen
    observation cutoff, so a later repair cannot make an earlier plan appear
    complete.
    """

    # The CTE already constrains both source sets to the frozen cutoff.  Keep
    # the readiness predicate parameter-free so the query binds exactly one
    # date/cutoff pair per raw/snapshot source.
    # The visibility WHERE clauses below already require all four strict PIT
    # clocks to be parseable, explicit and no later than the frozen cutoff.
    # Repeating those same regex/pg_input_is_valid expressions in every
    # aggregate flag made this 20k-row query CPU-bound.  After that filter,
    # the original observation predicate is exactly this metadata remainder.
    # Keep the full four-clock expressions in the WHERE clauses: this only
    # removes a logically redundant re-evaluation, not a PIT requirement.
    def _visible_metadata_ready(alias: str) -> str:
        return f"""
            NULLIF(BTRIM(COALESCE({alias}.observation_provenance, '')), '') IS NOT NULL
            AND LOWER(COALESCE({alias}.observation_provenance, '')) NOT LIKE 'legacy_%'
            AND NULLIF(BTRIM(COALESCE({alias}.content_hash, '')), '') IS NOT NULL
            AND NULLIF(BTRIM(COALESCE({alias}.source_code, '')), '') IS NOT NULL
        """

    raw_observation = _visible_metadata_ready("raw")
    snapshot_observation = _visible_metadata_ready("snapshot")
    raw_observed_at = _safe_timestamp_value("raw", "observed_at")
    raw_first_observed_at = _safe_timestamp_value("raw", "first_observed_at")
    raw_economic_available_time = _safe_timestamp_value("raw", "economic_available_time")
    raw_created_at = _safe_timestamp_value("raw", "created_at")
    snapshot_observed_at = _safe_timestamp_value("snapshot", "observed_at")
    snapshot_first_observed_at = _safe_timestamp_value("snapshot", "first_observed_at")
    snapshot_economic_available_time = _safe_timestamp_value("snapshot", "economic_available_time")
    snapshot_created_at = _safe_timestamp_value("snapshot", "created_at")
    # Decode each accepted immutable payload once into the twelve narrow text
    # values used by this contract.  Materializing a full jsonb document can
    # spill the raw payload CTE on the 4 GB data node, so only the small flags
    # consumed more than once are materialized below.
    def _decoded_value(alias: str, field: str) -> str:
        return f"{alias}.{field}"

    def _raw_ready(alias: str, field: str) -> str:
        value = f"NULLIF(BTRIM({_decoded_value(alias, field)}), '')"
        return f"{value} IS NOT NULL"

    def _snapshot_ready(alias: str, field: str) -> str:
        # Preserve the existing normalized snapshot contract: a JSON key is
        # considered present even when its text is empty; the raw side keeps
        # its stricter non-empty check for source-field coverage.
        return f"{_decoded_value(alias, field)} IS NOT NULL"

    def _positive(alias: str, field: str) -> str:
        value = f"NULLIF(BTRIM({_decoded_value(alias, field)}), '')"
        return (
            f"{value} IS NOT NULL AND pg_input_is_valid({value}, 'double precision') "
            f"AND {value}::double precision > 0 "
            f"AND {value}::double precision < 'Infinity'::double precision"
        )

    raw_field_conditions: dict[str, dict[str, str]] = {}
    snapshot_field_conditions: dict[str, dict[str, str]] = {}
    raw_complete_conditions: dict[str, str] = {}
    snapshot_complete_conditions: dict[str, str] = {}
    for api in REQUIRED_DECISION_APIS:
        raw_field_conditions[api] = {
            raw_field: _raw_ready("raw", raw_field)
            for raw_field, _snapshot_field in REQUIRED_DECISION_FIELDS[api]
        }
        snapshot_field_conditions[api] = {
            raw_field: _snapshot_ready("snapshot", snapshot_field)
            for raw_field, snapshot_field in REQUIRED_DECISION_FIELDS[api]
        }
        raw_complete_conditions[api] = " AND ".join(
            raw_field_conditions[api].values()
        )
        snapshot_complete_conditions[api] = " AND ".join(
            snapshot_field_conditions[api].values()
        )

    def _raw_bool_or(api: str, condition: str) -> str:
        # A raw-flags group has one API name.  FILTER prevents the daily
        # JSON predicates from being evaluated for daily_basic/stk_limit rows
        # (and vice versa), rather than asking every raw payload to expose all
        # twelve fields.
        return (
            "COALESCE(bool_or(COALESCE(observation_ready,FALSE) AND "
            f"({condition})) FILTER (WHERE api_name='{api}'),FALSE)"
        )

    raw_flag_columns = [
        "COALESCE(bool_or(COALESCE(observation_ready,FALSE)),FALSE) AS observation_ready",
        (
            f"{_raw_bool_or('daily', _positive('raw', 'open') + ' AND ' + _positive('raw', 'pre_close'))} "
            "AS execution_price_ready"
        ),
    ]
    snapshot_flag_columns = [
        "bool_or(COALESCE(observation_ready,FALSE)) AS observation_ready",
        (
            "bool_or(COALESCE(observation_ready,FALSE) AND "
            f"({_positive('snapshot', 'open')}) AND ({_positive('snapshot', 'pre_close')})) "
            "AS execution_price_ready"
        ),
    ]
    for api in REQUIRED_DECISION_APIS:
        raw_flag_columns.append(
            f"{_raw_bool_or(api, raw_complete_conditions[api])} AS {api}_complete_ready"
        )
        snapshot_flag_columns.append(
            f"bool_or(COALESCE(observation_ready,FALSE) AND "
            f"({snapshot_complete_conditions[api]})) AS {api}_complete_ready"
        )
        for raw_field in raw_field_conditions[api]:
            raw_flag_columns.append(
                f"{_raw_bool_or(api, raw_field_conditions[api][raw_field])} "
                f"AS {api}_{raw_field}_ready"
            )
            snapshot_flag_columns.append(
                f"bool_or(COALESCE(observation_ready,FALSE) AND "
                f"({snapshot_field_conditions[api][raw_field]})) "
                f"AS {api}_{raw_field}_ready"
            )

    raw_evidence_columns = []
    snapshot_evidence_columns = [
        "COUNT(*) AS snapshot_rows",
        "COUNT(*) FILTER (WHERE observation_ready) AS snapshot_observation_rows",
    ]
    for api in REQUIRED_DECISION_APIS:
        raw_evidence_columns.extend((
            f"COUNT(*) FILTER (WHERE api_name='{api}') AS {api}_raw_rows",
            (
                f"COUNT(*) FILTER (WHERE api_name='{api}' AND observation_ready) "
                f"AS {api}_raw_observation_rows"
            ),
        ))
        snapshot_evidence_columns.append(
            f"COUNT(*) FILTER (WHERE observation_ready AND {api}_complete_ready) "
            f"AS {api}_snapshot_complete_rows"
        )
        for raw_field in raw_field_conditions[api]:
            raw_evidence_columns.append(
                f"COUNT(*) FILTER (WHERE api_name='{api}' AND "
                f"{api}_{raw_field}_ready) AS {api}_raw_{raw_field}_rows"
            )
            snapshot_evidence_columns.append(
                f"COUNT(*) FILTER (WHERE observation_ready AND "
                f"{api}_{raw_field}_ready) AS {api}_snapshot_{raw_field}_rows"
            )

    qualified_evidence_columns = [
        (
            "COUNT(*) FILTER (WHERE r.api_name='daily' AND "
            "r.execution_price_ready AND s.execution_price_ready) "
            "AS daily_execution_price_rows"
        ),
    ]
    for api in REQUIRED_DECISION_APIS:
        qualified_evidence_columns.append(
            f"COUNT(*) FILTER (WHERE r.api_name='{api}' AND "
            f"r.observation_ready AND r.{api}_complete_ready AND "
            f"s.observation_ready AND s.{api}_complete_ready) "
            f"AS {api}_qualified_rows"
        )

    raw_flags_sql = ",\n                   ".join(raw_flag_columns)
    snapshot_flags_sql = ",\n                   ".join(snapshot_flag_columns)
    raw_evidence_sql = ",\n                   ".join(raw_evidence_columns)
    snapshot_evidence_sql = ",\n                   ".join(snapshot_evidence_columns)
    qualified_evidence_sql = ",\n                   ".join(qualified_evidence_columns)
    return f"""
        /*
         * Accepted projections select the immutable content hash for each
         * logical key.  The exact hash plus trade_date join is therefore the
         * only version lookup permitted here.  Every source is reduced to a
         * single (api, symbol) or (symbol) row before counts are computed;
         * no historical window ranking, fan-out join, or DISTINCT aggregate
         * is needed.
         */
        WITH raw_values AS (
            SELECT raw.api_name,raw.ts_code,raw_fields.*,
                   ({raw_observation}) AS observation_ready
              FROM dc_tushare_raw_records accepted
              JOIN dc_tushare_raw_record_version raw
                ON raw.api_name=accepted.api_name
               AND raw.record_key=accepted.record_key
               AND raw.content_hash=accepted.content_hash
               AND raw.trade_date=accepted.trade_date
              JOIN dc_stock_master stock ON stock.ts_code=raw.ts_code
              CROSS JOIN LATERAL jsonb_to_record(raw.payload_json::jsonb) AS raw_fields(
                  open TEXT,high TEXT,low TEXT,close TEXT,pre_close TEXT,
                  vol TEXT,amount TEXT,total_mv TEXT,circ_mv TEXT,
                  turnover_rate TEXT,up_limit TEXT,down_limit TEXT
              )
             WHERE accepted.trade_date=?
               AND accepted.api_name IN ('daily','daily_basic','stk_limit')
               AND raw.ts_code<>''
               AND {_explicit_timestamp_predicate('raw', 'observed_at')}
               AND {_explicit_timestamp_predicate('raw', 'created_at')}
               AND {raw_observed_at} <= ?::timestamptz
               AND {raw_first_observed_at} <= ?::timestamptz
               AND {raw_economic_available_time} <= ?::timestamptz
               AND {raw_created_at} <= ?::timestamptz
        ), raw_flags AS MATERIALIZED (
            SELECT api_name,ts_code,
                   {raw_flags_sql}
              FROM raw_values raw
             GROUP BY api_name,ts_code
        ), snapshot_values AS (
            SELECT snapshot.ts_code,snapshot_fields.*,
                   ({snapshot_observation}) AS observation_ready
              FROM dc_stock_daily_snapshot accepted
              JOIN dc_stock_daily_snapshot_version snapshot
                ON snapshot.ts_code=accepted.ts_code
               AND snapshot.trade_date=accepted.trade_date
               AND snapshot.content_hash=accepted.content_hash
              JOIN dc_stock_master stock ON stock.ts_code=snapshot.ts_code
              CROSS JOIN LATERAL jsonb_to_record(snapshot.payload_json::jsonb) AS snapshot_fields(
                  open TEXT,high TEXT,low TEXT,close TEXT,pre_close TEXT,
                  volume TEXT,turnover TEXT,total_mv TEXT,circ_mv TEXT,
                  turnover_rate TEXT,up_limit TEXT,down_limit TEXT
              )
             WHERE accepted.trade_date=?
               AND {_explicit_timestamp_predicate('snapshot', 'observed_at')}
               AND {_explicit_timestamp_predicate('snapshot', 'created_at')}
               AND {snapshot_observed_at} <= ?::timestamptz
               AND {snapshot_first_observed_at} <= ?::timestamptz
               AND {snapshot_economic_available_time} <= ?::timestamptz
               AND {snapshot_created_at} <= ?::timestamptz
        ), snapshot_flags AS MATERIALIZED (
            SELECT ts_code,
                   {snapshot_flags_sql}
              FROM snapshot_values snapshot
             GROUP BY ts_code
        ), raw_evidence AS (
            SELECT {raw_evidence_sql}
              FROM raw_flags
        ), snapshot_evidence AS (
            SELECT {snapshot_evidence_sql}
              FROM snapshot_flags
        ), qualified_evidence AS (
            SELECT {qualified_evidence_sql}
              /*
               * A full join deliberately prevents PostgreSQL from selecting
               * a nested-loop cross product when the CTE's strict timestamp
               * predicates make its cardinality estimate too small.  NULL
               * extended rows never satisfy these r/s conjunctions, so the
               * qualified counts remain the exact inner intersection.
               */
              FROM raw_flags r
              FULL OUTER JOIN snapshot_flags s ON s.ts_code=r.ts_code
        )
        SELECT raw_evidence.*,
               snapshot_evidence.*,
               qualified_evidence.*
          FROM raw_evidence
          CROSS JOIN snapshot_evidence
          CROSS JOIN qualified_evidence
    """


def _base_counts_query() -> str:
    observed_at = _safe_timestamp_value("raw", "observed_at")
    created_at = _safe_timestamp_value("raw", "created_at")
    # ``dc_tushare_raw_records`` has one accepted projection per logical
    # record and the exact content-hash join selects at most one immutable
    # version. Counting the joined accepted rows is therefore exact. A
    # DISTINCT aggregate here forced a large sort/hash during console
    # prewarming and could exceed the data database statement timeout.
    return f"""SELECT raw.api_name,COUNT(*) AS rows
                   FROM dc_tushare_raw_records accepted
                   JOIN dc_tushare_raw_record_version raw
                     ON raw.api_name=accepted.api_name
                    AND raw.record_key=accepted.record_key
                    AND raw.content_hash=accepted.content_hash
                    AND raw.trade_date=accepted.trade_date
                   JOIN dc_stock_master stock ON stock.ts_code=raw.ts_code
                  WHERE accepted.trade_date=? AND raw.ts_code<>''
                    AND accepted.api_name IN ('daily','daily_basic','stk_limit')
                    AND {_explicit_timestamp_predicate('raw', 'observed_at')}
                    AND {_explicit_timestamp_predicate('raw', 'created_at')}
                    AND {observed_at}<=?::timestamptz
                    AND {created_at}<=?::timestamptz
                  GROUP BY raw.api_name"""


def _provider_counts_query() -> str:
    observed_at = _safe_timestamp_value("raw", "observed_at")
    created_at = _safe_timestamp_value("raw", "created_at")
    return f"""SELECT raw.api_name,raw.source_code,COUNT(*) AS rows
                   FROM dc_tushare_raw_records accepted
                   JOIN dc_tushare_raw_record_version raw
                     ON raw.api_name=accepted.api_name
                    AND raw.record_key=accepted.record_key
                    AND raw.content_hash=accepted.content_hash
                    AND raw.trade_date=accepted.trade_date
                   JOIN dc_stock_master stock ON stock.ts_code=raw.ts_code
                  WHERE accepted.trade_date=? AND raw.ts_code<>''
                    AND accepted.api_name IN ('daily','daily_basic','stk_limit')
                    AND {_explicit_timestamp_predicate('raw', 'observed_at')}
                    AND {_explicit_timestamp_predicate('raw', 'created_at')}
                    AND {observed_at}<=?::timestamptz
                    AND {created_at}<=?::timestamptz
                  GROUP BY raw.api_name,raw.source_code
                  ORDER BY raw.api_name,raw.source_code"""


def _field_provider_counts_query() -> str:
    observed_at = _safe_timestamp_value("raw", "observed_at")
    created_at = _safe_timestamp_value("raw", "created_at")
    return f"""SELECT fields.key AS field_name,fields.value AS source_code,COUNT(*) AS rows
                   FROM dc_tushare_raw_records accepted
                   JOIN dc_tushare_raw_record_version raw
                     ON raw.api_name=accepted.api_name
                    AND raw.record_key=accepted.record_key
                    AND raw.content_hash=accepted.content_hash
                    AND raw.trade_date=accepted.trade_date
                   CROSS JOIN LATERAL jsonb_each_text(
                       COALESCE(raw.payload_json::jsonb->'_field_sources','{{}}'::jsonb)
                   ) fields
                  WHERE accepted.trade_date=? AND accepted.api_name='daily_basic'
                    AND {_explicit_timestamp_predicate('raw', 'observed_at')}
                    AND {_explicit_timestamp_predicate('raw', 'created_at')}
                    AND {observed_at}<=?::timestamptz
                    AND {created_at}<=?::timestamptz
                  GROUP BY fields.key,fields.value ORDER BY fields.key,fields.value"""


def _empty_evidence() -> dict[str, int]:
    result = {
        "snapshot_rows": 0,
        "snapshot_observation_rows": 0,
        "daily_execution_price_rows": 0,
    }
    for api in REQUIRED_DECISION_APIS:
        result[f"{api}_raw_rows"] = 0
        result[f"{api}_raw_observation_rows"] = 0
        result[f"{api}_snapshot_complete_rows"] = 0
        result[f"{api}_qualified_rows"] = 0
        for raw_field, _snapshot_field in REQUIRED_DECISION_FIELDS[api]:
            result[f"{api}_raw_{raw_field}_rows"] = 0
            result[f"{api}_snapshot_{raw_field}_rows"] = 0
    return result


def _field_coverage(
    evidence: dict[str, Any],
    expected_rows: dict[str, int],
    minimum_rows_by_api: dict[str, int],
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    coverage: dict[str, dict[str, dict[str, Any]]] = {}
    incomplete_apis: list[str] = []
    for api in REQUIRED_DECISION_APIS:
        api_fields: dict[str, dict[str, Any]] = {}
        for raw_field, snapshot_field in REQUIRED_DECISION_FIELDS[api]:
            raw_rows = _count(evidence.get(f"{api}_raw_{raw_field}_rows"))
            snapshot_rows = _count(evidence.get(f"{api}_snapshot_{raw_field}_rows"))
            minimum = minimum_rows_by_api[api]
            api_fields[raw_field] = {
                "snapshot_field": snapshot_field,
                "raw_rows": raw_rows,
                "snapshot_rows": snapshot_rows,
                "raw_coverage": round(raw_rows / expected_rows[api], 6) if expected_rows[api] else 0.0,
                "snapshot_coverage": round(snapshot_rows / expected_rows[api], 6) if expected_rows[api] else 0.0,
                "minimum_rows": minimum,
                "ready": raw_rows >= minimum and snapshot_rows >= minimum,
            }
        coverage[api] = api_fields
        if any(not item["ready"] for item in api_fields.values()):
            incomplete_apis.append(api)
    return coverage, incomplete_apis


def decision_data_contract(
    trade_date: str | None = None,
    *,
    conn: Any | None = None,
    as_of_time: str | None = None,
    include_diagnostics: bool = True,
) -> dict[str, Any]:
    """Return the fail-closed production decision-data contract for one date.

    ``as_of_time`` freezes the real observation clock used by this evaluation.
    It is intentionally distinct from the date's economic close: end-of-day
    data received after 15:00 can support a newly created T-day plan, but can
    never be represented as if it had been available at 15:00 for replay.
    """

    # Standalone contract/status requests must not leave a transaction open
    # while their result is serialized by the API.  A caller-supplied
    # connection deliberately keeps its existing transactional snapshot.
    context = _read_conn() if conn is None else None
    active = context.__enter__() if context is not None else conn
    try:
        # PostgreSQL does not permit SAVEPOINT in an autocommit session.  That
        # is intentional for a standalone status contract: each statement is
        # already its own transaction, so a timeout/error cannot poison the
        # next diagnostic query.  A caller-provided write connection retains
        # the savepoint recovery path below.
        use_savepoints = not _autocommit_read_only_connection(active)
        # A caller which omits the date is asking about today's production
        # decision contract, not the newest historical row in a mutable
        # snapshot table.  Selecting MAX(trade_date) here could let yesterday's
        # completed slice authorize work while the exchange-calendar target is
        # still missing.
        if trade_date:
            value = str(trade_date).replace("-", "")[:8]
        else:
            from app.data_platform.services.market_calendar import target_trade_date

            value = str(target_trade_date(active)).replace("-", "")[:8]
        cutoff, cutoff_valid = _normalize_observation_cutoff(as_of_time)
        stock_row = active.execute("SELECT COUNT(*) AS count FROM dc_stock_master").fetchone()
        stock_count = _count(_row_dict(stock_row).get("count"))

        api_counts: dict[str, int] = {}
        provider_counts: dict[str, dict[str, int]] = {}
        field_provider_counts: dict[str, dict[str, int]] = {}
        base_query_error = ""
        if value and include_diagnostics:
            # The aggregate/source counts are diagnostic inputs.  They share a
            # caller-owned data-platform connection with the scheduler, so a
            # PostgreSQL statement timeout here must not leave that outer
            # transaction poisoned.  A later readiness check on the same
            # connection otherwise reports only ``current transaction is
            # aborted`` and hides the original bounded timeout.
            #
            # Do not suppress a failed savepoint cleanup: the owning _conn()
            # context must see it and rollback/discard the whole connection.
            if use_savepoints:
                active.execute("SAVEPOINT decision_base_counts")
            try:
                api_rows = active.execute(_base_counts_query(), (value, cutoff, cutoff)).fetchall()
                api_counts = {
                    str(_row_dict(row).get("api_name") or ""): _count(_row_dict(row).get("rows"))
                    for row in api_rows
                }
                provider_rows = active.execute(_provider_counts_query(), (value, cutoff, cutoff)).fetchall()
                for row in provider_rows:
                    item = _row_dict(row)
                    provider_counts.setdefault(str(item.get("api_name") or ""), {})[
                        str(item.get("source_code") or "unknown")
                    ] = _count(item.get("rows"))
                # Field lineage is explanatory only.  An older role which
                # cannot read JSONB lineage must not weaken the hard contract.
                try:
                    # This query is explanatory only.  Isolate it from the
                    # hard decision gate because PostgreSQL leaves the whole
                    # transaction aborted after any statement error.  The old
                    # bare catch turned a harmless lineage diagnostic failure
                    # into zero rows for the following strict evidence query,
                    # permanently blocking otherwise complete trading data.
                    if use_savepoints:
                        active.execute("SAVEPOINT decision_field_lineage")
                    field_provider_rows = active.execute(
                        _field_provider_counts_query(), (value, cutoff, cutoff),
                    ).fetchall()
                    if use_savepoints:
                        active.execute("RELEASE SAVEPOINT decision_field_lineage")
                except Exception:  # noqa: BLE001 - optional diagnostic only
                    if use_savepoints:
                        try:
                            active.execute("ROLLBACK TO SAVEPOINT decision_field_lineage")
                            active.execute("RELEASE SAVEPOINT decision_field_lineage")
                        except Exception:  # noqa: BLE001 - outer hard query will fail closed
                            pass
                    field_provider_rows = []
                for row in field_provider_rows:
                    item = _row_dict(row)
                    field_provider_counts.setdefault(str(item.get("field_name") or ""), {})[
                        str(item.get("source_code") or "unknown")
                    ] = _count(item.get("rows"))
            except Exception as exc:  # noqa: BLE001 - production gates fail closed
                if use_savepoints:
                    active.execute("ROLLBACK TO SAVEPOINT decision_base_counts")
                    active.execute("RELEASE SAVEPOINT decision_base_counts")
                base_query_error = type(exc).__name__
            else:
                if use_savepoints:
                    active.execute("RELEASE SAVEPOINT decision_base_counts")

        evidence = _empty_evidence()
        strict_schema_ready = False
        strict_query_error = ""
        if value and cutoff_valid and not base_query_error:
            if use_savepoints:
                active.execute("SAVEPOINT decision_strict_evidence")
            try:
                row = active.execute(
                    _strict_evidence_query(),
                    # Raw date/cutoff and normalized snapshot date/cutoff.
                    (
                        value, cutoff, cutoff, cutoff, cutoff,
                        value, cutoff, cutoff, cutoff, cutoff,
                    ),
                ).fetchone()
                evidence.update(_row_dict(row))
                strict_schema_ready = True
                if use_savepoints:
                    active.execute("RELEASE SAVEPOINT decision_strict_evidence")
            except Exception as exc:  # noqa: BLE001 - migration may be absent
                # A PostgreSQL timeout aborts the current transaction even
                # when the caller supplied a shared connection.  Roll back
                # only this bounded gate so later health/ledger reads can
                # continue; if cleanup itself fails, let the outer owner
                # discard the poisoned connection rather than hiding it.
                if use_savepoints:
                    active.execute("ROLLBACK TO SAVEPOINT decision_strict_evidence")
                    active.execute("RELEASE SAVEPOINT decision_strict_evidence")
                strict_query_error = type(exc).__name__
    finally:
        if context is not None:
            context.__exit__(None, None, None)

    raw_daily_population = _count(evidence.get("daily_raw_rows")) or _count(api_counts.get("daily"))
    expected_rows = {
        "daily": stock_count,
        "daily_basic": stock_count,
        # Limit prices are evaluated against the actual daily universe.  The
        # daily 99.5% gate independently prevents a reduced daily universe
        # from becoming an escape hatch.
        "stk_limit": raw_daily_population or stock_count,
    }
    minimum_rows_by_api = {
        api: _minimum_rows(expected_rows[api], DECISION_MIN_STOCK_COVERAGE_BY_API[api])
        for api in REQUIRED_DECISION_APIS
    }
    counts = {
        "snapshot": _count(evidence.get("snapshot_rows")),
        **{
            api: _count(evidence.get(f"{api}_qualified_rows"))
            for api in REQUIRED_DECISION_APIS
        },
    }
    raw_observation_rows = {
        api: _count(evidence.get(f"{api}_raw_observation_rows"))
        for api in REQUIRED_DECISION_APIS
    }
    snapshot_complete_rows = {
        api: _count(evidence.get(f"{api}_snapshot_complete_rows"))
        for api in REQUIRED_DECISION_APIS
    }
    field_coverage, field_incomplete_apis = _field_coverage(
        evidence, expected_rows, minimum_rows_by_api,
    )
    present_apis = [
        api
        for api in REQUIRED_DECISION_APIS
        if (
            api_counts.get(api, 0) > 0
            if include_diagnostics
            else _count(evidence.get(f"{api}_raw_rows")) > 0
        )
    ]
    missing_apis = [api for api in REQUIRED_DECISION_APIS if api not in present_apis]
    incomplete_apis = [
        api for api in REQUIRED_DECISION_APIS
        if counts[api] < minimum_rows_by_api[api]
    ]
    observation_incomplete_apis = [
        api for api in REQUIRED_DECISION_APIS
        if raw_observation_rows[api] < minimum_rows_by_api[api]
        or snapshot_complete_rows[api] < minimum_rows_by_api[api]
    ]
    coverage = {
        api: round(counts[api] / expected_rows[api], 6) if expected_rows[api] else 0.0
        for api in REQUIRED_DECISION_APIS
    }
    snapshot_minimum_rows = minimum_rows_by_api["daily"]
    snapshot_ready = (
        _count(evidence.get("snapshot_rows")) >= snapshot_minimum_rows
        and _count(evidence.get("snapshot_observation_rows")) >= snapshot_minimum_rows
        and snapshot_complete_rows["daily"] >= snapshot_minimum_rows
    )
    blocking_reasons: list[str] = []
    if not value:
        blocking_reasons.append("missing_trade_date")
    if not cutoff_valid:
        blocking_reasons.append("invalid_observation_cutoff")
    if base_query_error:
        blocking_reasons.append("decision_contract_query_failed")
    if not strict_schema_ready and not base_query_error and value and cutoff_valid:
        # data_0011 is intentionally mandatory.  Do not fall back to legacy
        # available_time values, which may have been fabricated as T15.
        blocking_reasons.append("pit_observation_schema_unavailable")
    if missing_apis:
        blocking_reasons.append("missing_required_api")
    if incomplete_apis:
        blocking_reasons.append("insufficient_stock_coverage")
    if field_incomplete_apis:
        blocking_reasons.append("missing_required_fields")
    if observation_incomplete_apis and strict_schema_ready:
        blocking_reasons.append("unverifiable_observation_evidence")
    if not snapshot_ready:
        blocking_reasons.append("insufficient_snapshot_coverage")
    ready = bool(value) and cutoff_valid and strict_schema_ready and not blocking_reasons
    reason = "" if ready else (
        f"{value or '最新交易日'} 决策数据未就绪：" + ",".join(blocking_reasons or ["unknown"])
    )
    return {
        "ok": ready,
        "schema_version": DECISION_DATA_CONTRACT_SCHEMA_VERSION,
        "latest_trade_date": value,
        "required_apis": list(REQUIRED_DECISION_APIS),
        "required_fields": {
            api: [raw_field for raw_field, _snapshot_field in fields]
            for api, fields in REQUIRED_DECISION_FIELDS.items()
        },
        "snapshot_field_mapping": {
            api: {raw_field: snapshot_field for raw_field, snapshot_field in fields}
            for api, fields in REQUIRED_DECISION_FIELDS.items()
        },
        "present_apis": present_apis,
        "missing_apis": missing_apis,
        "incomplete_apis": incomplete_apis,
        "field_incomplete_apis": field_incomplete_apis,
        "observation_incomplete_apis": observation_incomplete_apis,
        "stock_count": stock_count,
        "minimum_coverage": DECISION_MIN_STOCK_COVERAGE_BY_API,
        "minimum_coverage_by_api": DECISION_MIN_STOCK_COVERAGE_BY_API,
        "minimum_rows": snapshot_minimum_rows,
        "minimum_rows_by_api": minimum_rows_by_api,
        "expected_rows": expected_rows,
        "counts": counts,
        # This deliberately reports a narrower, execution-only field
        # intersection.  A frozen T+1 order must have valid open/pre-close
        # prices and limit bounds, but must not wait for fresh valuation data.
        "execution_price_rows": _count(evidence.get("daily_execution_price_rows")),
        "raw_rows": {api: _count(evidence.get(f"{api}_raw_rows")) for api in REQUIRED_DECISION_APIS},
        "raw_observation_rows": raw_observation_rows,
        "snapshot_complete_rows": snapshot_complete_rows,
        "field_coverage": field_coverage,
        "provider_rows": provider_counts,
        "daily_basic_field_provider_rows": field_provider_counts,
        "provider_policy": "hedged_first_complete_slice_wins_field_authority_reconciles_v2",
        "field_source_authority": {
            "daily": "first_valid_full_slice_then_tushare_reconcile",
            "stk_limit": "first_valid_full_slice_then_exact_cross_check",
            "daily_basic": "tushare_authoritative_after_fast_provisional_slice",
        },
        "observation_contract": {
            "schema_version": "alis_market_observation_clock_v1",
            "cutoff": cutoff or None,
            "strict_schema_ready": strict_schema_ready,
            "required_columns": [
                "observed_at", "first_observed_at", "economic_available_time",
                "observation_provenance", "content_hash", "created_at",
            ],
            "legacy_rows_allowed": False,
            "current_decision_policy": "actual_observed_at_must_not_exceed_frozen_observation_cutoff",
            "historical_policy": "append_only_version_evidence_required",
            "query_error_type": strict_query_error or None,
        },
        "snapshot_limit_rows": _count(evidence.get("stk_limit_snapshot_complete_rows")),
        "raw_limit_rows": counts["stk_limit"],
        "coverage_basis": "qualified_target_date_daily_intersection_stock_master",
        "stock_master_count": stock_count,
        "daily_expected_count": raw_daily_population,
        "stk_limit_raw_count": _count(evidence.get("stk_limit_raw_rows")),
        "stk_limit_snapshot_count": _count(evidence.get("stk_limit_snapshot_complete_rows")),
        "stk_limit_matched_count": counts["stk_limit"],
        "shortfall_to_expected": max(0, expected_rows["stk_limit"] - counts["stk_limit"]),
        "coverage": coverage,
        "shortfall": {
            api: max(0, minimum_rows_by_api[api] - counts[api])
            for api in REQUIRED_DECISION_APIS
        },
        "snapshot_ready": snapshot_ready,
        # Keep the frozen observation instant at the contract root as well as
        # in the nested audit payload.  The formal plan validator uses this
        # field to align the data gate with the selection and manifest clocks.
        "observation_cutoff": cutoff or None,
        "blocking_reasons": blocking_reasons,
        "reason": reason,
    }


def latest_decision_ready_trade_date(conn: Any) -> str:
    latest = latest_market_trade_date(conn)
    if latest and decision_data_contract(latest, conn=conn).get("ok"):
        return latest
    for candidate in candidate_snapshot_dates(conn, before=latest or "", limit=12):
        if decision_data_contract(candidate, conn=conn).get("ok"):
            return candidate
    return ""
