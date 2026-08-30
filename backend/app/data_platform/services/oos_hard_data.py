"""Progressive PIT evidence contract for OOS simulation qualification."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Any
from zoneinfo import ZoneInfo

from app.data_platform.db import _conn
from app.data_platform.services.market_calendar import TradeCalendarNotReady, target_trade_date
from app.data_platform.services.observation_clock import _valid_provenance_sql
from app.data_platform.services.snapshot_health import snapshot_stats_for_range

LEGACY_CONTRACT_VERSION = "alis_oos_hard_data_v4_immutable_observation"
CONTRACT_VERSION = "alis_progressive_oos_v5"
TRAIN_SESSIONS = 756
PURGE_SESSIONS = 20
VALIDATION_SESSIONS = 126
EMBARGO_SESSIONS = 5
TEST_SESSIONS = 126
STEP_SESSIONS = 126
MINIMUM_WINDOWS = 5
FROZEN_REPLAY_MINIMUM_WINDOWS = 4
MINIMUM_DAILY_ROWS = 500
MINIMUM_HARD_API_ROWS = 500
MINIMUM_MARKET_FIELD_RATIO = 0.80
PENDING_TRADE_DAY_BUFFER = 30
MAX_COMPACT_NON_REPAIRABLE_DATE_SAMPLES = 20
# A visible progress target for the post-observation-clock evidence run.  It
# is not an alternative to the full formal OOS requirement below.
NEW_STRICT_BASELINE_SESSIONS = 126
PROGRESSIVE_DIAGNOSTIC_SESSIONS = 60
PROGRESSIVE_SIMULATION_SESSIONS = 126
PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS = 252
PROGRESSIVE_CONSUMER_TIERS = {
    "diagnostic_research": PROGRESSIVE_DIAGNOSTIC_SESSIONS,
    "simulation_evaluation": PROGRESSIVE_SIMULATION_SESSIONS,
    "simulation_champion_evaluation": PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS,
}
HARD_APIS = ("daily", "daily_basic", "stk_limit", "adj_factor")
BENCHMARK_INDEX_CODE = "000300.SH"
BENCHMARK_API_KEY = f"index_daily:{BENCHMARK_INDEX_CODE}"
REQUIRED_SESSIONS = (
    TRAIN_SESSIONS + PURGE_SESSIONS + VALIDATION_SESSIONS + EMBARGO_SESSIONS
    + TEST_SESSIONS + (MINIMUM_WINDOWS - 1) * STEP_SESSIONS
)
FROZEN_REPLAY_REQUIRED_SESSIONS = (
    TRAIN_SESSIONS + PURGE_SESSIONS + VALIDATION_SESSIONS + EMBARGO_SESSIONS
    + TEST_SESSIONS + (FROZEN_REPLAY_MINIMUM_WINDOWS - 1) * STEP_SESSIONS
)

_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def frozen_replay_oos_fallback_enabled() -> bool:
    return str(os.getenv("ALIS_FROZEN_REPLAY_OOS_FALLBACK", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _business_cutoff(trade_date: str) -> str:
    """Return the only valid OOS close cutoff, with an explicit UTC+8 offset."""

    value = str(trade_date or "").replace("-", "")[:8]
    if len(value) != 8 or not value.isdigit():
        return ""
    return f"{value[:4]}-{value[4:6]}-{value[6:]}T15:00:00+08:00"


def _parse_observed_timestamp(value: Any) -> datetime | None:
    """Parse only an explicit-offset timestamp; naive clocks are not PIT proof."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_BUSINESS_TZ)


def _timestamp_at_or_before(value: Any, cutoff: str) -> bool:
    observed = _parse_observed_timestamp(value)
    boundary = _parse_observed_timestamp(cutoff)
    return bool(observed is not None and boundary is not None and observed <= boundary)


def _trade_cutoff_elapsed(trade_date: str, *, now: datetime | None = None) -> bool:
    """Whether a missing observation can no longer be captured on time.

    Re-fetching a historical slice after its T-day close can improve the
    current projection, but it cannot create proof that the slice was
    available for the historical decision.  Keep that distinction at the
    classification boundary so no repair caller can accidentally turn a
    historical PIT absence into an upstream retry loop.
    """

    cutoff = _parse_observed_timestamp(_business_cutoff(trade_date))
    if cutoff is None:
        return True
    current = now or datetime.now(_BUSINESS_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_BUSINESS_TZ)
    return current.astimezone(_BUSINESS_TZ) >= cutoff


def _timestamp_is_explicit_sql(column: str, *, is_postgres: bool) -> str:
    """SQL guard matching the strict Python parser without trusting text order."""

    normalized = f"TRIM(COALESCE({column},''))"
    if is_postgres:
        return (
            f"NULLIF({normalized},'') IS NOT NULL AND "
            f"NULLIF({normalized},'') ~ '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$'"
        )
    # SQLite fixtures use the same normalized ISO strings.  ``julianday``
    # performs the actual offset conversion; accepting the final six-character
    # offset also supports fractional seconds without accepting naive clocks.
    return (
        f"NULLIF({normalized},'') IS NOT NULL AND "
        f"(SUBSTR({normalized},-6,1) IN ('+','-') OR SUBSTR({normalized},-1)='Z') AND "
        f"julianday(NULLIF({normalized},'')) IS NOT NULL"
    )


def _timestamp_at_or_before_sql(column: str, cutoff_expression: str, *, is_postgres: bool) -> str:
    explicit = _timestamp_is_explicit_sql(column, is_postgres=is_postgres)
    value = f"NULLIF(TRIM(COALESCE({column},'')),'')"
    if is_postgres:
        return f"({explicit} AND {value}::timestamptz <= ({cutoff_expression})::timestamptz)"
    return f"({explicit} AND julianday({value}) <= julianday({cutoff_expression}))"


def _timestamp_value_sql(column: str, *, is_postgres: bool) -> str:
    value = f"NULLIF(TRIM(COALESCE({column},'')),'')"
    return f"{value}::timestamptz" if is_postgres else f"julianday({value})"


def _cutoff_expression(date_column: str = "trade_date") -> str:
    return (
        f"(SUBSTR({date_column},1,4)||'-'||SUBSTR({date_column},5,2)||'-'||"
        f"SUBSTR({date_column},7,2)||'T15:00:00+08:00')"
    )


def _date_api_sets(start_date: str, end_date: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {api: set() for api in HARD_APIS}
    with _conn() as conn:
        try:
            rows = conn.execute(
                """SELECT api_name,trade_date,point_in_time_rows
                     FROM dc_oos_hard_api_stats_live
                    WHERE api_name IN (?,?,?,?) AND trade_date BETWEEN ? AND ?""",
                (*HARD_APIS, start_date, end_date),
            ).fetchall()
        except Exception:
            # The persisted summary is part of the hard-data contract. Do not
            # turn a missing migration into a 1,537-day raw-table scan.
            return result
    for row in rows:
        item = dict(row)
        api = str(item.get("api_name") or "")
        trade_date = str(item.get("trade_date") or "")
        if api in result and trade_date and int(item.get("point_in_time_rows") or 0) >= MINIMUM_HARD_API_ROWS:
            result[api].add(trade_date)
    return result


def _strict_date_api_sets(start_date: str, end_date: str) -> tuple[dict[str, set[str]], dict[tuple[str, str], dict[str, Any]]]:
    """Return per-date hard-API coverage from immutable observation versions.

    The current raw table is a projection and can only tell us what is present
    now.  Formal PIT evidence must select a version that was observed no later
    than that date's 15:00 cutoff.  A missing version table, legacy provenance,
    or a partial date is therefore an actual hard-data failure.
    """
    result: dict[str, set[str]] = {api: set() for api in HARD_APIS}
    evidence: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        with _conn() as conn:
            is_postgres = bool(getattr(conn, "_is_postgres", False))
            cutoff = _cutoff_expression("v.trade_date")
            observed_pit = _timestamp_at_or_before_sql(
                "v.observed_at", cutoff, is_postgres=is_postgres,
            )
            first_observed_pit = _timestamp_at_or_before_sql(
                "v.first_observed_at", cutoff, is_postgres=is_postgres,
            )
            economic_pit = _timestamp_at_or_before_sql(
                "v.economic_available_time", cutoff, is_postgres=is_postgres,
            )
            created_pit = _timestamp_at_or_before_sql(
                "v.created_at", cutoff, is_postgres=is_postgres,
            )
            observed_order = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
            created_order = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
            rows = conn.execute(
                f"""SELECT api_name,trade_date,
                          COUNT(DISTINCT ts_code) AS point_in_time_rows,
                          COUNT(DISTINCT ts_code) AS raw_rows,
                          COALESCE(MIN(observed_at), '') AS first_observed_at,
                          COALESCE(MIN(created_at), '') AS first_created_at
                     FROM (
                         SELECT v.api_name,v.trade_date,v.ts_code,v.observed_at,v.created_at,v.id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY v.api_name,v.record_key
                                    ORDER BY {observed_order} DESC,{created_order} DESC,v.id DESC
                                ) AS row_rank
                           FROM dc_tushare_raw_record_version v
                          WHERE v.api_name IN (?,?,?,?)
                            AND v.trade_date BETWEEN ? AND ?
                            AND v.ts_code<>''
                            AND v.observed_at<>''
                            AND v.first_observed_at<>''
                            AND v.economic_available_time<>''
                            AND v.created_at<>''
                            AND v.content_hash<>''
                            AND {_valid_provenance_sql('v.observation_provenance')}
                            AND {observed_pit}
                            AND {first_observed_pit}
                            AND {economic_pit}
                            AND {created_pit}
                     ) visible
                    WHERE row_rank=1
                    GROUP BY api_name,trade_date""",
                (*HARD_APIS, start_date, end_date),
            ).fetchall()
    except Exception:
        return result, evidence
    for row in rows:
        item = dict(row)
        api = str(item.get("api_name") or "")
        trade_date = str(item.get("trade_date") or "")
        if api not in result or not trade_date:
            continue
        point_rows = int(item.get("point_in_time_rows") or 0)
        raw_rows = int(item.get("raw_rows") or 0)
        evidence[(api, trade_date)] = {
            "point_in_time_rows": point_rows,
            "raw_rows": raw_rows,
            "first_observed_at": str(item.get("first_observed_at") or ""),
            "first_created_at": str(item.get("first_created_at") or ""),
        }
        if point_rows >= MINIMUM_HARD_API_ROWS:
            result[api].add(trade_date)
    return result, evidence


def _observation_evidence_available(start_date: str, end_date: str) -> bool:
    """Strict OOS needs append-only evidence, never inferred legacy clocks."""

    with _conn() as conn:
        try:
            row = conn.execute(
                f"""SELECT COUNT(*) AS count FROM dc_tushare_raw_record_version
                    WHERE trade_date BETWEEN ? AND ?
                      AND observed_at<>'' AND first_observed_at<>''
                      AND {_valid_provenance_sql('observation_provenance')}""",
                (start_date, end_date),
            ).fetchone()
            return int(dict(row).get("count") or 0) > 0
        except Exception:
            return False


def _strict_observation_schema_available() -> bool:
    """Return whether both append-only PIT version tables are queryable.

    An empty version table is still a valid schema; callers must separately
    require per-date evidence for eligibility.  If either table is absent or
    the migration is only partially applied, repair and formal consumers fail
    closed instead of reading mutable projections.
    """

    try:
        with _conn() as conn:
            required_raw = {
                "api_name", "record_key", "ts_code", "trade_date",
                "payload_json", "source_code", "economic_available_time",
                "observed_at", "first_observed_at", "observation_provenance",
                "content_hash", "created_at",
            }
            required_snapshot = {
                "ts_code", "trade_date", "payload_json", "source_code",
                "economic_available_time", "observed_at", "first_observed_at",
                "observation_provenance", "content_hash", "created_at",
            }
            def columns(table: str) -> set[str]:
                if bool(getattr(conn, "_is_postgres", False)):
                    rows = conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=?",
                        (table,),
                    ).fetchall()
                    return {str(dict(row).get("column_name") or "") for row in rows}
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                return {str(dict(row).get("name") or "") for row in rows}
            return required_raw.issubset(columns("dc_tushare_raw_record_version")) and required_snapshot.issubset(columns("dc_stock_daily_snapshot_version"))
    except Exception:
        return False


def _benchmark_date_set(start_date: str, end_date: str) -> set[str]:
    """Compatibility name for strict benchmark availability.

    Formal repair verification must use the append-only observation versions.
    The mutable raw projection can contain a complete bar fetched after the
    historical close, which is useful for today's display but cannot prove
    that the bar was available to the historical decision.
    """
    return set(_strict_benchmark_date_set(start_date, end_date))


def _strict_benchmark_date_set(
    start_date: str,
    end_date: str,
    *,
    include_evidence: bool = False,
) -> set[str] | tuple[set[str], dict[str, dict[str, Any]]]:
    """Benchmark bars with immutable, field-complete observation evidence."""

    evidence: dict[str, dict[str, Any]] = {}
    try:
        with _conn() as conn:
            is_postgres = bool(getattr(conn, "_is_postgres", False))
            cutoff = _cutoff_expression("v.trade_date")
            observed_pit = _timestamp_at_or_before_sql(
                "v.observed_at", cutoff, is_postgres=is_postgres,
            )
            first_observed_pit = _timestamp_at_or_before_sql(
                "v.first_observed_at", cutoff, is_postgres=is_postgres,
            )
            economic_pit = _timestamp_at_or_before_sql(
                "v.economic_available_time", cutoff, is_postgres=is_postgres,
            )
            created_pit = _timestamp_at_or_before_sql(
                "v.created_at", cutoff, is_postgres=is_postgres,
            )
            if is_postgres:
                field_complete = (
                    "NULLIF(visible.payload_json::jsonb->>'open','') IS NOT NULL AND "
                    "NULLIF(visible.payload_json::jsonb->>'high','') IS NOT NULL AND "
                    "NULLIF(visible.payload_json::jsonb->>'low','') IS NOT NULL AND "
                    "NULLIF(visible.payload_json::jsonb->>'close','') IS NOT NULL AND "
                    "NULLIF(visible.payload_json::jsonb->>'vol','') IS NOT NULL"
                )
            else:
                field_complete = (
                    "json_extract(visible.payload_json,'$.open') IS NOT NULL AND "
                    "json_extract(visible.payload_json,'$.high') IS NOT NULL AND "
                    "json_extract(visible.payload_json,'$.low') IS NOT NULL AND "
                    "json_extract(visible.payload_json,'$.close') IS NOT NULL AND "
                    "json_extract(visible.payload_json,'$.vol') IS NOT NULL"
                )
            observed_order = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
            created_order = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
            rows = conn.execute(
                f"""SELECT trade_date,
                          COUNT(DISTINCT CASE WHEN {field_complete} THEN ts_code END) AS point_in_time_rows,
                          COUNT(DISTINCT ts_code) AS raw_rows,
                          COALESCE(MIN(CASE WHEN {field_complete} THEN observed_at END), '') AS first_observed_at,
                          COALESCE(MIN(CASE WHEN {field_complete} THEN created_at END), '') AS first_created_at
                     FROM (
                         SELECT v.trade_date,v.ts_code,v.payload_json,v.observed_at,v.created_at,v.id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY v.record_key
                                    ORDER BY {observed_order} DESC,{created_order} DESC,v.id DESC
                                ) AS row_rank
                           FROM dc_tushare_raw_record_version v
                          WHERE v.api_name='index_daily' AND v.ts_code=?
                            AND v.trade_date BETWEEN ? AND ?
                            AND v.observed_at<>'' AND v.first_observed_at<>''
                            AND v.economic_available_time<>'' AND v.created_at<>''
                            AND v.content_hash<>''
                            AND {_valid_provenance_sql('v.observation_provenance')}
                            AND {observed_pit} AND {first_observed_pit}
                            AND {economic_pit} AND {created_pit}
                     ) visible
                    WHERE row_rank=1
                    GROUP BY trade_date""",
                (BENCHMARK_INDEX_CODE, start_date, end_date),
            ).fetchall()
    except Exception:
        return (set(), evidence) if include_evidence else set()
    valid: set[str] = set()
    for row in rows:
        item = dict(row)
        trade_date = str(item.get("trade_date") or "")
        if not trade_date:
            continue
        point_rows = int(item.get("point_in_time_rows") or 0)
        evidence[trade_date] = {
            "point_in_time_rows": point_rows,
            "raw_rows": int(item.get("raw_rows") or 0),
            "first_observed_at": str(item.get("first_observed_at") or ""),
            "first_created_at": str(item.get("first_created_at") or ""),
            "required_fields": ["open", "high", "low", "close", "vol"],
        }
        if point_rows >= 1:
            valid.add(trade_date)
    return (valid, evidence) if include_evidence else valid


def _unpack_benchmark_evidence(
    value: set[str] | tuple[set[str], dict[str, dict[str, Any]]],
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Keep internal test seams compatible with the original set-only helper."""

    if isinstance(value, tuple) and len(value) == 2:
        dates, evidence = value
        if isinstance(dates, set) and isinstance(evidence, dict):
            return dates, evidence
    return set(value or ()), {}


def _strict_benchmark_with_evidence(
    start_date: str,
    end_date: str,
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Call the benchmark helper while preserving simple monkeypatch seams."""

    try:
        value = _strict_benchmark_date_set(start_date, end_date, include_evidence=True)
    except TypeError:
        value = _strict_benchmark_date_set(start_date, end_date)
    return _unpack_benchmark_evidence(value)


def _strict_snapshot_stats(start_date: str, end_date: str) -> dict[str, dict[str, Any]]:
    """Aggregate latest normalized versions visible at each T-day cutoff.

    Versions must be filtered by the cutoff *before* their logical
    ``(ts_code, trade_date)`` rank is calculated.  Otherwise a late repair
    could replace an earlier valid snapshot in the aggregate merely because
    it is the latest row today.
    """
    try:
        with _conn() as conn:
            is_postgres = bool(getattr(conn, "_is_postgres", False))
            cutoff = _cutoff_expression("v.trade_date")
            observed_pit = _timestamp_at_or_before_sql(
                "v.observed_at", cutoff, is_postgres=is_postgres,
            )
            first_observed_pit = _timestamp_at_or_before_sql(
                "v.first_observed_at", cutoff, is_postgres=is_postgres,
            )
            economic_pit = _timestamp_at_or_before_sql(
                "v.economic_available_time", cutoff, is_postgres=is_postgres,
            )
            created_pit = _timestamp_at_or_before_sql(
                "v.created_at", cutoff, is_postgres=is_postgres,
            )
            observed_explicit = _timestamp_is_explicit_sql(
                "v.observed_at", is_postgres=is_postgres,
            )
            created_explicit = _timestamp_is_explicit_sql(
                "v.created_at", is_postgres=is_postgres,
            )
            first_observed_explicit = _timestamp_is_explicit_sql(
                "v.first_observed_at", is_postgres=is_postgres,
            )
            economic_explicit = _timestamp_is_explicit_sql(
                "v.economic_available_time", is_postgres=is_postgres,
            )
            if is_postgres:
                late_arrival = (
                    f"({observed_explicit} AND {_timestamp_value_sql('v.observed_at', is_postgres=True)} > ({cutoff})::timestamptz) "
                    f"OR ({first_observed_explicit} AND {_timestamp_value_sql('v.first_observed_at', is_postgres=True)} > ({cutoff})::timestamptz) "
                    f"OR ({economic_explicit} AND {_timestamp_value_sql('v.economic_available_time', is_postgres=True)} > ({cutoff})::timestamptz) "
                    f"OR ({created_explicit} AND {_timestamp_value_sql('v.created_at', is_postgres=True)} > ({cutoff})::timestamptz)"
                )
            else:
                late_arrival = (
                    f"({observed_explicit} AND {_timestamp_value_sql('v.observed_at', is_postgres=False)} > julianday({cutoff})) "
                    f"OR ({first_observed_explicit} AND {_timestamp_value_sql('v.first_observed_at', is_postgres=False)} > julianday({cutoff})) "
                    f"OR ({economic_explicit} AND {_timestamp_value_sql('v.economic_available_time', is_postgres=False)} > julianday({cutoff})) "
                    f"OR ({created_explicit} AND {_timestamp_value_sql('v.created_at', is_postgres=False)} > julianday({cutoff}))"
                )
            market_fields = (
                "visible.payload_json::jsonb->>'close' <> '' AND visible.payload_json::jsonb->>'volume' <> '' "
                "AND visible.payload_json::jsonb->>'turnover' <> '' AND visible.payload_json::jsonb->>'adj_factor' <> '' "
                "AND visible.payload_json::jsonb->>'total_mv' <> '' AND visible.payload_json::jsonb->>'circ_mv' <> '' "
                "AND visible.payload_json::jsonb->>'pe_ttm' <> '' AND visible.payload_json::jsonb->>'pb' <> '' "
                "AND visible.payload_json::jsonb->>'turnover_rate' <> '' AND visible.payload_json::jsonb->>'up_limit' <> '' "
                "AND visible.payload_json::jsonb->>'down_limit' <> ''"
                if is_postgres
                else "json_extract(visible.payload_json,'$.close') IS NOT NULL AND json_extract(visible.payload_json,'$.volume') IS NOT NULL "
                     "AND json_extract(visible.payload_json,'$.turnover') IS NOT NULL AND json_extract(visible.payload_json,'$.adj_factor') IS NOT NULL "
                     "AND json_extract(visible.payload_json,'$.total_mv') IS NOT NULL AND json_extract(visible.payload_json,'$.circ_mv') IS NOT NULL "
                     "AND json_extract(visible.payload_json,'$.pe_ttm') IS NOT NULL AND json_extract(visible.payload_json,'$.pb') IS NOT NULL "
                     "AND json_extract(visible.payload_json,'$.turnover_rate') IS NOT NULL AND json_extract(visible.payload_json,'$.up_limit') IS NOT NULL "
                     "AND json_extract(visible.payload_json,'$.down_limit') IS NOT NULL"
            )
            observed_order = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
            created_order = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
            rows = conn.execute(
                f"""WITH raw_summary AS (
                         SELECT v.trade_date,
                                COUNT(DISTINCT v.ts_code) AS raw_rows,
                                COUNT(DISTINCT CASE WHEN ({late_arrival}) THEN v.ts_code END) AS late_rows
                          FROM dc_stock_daily_snapshot_version v
                          WHERE v.trade_date BETWEEN ? AND ?
                            AND v.observed_at<>'' AND v.first_observed_at<>''
                            AND v.economic_available_time<>'' AND v.created_at<>''
                            AND v.content_hash<>''
                            AND {_valid_provenance_sql('v.observation_provenance')}
                          GROUP BY v.trade_date
                     ), visible_ranked AS (
                         SELECT v.ts_code,v.trade_date,v.payload_json,v.observed_at,v.created_at,v.id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY v.ts_code,v.trade_date
                                    ORDER BY {observed_order} DESC,{created_order} DESC,v.id DESC
                                ) AS row_rank
                          FROM dc_stock_daily_snapshot_version v
                          WHERE v.trade_date BETWEEN ? AND ?
                            AND v.observed_at<>'' AND v.first_observed_at<>''
                            AND v.economic_available_time<>'' AND v.created_at<>''
                            AND v.content_hash<>''
                            AND {_valid_provenance_sql('v.observation_provenance')}
                            AND {observed_pit} AND {first_observed_pit}
                            AND {economic_pit} AND {created_pit}
                     ), visible AS (
                         SELECT * FROM visible_ranked WHERE row_rank=1
                     )
                     SELECT COALESCE(visible.trade_date, raw_summary.trade_date) AS trade_date,
                            COALESCE(raw_summary.raw_rows, 0) AS raw_rows,
                            COALESCE(raw_summary.late_rows, 0) AS late_rows,
                            COUNT(DISTINCT visible.ts_code) AS rows_total,
                            COUNT(DISTINCT CASE WHEN {market_fields} THEN visible.ts_code END) AS market_rows,
                            COALESCE(MIN(visible.observed_at), '') AS min_observed_at,
                            COALESCE(MAX(visible.observed_at), '') AS max_observed_at,
                            COALESCE(MIN(visible.created_at), '') AS min_created_at,
                            COALESCE(MAX(visible.created_at), '') AS max_created_at
                       FROM raw_summary
                       FULL OUTER JOIN visible ON visible.trade_date=raw_summary.trade_date
                      GROUP BY COALESCE(visible.trade_date, raw_summary.trade_date),
                               raw_summary.raw_rows, raw_summary.late_rows""",
                (start_date, end_date, start_date, end_date),
            ).fetchall()
    except Exception:
        return {}
    return {str(dict(row).get("trade_date") or ""): dict(row) for row in rows if dict(row).get("trade_date")}


def _current_api_row_counts(start_date: str, end_date: str) -> dict[tuple[str, str], int]:
    """Current projection counts used only to classify legacy evidence."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT api_name,trade_date,COUNT(DISTINCT ts_code) AS rows_total
                     FROM dc_tushare_raw_records
                    WHERE api_name IN (?,?,?,?) AND trade_date BETWEEN ? AND ?
                      AND ts_code<>''
                    GROUP BY api_name,trade_date""",
                (*HARD_APIS, start_date, end_date),
            ).fetchall()
    except Exception:
        return {}
    return {
        (str(dict(row).get("api_name") or ""), str(dict(row).get("trade_date") or "")): int(dict(row).get("rows_total") or 0)
        for row in rows
    }


def _late_arrival_hard_api_sets(details: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Identify complete raw slices that arrived after their historical cutoff.

    Those slices are evidence of a real point-in-time gap, not an upstream
    retrieval gap. Retrying them cannot make an old decision observable, so
    they must remain blocked without repeatedly consuming repair quota.
    """
    candidates = {
        str(item.get("trade_date") or ""): set(item.get("missing_apis") or []) & set(HARD_APIS)
        for item in details
        if isinstance(item, dict) and not bool(item.get("eligible"))
    }
    candidates = {date: apis for date, apis in candidates.items() if date and apis}
    if not candidates:
        return {}
    dates = sorted(candidates)
    placeholders = ",".join("?" for _ in dates)
    with _conn() as conn:
        try:
            is_postgres = bool(getattr(conn, "_is_postgres", False))
            cutoff = _cutoff_expression("trade_date")
            observed_pit = _timestamp_at_or_before_sql(
                "observed_at", cutoff, is_postgres=is_postgres,
            )
            first_observed_pit = _timestamp_at_or_before_sql(
                "first_observed_at", cutoff, is_postgres=is_postgres,
            )
            economic_pit = _timestamp_at_or_before_sql(
                "economic_available_time", cutoff, is_postgres=is_postgres,
            )
            created_pit = _timestamp_at_or_before_sql(
                "created_at", cutoff, is_postgres=is_postgres,
            )
            observed_explicit = _timestamp_is_explicit_sql(
                "observed_at", is_postgres=is_postgres,
            )
            first_observed_explicit = _timestamp_is_explicit_sql(
                "first_observed_at", is_postgres=is_postgres,
            )
            economic_explicit = _timestamp_is_explicit_sql(
                "economic_available_time", is_postgres=is_postgres,
            )
            created_explicit = _timestamp_is_explicit_sql(
                "created_at", is_postgres=is_postgres,
            )
            if is_postgres:
                after_cutoff = (
                    f"({observed_explicit} AND {_timestamp_value_sql('observed_at', is_postgres=True)} > ({cutoff})::timestamptz) "
                    f"OR ({first_observed_explicit} AND {_timestamp_value_sql('first_observed_at', is_postgres=True)} > ({cutoff})::timestamptz) "
                    f"OR ({economic_explicit} AND {_timestamp_value_sql('economic_available_time', is_postgres=True)} > ({cutoff})::timestamptz) "
                    f"OR ({created_explicit} AND {_timestamp_value_sql('created_at', is_postgres=True)} > ({cutoff})::timestamptz)"
                )
            else:
                after_cutoff = (
                    f"({observed_explicit} AND {_timestamp_value_sql('observed_at', is_postgres=False)} > julianday({cutoff})) "
                    f"OR ({first_observed_explicit} AND {_timestamp_value_sql('first_observed_at', is_postgres=False)} > julianday({cutoff})) "
                    f"OR ({economic_explicit} AND {_timestamp_value_sql('economic_available_time', is_postgres=False)} > julianday({cutoff})) "
                    f"OR ({created_explicit} AND {_timestamp_value_sql('created_at', is_postgres=False)} > julianday({cutoff}))"
                )
            rows = conn.execute(
                f"""SELECT api_name,trade_date,
                          COUNT(DISTINCT ts_code) AS raw_rows,
                          COUNT(DISTINCT CASE WHEN {observed_pit} AND {first_observed_pit}
                            AND {economic_pit} AND {created_pit}
                            THEN ts_code END) AS point_in_time_rows,
                          COUNT(DISTINCT CASE WHEN ({after_cutoff}) THEN ts_code END) AS late_rows,
                          COALESCE(MIN(first_observed_at), '') AS first_observed_at,
                          COALESCE(MIN(economic_available_time), '') AS first_economic_available_time,
                          COALESCE(MIN(created_at), '') AS first_created_at
                      FROM dc_tushare_raw_record_version
                     WHERE api_name IN (?,?,?,?) AND trade_date IN ({placeholders})
                       AND ts_code<>'' AND observed_at<>'' AND first_observed_at<>''
                       AND economic_available_time<>'' AND created_at<>''
                       AND content_hash<>''
                       AND {_valid_provenance_sql('observation_provenance')}
                     GROUP BY api_name,trade_date""",
                (*HARD_APIS, *dates),
            ).fetchall()
        except Exception:
            # No immutable version evidence means the gap cannot safely be
            # classified as repairable or late.  The caller remains blocked.
            return {}
    late: dict[str, set[str]] = {}
    for row in rows:
        item = dict(row)
        api = str(item.get("api_name") or "")
        trade_date = str(item.get("trade_date") or "")
        if (
            api in candidates.get(trade_date, set())
            and int(item.get("raw_rows") or 0) >= MINIMUM_HARD_API_ROWS
            and int(item.get("point_in_time_rows") or 0) < MINIMUM_HARD_API_ROWS
            and int(item.get("late_rows") or 0) >= MINIMUM_HARD_API_ROWS
        ):
            late.setdefault(trade_date, set()).add(api)
    return late


def _classify_snapshot_late_arrivals(
    details: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
) -> set[str]:
    """A snapshot rebuilt after cutoff is an irreversible historical gap."""

    late: set[str] = set()
    for item in details:
        trade_date = str(item.get("trade_date") or "")
        snapshot = snapshots.get(trade_date) or {}
        if not trade_date or bool(item.get("eligible")):
            continue
        if (
            int(snapshot.get("raw_rows") or 0) >= MINIMUM_DAILY_ROWS
            and int(snapshot.get("rows_total") or 0) < MINIMUM_DAILY_ROWS
            and int(snapshot.get("late_rows") or 0) >= MINIMUM_DAILY_ROWS
        ):
            late.add(trade_date)
    return late


def _classify_repairable_missing_apis(
    item: dict[str, Any],
    *,
    late_hard_apis: set[str] | None = None,
    snapshot_late_arrival: bool = False,
    strict_evidence_backend_available: bool,
    historical_cutoff_elapsed: bool | None = None,
) -> tuple[list[str], bool, bool]:
    """Separate a fetchable gap from an irreversible historical PIT gap.

    A version that first arrived after a historical cutoff cannot be repaired
    by fetching it again today: doing so would only create another late
    observation.  Nor can an absent version be repaired once that cutoff has
    elapsed: absence is not proof of late arrival, but it is still not an
    actionable source gap.  The benchmark is independent from the stock
    slice only while its own cutoff is still open.

    The return is ``(repairable_missing_apis, repairable, permanently_late)``.
    It deliberately does not infer completeness from mutable projections.
    """

    missing = set(item.get("missing_apis") or [])
    late = set(late_hard_apis or ()) & set(HARD_APIS)
    calendar_present = item.get("trade_calendar_present")
    if calendar_present is False:
        # Calendar repair has its own bounded path.  Do not turn a calendar
        # absence into a fake retained-API gap or invoke a market fetch.
        return [], False, False
    if not strict_evidence_backend_available:
        # A missing append-only schema is a deployment/contract error, not a
        # source-data gap.  Fetching cannot make the historical clock valid.
        # Once its cutoff elapsed it is still historical PIT evidence absent,
        # not a generic deployment retry.  Preserve that explicit reason for
        # the caller while keeping the repair set empty.
        if historical_cutoff_elapsed is None:
            historical_cutoff_elapsed = _trade_cutoff_elapsed(str(item.get("trade_date") or ""))
        # A missing/invalid append-only backend proves that the PIT evidence
        # contract cannot be evaluated, but it does not prove that the source
        # arrived late.  Keep the distinction explicit so operations can
        # diagnose a deployment/schema issue separately from a late source.
        return [], False, False

    if historical_cutoff_elapsed is None:
        historical_cutoff_elapsed = _trade_cutoff_elapsed(str(item.get("trade_date") or ""))
    if historical_cutoff_elapsed:
        # Missing immutable evidence after the close remains a strict formal
        # research block.  It must never be sent to TuShare or a fallback,
        # because a new response would necessarily be observed too late.
        # Historical absence is non-repairable, but absence alone is not
        # evidence that a source was observed after the cutoff.  The caller
        # still records ``historical_observation_evidence_missing``; only the
        # concrete late-arrival classifiers below may add the stronger
        # ``historical_point_in_time_unavailable`` reason.
        return [], False, bool(snapshot_late_arrival or late)

    permanently_late = bool(snapshot_late_arrival or late)
    if permanently_late:
        # Preserve the independently fetchable benchmark case, but never
        # retry stock slices whose historical availability is already disproved.
        repairable_missing = [BENCHMARK_API_KEY] if BENCHMARK_API_KEY in missing else []
    else:
        repairable_missing = sorted(missing)
    return repairable_missing, bool(repairable_missing), permanently_late


def _repair_benchmark_gaps(
    benchmark_dates: list[str], benchmark_available: set[str]
) -> list[dict[str, Any]]:
    """Use audited peer sources for explicit dates still absent after TuShare."""
    from app.data_platform.services.eastmoney_index_daily_fallback import (
        repair_hs300_index_daily_from_eastmoney,
    )

    results: list[dict[str, Any]] = []
    for trade_date in benchmark_dates:
        if trade_date in benchmark_available:
            continue
        fallback = repair_hs300_index_daily_from_eastmoney(trade_date)
        results.append({"trade_date": trade_date, **fallback})
    return results


def _calendar_not_ready_audit(
    exc: TradeCalendarNotReady,
    *,
    required_sessions: int = REQUIRED_SESSIONS,
) -> dict[str, Any]:
    """Return an explicit hard-data block without inventing an OOS window."""
    consumer_eligibility = {
        name: {
            "eligible": False,
            "required_sessions": sessions,
            "eligible_sessions": 0,
            "production_change_authorized": False,
        }
        for name, sessions in PROGRESSIVE_CONSUMER_TIERS.items()
    }
    return {
        "ok": False,
        "audit_completed": False,
        "schema_version": CONTRACT_VERSION,
        "status": "blocked",
        "failure_category": exc.code,
        "reason": str(exc),
        "hard_requirement": False,
        "consumer": "Progressive OOS simulation qualification",
        "required_sessions": required_sessions,
        "calendar_sessions": 0,
        "eligible_sessions": 0,
        "missing_sessions": required_sessions,
        "calendar_missing_sessions": required_sessions,
        "coverage": 0.0,
        "window_start": "",
        "window_end": "",
        "hard_apis": [*HARD_APIS, BENCHMARK_API_KEY],
        "repair_dates": [],
        "non_repairable_dates": [],
        "non_repairable_sessions": 0,
        "legacy_reference": {"status": "not_assessed"},
        "new_strict_baseline": {
            "status": "unavailable", "eligible_sessions": 0,
            "required_sessions": NEW_STRICT_BASELINE_SESSIONS,
            "consumer_eligibility": consumer_eligibility,
        },
        "consumer_eligibility": consumer_eligibility,
        "trailing_pending_dates": [],
        "observation_evidence": {
            "required": True,
            "append_only_versions_present": False,
            "legacy_rows_allowed": False,
            "missing_reason": exc.code,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _new_strict_baseline(details: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe consecutive current PIT evidence without relaxing formal OOS."""
    contiguous: list[str] = []
    for item in reversed(details):
        if not bool(item.get("eligible")):
            break
        contiguous.append(str(item.get("trade_date") or ""))
    contiguous.reverse()
    observed = len(contiguous)
    if observed >= PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS:
        status = "simulation_champion_evaluation_data_ready"
    elif observed >= PROGRESSIVE_SIMULATION_SESSIONS:
        status = "simulation_evaluation_data_ready"
    elif observed >= PROGRESSIVE_DIAGNOSTIC_SESSIONS:
        status = "diagnostic_data_ready"
    else:
        status = "accumulating"
    return {
        "schema_version": CONTRACT_VERSION,
        "status": status,
        "eligible_sessions": observed,
        "required_sessions": NEW_STRICT_BASELINE_SESSIONS,
        "coverage": round(min(1.0, observed / NEW_STRICT_BASELINE_SESSIONS), 6),
        "window_start": contiguous[0] if contiguous else "",
        "window_end": contiguous[-1] if contiguous else "",
        "qualification_authority": "data_gate_only",
        "strategy_validation_required": True,
        "diagnostic_data_ready": observed >= PROGRESSIVE_DIAGNOSTIC_SESSIONS,
        "simulation_evaluation_data_ready": observed >= PROGRESSIVE_SIMULATION_SESSIONS,
        "simulation_champion_evaluation_data_ready": observed >= PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS,
        "live_promotion_authorized": False,
        "consumer_eligibility": {
            name: {
                "eligible": observed >= sessions,
                "required_sessions": sessions,
                "eligible_sessions": observed,
                "production_change_authorized": False,
            }
            for name, sessions in PROGRESSIVE_CONSUMER_TIERS.items()
        },
        "tiers": {
            "diagnostic": PROGRESSIVE_DIAGNOSTIC_SESSIONS,
            "simulation": PROGRESSIVE_SIMULATION_SESSIONS,
            "simulation_champion": PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS,
        },
        "formal_oos_required_sessions": REQUIRED_SESSIONS,
        "note": "仅用于新严格 PIT 证据积累；不替代正式五窗口 OOS 或 Champion 晋升门",
    }


def audit_oos_hard_data(
    *,
    end_date: str | None = None,
    include_dates: bool = True,
    window_sessions: int | None = None,
) -> dict[str, Any]:
    """Audit the latest contiguous protocol-sized calendar window.

    A session is eligible only when every hard API has a retained slice, the
    normalized snapshot has broad OHLC-volume coverage, and its availability
    timestamp is no later than the T-day close cutoff.
    """
    required_sessions = max(1, int(window_sessions or REQUIRED_SESSIONS))
    try:
        with _conn() as conn:
            end = str(end_date or target_trade_date(conn)).replace("-", "")[:8]
    except TradeCalendarNotReady as exc:
        return _calendar_not_ready_audit(exc, required_sessions=required_sessions)
    with _conn() as conn:
        calendar_limit = required_sessions if end_date else required_sessions + PENDING_TRADE_DAY_BUFFER
        calendar_rows = conn.execute(
            """SELECT cal_date FROM dc_trade_calendar
               WHERE exchange='SSE' AND is_open=1 AND cal_date<=?
               ORDER BY cal_date DESC LIMIT ?""",
            (end, calendar_limit),
        ).fetchall()
        calendar_dates = list(reversed([str(dict(row).get("cal_date") or "") for row in calendar_rows]))
    start = calendar_dates[0] if calendar_dates else end
    api_dates, api_evidence = _strict_date_api_sets(start, end)
    strict_schema = _strict_observation_schema_available()
    benchmark_dates, benchmark_evidence = _strict_benchmark_with_evidence(start, end)
    with _conn() as conn:
        snapshots = snapshot_stats_for_range(
            conn,
            start,
            end,
            expected_dates=calendar_dates,
        )
        if snapshots is None:
            snapshots = {}
    # Persisted current summaries are useful for operations, but formal PIT
    # evidence must come from the immutable version table.  Use it whenever
    # available and fail closed when the migration/evidence is absent.
    # A successful strict evidence query itself proves that the append-only
    # table is usable.  This also keeps thin test/read-only replicas from
    # falsely reporting a schema outage when they can in fact read the data.
    # An empty version table remains fail-closed below because no date can
    # become eligible without concrete raw and snapshot evidence.
    strict_snapshots = _strict_snapshot_stats(start, end)
    strict_evidence_backend_available = bool(strict_schema or api_evidence)
    strict_observation_evidence = bool(api_evidence) and strict_evidence_backend_available
    details: list[dict[str, Any]] = []
    for trade_date in calendar_dates:
        snapshot = strict_snapshots.get(trade_date) or {}
        frozen_replay_fallback = frozen_replay_oos_fallback_enabled()
        if not snapshot:
            # Keep row counts for diagnostics only; they must not make a date
            # eligible without an observation-version snapshot.
            snapshot = dict(snapshots.get(trade_date) or {})
            if not frozen_replay_fallback:
                snapshot["rows_total"] = 0
                snapshot["market_rows"] = 0
                snapshot["max_available_time"] = ""
        rows_total = int(snapshot.get("rows_total") or 0)
        market_rows = int(snapshot.get("market_rows") or 0)
        ratio = market_rows / rows_total if rows_total else 0.0
        cutoff = _business_cutoff(trade_date)
        missing_apis = [api for api in HARD_APIS if trade_date not in api_dates[api]]
        if trade_date not in benchmark_dates:
            missing_apis.append(BENCHMARK_API_KEY)
        calendar_ok = bool(trade_date)
        pit_ok = bool(snapshot.get("max_observed_at")) and _timestamp_at_or_before(
            snapshot.get("max_observed_at"), cutoff,
        )
        strict_eligible = (
            strict_observation_evidence and rows_total >= MINIMUM_DAILY_ROWS
            and ratio >= MINIMUM_MARKET_FIELD_RATIO and not missing_apis and pit_ok
        )
        frozen_replay_eligible = bool(
            frozen_replay_fallback
            and rows_total >= MINIMUM_DAILY_ROWS
            and ratio >= MINIMUM_MARKET_FIELD_RATIO
        )
        eligible = strict_eligible or frozen_replay_eligible
        reasons = []
        if rows_total < MINIMUM_DAILY_ROWS:
            reasons.append("snapshot_rows_insufficient")
        if ratio < MINIMUM_MARKET_FIELD_RATIO:
            reasons.append("market_fields_incomplete")
        if missing_apis:
            reasons.append("missing_hard_api_slices")
        if BENCHMARK_API_KEY in missing_apis:
            reasons.append("benchmark_index_missing")
        if not pit_ok:
            reasons.append("point_in_time_invalid")
        date_observation_complete = all(
            int((api_evidence.get((api, trade_date)) or {}).get("point_in_time_rows") or 0) >= MINIMUM_HARD_API_ROWS
            for api in HARD_APIS
        ) and trade_date in benchmark_dates and trade_date in strict_snapshots
        if calendar_ok and not date_observation_complete and _trade_cutoff_elapsed(trade_date):
            reasons.append("historical_observation_evidence_missing")
        details.append({
            "trade_date": trade_date, "eligible": eligible, "rows_total": rows_total,
            "market_rows": market_rows, "market_field_ratio": round(ratio, 6),
            "missing_apis": missing_apis, "point_in_time_passed": pit_ok,
            "max_available_time": snapshot.get("max_observed_at") or snapshot.get("max_available_time"),
            "observation_evidence": {
                api: dict(api_evidence.get((api, trade_date)) or {})
                for api in HARD_APIS
            },
            "benchmark_observation_evidence": dict(benchmark_evidence.get(trade_date) or {}),
            "observation_evidence_complete": date_observation_complete,
            "qualification_mode": "strict_pit" if strict_eligible else "frozen_replay_before_candidate",
            "reasons": reasons,
        })
    late_hard_apis = _late_arrival_hard_api_sets(details)
    late_snapshot_dates = _classify_snapshot_late_arrivals(details, strict_snapshots)
    for item in details:
        trade_date = str(item["trade_date"])
        late = sorted(late_hard_apis.get(trade_date, set()))
        missing = set(item.get("missing_apis") or [])
        missing_hard = missing & set(HARD_APIS)
        repairable_missing, repairable, permanently_late = _classify_repairable_missing_apis(
            item,
            late_hard_apis=set(late),
            snapshot_late_arrival=trade_date in late_snapshot_dates,
            strict_evidence_backend_available=strict_evidence_backend_available,
            historical_cutoff_elapsed=_trade_cutoff_elapsed(trade_date),
        )
        item["late_arrival_apis"] = late
        item["snapshot_late_arrival"] = trade_date in late_snapshot_dates
        item["repairable_missing_apis"] = repairable_missing
        item["repairable"] = repairable
        if permanently_late:
            if "historical_point_in_time_unavailable" not in item["reasons"]:
                item["reasons"].append("historical_point_in_time_unavailable")
            if item.get("snapshot_late_arrival"):
                item["reasons"].append("historical_snapshot_materialized_after_cutoff")
    trailing_pending_dates: list[str] = []
    if not end_date:
        contiguous_newest_first: list[dict[str, Any]] = []
        started = False
        for item in reversed(details):
            if not started and not item["eligible"]:
                trade_date = str(item["trade_date"])
                # Only an as-yet open cutoff is a normal current-data
                # publication wait.  Once T15 has elapsed, absence of
                # immutable evidence is a formal PIT block and must remain in
                # the audited window rather than being silently skipped.
                if not _trade_cutoff_elapsed(trade_date):
                    trailing_pending_dates.append(trade_date)
                    continue
                break
            if not item["eligible"]:
                break
            started = True
            contiguous_newest_first.append(item)
        if len(contiguous_newest_first) >= required_sessions:
            details = list(reversed(contiguous_newest_first[:required_sessions]))
        else:
            usable = [item for item in details if str(item["trade_date"]) not in set(trailing_pending_dates)]
            details = usable[-required_sessions:]
    else:
        details = details[-required_sessions:]
    required_dates = [str(item["trade_date"]) for item in details]
    start = required_dates[0] if required_dates else end
    effective_end = required_dates[-1] if required_dates else end
    eligible_dates = [item["trade_date"] for item in details if item["eligible"]]
    missing_dates = [item["trade_date"] for item in details if not item["eligible"]]
    repair_dates = [item["trade_date"] for item in details if not item["eligible"] and item.get("repairable")]
    all_non_repairable_dates = [
        item["trade_date"] for item in details
        if not item["eligible"] and not item.get("repairable")
    ]
    non_repairable_dates = (
        all_non_repairable_dates
        if include_dates
        else all_non_repairable_dates[:MAX_COMPACT_NON_REPAIRABLE_DATE_SAMPLES]
    )
    calendar_missing = max(0, required_sessions - len(required_dates))
    coverage = len(eligible_dates) / required_sessions
    baseline = _new_strict_baseline(details)
    legacy_reasons = {
        "historical_observation_evidence_missing",
        "historical_point_in_time_unavailable",
        "historical_snapshot_materialized_after_cutoff",
    }
    legacy_reference_mode = bool(all_non_repairable_dates) and not repair_dates and all(
        bool(set(item.get("reasons") or []) & legacy_reasons)
        for item in details if not item["eligible"]
    )
    frozen_replay_formal_ready = bool(
        frozen_replay_fallback
        and required_sessions >= REQUIRED_SESSIONS
        and len(eligible_dates) >= FROZEN_REPLAY_REQUIRED_SESSIONS
    )
    formal_ready = bool(
        required_sessions >= REQUIRED_SESSIONS
        and (
            len(eligible_dates) >= REQUIRED_SESSIONS
            or frozen_replay_formal_ready
        )
    )
    effective_required_sessions = (
        FROZEN_REPLAY_REQUIRED_SESSIONS
        if frozen_replay_formal_ready else required_sessions
    )
    coverage = len(eligible_dates) / effective_required_sessions
    if frozen_replay_formal_ready and eligible_dates:
        start = str(eligible_dates[-effective_required_sessions])
        effective_end = str(eligible_dates[-1])
    audit_backend_failed = not strict_evidence_backend_available
    actionable_gate_failure = bool(repair_dates)
    active_status = (
        "ready" if formal_ready
        else "blocked" if audit_backend_failed or actionable_gate_failure
        else "accumulating" if legacy_reference_mode or required_sessions < REQUIRED_SESSIONS
        else "blocked"
    )
    result = {
        # The audit completed successfully even while evidence accumulates.
        # Formal consumers must use promotion_authorized/status, not turn a
        # normal prospective wait into an operational failure.
        "ok": active_status != "blocked",
        "audit_completed": True,
        "schema_version": CONTRACT_VERSION,
        "status": active_status,
        "failure_category": (
            "oos_audit_backend_unavailable"
            if audit_backend_failed
            else "repairable_oos_data_gap"
            if actionable_gate_failure
            else None
        ),
        "hard_requirement": False,
        "consumer": "Progressive OOS simulation qualification",
        "required_sessions": effective_required_sessions, "calendar_sessions": len(required_dates),
        "eligible_sessions": len(eligible_dates), "missing_sessions": max(0, effective_required_sessions - len(eligible_dates)),
        "calendar_missing_sessions": calendar_missing, "coverage": round(min(1.0, coverage), 6),
        "window_start": start, "window_end": effective_end,
        "hard_apis": [*HARD_APIS, BENCHMARK_API_KEY],
        "hard_fields": ["close", "volume", "turnover", "total_mv", "circ_mv", "pe_ttm", "pb", "turnover_rate", "up_limit", "down_limit", "benchmark_close", "available_time"],
        "observation_evidence": {
            "required": True,
            "append_only_versions_present": strict_observation_evidence,
            "legacy_rows_allowed": False,
            "missing_reason": None if strict_observation_evidence else "historical_observation_evidence_missing",
            "qualification_mode": (
                "frozen_replay_before_candidate"
                if frozen_replay_fallback else "strict_pit"
            ),
        },
        "thresholds": {"minimum_daily_rows": MINIMUM_DAILY_ROWS, "minimum_hard_api_rows": MINIMUM_HARD_API_ROWS, "minimum_market_field_ratio": MINIMUM_MARKET_FIELD_RATIO},
        "repair_dates": repair_dates,
        "non_repairable_dates": non_repairable_dates,
        "non_repairable_sessions": len(all_non_repairable_dates),
        "non_repairable_dates_truncated": len(non_repairable_dates) < len(all_non_repairable_dates),
        "trailing_pending_dates": list(reversed(trailing_pending_dates)),
        "legacy_reference": {
            "contract_version": LEGACY_CONTRACT_VERSION,
            "status": "retired_unrecoverable" if legacy_reference_mode else "not_activated",
            "strict_pit_recovery": "external_immutable_evidence_only" if legacy_reference_mode else "not_applicable",
            "repair_loop_disabled": legacy_reference_mode,
            "runtime_health_impact": False,
            "current_decision_impact": False,
            "promotion_authority": False,
            "note": (
                "旧窗口缺少可验证的不可变观测证据，仅保留经济时间诊断价值；不得重抓后追认为历史 PIT。"
                if legacy_reference_mode else ""
            ),
        },
        "new_strict_baseline": baseline,
        "consumer_eligibility": baseline["consumer_eligibility"],
        "promotion_authorized": formal_ready,
        "requested_strict_sessions": required_sessions,
        "minimum_windows": (
            FROZEN_REPLAY_MINIMUM_WINDOWS
            if frozen_replay_formal_ready else MINIMUM_WINDOWS
        ),
        "qualification": baseline,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if include_dates:
        result["dates"] = details
    return result


def audit_progressive_oos(*, end_date: str | None = None, include_dates: bool = False) -> dict[str, Any]:
    """Bounded daily audit for simulation qualification and UI health.

    Full legacy-sized evidence is no longer scanned by routine autonomy.  A
    formal historical replay may still request the default full window for an
    explicit archived comparison, but that retired contract has no runtime
    health authority.
    """
    return audit_oos_hard_data(
        end_date=end_date,
        include_dates=include_dates,
        window_sessions=PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS,
    )


def progressive_oos_consumer_eligibility(
    audit: dict[str, Any],
    consumer: str,
) -> dict[str, Any]:
    """Resolve one bounded OOS consumer without granting production authority."""

    name = str(consumer or "")
    required = PROGRESSIVE_CONSUMER_TIERS.get(name)
    if required is None:
        raise ValueError(f"unknown progressive OOS consumer: {name}")
    baseline = audit.get("new_strict_baseline") if isinstance(
        audit.get("new_strict_baseline"), dict
    ) else audit.get("qualification") if isinstance(audit.get("qualification"), dict) else {}
    qualification_mode = str(
        ((audit.get("observation_evidence") or {}).get("qualification_mode"))
        if isinstance(audit.get("observation_evidence"), dict)
        else ""
    )
    frozen_replay = bool(
        frozen_replay_oos_fallback_enabled()
        and qualification_mode == "frozen_replay_before_candidate"
    )
    observed = int(
        audit.get("eligible_sessions")
        if frozen_replay
        else (baseline or {}).get("eligible_sessions")
        or 0
    )
    audit_status = str(audit.get("status") or "")
    eligible = (
        bool(audit.get("audit_completed", True))
        and audit_status != "blocked"
        and observed >= required
    )
    return {
        "schema_version": CONTRACT_VERSION,
        "consumer": name,
        "status": "eligible" if eligible else "blocked" if audit_status == "blocked" else "accumulating",
        "eligible": eligible,
        "eligible_sessions": observed,
        "required_sessions": required,
        "qualification_mode": qualification_mode or "strict_pit",
        "production_change_authorized": False,
        "formal_oos_required_sessions": REQUIRED_SESSIONS,
    }


def oos_hard_data_asset() -> dict[str, Any]:
    audit = audit_progressive_oos(include_dates=False)
    return {
        "key": "oos_hard_data", "name": "渐进 OOS 点时证据", "domain": "模型验证",
        "consumer": audit["consumer"], "weight": 30,
        "target_scope": f"最近 {PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS} 个交易日",
        "frequency": "daily", "source": "TuShare + 标准化日截面", "window_start": audit["window_start"],
        "window_end": audit["window_end"], "window_depth": f"{PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS} sessions",
        "rationale": "按 60/126/252 日逐级开放策略诊断与模拟资格验证；样本数量本身不授予策略权限",
        "actual_records": audit["eligible_sessions"], "expected_records": PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS,
        "coverage": audit["coverage"], "coverage_basis": audit["eligible_sessions"],
        "coverage_expected": PROGRESSIVE_SIMULATION_CHAMPION_SESSIONS, "symbols": 0, "dates": audit["eligible_sessions"],
        "periods_actual": None, "periods_expected": None, "min_date": audit["window_start"],
        "max_date": audit["window_end"], "status": "complete" if audit["status"] == "ready" else "partial",
        "measurement": "eligible_sessions", "period_details": audit.get("qualification"),
        "hard_requirement": False,
    }


def repair_oos_hard_data_batch(trade_dates: list[str], *, date_gaps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Repair only dates a strict audit explicitly marked repairable.

    This is a write boundary, not a convenience fetch helper.  A caller cannot
    turn a late historical observation into a valid PIT record by passing a
    date directly.  Current operational gaps must carry the audit evidence
    that made them repairable.
    """

    from app.data_platform.services.data_center import (
        sync_market_bundle,
        _sync_tushare_compatible_range,
        sync_tushare_date_batch,
    )

    requested = [str(value or "").replace("-", "")[:8] for value in trade_dates]
    requested = [value for value in requested if len(value) == 8 and value.isdigit()]
    gaps = date_gaps or {}
    rejected: dict[str, str] = {}
    authorized: list[str] = []
    for trade_date in requested:
        gap = gaps.get(trade_date)
        if not isinstance(gap, dict):
            rejected[trade_date] = "missing_strict_audit_gap"
            continue
        if not bool(gap.get("repairable")):
            rejected[trade_date] = "not_repairable_by_strict_pit_audit"
            continue
        missing = set(gap.get("repairable_missing_apis") or [])
        if not missing:
            rejected[trade_date] = "no_explicit_repairable_missing_api"
            continue
        authorized.append(trade_date)
    if not authorized:
        return {
            "ok": False,
            "status": "no_repairable_dates",
            "dates": [],
            "rejected_dates": rejected,
            "contract": CONTRACT_VERSION,
        }

    trade_dates = authorized
    market_results: dict[str, dict[str, Any]] = {}
    adj_dates = [
        value for value in trade_dates
        if "adj_factor" in set((gaps.get(value) or {}).get("repairable_missing_apis") or [])
    ]
    benchmark_dates = [
        value for value in trade_dates
        if BENCHMARK_API_KEY in set((gaps.get(value) or {}).get("repairable_missing_apis") or [])
    ]
    for trade_date in trade_dates:
        gap = gaps.get(trade_date) or {}
        missing = set(gap.get("repairable_missing_apis") or [])
        core = [api for api in ("daily", "daily_basic", "stk_limit") if api in missing]
        reasons = set(gap.get("reasons") or [])
        rebuild_snapshot = bool(reasons & {"snapshot_rows_insufficient", "market_fields_incomplete", "point_in_time_invalid"})
        if rebuild_snapshot:
            core = ["daily", "daily_basic", "stk_limit"]
        market_results[trade_date] = sync_market_bundle(trade_date, apis=core) if core else {"ok": True, "skipped": True, "reason": "core_market_already_complete"}
    adj_batch = sync_tushare_date_batch(
        "adj_factor", trade_dates=adj_dates, limit=max(1, len(adj_dates)), require_rows=True,
        minimum_rows=MINIMUM_HARD_API_ROWS, require_point_in_time=True,
    ) if adj_dates else {"ok": True, "skipped": True, "results": []}
    adj_by_date = {str(item.get("trade_date") or ""): item for item in adj_batch.get("results") or [] if isinstance(item, dict)}
    benchmark_batch = (
        _sync_tushare_compatible_range(
            "index_daily",
            {
                "ts_code": BENCHMARK_INDEX_CODE,
                "start_date": min(benchmark_dates),
                "end_date": max(benchmark_dates),
            },
            discovery_dataset="index_daily",
            discovery_date=max(benchmark_dates),
        )
        if benchmark_dates
        else {"ok": True, "skipped": True}
    )
    benchmark_available = (
        _benchmark_date_set(min(benchmark_dates), max(benchmark_dates))
        if benchmark_dates else set()
    )
    benchmark_fallback_results: list[dict[str, Any]] = []
    # A historical OOS repair is not allowed to depend forever on TuShare.
    # The batch request can legitimately return only a subset (or an empty
    # frame) even though an audited equivalent source already publishes the
    # exact daily bar.  Repair every still-missing explicit date through the
    # same bounded, cross-checked multi-source adapter used by the daily path.
    if benchmark_dates:
        benchmark_fallback_results = _repair_benchmark_gaps(
            benchmark_dates, benchmark_available
        )
        benchmark_available = _benchmark_date_set(
            min(benchmark_dates), max(benchmark_dates)
        )
    results = []
    for trade_date in trade_dates:
        adj = adj_by_date.get(trade_date) or ({"ok": bool(adj_batch.get("ok")), "batch": True} if trade_date in adj_dates else {"ok": True, "skipped": True})
        market = market_results[trade_date]
        benchmark = (
            {"ok": trade_date in benchmark_available, "batch": True}
            if trade_date in benchmark_dates
            else {"ok": True, "skipped": True}
        )
        results.append({
            "trade_date": trade_date,
            "ok": bool(market.get("ok")) and bool(adj.get("ok")) and bool(benchmark.get("ok")),
            "market": market,
            "adj_factor": adj,
            "benchmark_index": benchmark,
        })
    return {
        "ok": bool(results) and all(item.get("ok") for item in results),
        "status": "repair_requested",
        "results": results,
        "dates": trade_dates,
        "rejected_dates": rejected,
        "adj_factor_batch": adj_batch,
        "benchmark_batch": benchmark_batch,
        "benchmark_fallback_results": benchmark_fallback_results,
        "contract": CONTRACT_VERSION,
    }
