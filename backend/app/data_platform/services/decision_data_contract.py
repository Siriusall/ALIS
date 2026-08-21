from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn
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
    raw_observation = _version_observation_ready_predicate("raw")
    snapshot_observation = _version_observation_ready_predicate("snapshot")
    raw_observed_at = _safe_timestamp_value("raw", "observed_at")
    raw_first_observed_at = _safe_timestamp_value("raw", "first_observed_at")
    raw_economic_available_time = _safe_timestamp_value("raw", "economic_available_time")
    raw_created_at = _safe_timestamp_value("raw", "created_at")
    snapshot_observed_at = _safe_timestamp_value("snapshot", "observed_at")
    snapshot_first_observed_at = _safe_timestamp_value("snapshot", "first_observed_at")
    snapshot_economic_available_time = _safe_timestamp_value("snapshot", "economic_available_time")
    snapshot_created_at = _safe_timestamp_value("snapshot", "created_at")
    raw_api_conditions = {
        api: _fields_ready_predicate("raw", api, raw=True)
        for api in REQUIRED_DECISION_APIS
    }
    snapshot_api_conditions = {
        api: _fields_ready_predicate("snapshot", api, raw=False)
        for api in REQUIRED_DECISION_APIS
    }
    columns = [
        "COUNT(DISTINCT snapshot.ts_code) AS snapshot_rows",
        (
            "COUNT(DISTINCT CASE WHEN snapshot.observation_ready "
            "THEN snapshot.ts_code END) AS snapshot_observation_rows"
        ),
    ]
    for api in REQUIRED_DECISION_APIS:
        columns.extend((
            (
                f"COUNT(DISTINCT CASE WHEN raw.api_name='{api}' "
                f"THEN raw.ts_code END) AS {api}_raw_rows"
            ),
            (
                f"COUNT(DISTINCT CASE WHEN raw.api_name='{api}' AND raw.observation_ready "
                f"THEN raw.ts_code END) AS {api}_raw_observation_rows"
            ),
            (
                f"COUNT(DISTINCT CASE WHEN snapshot.observation_ready AND ({snapshot_api_conditions[api]}) "
                f"THEN snapshot.ts_code END) AS {api}_snapshot_complete_rows"
            ),
            (
                f"COUNT(DISTINCT CASE WHEN raw.api_name='{api}' AND raw.observation_ready "
                f"AND ({raw_api_conditions[api]}) AND snapshot.observation_ready "
                f"AND ({snapshot_api_conditions[api]}) THEN raw.ts_code END) "
                f"AS {api}_qualified_rows"
            ),
        ))
        for raw_field, snapshot_field in REQUIRED_DECISION_FIELDS[api]:
            columns.extend((
                (
                    f"COUNT(DISTINCT CASE WHEN raw.api_name='{api}' AND raw.observation_ready "
                    f"AND ({_raw_field_ready_predicate('raw', raw_field)}) THEN raw.ts_code END) "
                    f"AS {api}_raw_{raw_field}_rows"
                ),
                (
                    f"COUNT(DISTINCT CASE WHEN snapshot.observation_ready "
                    f"AND snapshot.{snapshot_field} IS NOT NULL THEN snapshot.ts_code END) "
                    f"AS {api}_snapshot_{raw_field}_rows"
                ),
            ))
    return f"""
        WITH raw_ranked AS (
            SELECT raw.api_name,raw.record_key,raw.ts_code,raw.payload_json,
                   raw.source_code,raw.economic_available_time,raw.observed_at,
                   raw.first_observed_at,raw.observation_provenance,raw.content_hash,raw.created_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY raw.api_name,raw.record_key
                       ORDER BY {raw_observed_at} DESC,{raw_created_at} DESC,raw.id DESC
                   ) AS row_rank
              FROM dc_tushare_raw_record_version raw
             WHERE raw.trade_date=?
               AND raw.api_name IN ('daily','daily_basic','stk_limit')
               AND raw.ts_code<>''
               AND EXISTS (
                   SELECT 1
                     FROM dc_tushare_raw_records accepted
                    WHERE accepted.api_name=raw.api_name
                      AND accepted.record_key=raw.record_key
                      AND accepted.content_hash=raw.content_hash
               )
               AND {_explicit_timestamp_predicate('raw', 'observed_at')}
               AND {_explicit_timestamp_predicate('raw', 'created_at')}
               AND {raw_observed_at} <= ?::timestamptz
               AND {raw_first_observed_at} <= ?::timestamptz
               AND {raw_economic_available_time} <= ?::timestamptz
               AND {raw_created_at} <= ?::timestamptz
        ), raw AS (
            SELECT raw.*,({raw_observation}) AS observation_ready
              FROM raw_ranked raw
              JOIN dc_stock_master stock ON stock.ts_code=raw.ts_code
             WHERE raw.row_rank=1
        ), snapshot_ranked AS (
            SELECT snapshot.ts_code,snapshot.payload_json,snapshot.source_code,
                   snapshot.economic_available_time,snapshot.observed_at,
                   snapshot.first_observed_at,snapshot.observation_provenance,
                   snapshot.content_hash,snapshot.created_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY snapshot.ts_code,snapshot.trade_date
                       ORDER BY {snapshot_observed_at} DESC,{snapshot_created_at} DESC,snapshot.id DESC
                   ) AS row_rank
              FROM dc_stock_daily_snapshot_version snapshot
             WHERE snapshot.trade_date=?
               AND EXISTS (
                   SELECT 1
                     FROM dc_stock_daily_snapshot accepted
                    WHERE accepted.ts_code=snapshot.ts_code
                      AND accepted.trade_date=snapshot.trade_date
                      AND accepted.content_hash=snapshot.content_hash
               )
               AND {_explicit_timestamp_predicate('snapshot', 'observed_at')}
               AND {_explicit_timestamp_predicate('snapshot', 'created_at')}
               AND {snapshot_observed_at} <= ?::timestamptz
               AND {snapshot_first_observed_at} <= ?::timestamptz
               AND {snapshot_economic_available_time} <= ?::timestamptz
               AND {snapshot_created_at} <= ?::timestamptz
        ), snapshot AS (
            SELECT snapshot.ts_code,
                   snapshot.payload_json::jsonb->>'open' AS open,
                   snapshot.payload_json::jsonb->>'high' AS high,
                   snapshot.payload_json::jsonb->>'low' AS low,
                   snapshot.payload_json::jsonb->>'close' AS close,
                   snapshot.payload_json::jsonb->>'pre_close' AS pre_close,
                   snapshot.payload_json::jsonb->>'volume' AS volume,
                   snapshot.payload_json::jsonb->>'turnover' AS turnover,
                   snapshot.payload_json::jsonb->>'total_mv' AS total_mv,
                   snapshot.payload_json::jsonb->>'circ_mv' AS circ_mv,
                   snapshot.payload_json::jsonb->>'turnover_rate' AS turnover_rate,
                   snapshot.payload_json::jsonb->>'up_limit' AS up_limit,
                   snapshot.payload_json::jsonb->>'down_limit' AS down_limit,
                   snapshot.source_code,snapshot.economic_available_time,
                   snapshot.observed_at,snapshot.first_observed_at,
                   snapshot.observation_provenance,snapshot.content_hash,snapshot.created_at,
                   ({snapshot_observation}) AS observation_ready
              FROM snapshot_ranked snapshot
              JOIN dc_stock_master stock ON stock.ts_code=snapshot.ts_code
             WHERE snapshot.row_rank=1
        )
        SELECT {', '.join(columns)}
          FROM snapshot
          FULL OUTER JOIN raw ON raw.ts_code=snapshot.ts_code
    """


def _base_counts_query() -> str:
    observed_at = _safe_timestamp_value("raw", "observed_at")
    created_at = _safe_timestamp_value("raw", "created_at")
    return f"""WITH ranked AS (
                 SELECT raw.api_name,raw.record_key,raw.ts_code,
                        ROW_NUMBER() OVER (
                            PARTITION BY raw.api_name,raw.record_key
                            ORDER BY {observed_at} DESC,{created_at} DESC,raw.id DESC
                        ) AS row_rank
                   FROM dc_tushare_raw_record_version raw
                  WHERE raw.trade_date=? AND raw.ts_code<>''
                    AND raw.api_name IN ('daily','daily_basic','stk_limit')
                    AND EXISTS (
                        SELECT 1
                          FROM dc_tushare_raw_records accepted
                         WHERE accepted.api_name=raw.api_name
                           AND accepted.record_key=raw.record_key
                           AND accepted.content_hash=raw.content_hash
                    )
                    AND {_explicit_timestamp_predicate('raw', 'observed_at')}
                    AND {_explicit_timestamp_predicate('raw', 'created_at')}
                    AND {observed_at}<=?::timestamptz
                    AND {created_at}<=?::timestamptz
             ) SELECT raw.api_name,COUNT(DISTINCT raw.ts_code) AS rows
                   FROM ranked raw JOIN dc_stock_master stock ON stock.ts_code=raw.ts_code
                  WHERE raw.row_rank=1 GROUP BY raw.api_name"""


def _provider_counts_query() -> str:
    observed_at = _safe_timestamp_value("raw", "observed_at")
    created_at = _safe_timestamp_value("raw", "created_at")
    return f"""WITH ranked AS (
                 SELECT raw.api_name,raw.record_key,raw.ts_code,raw.source_code,
                        ROW_NUMBER() OVER (
                            PARTITION BY raw.api_name,raw.record_key
                            ORDER BY {observed_at} DESC,{created_at} DESC,raw.id DESC
                        ) AS row_rank
                   FROM dc_tushare_raw_record_version raw
                  WHERE raw.trade_date=? AND raw.ts_code<>''
                    AND raw.api_name IN ('daily','daily_basic','stk_limit')
                    AND EXISTS (
                        SELECT 1
                          FROM dc_tushare_raw_records accepted
                         WHERE accepted.api_name=raw.api_name
                           AND accepted.record_key=raw.record_key
                           AND accepted.content_hash=raw.content_hash
                    )
                    AND {_explicit_timestamp_predicate('raw', 'observed_at')}
                    AND {_explicit_timestamp_predicate('raw', 'created_at')}
                    AND {observed_at}<=?::timestamptz
                    AND {created_at}<=?::timestamptz
             ) SELECT raw.api_name,raw.source_code,COUNT(DISTINCT raw.ts_code) AS rows
                   FROM ranked raw JOIN dc_stock_master stock ON stock.ts_code=raw.ts_code
                  WHERE raw.row_rank=1
                  GROUP BY raw.api_name,raw.source_code ORDER BY raw.api_name,raw.source_code"""


def _field_provider_counts_query() -> str:
    observed_at = _safe_timestamp_value("raw", "observed_at")
    created_at = _safe_timestamp_value("raw", "created_at")
    return f"""WITH ranked AS (
                 SELECT raw.record_key,raw.payload_json,
                        ROW_NUMBER() OVER (
                            PARTITION BY raw.record_key
                            ORDER BY {observed_at} DESC,{created_at} DESC,raw.id DESC
                        ) AS row_rank
                   FROM dc_tushare_raw_record_version raw
                  WHERE raw.trade_date=? AND raw.api_name='daily_basic'
                    AND EXISTS (
                        SELECT 1
                          FROM dc_tushare_raw_records accepted
                         WHERE accepted.api_name=raw.api_name
                           AND accepted.record_key=raw.record_key
                           AND accepted.content_hash=raw.content_hash
                    )
                    AND {_explicit_timestamp_predicate('raw', 'observed_at')}
                    AND {_explicit_timestamp_predicate('raw', 'created_at')}
                    AND {observed_at}<=?::timestamptz
                    AND {created_at}<=?::timestamptz
             ) SELECT fields.key AS field_name,fields.value AS source_code,COUNT(*) AS rows
                   FROM ranked raw
                   CROSS JOIN LATERAL jsonb_each_text(
                       COALESCE(raw.payload_json::jsonb->'_field_sources','{{}}'::jsonb)
                   ) fields
                  WHERE raw.row_rank=1
                  GROUP BY fields.key,fields.value ORDER BY fields.key,fields.value"""


def _empty_evidence() -> dict[str, int]:
    result = {"snapshot_rows": 0, "snapshot_observation_rows": 0}
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
) -> dict[str, Any]:
    """Return the fail-closed production decision-data contract for one date.

    ``as_of_time`` freezes the real observation clock used by this evaluation.
    It is intentionally distinct from the date's economic close: end-of-day
    data received after 15:00 can support a newly created T-day plan, but can
    never be represented as if it had been available at 15:00 for replay.
    """

    context = _conn() if conn is None else None
    active = context.__enter__() if context is not None else conn
    try:
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
        if value:
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
                    active.execute("SAVEPOINT decision_field_lineage")
                    field_provider_rows = active.execute(
                        _field_provider_counts_query(), (value, cutoff, cutoff),
                    ).fetchall()
                    active.execute("RELEASE SAVEPOINT decision_field_lineage")
                except Exception:  # noqa: BLE001 - optional diagnostic only
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
                base_query_error = type(exc).__name__

        evidence = _empty_evidence()
        strict_schema_ready = False
        strict_query_error = ""
        if value and cutoff_valid and not base_query_error:
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
            except Exception as exc:  # noqa: BLE001 - migration may be absent
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
    present_apis = [api for api in REQUIRED_DECISION_APIS if api_counts.get(api, 0) > 0]
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
