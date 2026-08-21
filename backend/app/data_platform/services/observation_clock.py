"""Point-in-time observation metadata shared by every data writer.

The market date describes when a value belongs to the market (the economic
clock).  ``observed_at`` describes when ALIS actually received that value (the
observation clock).  Keeping the two clocks separate is essential for replay:
an end-of-day row fetched at 16:00 must never be rewritten as if it existed at
15:00.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


OBSERVATION_CLOCK_SCHEMA_VERSION = "alis_market_observation_clock_v1"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _valid_provenance_sql(column: str) -> str:
    """Return the shared SQL guard for an auditable observation source.

    A blank string (including whitespace) is not provenance.  Keeping this
    predicate at the immutable-reader boundary prevents an old/manual row
    from becoming point-in-time evidence merely because it has a timestamp.
    """

    normalized = f"TRIM(COALESCE({column},''))"
    return (
        f"NULLIF({normalized},'') IS NOT NULL AND "
        f"LOWER({normalized}) NOT LIKE 'legacy_%'"
    )


def _explicit_timestamp_sql(column: str, *, is_postgres: bool) -> str:
    """Require an ISO timestamp with an explicit UTC offset or ``Z`` suffix."""

    normalized = f"TRIM(COALESCE({column},''))"
    nonempty = f"NULLIF({normalized},'') IS NOT NULL"
    if is_postgres:
        # A shape-only regex still lets invalid dates reach ``::timestamptz``
        # and abort an entire audit.  pg_input_is_valid is evaluated before
        # the cast, so one malformed persisted row is simply excluded.
        return (
            f"{nonempty} AND NULLIF({normalized},'') ~ "
            f"'^(19|20)[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}"
            f"(\\.[0-9]+)?(Z|[+-][0-9]{{2}}:[0-9]{{2}})$' "
            f"AND pg_input_is_valid(NULLIF({normalized},''), 'timestamptz')"
        )
    # ``julianday`` verifies parseability.  Looking six characters from the
    # end also accepts ISO strings with fractional seconds before the offset.
    return (
        f"{nonempty} AND "
        f"(SUBSTR({normalized},-1)='Z' OR SUBSTR({normalized},-6,1) IN ('+','-')) AND "
        f"julianday(NULLIF({normalized},'')) IS NOT NULL"
    )


def _timestamp_value_sql(column: str, *, is_postgres: bool) -> str:
    normalized = f"NULLIF(TRIM(COALESCE({column},'')),'')"
    if is_postgres:
        return (
            f"CASE WHEN {normalized} ~ "
            f"'^(19|20)[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}"
            f"(\\.[0-9]+)?(Z|[+-][0-9]{{2}}:[0-9]{{2}})$' "
            f"AND pg_input_is_valid({normalized}, 'timestamptz') "
            f"THEN {normalized}::timestamptz END"
        )
    return f"julianday({normalized})"


def _timestamp_parameter_sql(*, is_postgres: bool) -> str:
    """Return a bind expression comparable with ``_timestamp_value_sql``."""

    return "?::timestamptz" if is_postgres else "julianday(?)"


def _trade_date_cutoff_sql(column: str, *, is_postgres: bool) -> str:
    """Build a safe T-day 15:00 cutoff for a YYYYMMDD trade-date column."""

    value = f"TRIM(COALESCE({column},''))"
    if is_postgres:
        text = (
            f"SUBSTRING({value},1,4)||'-'||SUBSTRING({value},5,2)||'-'"
            f"||SUBSTRING({value},7,2)||'T15:00:00+08:00'"
        )
        return (
            f"CASE WHEN {value} ~ '^[0-9]{{8}}$' "
            f"AND pg_input_is_valid({text}, 'timestamptz') "
            f"THEN ({text})::timestamptz END"
        )
    text = (
        f"SUBSTR({value},1,4)||'-'||SUBSTR({value},5,2)||'-'"
        f"||SUBSTR({value},7,2)||'T15:00:00+08:00'"
    )
    return f"julianday({text})"


def now_observed_at() -> str:
    return datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")


def normalize_timestamp(
    value: Any,
    *,
    fallback: str | None = None,
    require_explicit_offset: bool = False,
) -> str:
    """Normalize a timestamp to an explicit UTC+8 ISO value.

    Date-only values are intentionally not accepted as observation evidence;
    callers must supply a real fetch timestamp or receive the fallback.  The
    observation clock can additionally require an explicit source offset: a
    naive provider string has no trustworthy timezone and must not be allowed
    to backdate when ALIS first saw the response.
    """

    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return str(fallback or "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return str(fallback or "")
    if parsed.tzinfo is None:
        if require_explicit_offset:
            return str(fallback or "")
        parsed = parsed.replace(tzinfo=BUSINESS_TZ)
    return parsed.astimezone(BUSINESS_TZ).isoformat(timespec="seconds")


def normalize_explicit_timestamp(value: Any, *, fallback: str | None = None) -> str:
    """Normalize an explicitly timezone-qualified instant, fail closed on naive input."""

    for candidate in (value, fallback):
        text = str(candidate or "").strip().replace("Z", "+00:00")
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        return parsed.astimezone(BUSINESS_TZ).isoformat(timespec="seconds")
    return ""


def economic_available_time(api_name: str, payload: dict[str, Any], *, fallback: str = "") -> str:
    """Infer the economic clock without claiming an observation happened then."""

    if api_name == "major_news":
        return normalize_explicit_timestamp(payload.get("pub_time"), fallback=fallback)
    if api_name in {
        "income", "balancesheet", "cashflow", "fina_indicator", "fina_audit",
        "forecast", "express", "dividend", "fund_div", "top10_holders",
        "top10_floatholders", "stk_holdernumber", "pledge_detail", "disclosure_date",
        "repurchase", "stk_holdertrade", "report_rc",
    }:
        raw = (
            payload.get("actual_date") or payload.get("f_ann_date")
            or payload.get("ann_date") or payload.get("report_date")
            or payload.get("trade_date")
        )
        compact = str(raw or "").replace("-", "")[:8]
        if len(compact) == 8 and compact.isdigit():
            return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}T23:59:59+08:00"
        return normalize_explicit_timestamp(fallback)
    raw = payload.get("trade_date") or payload.get("date")
    compact = str(raw or "").replace("-", "")[:8]
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}T15:00:00+08:00"
    return normalize_explicit_timestamp(fallback)


def payload_content_hash(
    payload: dict[str, Any], *, source_code: str, economic_time: str,
) -> str:
    """Return a stable hash for one source/version of a logical record."""

    clean = {
        str(key): value for key, value in payload.items()
        if str(key) not in {"observed_at", "first_observed_at", "content_hash"}
    }
    body = json.dumps(
        {"source_code": source_code, "economic_available_time": economic_time, "payload": clean},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def observation_metadata(
    api_name: str,
    payload: dict[str, Any],
    *,
    source_code: str,
    observed_at: str | None = None,
    economic_time: str | None = None,
    provenance: str | None = None,
) -> dict[str, str]:
    observed = normalize_timestamp(
        observed_at,
        fallback=now_observed_at(),
        require_explicit_offset=True,
    )
    economic = normalize_explicit_timestamp(economic_time, fallback="") if economic_time else ""
    if not economic:
        economic = economic_available_time(api_name, payload, fallback="")
    source = str(source_code or "unknown")
    return {
        "economic_available_time": economic,
        "observed_at": observed,
        "first_observed_at": observed,
        "observation_provenance": str(provenance or f"{source}:direct_fetch"),
        "content_hash": payload_content_hash(payload, source_code=source, economic_time=economic),
    }


def append_raw_record_version(c: Any, *, api_name: str, record_key: str, ts_code: str,
                              trade_date: str, period: str, ann_date: str,
                              payload_json: str, source_code: str,
                              metadata: dict[str, str], created_at: str) -> None:
    """Append a raw version idempotently before updating the current projection."""

    created = normalize_explicit_timestamp(created_at, fallback="")
    c.execute(
        """INSERT INTO dc_tushare_raw_record_version
           (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,
            source_code,economic_available_time,observed_at,first_observed_at,
            observation_provenance,content_hash,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(api_name,record_key,content_hash) DO NOTHING""",
        (api_name, record_key, ts_code, trade_date, period, ann_date, payload_json,
         source_code, metadata.get("economic_available_time", ""),
         metadata.get("observed_at", ""), metadata.get("first_observed_at", ""),
         metadata.get("observation_provenance", ""), metadata.get("content_hash", ""), created),
    )


def append_snapshot_version(c: Any, *, ts_code: str, trade_date: str,
                            payload_json: str, source_code: str,
                            metadata: dict[str, str], created_at: str) -> None:
    created = normalize_explicit_timestamp(created_at, fallback="")
    c.execute(
        """INSERT INTO dc_stock_daily_snapshot_version
           (ts_code,trade_date,payload_json,source_code,economic_available_time,
            observed_at,first_observed_at,observation_provenance,content_hash,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ts_code,trade_date,content_hash) DO NOTHING""",
        (ts_code, trade_date, payload_json, source_code,
         metadata.get("economic_available_time", ""), metadata.get("observed_at", ""),
         metadata.get("first_observed_at", ""), metadata.get("observation_provenance", ""),
         metadata.get("content_hash", ""), created),
    )


def load_raw_record_versions(
    c: Any,
    api_name: str,
    *,
    cutoff: str = "",
    cutoff_by_trade_date: bool = False,
    observed_start: str = "",
    ts_codes: list[str] | tuple[str, ...] | None = None,
    trade_date_start: str = "",
    trade_date_end: str = "",
    period_limit: int = 0,
    limit: int = 0,
) -> list[dict[str, Any]] | None:
    """Read the latest immutable raw observations visible at ``cutoff``.

    With ``cutoff_by_trade_date=True``, each row is instead limited by that
    row's own trading-session close (15:00 Asia/Shanghai) before version
    ranking.  This is for historical training/replay, where a single current
    cutoff would otherwise admit a later repair for an earlier date.

    ``None`` means the observation-clock migration is unavailable.  An empty
    list is a valid, strict result: the migration exists but no eligible
    observation was available.  Callers may only fall back to the mutable
    projection for isolated, non-PostgreSQL legacy fixtures; production must
    treat ``None`` as missing evidence.

    ``period_limit`` optionally keeps only the newest N distinct ``period``
    values per ``ts_code``.  The bound is applied *after* the accepted
    projection hash and all point-in-time clocks have been checked, and one
    latest immutable version is retained for each code/period.  This is used
    by wide financial readers that need recent reporting periods rather than
    the complete append-only archive; the default ``0`` preserves the full
    logical result.
    """
    columns = _table_columns(c, "dc_tushare_raw_record_version")
    required = {
        "api_name", "record_key", "ts_code", "trade_date", "period",
        "ann_date", "payload_json", "source_code", "observed_at",
        "first_observed_at", "economic_available_time", "observation_provenance",
        "content_hash", "created_at", "id",
    }
    if not required.issubset(columns):
        return None
    # The mutable projection is the accepted winner for each logical key.  A
    # publication race can append a slower provider after the first provider
    # has already populated that projection; ranking immutable versions only
    # by ``observed_at`` would then silently replace the selected source.  Use
    # the projection hash as the first ranking key when the migrated column is
    # present.  Explicit callers that use ``replace=True`` update the
    # projection hash and therefore still make an intentional revision the
    # visible version.  If a small isolated fixture has no projection table,
    # retain the historical latest-observation behaviour.
    projection_columns = _table_columns(c, "dc_tushare_raw_records")
    projection_winner_sql = "0"
    projection_filter_sql = ""
    accepted_trade_date_join = ""
    if {"api_name", "record_key", "content_hash"}.issubset(projection_columns):
        projection_winner_sql = "1"
        # Do not fall back to another immutable version when the accepted
        # projection is outside a requested PIT cutoff.  That is a real data
        # contract gap and must remain empty/blocked instead of being hidden
        # by an older or slower source.
        projection_filter_sql = ""
        if "trade_date" in projection_columns and "trade_date" in columns:
            # A repaired projection must remain tied to the immutable
            # version's economic session.  Without this equality, a changed
            # projection date could select an old version with the same
            # logical key/hash and silently move evidence across sessions.
            accepted_trade_date_join = " AND p.trade_date=v.trade_date"
        # Join the accepted projection once below. The old correlated EXISTS
        # was evaluated for every immutable version and made daily planning
        # scan the full raw-history table.
    normalized_cutoff = normalize_explicit_timestamp(cutoff) if cutoff else ""
    if cutoff and not normalized_cutoff:
        return []
    normalized_observed_start = normalize_explicit_timestamp(observed_start) if observed_start else ""
    if observed_start and not normalized_observed_start:
        return []
    is_postgres = bool(getattr(c, "_is_postgres", False))
    timestamp_parameter = _timestamp_parameter_sql(is_postgres=is_postgres)
    timestamp_cast = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
    first_observed_cast = _timestamp_value_sql("v.first_observed_at", is_postgres=is_postgres)
    economic_available_cast = _timestamp_value_sql("v.economic_available_time", is_postgres=is_postgres)
    created_cast = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
    predicates = [
        "v.api_name=?", "v.content_hash<>''",
        "NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL",
        _valid_provenance_sql("v.observation_provenance"),
        _explicit_timestamp_sql("v.observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.first_observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.economic_available_time", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.created_at", is_postgres=is_postgres),
    ]
    if projection_filter_sql:
        predicates.append(projection_filter_sql)
    params: list[Any] = [api_name]
    if normalized_cutoff:
        predicates.append(f"{timestamp_cast}<={timestamp_parameter}")
        predicates.append(f"{first_observed_cast}<={timestamp_parameter}")
        predicates.append(f"{economic_available_cast}<={timestamp_parameter}")
        predicates.append(f"{created_cast}<={timestamp_parameter}")
        params.append(normalized_cutoff)
        params.append(normalized_cutoff)
        params.append(normalized_cutoff)
        params.append(normalized_cutoff)
    elif cutoff_by_trade_date:
        cutoff_expression = _trade_date_cutoff_sql("v.trade_date", is_postgres=is_postgres)
        predicates.append(f"{cutoff_expression} IS NOT NULL")
        predicates.append(f"{timestamp_cast}<={cutoff_expression}")
        predicates.append(f"{first_observed_cast}<={cutoff_expression}")
        predicates.append(f"{economic_available_cast}<={cutoff_expression}")
        predicates.append(f"{created_cast}<={cutoff_expression}")
    if normalized_observed_start:
        predicates.append(f"{timestamp_cast}>={timestamp_parameter}")
        params.append(normalized_observed_start)
    if ts_codes:
        values = [str(value) for value in ts_codes if str(value)]
        if values:
            placeholders = ",".join("?" for _ in values)
            predicates.append(f"v.ts_code IN ({placeholders})")
            params.extend(values)
    if trade_date_start:
        predicates.append("v.trade_date>=?")
        params.append(str(trade_date_start))
    if trade_date_end:
        predicates.append("v.trade_date<=?")
        params.append(str(trade_date_end))
    bounded_periods = max(1, int(period_limit)) if period_limit else 0
    source_sql = "dc_tushare_raw_record_version v"
    if {"api_name", "record_key", "content_hash"}.issubset(projection_columns):
        source_sql = (
            "dc_tushare_raw_record_version v JOIN dc_tushare_raw_records p "
            "ON p.api_name=v.api_name AND p.record_key=v.record_key AND p.content_hash=v.content_hash"
            f"{accepted_trade_date_join}"
        )
    selected_columns = (
        "id,api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,"
        "source_code,economic_available_time,observed_at,first_observed_at,"
        "observation_provenance,content_hash,created_at"
    )
    qualified_columns = (
        "v.id,v.api_name,v.record_key,v.ts_code,v.trade_date,v.period,v.ann_date,"
        "v.payload_json,v.source_code,v.economic_available_time,v.observed_at,"
        "v.first_observed_at,v.observation_provenance,v.content_hash,v.created_at"
    )
    if bounded_periods:
        # The accepted-hash join above is already unique per logical record
        # in production.  The compatibility branch first applies the same
        # latest-version rule as the unbounded reader, then both branches
        # select one latest version for each code/period and the newest N
        # periods for each code.  Ranking occurs only after all PIT predicates
        # have filtered the source, so a late accepted revision cannot leak
        # into a historical cutoff or force a fallback to an older hash.
        observed_rank = _timestamp_value_sql("visible.observed_at", is_postgres=is_postgres)
        created_rank = _timestamp_value_sql("visible.created_at", is_postgres=is_postgres)
        period_rank = "NULLIF(TRIM(COALESCE(visible.period,'')),'')"
        if {"api_name", "record_key", "content_hash"}.issubset(projection_columns):
            visible_cte = f"""visible AS (
                SELECT {qualified_columns}
                  FROM {source_sql}
                 WHERE {' AND '.join(predicates)}
            )"""
        else:
            visible_cte = f"""visible_versions AS (
                SELECT {qualified_columns},
                       ROW_NUMBER() OVER (
                           PARTITION BY v.api_name,v.record_key
                           ORDER BY {timestamp_cast} DESC,{created_cast} DESC,v.id DESC
                       ) AS logical_rank
                  FROM {source_sql}
                 WHERE {' AND '.join(predicates)}
            ), visible AS (
                SELECT {selected_columns}
                  FROM visible_versions
                 WHERE logical_rank=1
            )"""
        sql = f"""WITH {visible_cte}, period_ranked AS (
                SELECT visible.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY visible.ts_code,{period_rank}
                           ORDER BY {observed_rank} DESC,{created_rank} DESC,visible.id DESC
                       ) AS version_rank,
                       DENSE_RANK() OVER (
                           PARTITION BY visible.ts_code
                           ORDER BY {period_rank} DESC
                       ) AS period_rank
                  FROM visible
             )
             SELECT {selected_columns}
               FROM period_ranked
              WHERE version_rank=1 AND period_rank<=?
              ORDER BY ts_code,period DESC,observed_at,id"""
        params.append(bounded_periods)
    elif {"api_name", "record_key", "content_hash"}.issubset(projection_columns):
        # The accepted projection has one row per (api_name, record_key), and
        # the immutable table is unique on (api_name, record_key,
        # content_hash).  Their exact-hash join therefore returns at most one
        # immutable winner per logical key.  Ranking that already-unique set
        # used to sort years of full-market financial observations during
        # daily planning and could exceed PostgreSQL's statement timeout.
        # Keep all four PIT clock predicates above: an accepted winner first
        # observed after the cutoff must remain invisible rather than falling
        # back to an older immutable version.
        sql = f"""SELECT v.id,v.api_name,v.record_key,v.ts_code,v.trade_date,
                        v.period,v.ann_date,v.payload_json,v.source_code,
                        v.economic_available_time,v.observed_at,
                        v.first_observed_at,v.observation_provenance,v.content_hash,v.created_at
                   FROM {source_sql}
                  WHERE {' AND '.join(predicates)}
                  ORDER BY v.observed_at,v.id"""
    else:
        sql = f"""WITH ranked AS (
                 SELECT v.id,v.api_name,v.record_key,v.ts_code,v.trade_date,
                        v.period,v.ann_date,v.payload_json,v.source_code,
                        v.economic_available_time,v.observed_at,
                        v.first_observed_at,v.observation_provenance,v.content_hash,v.created_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY v.api_name,v.record_key
                            ORDER BY {projection_winner_sql} DESC,
                                     {timestamp_cast} DESC,{created_cast} DESC,v.id DESC
                        ) AS row_rank
                   FROM {source_sql}
                  WHERE {' AND '.join(predicates)}
             )
             SELECT id,api_name,record_key,ts_code,trade_date,period,ann_date,
                    payload_json,source_code,economic_available_time,observed_at,
                    first_observed_at,observation_provenance,content_hash,created_at
              FROM ranked
              WHERE row_rank=1
              ORDER BY observed_at,id"""
    if limit:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    return [dict(row) for row in c.execute(sql, tuple(params)).fetchall()]


def load_current_raw_record_versions(
    c: Any,
    api_name: str,
    trade_date: str,
) -> list[dict[str, Any]] | None:
    """Load an accepted current projection joined to its immutable version.

    Daily snapshot materialization already has an exact accepted projection for
    the requested session.  Re-running the general PIT reader there performs a
    window rank and correlated projection checks for every market API.  This
    direct join retains the immutable-evidence requirement while letting the
    database use the projection and version keys directly.

    There is deliberately no cutoff argument: historical replay and OOS keep
    using ``load_raw_record_versions`` and their per-date PIT clocks.
    """
    projection_columns = _table_columns(c, "dc_tushare_raw_records")
    version_columns = _table_columns(c, "dc_tushare_raw_record_version")
    required_projection = {"api_name", "record_key", "trade_date", "content_hash"}
    required_version = {
        "api_name", "record_key", "ts_code", "trade_date", "period",
        "ann_date", "payload_json", "source_code", "economic_available_time",
        "observed_at", "first_observed_at", "observation_provenance",
        "content_hash", "created_at", "id",
    }
    if not required_projection.issubset(projection_columns) or not required_version.issubset(version_columns):
        return None
    is_postgres = bool(getattr(c, "_is_postgres", False))
    predicates = [
        "p.api_name=?",
        "p.trade_date=?",
        "NULLIF(TRIM(COALESCE(p.content_hash,'')),'') IS NOT NULL",
        "NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL",
        _valid_provenance_sql("v.observation_provenance"),
        _explicit_timestamp_sql("v.observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.first_observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.economic_available_time", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.created_at", is_postgres=is_postgres),
    ]
    sql = f"""
        SELECT v.id,v.api_name,v.record_key,v.ts_code,v.trade_date,
               v.period,v.ann_date,v.payload_json,v.source_code,
               v.economic_available_time,v.observed_at,v.first_observed_at,
               v.observation_provenance,v.content_hash,v.created_at
        FROM dc_tushare_raw_records p
        JOIN dc_tushare_raw_record_version v
          ON v.api_name=p.api_name
         AND v.record_key=p.record_key
         AND v.content_hash=p.content_hash
         AND v.trade_date=p.trade_date
         WHERE {' AND '.join(predicates)}
         ORDER BY v.observed_at,v.id
    """
    return [dict(row) for row in c.execute(sql, (str(api_name), str(trade_date))).fetchall()]


def load_snapshot_versions(
    c: Any,
    *,
    cutoff: str = "",
    cutoff_by_trade_date: bool = False,
    ts_codes: list[str] | tuple[str, ...] | None = None,
    trade_date_start: str = "",
    trade_date_end: str = "",
    trade_date: str = "",
    per_ts_code_limit: int = 0,
    limit: int = 0,
) -> list[dict[str, Any]] | None:
    """Read latest immutable normalized snapshot rows under the PIT clock.

    ``per_ts_code_limit`` bounds a historical series independently for each
    security.  A global ``LIMIT`` is not sufficient for a batch valuation: one
    dense symbol can consume the result set and starve the remaining holdings.
    The per-code window is applied after the accepted projection hash and all
    point-in-time predicates, so it cannot select an unaccepted revision.
    """
    columns = _table_columns(c, "dc_stock_daily_snapshot_version")
    required = {
        "id", "ts_code", "trade_date", "payload_json", "source_code",
        "economic_available_time", "observed_at", "first_observed_at",
        "observation_provenance", "content_hash", "created_at",
    }
    if not required.issubset(columns):
        return None
    normalized_cutoff = normalize_explicit_timestamp(cutoff) if cutoff else ""
    if cutoff and not normalized_cutoff:
        return []
    is_postgres = bool(getattr(c, "_is_postgres", False))
    timestamp_parameter = _timestamp_parameter_sql(is_postgres=is_postgres)
    timestamp_cast = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
    first_observed_cast = _timestamp_value_sql("v.first_observed_at", is_postgres=is_postgres)
    economic_available_cast = _timestamp_value_sql("v.economic_available_time", is_postgres=is_postgres)
    created_cast = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
    projection_columns = _table_columns(c, "dc_stock_daily_snapshot")
    has_accepted_projection = {
        "ts_code", "trade_date", "content_hash",
    }.issubset(projection_columns)
    predicates = [
        "v.content_hash<>''",
        "NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL",
        _valid_provenance_sql("v.observation_provenance"),
        _explicit_timestamp_sql("v.observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.first_observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.economic_available_time", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.created_at", is_postgres=is_postgres),
    ]
    params: list[Any] = []
    if normalized_cutoff:
        predicates.append(f"{timestamp_cast}<={timestamp_parameter}")
        predicates.append(f"{first_observed_cast}<={timestamp_parameter}")
        predicates.append(f"{economic_available_cast}<={timestamp_parameter}")
        predicates.append(f"{created_cast}<={timestamp_parameter}")
        params.append(normalized_cutoff)
        params.append(normalized_cutoff)
        params.append(normalized_cutoff)
        params.append(normalized_cutoff)
    elif cutoff_by_trade_date:
        cutoff_expression = _trade_date_cutoff_sql("v.trade_date", is_postgres=is_postgres)
        predicates.append(f"{timestamp_cast} <= {cutoff_expression}")
        predicates.append(f"{first_observed_cast} <= {cutoff_expression}")
        predicates.append(f"{economic_available_cast} <= {cutoff_expression}")
        predicates.append(f"{created_cast} <= {cutoff_expression}")
    if ts_codes:
        values = [str(value) for value in ts_codes if str(value)]
        if values:
            placeholders = ",".join("?" for _ in values)
            predicates.append(f"v.ts_code IN ({placeholders})")
            params.extend(values)
    if trade_date:
        predicates.append("v.trade_date=?")
        params.append(str(trade_date))
    if trade_date_start:
        predicates.append("v.trade_date>=?")
        params.append(str(trade_date_start))
    if trade_date_end:
        predicates.append("v.trade_date<=?")
        params.append(str(trade_date_end))
    per_code_limit = max(1, int(per_ts_code_limit)) if per_ts_code_limit else 0
    selected_columns = (
        "id,ts_code,trade_date,payload_json,source_code,economic_available_time,"
        "observed_at,first_observed_at,observation_provenance,content_hash,created_at"
    )
    qualified_columns = (
        "v.id,v.ts_code,v.trade_date,v.payload_json,v.source_code,"
        "v.economic_available_time,v.observed_at,v.first_observed_at,"
        "v.observation_provenance,v.content_hash,v.created_at"
    )
    if has_accepted_projection:
        # The accepted projection has one row per (ts_code, trade_date), and
        # append-only snapshots are unique on that key plus content_hash.
        # An exact hash join is therefore already one row per logical bar.
        # Do not rank or correlate against the 7 GB projection: if its chosen
        # hash arrived after the caller's PIT cutoff, returning no row is the
        # required fail-closed result.
        source_sql = f"""SELECT {qualified_columns}
                              FROM dc_stock_daily_snapshot accepted
                              JOIN dc_stock_daily_snapshot_version v
                                ON v.ts_code=accepted.ts_code
                               AND v.trade_date=accepted.trade_date
                               AND v.content_hash=accepted.content_hash
                             WHERE {' AND '.join(predicates)}"""
        if per_code_limit:
            sql = f"""WITH visible AS ({source_sql}), ranked AS (
                           SELECT visible.*,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY visible.ts_code
                                      ORDER BY visible.trade_date DESC,
                                               {_timestamp_value_sql('visible.observed_at', is_postgres=is_postgres)} DESC,
                                               visible.id DESC
                                  ) AS code_rank
                             FROM visible
                        )
                        SELECT {selected_columns}
                          FROM ranked
                         WHERE code_rank<=?
                         ORDER BY trade_date,ts_code,observed_at,id"""
            params.append(per_code_limit)
        else:
            sql = f"""SELECT {selected_columns}
                              FROM ({source_sql}) visible
                             ORDER BY trade_date,ts_code,observed_at,id"""
    else:
        sql = f"""WITH logical AS (
                     SELECT {qualified_columns},
                            ROW_NUMBER() OVER (
                                PARTITION BY v.ts_code,v.trade_date
                                ORDER BY {timestamp_cast} DESC,{created_cast} DESC,v.id DESC
                            ) AS row_rank
                       FROM dc_stock_daily_snapshot_version v
                      WHERE {' AND '.join(predicates)}
                 ), visible AS (
                     SELECT {selected_columns}
                       FROM logical
                      WHERE row_rank=1
                 )"""
        if per_code_limit:
            sql += f""", ranked AS (
                     SELECT visible.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY visible.ts_code
                                ORDER BY visible.trade_date DESC,
                                         {_timestamp_value_sql('visible.observed_at', is_postgres=is_postgres)} DESC,
                                         visible.id DESC
                            ) AS code_rank
                       FROM visible
                 )
                 SELECT {selected_columns}
                   FROM ranked
                  WHERE code_rank<=?
                  ORDER BY trade_date,ts_code,observed_at,id"""
            params.append(per_code_limit)
        else:
            sql += f"""
                 SELECT {selected_columns}
                   FROM visible
                  ORDER BY trade_date,ts_code,observed_at,id"""
    if limit:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    return [dict(row) for row in c.execute(sql, tuple(params)).fetchall()]


def load_snapshot_market_aggregates(
    c: Any,
    *,
    trade_dates: list[str] | tuple[str, ...],
    observation_cutoff: str | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Aggregate immutable snapshots at each row's own T-day close.

    This is deliberately separate from the mutable ``dc_stock_daily_snapshot``
    projection.  Formal OOS attribution needs broad-market aggregates, but a
    repair received after an historical close must not rewrite the observed
    market state for that session.  ``None`` means the append-only schema is
    unavailable; an empty mapping is a valid strict result with no visible
    rows.
    """
    compact_dates = sorted({
        str(value or "").replace("-", "")[:8]
        for value in trade_dates
        if len(str(value or "").replace("-", "")[:8]) == 8
        and str(value or "").replace("-", "")[:8].isdigit()
    })
    if not compact_dates:
        return {}
    columns = _table_columns(c, "dc_stock_daily_snapshot_version")
    required = {
        "id", "ts_code", "trade_date", "payload_json", "source_code",
        "economic_available_time", "observed_at", "first_observed_at",
        "observation_provenance", "content_hash", "created_at",
    }
    if not required.issubset(columns):
        return None

    is_postgres = bool(getattr(c, "_is_postgres", False))
    observed_cast = _timestamp_value_sql("v.observed_at", is_postgres=is_postgres)
    created_cast = _timestamp_value_sql("v.created_at", is_postgres=is_postgres)
    cutoff = _trade_date_cutoff_sql("v.trade_date", is_postgres=is_postgres)
    observed_cutoff = normalize_explicit_timestamp(observation_cutoff) if observation_cutoff else ""
    if observation_cutoff and not observed_cutoff:
        return {}
    observed_limit_sql = (
        _timestamp_parameter_sql(is_postgres=is_postgres)
        if observed_cutoff
        else cutoff
    )
    # Market-state aggregates are a consumer of the same normalized snapshot
    # used by selection.  Restrict them to the accepted projection hash so a
    # slower provider cannot create a different market regime than the one
    # whose bars funded the decision.  If a later intentional projection
    # revision is not visible at the historical close, the row stays absent
    # and the formal PIT contract blocks rather than using an older provider.
    projection_columns = _table_columns(c, "dc_stock_daily_snapshot")
    has_accepted_projection = {
        "ts_code", "trade_date", "content_hash",
    }.issubset(projection_columns)
    predicates = [
        "v.content_hash<>''",
        "NULLIF(TRIM(COALESCE(v.source_code,'')),'') IS NOT NULL",
        _valid_provenance_sql("v.observation_provenance"),
        _explicit_timestamp_sql("v.observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.first_observed_at", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.economic_available_time", is_postgres=is_postgres),
        _explicit_timestamp_sql("v.created_at", is_postgres=is_postgres),
        f"{observed_cast}<={observed_limit_sql}",
        f"{_timestamp_value_sql('v.first_observed_at', is_postgres=is_postgres)}<={observed_limit_sql}",
        f"{_timestamp_value_sql('v.economic_available_time', is_postgres=is_postgres)}<={cutoff}",
        f"{created_cast}<={observed_limit_sql}",
    ]
    placeholders = ",".join("?" for _ in compact_dates)
    predicates.append(f"v.trade_date IN ({placeholders})")

    if is_postgres:
        payload = (
            "CASE WHEN pg_input_is_valid(v.payload_json, 'jsonb') "
            "THEN v.payload_json::jsonb END"
        )
        change_pct = (
            "CASE WHEN jsonb_typeof(payload->'change_pct')='number' "
            "THEN (payload->>'change_pct')::double precision END"
        )
        turnover = (
            "CASE WHEN jsonb_typeof(payload->'turnover')='number' "
            "THEN (payload->>'turnover')::double precision END"
        )
    else:
        payload = "CASE WHEN json_valid(v.payload_json) THEN v.payload_json END"
        change_pct = (
            "CASE WHEN json_type(payload,'$.change_pct') IN ('integer','real') "
            "THEN CAST(json_extract(payload,'$.change_pct') AS REAL) END"
        )
        turnover = (
            "CASE WHEN json_type(payload,'$.turnover') IN ('integer','real') "
            "THEN CAST(json_extract(payload,'$.turnover') AS REAL) END"
        )

    if has_accepted_projection:
        # The accepted hash is authoritative for each date/code.  Joining it
        # directly avoids sorting every immutable version and keeps late
        # repairs invisible when they are not the accepted projection.
        visible_sql = f"""visible AS (
                         SELECT v.ts_code,v.trade_date,v.payload_json
                           FROM dc_stock_daily_snapshot accepted
                           JOIN dc_stock_daily_snapshot_version v
                             ON v.ts_code=accepted.ts_code
                            AND v.trade_date=accepted.trade_date
                            AND v.content_hash=accepted.content_hash
                          WHERE {' AND '.join(predicates)}
                     ), decoded AS (
        """
    else:
        visible_sql = f"""ranked AS (
                         SELECT v.ts_code,v.trade_date,v.payload_json,v.observed_at,v.created_at,v.id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY v.ts_code,v.trade_date
                                    ORDER BY {observed_cast} DESC,{created_cast} DESC,v.id DESC
                                ) AS row_rank
                           FROM dc_stock_daily_snapshot_version v
                          WHERE {' AND '.join(predicates)}
                     ), visible AS (
                         SELECT ts_code,trade_date,payload_json
                           FROM ranked WHERE row_rank=1
                     ), decoded AS (
        """
    sql = f"""WITH {visible_sql}
                     SELECT ts_code,trade_date,{payload} AS payload
                       FROM visible v
                 ), numeric_values AS (
                     SELECT ts_code,trade_date,{change_pct} AS change_pct,{turnover} AS turnover
                       FROM decoded
                 )
                 SELECT trade_date,
                        COUNT(*) AS rows_total,
                        SUM(CASE WHEN change_pct IS NOT NULL AND turnover IS NOT NULL THEN 1 ELSE 0 END) AS market_rows,
                        AVG(CASE WHEN change_pct IS NOT NULL AND turnover IS NOT NULL
                                 THEN CASE WHEN change_pct>0 THEN 1.0 ELSE 0.0 END END) AS breadth,
                        AVG(CASE WHEN change_pct IS NOT NULL AND turnover IS NOT NULL
                                 THEN change_pct/100.0 END) AS average_return,
                        SUM(CASE WHEN change_pct IS NOT NULL AND turnover IS NOT NULL
                                 THEN turnover ELSE 0.0 END) AS amount,
                        AVG(CASE WHEN change_pct IS NOT NULL AND turnover IS NOT NULL
                                 THEN CASE WHEN change_pct>=9.7 THEN 1.0 ELSE 0.0 END END) AS limit_up_ratio,
                        AVG(CASE WHEN change_pct IS NOT NULL AND turnover IS NOT NULL
                                 THEN CASE WHEN change_pct<=-9.7 THEN 1.0 ELSE 0.0 END END) AS limit_down_ratio
                   FROM numeric_values
                  GROUP BY trade_date
                  ORDER BY trade_date"""
    params: list[Any] = []
    if observed_cutoff:
        params.extend([observed_cutoff] * 3)
    params.extend(compact_dates)
    rows = c.execute(sql, tuple(params)).fetchall()
    return {
        str(dict(row).get("trade_date") or ""): dict(row)
        for row in rows
        if str(dict(row).get("trade_date") or "")
    }


def _table_columns(c: Any, table: str) -> set[str]:
    try:
        if bool(getattr(c, "_is_postgres", False)):
            rows = c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=?",
                (table,),
            ).fetchall()
            return {str(dict(row).get("column_name") or "") for row in rows}
        rows = c.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(dict(row).get("name") or "") for row in rows}
    except Exception:
        return set()


def upsert_raw_record(
    c: Any,
    *,
    api_name: str,
    record_key: str,
    ts_code: str,
    trade_date: str,
    period: str = "",
    ann_date: str = "",
    payload: dict[str, Any],
    source_code: str,
    created_at: str | None = None,
    observed_at: str | None = None,
    economic_time: str | None = None,
    provenance: str | None = None,
    replace: bool = False,
) -> int:
    """Write a raw record and its immutable observation evidence.

    This adapter is intentionally usable by fallback modules and old isolated
    fixtures.  On a pre-0011 table it writes only the legacy projection; such
    rows remain `legacy_unknown`/ineligible in strict production gates.
    """

    now = normalize_explicit_timestamp(created_at, fallback=now_observed_at())
    metadata = observation_metadata(
        api_name, payload, source_code=source_code, observed_at=observed_at or now,
        economic_time=economic_time, provenance=provenance,
    )
    payload_with_meta = dict(payload)
    payload_with_meta.update({key: value for key, value in metadata.items() if key != "content_hash"})
    payload_with_meta["content_hash"] = metadata["content_hash"]
    payload_json = json.dumps(payload_with_meta, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    columns = _table_columns(c, "dc_tushare_raw_records")
    has_clock = {"economic_available_time", "observed_at", "first_observed_at", "observation_provenance", "content_hash"}.issubset(columns)
    if has_clock:
        try:
            append_raw_record_version(
                c, api_name=api_name, record_key=record_key, ts_code=ts_code, trade_date=trade_date,
                period=period, ann_date=ann_date, payload_json=payload_json, source_code=source_code,
                metadata=metadata, created_at=now,
            )
        except Exception:
            if bool(getattr(c, "_is_postgres", False)):
                raise
        values = (api_name, record_key, ts_code, trade_date, period, ann_date, payload_json,
                  source_code, metadata["observed_at"], metadata["economic_available_time"],
                  metadata["observed_at"], metadata["first_observed_at"], metadata["observation_provenance"],
                  metadata["content_hash"], now)
        if replace:
            sql = """INSERT INTO dc_tushare_raw_records
                (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,source_code,available_time,
                 economic_available_time,observed_at,first_observed_at,observation_provenance,content_hash,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(api_name,record_key) DO UPDATE SET
                 ts_code=excluded.ts_code,trade_date=excluded.trade_date,period=excluded.period,ann_date=excluded.ann_date,
                 payload_json=excluded.payload_json,source_code=excluded.source_code,available_time=excluded.available_time,
                 economic_available_time=excluded.economic_available_time,observed_at=excluded.observed_at,
                 first_observed_at=CASE WHEN dc_tushare_raw_records.first_observed_at='' THEN excluded.first_observed_at ELSE dc_tushare_raw_records.first_observed_at END,
                 observation_provenance=excluded.observation_provenance,content_hash=excluded.content_hash,updated_at=excluded.updated_at"""
        else:
            sql = """INSERT INTO dc_tushare_raw_records
                (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,source_code,available_time,
                 economic_available_time,observed_at,first_observed_at,observation_provenance,content_hash,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(api_name,record_key) DO NOTHING"""
    else:
        values = (api_name, record_key, ts_code, trade_date, period, ann_date, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                  source_code, metadata["observed_at"], now)
        if replace:
            sql = """INSERT INTO dc_tushare_raw_records
                (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,source_code,available_time,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(api_name,record_key) DO UPDATE SET
                payload_json=excluded.payload_json,source_code=excluded.source_code,available_time=excluded.available_time,updated_at=excluded.updated_at"""
        else:
            sql = """INSERT INTO dc_tushare_raw_records
                (api_name,record_key,ts_code,trade_date,period,ann_date,payload_json,source_code,available_time,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(api_name,record_key) DO NOTHING"""
    cursor = c.execute(sql, values)
    return max(0, int(getattr(cursor, "rowcount", 0) or 0))
