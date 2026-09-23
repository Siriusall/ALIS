from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from app.core.a_share_fees import is_exchange_etf
from app.data_platform import db as _data_platform_db
from app.data_platform.db import _conn, _is_postgres, _read_conn
from app.data_platform.services.observation_clock import (
    _explicit_timestamp_sql,
    _table_columns,
    _timestamp_parameter_sql,
    _timestamp_value_sql,
)
from app.services.compact_feature_store import load_snapshot_bundle_from_local_cache
from app.services.market_dataset import configured_dataset
from app.services.point_in_time import (
    is_available_at,
    normalize_explicit_timestamp,
)

logger = logging.getLogger("quant.backtest_data_source")


@contextmanager
def _read_data_conn():
    """Use autocommit reads on PostgreSQL without breaking SQLite fixtures."""

    # Backtest/replay data reads are never part of an enclosing data-platform
    # write transaction.  Production therefore uses an autocommit, read-only
    # session so payload decoding cannot leave an ``idle in transaction``
    # backend open.  Existing isolated SQLite fixtures keep their injected
    # ``_conn`` context unchanged.
    # Tests deliberately replace this module's ``_conn`` with an in-memory
    # fixture.  Preserve that injection rather than opening a real PostgreSQL
    # connection merely because the developer environment has a production
    # URL configured.
    use_production_reader = _is_postgres() and _conn is _data_platform_db._conn
    opener = _read_conn if use_production_reader else _conn
    with opener() as conn:
        yield conn


@dataclass
class SnapshotRow:
    symbol: str
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    prev_close: float
    change_pct: float
    volume: float
    turnover: float
    name: str = ""
    adj_factor: float = 1.0


@dataclass
class HistBar:
    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


class BacktestDataSource:
    def __init__(self) -> None:
        self.market_dataset = configured_dataset()
        self._hist_cache: Dict[str, List[HistBar]] = {}
        self._hist_cache_through: Dict[str, str] = {}
        self._history_load_failed: set[str] = set()
        self._snapshot_cache: Dict[str, List[SnapshotRow]] = {}
        self._daily_basic_cache: Dict[str, Dict[str, Dict[str, float]]] = {}
        self._versioned_snapshot_cache: Dict[tuple, List[dict]] = {}
        self.data_warnings: List[str] = []
        self.fallback_events: List[Dict[str, str]] = []
        self._as_of_time: str | None = None
        self._pit_sources: Dict[str, Dict[str, object]] = {}
        # Replay walks forward chronologically.  Anchoring each security to the
        # first factor seen keeps prices continuous without using a future
        # end-of-window factor (which would leak information into lot sizing).
        self._replay_adjustment_bases: Dict[str, float] = {}

    def set_as_of_time(self, cutoff: str) -> None:
        cutoff = normalize_explicit_timestamp(cutoff) if cutoff else ""
        if cutoff == self._as_of_time:
            return
        # A forward replay only gains one newly visible session at each cutoff.
        # Keep earlier bars: they were read under an earlier (therefore stricter)
        # cutoff. A backward or unrestricted switch must discard them to avoid
        # carrying data observed later into an earlier point-in-time replay.
        if not cutoff or not self._as_of_time or not is_available_at(self._as_of_time, cutoff):
            self._hist_cache.clear()
            self._hist_cache_through.clear()
            self._history_load_failed.clear()
        self._as_of_time = cutoff or None
        self._snapshot_cache.clear()
        self._daily_basic_cache.clear()
        self._versioned_snapshot_cache.clear()
        self._pit_sources.clear()

    def _record_pit_read(self, source: str, rows: List[dict]) -> None:
        available: list[str] = []
        invalid_observation = 0
        missing_clock = 0
        future_clock = 0
        clock_fields = ("observed_at", "first_observed_at", "economic_available_time", "created_at")
        for row in rows:
            raw_values = [row.get(field) for field in clock_fields]
            if not any(raw_values) and row.get("available_time"):
                # Compatibility rows are used only by isolated local fixtures
                # and non-formal CSV paths.  Formal PostgreSQL readers always
                # return all four clocks from the immutable version table.
                raw_values = [row.get("available_time")] * len(clock_fields)
            if any(not str(value or "").strip() for value in raw_values):
                missing_clock += 1
                continue
            normalized_values = [normalize_explicit_timestamp(value) for value in raw_values]
            if any(not value for value in normalized_values):
                invalid_observation += 1
                continue
            available.extend(normalized_values)
            if self._as_of_time and any(
                not is_available_at(value, str(self._as_of_time), require_explicit_offset=True)
                for value in normalized_values
            ):
                future_clock += 1
        available = [value for value in available if value]
        current = self._pit_sources.get(source, {})
        current_max = str(current.get("max_available_time") or "")
        observed_max = max(available) if available else ""
        passed = (
            bool(rows)
            and bool(self._as_of_time)
            and all(row.get("observed_at") or row.get("available_time") for row in rows)
            and all(
                is_available_at(value, str(self._as_of_time), require_explicit_offset=True)
                for value in available
            )
        )
        # All-market history is read in batches. Preserve the full audit
        # instead of letting a later, possibly empty, batch erase prior reads.
        self._pit_sources[source] = {
            "source": source,
            "cutoff": self._as_of_time,
            "read_count": int(current.get("read_count") or 0) + len(rows),
            "batch_count": int(current.get("batch_count") or 0) + 1,
            "future_excluded_count": int(current.get("future_excluded_count") or 0) + future_clock,
            "missing_available_time_count": (
                int(current.get("missing_available_time_count") or 0)
                + missing_clock
            ),
            "invalid_available_time_count": (
                int(current.get("invalid_available_time_count") or 0)
                + invalid_observation
            ),
            "max_available_time": max(
                (value for value in (current_max, observed_max) if value),
                key=lambda value: normalize_explicit_timestamp(value) or value,
                default="",
            ) or None,
            "sql_predicate": "all_observation_clocks <= decision_cutoff" if self._as_of_time else "disabled",
            "passed": (
                bool(current.get("passed", True))
                and passed
                and invalid_observation == 0
                and missing_clock == 0
                and future_clock == 0
            ),
        }

    def point_in_time_sources(self) -> List[Dict[str, object]]:
        return list(self._pit_sources.values())

    def _record_pit_failure(self, source: str, reason: str) -> None:
        """Record an unavailable PIT source as a hard replay failure."""
        current = self._pit_sources.get(source, {})
        self._pit_sources[source] = {
            **current,
            "source": source,
            "cutoff": self._as_of_time,
            "read_count": int(current.get("read_count") or 0),
            "batch_count": int(current.get("batch_count") or 0) + 1,
            "sql_predicate": "immutable_observation_version_required",
            "passed": False,
            "error": str(reason),
        }

    @staticmethod
    def _payload_fields(raw: dict) -> dict:
        """Read legacy DB JSON or an explicit derived-dataset row."""
        if "payload_json" not in raw:
            return dict(raw)
        try:
            payload = json.loads(raw.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _decode_versioned_snapshot_row(raw: dict) -> dict:
        """Expand one immutable snapshot payload into the legacy read shape.

        Version rows store the normalized snapshot as a payload. Some readers
        use market names such as open and vol, while older readers use
        projection aliases such as daily_open and volume. Return both without
        consulting the mutable projection table.
        """
        payload = BacktestDataSource._payload_fields(raw)
        result = {
            **payload,
            "ts_code": raw.get("ts_code"),
            "trade_date": raw.get("trade_date"),
            "economic_available_time": raw.get("economic_available_time") or "",
            "observed_at": raw.get("observed_at") or "",
            "first_observed_at": raw.get("first_observed_at") or "",
            "observation_provenance": raw.get("observation_provenance") or "",
            "content_hash": raw.get("content_hash") or "",
            "created_at": raw.get("created_at") or "",
            "available_time": raw.get("observed_at") or "",
            "source_code": raw.get("source_code") or "",
        }
        aliases = {
            "daily_open": "open",
            "daily_high": "high",
            "daily_low": "low",
            "daily_pre_close": "pre_close",
            "volume": "vol",
            "turnover": "amount",
            "change_pct": "pct_chg",
        }
        for alias, field in aliases.items():
            if result.get(alias) is None and payload.get(field) is not None:
                result[alias] = payload.get(field)
        return result

    @staticmethod
    def _normalise_date_clause(date_clause: str) -> str:
        clause = str(date_clause or "").strip()
        if clause.lower().startswith("and "):
            clause = clause[4:].strip()
        return clause.replace("trade_date", "v.trade_date")

    @staticmethod
    def _observed_before_predicate(*, postgres: bool) -> str:
        return f"{_timestamp_value_sql('v.observed_at', is_postgres=postgres)}<={_timestamp_parameter_sql(is_postgres=postgres)}"

    @staticmethod
    def _materialized_before_predicate(*, postgres: bool) -> str:
        return f"{_timestamp_value_sql('v.created_at', is_postgres=postgres)}<={_timestamp_parameter_sql(is_postgres=postgres)}"

    @staticmethod
    def _first_observed_before_predicate(*, postgres: bool) -> str:
        return f"{_timestamp_value_sql('v.first_observed_at', is_postgres=postgres)}<={_timestamp_parameter_sql(is_postgres=postgres)}"

    @staticmethod
    def _economic_before_predicate(*, postgres: bool) -> str:
        return f"{_timestamp_value_sql('v.economic_available_time', is_postgres=postgres)}<={_timestamp_parameter_sql(is_postgres=postgres)}"

    def _versioned_raw_rows(
        self,
        api_name: str,
        ymd: str = "",
        *,
        ts_codes: list[str] | None = None,
        date_clause: str = "",
        date_params: tuple = (),
    ) -> list[dict] | None:
        """Read the latest immutable provider version visible at the cutoff.

        ``None`` means the observation-version migration is unavailable; an
        empty list means it is installed but no version was visible.  Keeping
        those states distinct prevents a later current-table row from leaking
        into a historical replay.
        """
        if self.market_dataset is not None:
            return self.market_dataset.rows(api_name, cutoff=self._as_of_time, ymd=ymd,
                                            ts_codes=ts_codes, date_clause=date_clause, date_params=date_params)
        try:
            postgres = _is_postgres()
            # The mutable projection records the provider selected by the
            # publication probe. Bind the immutable read to that hash so a
            # slower source cannot replace the first accepted source merely
            # because its observed_at is later. If the projection table is
            # absent (small legacy fixtures), retain the old version ranking.
            with _read_data_conn() as schema_conn:
                projection_columns = _table_columns(schema_conn, "dc_tushare_raw_records")
            has_accepted_projection = {
                "api_name", "record_key", "content_hash",
            }.issubset(projection_columns)
            version_source_sql = "dc_tushare_raw_record_version v"
            if has_accepted_projection:
                version_source_sql = (
                    "dc_tushare_raw_record_version v "
                    "JOIN dc_tushare_raw_records p "
                    "ON p.api_name=v.api_name AND p.record_key=v.record_key "
                    "AND p.content_hash=v.content_hash"
                )
            predicates = [
                "v.api_name=?",
                "v.observed_at<>''",
                "v.content_hash<>''",
                "LOWER(COALESCE(v.observation_provenance,'')) NOT LIKE 'legacy_%'",
                _explicit_timestamp_sql("v.observed_at", is_postgres=postgres),
                _explicit_timestamp_sql("v.first_observed_at", is_postgres=postgres),
                _explicit_timestamp_sql("v.economic_available_time", is_postgres=postgres),
                _explicit_timestamp_sql("v.created_at", is_postgres=postgres),
            ]
            params: list = [api_name]
            if ymd:
                predicates.append("v.trade_date=?")
                params.append(ymd)
            if ts_codes:
                placeholders = ",".join("?" for _ in ts_codes)
                predicates.append(f"v.ts_code IN ({placeholders})")
                params.extend(ts_codes)
            if date_clause:
                predicates.append(self._normalise_date_clause(date_clause))
                params.extend(date_params)
            if self._as_of_time:
                predicates.append(self._observed_before_predicate(postgres=postgres))
                predicates.append(self._first_observed_before_predicate(postgres=postgres))
                predicates.append(self._economic_before_predicate(postgres=postgres))
                predicates.append(self._materialized_before_predicate(postgres=postgres))
                params.append(self._as_of_time)
                params.append(self._as_of_time)
                params.append(self._as_of_time)
                params.append(self._as_of_time)
            where = " AND ".join(predicates)
            if has_accepted_projection:
                # The accepted projection points at one immutable content hash
                # per logical raw record.  Ranking that already-unique join
                # scans the entire version history and was the source of the
                # production ``raw_ranked`` timeout.
                sql = f"""SELECT v.api_name,v.record_key,v.ts_code,v.trade_date,v.payload_json,
                                v.economic_available_time,v.observed_at,v.first_observed_at,
                                v.observation_provenance,v.content_hash,v.created_at,v.source_code
                           FROM {version_source_sql}
                          WHERE {where}
                          ORDER BY v.observed_at,v.id"""
            else:
                sql = f"""WITH ranked AS (
                             SELECT v.api_name,v.record_key,v.ts_code,v.trade_date,v.payload_json,
                                    v.economic_available_time,v.observed_at,v.first_observed_at,
                                    v.observation_provenance,v.content_hash,v.created_at,v.source_code,
                                    ROW_NUMBER() OVER (
                                      PARTITION BY v.api_name,v.record_key
                                      ORDER BY v.observed_at DESC,v.created_at DESC,v.id DESC
                                    ) AS row_rank
                               FROM {version_source_sql}
                              WHERE {where}
                           )
                           SELECT api_name,record_key,ts_code,trade_date,payload_json,
                                  economic_available_time,observed_at,first_observed_at,
                                  observation_provenance,content_hash,created_at,source_code
                             FROM ranked WHERE row_rank=1"""
            with _read_data_conn() as conn:
                rows = [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
            return rows
        except Exception as exc:  # migration not installed or incompatible adapter
            logger.debug("immutable raw PIT version read unavailable: %s", str(exc)[:160])
            return None

    def _versioned_snapshot_rows(self, ymd: str, *, ts_codes: list[str] | None = None,
                                 date_clause: str = "", date_params: tuple = (),
                                 columns: list[str] | None = None) -> list[dict] | None:
        if self.market_dataset is not None:
            rows = self.market_dataset.rows("snapshot", cutoff=self._as_of_time, ymd=ymd,
                                            ts_codes=ts_codes, date_clause=date_clause,
                                            date_params=date_params, columns=columns)
            return [self._decode_versioned_snapshot_row(row) for row in rows]
        cache_key = (
            str(self._as_of_time or ""), str(ymd or ""),
            tuple(sorted(ts_codes or [])), str(date_clause or ""), tuple(date_params),
        )
        cached = self._versioned_snapshot_cache.get(cache_key)
        if cached is not None:
            return [dict(row) for row in cached]
        try:
            postgres = _is_postgres()
            with _read_data_conn() as schema_conn:
                projection_columns = _table_columns(schema_conn, "dc_stock_daily_snapshot")
            has_accepted_projection = {
                "ts_code", "trade_date", "content_hash",
            }.issubset(projection_columns)
            version_source_sql = "dc_stock_daily_snapshot_version v"
            accepted_slice_cte = ""
            accepted_slice_params: list[str] = []
            if has_accepted_projection:
                # The accepted snapshot projection has a unique
                # (ts_code, trade_date) key. Join it directly to the immutable
                # version instead of running two correlated EXISTS probes for
                # every version row. The old form repeatedly scanned hundreds
                # of thousands of versions during daily plan generation and
                # exhausted PostgreSQL's statement timeout.
                if postgres and ymd:
                    accepted_slice_cte = (
                        "WITH accepted_snapshot AS MATERIALIZED ("
                        "SELECT ts_code,trade_date,content_hash,name,close,open,high,low,"
                        "pre_close,change_pct,volume,turnover,total_mv,circ_mv,pe_ttm,pb,"
                        "turnover_rate,adj_factor "
                        "FROM dc_stock_daily_snapshot WHERE trade_date=?"
                        ") "
                    )
                    accepted_slice_params.append(ymd)
                    version_source_sql = (
                        "accepted_snapshot p "
                        "JOIN dc_stock_daily_snapshot_version v "
                        "ON p.ts_code=v.ts_code AND p.trade_date=v.trade_date "
                        "AND p.content_hash=v.content_hash"
                    )
                else:
                    version_source_sql = (
                        "dc_stock_daily_snapshot_version v "
                        "JOIN dc_stock_daily_snapshot p "
                        "ON p.ts_code=v.ts_code AND p.trade_date=v.trade_date "
                        "AND p.content_hash=v.content_hash"
                    )
            predicates: list[str] = [
                "v.observed_at<>''",
                "v.content_hash<>''",
                "LOWER(COALESCE(v.observation_provenance,'')) NOT LIKE 'legacy_%'",
                _explicit_timestamp_sql("v.observed_at", is_postgres=postgres),
                _explicit_timestamp_sql("v.first_observed_at", is_postgres=postgres),
                _explicit_timestamp_sql("v.economic_available_time", is_postgres=postgres),
                _explicit_timestamp_sql("v.created_at", is_postgres=postgres),
            ]
            params: list = []
            if ymd:
                predicates.append("v.trade_date=?")
                params.append(ymd)
            if ts_codes:
                placeholders = ",".join("?" for _ in ts_codes)
                predicates.append(f"v.ts_code IN ({placeholders})")
                params.extend(ts_codes)
            if date_clause:
                predicates.append(self._normalise_date_clause(date_clause))
                params.extend(date_params)
            if self._as_of_time:
                predicates.append(self._observed_before_predicate(postgres=postgres))
                predicates.append(self._first_observed_before_predicate(postgres=postgres))
                predicates.append(self._economic_before_predicate(postgres=postgres))
                predicates.append(self._materialized_before_predicate(postgres=postgres))
                params.append(self._as_of_time)
                params.append(self._as_of_time)
                params.append(self._as_of_time)
                params.append(self._as_of_time)
            where = " AND ".join(predicates)
            if has_accepted_projection:
                # The immutable payload is a large JSON document.  Returning
                # it for every symbol made the production planner spend most
                # of its 30s statement budget in ClientWrite while sending
                # several thousand rows over the private link.  The snapshot
                # readers only consume this bounded field set, so decode the
                # accepted immutable version server-side and return narrow
                # scalar columns.  The exact accepted content-hash join is
                # unchanged; this is only a transport/query-shape change.
                if postgres:
                    sql = f"""{accepted_slice_cte}SELECT v.ts_code,v.trade_date,
                                p.name,p.close,
                                p.open AS daily_open,
                                p.high AS daily_high,
                                p.low AS daily_low,
                                p.pre_close AS daily_pre_close,
                                p.change_pct,
                                p.volume,p.turnover,
                                p.total_mv,p.circ_mv,
                                p.pe_ttm,p.pb,
                                p.turnover_rate,p.adj_factor,
                                v.economic_available_time,v.observed_at,
                                v.first_observed_at,v.created_at
                           FROM {version_source_sql}
                          WHERE {where}
                          ORDER BY v.ts_code,v.trade_date,v.observed_at,v.id"""
                else:
                    sql = f"""{accepted_slice_cte}SELECT v.ts_code,v.trade_date,v.payload_json,v.source_code,
                                v.economic_available_time,v.observed_at,v.first_observed_at,
                                v.observation_provenance,v.content_hash,v.created_at
                           FROM {version_source_sql}
                          WHERE {where}
                          ORDER BY v.ts_code,v.trade_date,v.observed_at,v.id"""
            else:
                sql = f"""WITH ranked AS (
                             SELECT v.ts_code,v.trade_date,v.payload_json,v.source_code,
                                    v.economic_available_time,v.observed_at,v.first_observed_at,
                                    v.observation_provenance,v.content_hash,v.created_at,
                                    ROW_NUMBER() OVER (
                                      PARTITION BY v.ts_code,v.trade_date
                                      ORDER BY v.observed_at DESC,v.created_at DESC,v.id DESC
                                    ) AS row_rank
                               FROM {version_source_sql}
                              WHERE {where}
                           )
                           SELECT ts_code,trade_date,payload_json,source_code,
                                  economic_available_time,observed_at,first_observed_at,
                                  observation_provenance,content_hash,created_at
                             FROM ranked WHERE row_rank=1"""
            with _read_data_conn() as conn:
                raw_rows = [dict(row) for row in conn.execute(
                    sql, tuple(accepted_slice_params + params),
                ).fetchall()]
            decoded = [self._decode_versioned_snapshot_row(raw) for raw in raw_rows]
            self._versioned_snapshot_cache[cache_key] = [dict(row) for row in decoded]
            return decoded
        except Exception as exc:
            logger.debug("immutable snapshot PIT version read unavailable: %s", str(exc)[:160])
            return None

    def _versioned_snapshot_history_rows(
        self,
        ts_codes: list[str],
        *,
        date_clause: str = "",
        date_params: tuple = (),
    ) -> list[dict] | None:
        """Read a bounded multi-date snapshot history from immutable versions.

        The current snapshot is an overwrite projection.  It is suitable for
        today's screen, but cannot prove what was visible on an earlier replay
        date after a late refresh.  Historical consumers must use this method
        whenever the observation-clock migration is installed.
        """
        if not ts_codes:
            return []
        if self.market_dataset is not None:
            return self._versioned_snapshot_rows("", ts_codes=ts_codes,
                                                 date_clause=date_clause, date_params=date_params,
                                                 columns=[
                                                     "ts_code", "trade_date", "close", "volume",
                                                     "turnover", "adj_factor", "observed_at",
                                                     "first_observed_at", "economic_available_time",
                                                     "created_at", "observation_provenance", "content_hash",
                                                 ])
        try:
            placeholders = ",".join("?" for _ in ts_codes)
            postgres = _is_postgres()
            with _read_data_conn() as schema_conn:
                projection_columns = _table_columns(schema_conn, "dc_stock_daily_snapshot")
            has_accepted_projection = {
                "ts_code", "trade_date", "content_hash",
            }.issubset(projection_columns)
            version_source_sql = "dc_stock_daily_snapshot_version v"
            if has_accepted_projection:
                version_source_sql = (
                    "dc_stock_daily_snapshot_version v "
                    "JOIN dc_stock_daily_snapshot p "
                    "ON p.ts_code=v.ts_code AND p.trade_date=v.trade_date "
                    "AND p.content_hash=v.content_hash"
                )
            predicates = [
                f"v.ts_code IN ({placeholders})",
                "v.observed_at<>''",
                "v.content_hash<>''",
                "LOWER(COALESCE(v.observation_provenance,'')) NOT LIKE 'legacy_%'",
                _explicit_timestamp_sql("v.observed_at", is_postgres=postgres),
                _explicit_timestamp_sql("v.first_observed_at", is_postgres=postgres),
                _explicit_timestamp_sql("v.economic_available_time", is_postgres=postgres),
                _explicit_timestamp_sql("v.created_at", is_postgres=postgres),
            ]
            params: list = list(ts_codes)
            if date_clause:
                predicates.append(self._normalise_date_clause(date_clause))
                params.extend(date_params)
            if self._as_of_time:
                predicates.append(self._observed_before_predicate(postgres=postgres))
                predicates.append(self._first_observed_before_predicate(postgres=postgres))
                predicates.append(self._economic_before_predicate(postgres=postgres))
                predicates.append(self._materialized_before_predicate(postgres=postgres))
                params.append(self._as_of_time)
                params.append(self._as_of_time)
                params.append(self._as_of_time)
                params.append(self._as_of_time)
            where = " AND ".join(predicates)
            if has_accepted_projection:
                sql = f"""SELECT v.ts_code,v.trade_date,v.payload_json,v.source_code,
                                v.economic_available_time,v.observed_at,v.first_observed_at,
                                v.observation_provenance,v.content_hash,v.created_at
                           FROM {version_source_sql}
                          WHERE {where}
                          ORDER BY v.ts_code,v.trade_date,v.observed_at,v.id"""
            else:
                sql = f"""WITH ranked AS (
                             SELECT v.ts_code,v.trade_date,v.payload_json,v.source_code,
                                    v.economic_available_time,v.observed_at,v.first_observed_at,
                                    v.observation_provenance,v.content_hash,v.created_at,
                                    ROW_NUMBER() OVER (
                                      PARTITION BY v.ts_code,v.trade_date
                                      ORDER BY v.observed_at DESC,v.id DESC
                                    ) AS row_rank
                               FROM {version_source_sql}
                              WHERE {where}
                           )
                           SELECT ts_code,trade_date,payload_json,source_code,
                                  economic_available_time,observed_at,first_observed_at,
                                  observation_provenance,content_hash,created_at
                             FROM ranked WHERE row_rank=1
                            ORDER BY ts_code,trade_date"""
            with _read_data_conn() as conn:
                raw_rows = [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
            return [self._decode_versioned_snapshot_row(raw) for raw in raw_rows]
        except Exception as exc:
            logger.debug("immutable snapshot PIT history read unavailable: %s", str(exc)[:160])
            return None

    def _postgres_trade_dates(self, start: str, end: str) -> List[str]:
        if self.market_dataset is not None:
            return [self._fmt_date(d) for d in self.market_dataset.dates(start, end)]
        try:
            with _read_data_conn() as conn:
                rows = conn.execute(
                    """SELECT trade_date FROM dc_available_trade_dates
                       WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date""",
                    (start.replace("-", ""), end.replace("-", "")),
                ).fetchall()
            return [self._fmt_date(str(dict(row).get("trade_date") or "")) for row in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest calendar read failed: %s", str(exc)[:160])
            return []

    def _postgres_daily_rows(self, ymd: str, symbol: str = "") -> List[dict]:
        try:
            wanted = []
            if symbol:
                code = str(symbol).strip().split(".", 1)[0].zfill(6)
                suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
                wanted = [f"{code}{suffix}"]
            versioned = self._versioned_raw_rows("daily", ymd, ts_codes=wanted)
            if versioned is not None:
                self._record_pit_read("tushare.daily", versioned)
                return versioned
            if _is_postgres() or self._as_of_time:
                self._record_pit_failure("tushare.daily", "observation_version_table_unavailable")
                return []
            sql = "SELECT ts_code,trade_date,payload_json,observed_at AS available_time,observed_at FROM dc_tushare_raw_records WHERE api_name='daily' AND trade_date=?"
            params: tuple = (ymd,)
            if self._as_of_time:
                sql += " AND observed_at<=?"
                params += (self._as_of_time,)
            if symbol:
                code = str(symbol).strip().split(".", 1)[0].zfill(6)
                suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
                sql += " AND ts_code=?"
                params += (f"{code}{suffix}",)
            with _read_data_conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            result = [dict(row) for row in rows]
            self._record_pit_read("tushare.daily", result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest daily read failed: %s", str(exc)[:160])
            return []

    def _postgres_snapshot_rows(self, ymd: str) -> List[dict]:
        if self.market_dataset is not None:
            rows = self._versioned_snapshot_rows(ymd)
            self._record_pit_read("snapshot.daily_structured", rows)
            return rows
        cached_rows = load_snapshot_bundle_from_local_cache(ymd, as_of_time=self._as_of_time)
        if cached_rows is not None:
            self._record_pit_read("snapshot.daily_structured.local_cache", cached_rows)
            return cached_rows
        try:
            versioned = self._versioned_snapshot_rows(ymd)
            if versioned is not None:
                self._record_pit_read("snapshot.daily_structured", versioned)
                return versioned
            if _is_postgres() or self._as_of_time:
                self._record_pit_failure("snapshot.daily_structured", "observation_version_table_unavailable")
                return []
            sql = """SELECT ts_code,trade_date,name,close,change_pct,volume,turnover,
                            total_mv,circ_mv,pe_ttm,pb,turnover_rate,observed_at AS available_time,observed_at,
                            open AS daily_open,high AS daily_high,low AS daily_low,
                            pre_close AS daily_pre_close,adj_factor
                     FROM dc_stock_daily_snapshot
                     WHERE trade_date=?"""
            params: tuple = (ymd,)
            if self._as_of_time:
                sql += " AND observed_at<=?"
                params += (self._as_of_time,)
            with _read_data_conn() as conn:
                rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            self._record_pit_read("snapshot.daily_structured", rows)
            return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL point-in-time snapshot read failed: %s", str(exc)[:160])
            self._record_pit_failure("snapshot.daily_structured", "immutable_snapshot_read_failed")
            return []

    def load_fund_snapshot(self, date_ymd: str, ts_codes: List[str]) -> List[SnapshotRow]:
        """Load tradable ETF bars under the same point-in-time cutoff as stocks."""
        codes = [str(code or "").strip() for code in ts_codes if str(code or "").strip()]
        if not codes:
            return []
        ymd = str(date_ymd).replace("-", "")[:8]
        versioned = self._versioned_raw_rows("fund_daily", ymd, ts_codes=codes)
        if versioned is not None:
            raw_rows = versioned
        else:
            if _is_postgres() or self._as_of_time:
                self._record_pit_failure("tushare.fund_daily", "observation_version_table_unavailable")
                return []
            placeholders = ",".join("?" for _ in codes)
            sql = f"""SELECT ts_code,trade_date,payload_json,observed_at AS available_time,observed_at
                      FROM dc_tushare_raw_records
                      WHERE api_name='fund_daily' AND trade_date=? AND ts_code IN ({placeholders})"""
            params: tuple = (ymd, *codes)
            if self._as_of_time:
                sql += " AND observed_at<=?"
                params += (self._as_of_time,)
            with _read_data_conn() as conn:
                raw_rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        self._record_pit_read("tushare.fund_daily", raw_rows)
        result: List[SnapshotRow] = []
        for raw in raw_rows:
            payload = self._payload_fields(raw)
            close = float(payload.get("close") or 0)
            if close <= 0:
                continue
            prev_close = float(payload.get("pre_close") or close)
            result.append(SnapshotRow(
                symbol=str(raw.get("ts_code") or "").split(".", 1)[0],
                date=self._fmt_date(str(raw.get("trade_date") or ymd)),
                open=float(payload.get("open") or close),
                high=float(payload.get("high") or close),
                low=float(payload.get("low") or close),
                close=close,
                prev_close=prev_close,
                change_pct=float(payload.get("pct_chg") or ((close / prev_close - 1.0) * 100 if prev_close else 0)),
                volume=float(payload.get("vol") or 0),
                turnover=float(payload.get("amount") or 0),
                name=str(payload.get("name") or f"{str(raw.get('ts_code') or '').split('.', 1)[0]} ETF"),
            ))
        return result

    def load_close_map(self, date_ymd: str, symbols: List[str]) -> Dict[str, float]:
        """Read only the requested execution-day closes.

        Ledger evidence needs a close for the small set of executed symbols, not
        the full market snapshot. Keep the query point-in-time safe while
        avoiding a several-thousand-row daily read on every execution batch.
        """
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            code = str(raw or "").strip().upper().split(".", 1)[0].zfill(6)
            if not code or code in seen:
                continue
            seen.add(code)
            normalized.append(code)
        if not normalized:
            return {}
        ymd = str(date_ymd or "").replace("-", "")[:8]
        if len(ymd) != 8 or not ymd.isdigit():
            return {}

        stock_codes: list[str] = []
        fund_codes: list[str] = []
        for code in normalized:
            if is_exchange_etf(code):
                suffix = ".SH" if code.startswith(("50", "51", "52", "56", "58")) else ".SZ"
                fund_codes.append(f"{code}{suffix}")
            else:
                suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
                stock_codes.append(f"{code}{suffix}")

        result: Dict[str, float] = {}
        if stock_codes:
            versioned = self._versioned_snapshot_rows(ymd, ts_codes=stock_codes)
            if versioned is not None:
                rows = [dict(row) for row in versioned if float(row.get("close") or 0) > 0]
                self._record_pit_read("snapshot.execution_close", rows)
                for row in rows:
                    code = str(row.get("ts_code") or "").split(".", 1)[0].zfill(6)
                    close = float(row.get("close") or 0)
                    if code and close > 0:
                        result[code] = close
                stock_codes = []
        if stock_codes:
            if _is_postgres() or self._as_of_time:
                self._record_pit_failure("snapshot.execution_close", "observation_version_table_unavailable")
                stock_codes = []
            else:
                placeholders = ",".join("?" for _ in stock_codes)
                sql = f"""SELECT ts_code,close,observed_at AS available_time,observed_at
                            FROM dc_stock_daily_snapshot
                           WHERE trade_date=? AND ts_code IN ({placeholders})
                             AND close IS NOT NULL"""
                params: tuple = (ymd, *stock_codes)
                if self._as_of_time:
                    sql += " AND observed_at<=?"
                    params += (self._as_of_time,)
                with _read_data_conn() as conn:
                    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
                self._record_pit_read("snapshot.execution_close", rows)
                for row in rows:
                    code = str(row.get("ts_code") or "").split(".", 1)[0].zfill(6)
                    close = float(row.get("close") or 0)
                    if code and close > 0:
                        result[code] = close

        if fund_codes:
            versioned = self._versioned_raw_rows("fund_daily", ymd, ts_codes=fund_codes)
            if versioned is not None:
                rows = versioned
                self._record_pit_read("tushare.fund_daily_execution_close", rows)
                for row in rows:
                    payload = self._payload_fields(row)
                    code = str(row.get("ts_code") or "").split(".", 1)[0].zfill(6)
                    close = float(payload.get("close") or 0)
                    if code and close > 0:
                        result[code] = close
                fund_codes = []
        if fund_codes:
            if _is_postgres() or self._as_of_time:
                self._record_pit_failure("tushare.fund_daily_execution_close", "observation_version_table_unavailable")
                fund_codes = []
        if fund_codes:
            placeholders = ",".join("?" for _ in fund_codes)
            sql = f"""WITH ranked AS (
                           SELECT ts_code,payload_json,observed_at AS available_time,observed_at,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY ts_code ORDER BY observed_at DESC,id DESC
                                  ) AS row_rank
                             FROM dc_tushare_raw_records
                            WHERE api_name='fund_daily' AND trade_date=?
                              AND ts_code IN ({placeholders})"""
            params = (ymd, *fund_codes)
            if self._as_of_time:
                sql += " AND observed_at<=?"
                params += (self._as_of_time,)
            sql += ") SELECT ts_code,payload_json,available_time,observed_at FROM ranked WHERE row_rank=1"
            with _read_data_conn() as conn:
                rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            self._record_pit_read("tushare.fund_daily_execution_close", rows)
            for row in rows:
                try:
                    payload = json.loads(row.get("payload_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                code = str(row.get("ts_code") or "").split(".", 1)[0].zfill(6)
                close = float(payload.get("close") or 0)
                if code and close > 0:
                    result[code] = close
        return result

    def preload_histories(self, symbols: List[str], *, end_date: str | None = None, lookback: int | None = None) -> None:
        """Batch-load only the history not already visible to a forward replay."""
        wanted = []
        seen_codes: set[str] = set()
        for raw in symbols:
            code = str(raw or "").strip().split(".", 1)[0].zfill(6)
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
            wanted.append((code, f"{code}{suffix}"))
        end_ymd = str(end_date or "").replace("-", "")[:8]
        if len(end_ymd) == 8 and end_ymd.isdigit():
            wanted = [
                item for item in wanted
                if self._hist_cache_through.get(item[0], "") < end_ymd
            ]
        else:
            wanted = [item for item in wanted if item[0] not in self._hist_cache]
        if not wanted:
            return
        grouped: Dict[str, List[HistBar]] = {
            code: list(self._hist_cache.get(code, [])) for code, _ in wanted
        }
        known_dates = {
            code: {bar.date for bar in bars}
            for code, bars in grouped.items()
        }
        query_groups: list[tuple[list[tuple[str, str]], str, tuple[str, ...]]] = []
        if len(end_ymd) == 8 and end_ymd.isdigit():
            cached = [item for item in wanted if self._hist_cache_through.get(item[0])]
            fresh = [item for item in wanted if not self._hist_cache_through.get(item[0])]
            if cached:
                earliest_cached = min(self._hist_cache_through[code] for code, _ in cached)
                query_groups.append((cached, " AND trade_date>? AND trade_date<=?", (earliest_cached, end_ymd)))
            if fresh:
                limit_days = max(1, int(lookback or 0) + 5) if lookback else 0
                start_ymd = ""
                if limit_days and self.market_dataset is not None:
                    days = self.market_dataset.dates(self.market_dataset.manifest["start"], end_ymd)
                    start_ymd = days[-limit_days] if len(days) >= limit_days else (days[0] if days else "")
                elif limit_days:
                    with _read_data_conn() as conn:
                        row = conn.execute(
                            """SELECT MIN(trade_date) AS start_date FROM (
                                 SELECT trade_date FROM dc_available_trade_dates
                                 WHERE trade_date<=?
                                 ORDER BY trade_date DESC LIMIT ?
                               ) d""",
                            (end_ymd, limit_days),
                        ).fetchone()
                    start_ymd = str(dict(row).get("start_date") or "") if row else ""
                if len(start_ymd) == 8 and start_ymd.isdigit():
                    query_groups.append((fresh, " AND trade_date BETWEEN ? AND ?", (start_ymd, end_ymd)))
                else:
                    query_groups.append((fresh, " AND trade_date<=?", (end_ymd,)))
        else:
            query_groups.append((wanted, "", ()))
        try:
            # The partial covering index makes this a point-in-time history
            # lookup instead of a full snapshot scan. SQLite's variable limit
            # remains protected for explicit fixture databases.
            batch_size = 500
            for group, date_clause, date_params in query_groups:
                for offset in range(0, len(group), batch_size):
                    batch = group[offset : offset + batch_size]
                    placeholders = ",".join("?" for _ in batch)
                    batch_codes = [ts_code for _, ts_code in batch]
                    versioned_history = self._versioned_snapshot_history_rows(
                        batch_codes, date_clause=date_clause, date_params=date_params,
                    )
                    if versioned_history is not None:
                        rows = versioned_history
                    elif _is_postgres() or self._as_of_time:
                        self._record_pit_failure("snapshot.history", "observation_version_table_unavailable")
                        rows = []
                    else:
                        with _read_data_conn() as conn:
                            rows = conn.execute(
                                f"""SELECT ts_code,trade_date,close,volume,turnover,observed_at AS available_time,observed_at,adj_factor
                                    FROM dc_stock_daily_snapshot
                                    WHERE ts_code IN ({placeholders}){date_clause}
                                      AND adj_factor IS NOT NULL
                                      {' AND observed_at<=?' if self._as_of_time else ''}
                                    ORDER BY ts_code,trade_date""",
                                tuple(batch_codes) + date_params + ((self._as_of_time,) if self._as_of_time else ()),
                            ).fetchall()
                    self._record_pit_read("snapshot.history", [dict(row) for row in rows])
                    for raw_row in rows:
                        item = dict(raw_row)
                        code = str(item.get("ts_code") or "").split(".", 1)[0].zfill(6)
                        if code in grouped:
                            row_date = self._fmt_date(str(item.get("trade_date") or ""))
                            if row_date in known_dates[code]:
                                continue
                            adjusted_close = float(item.get("close") or 0) * float(item.get("adj_factor") or 1.0)
                            grouped[code].append(HistBar(
                                date=row_date,
                                open=adjusted_close, high=adjusted_close,
                                low=adjusted_close, close=adjusted_close,
                                volume=float(item.get("volume") or 0), turnover=float(item.get("turnover") or 0),
                            ))
                            known_dates[code].add(row_date)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest history preload failed: %s", str(exc)[:160])
            # Do not fall through to one query per symbol after a batch
            # timeout. Keep the already loaded prefix, mark the unresolved
            # symbols for this replay, and let the data-quality contract
            # report the missing history instead of amplifying DB pressure.
            failed_codes = {code for group, _, _ in query_groups for code, _ in group}
            self._history_load_failed.update(failed_codes)
            self._hist_cache.update(grouped)
            self.data_warnings.append("PostgreSQL history preload deferred; per-symbol fallback disabled.")
            return
        for code, bars in grouped.items():
            bars.sort(key=lambda item: item.date)
            if bars:
                self._hist_cache_through[code] = bars[-1].date.replace("-", "")
        self._hist_cache.update(grouped)

    @staticmethod
    def _fmt_date(ymd: str) -> str:
        s = str(ymd).replace("-", "")[:8]
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return ymd

    def get_available_trade_dates(self, start_date: str, end_date: str) -> List[str]:
        """Read exchange sessions from normalized PostgreSQL coverage only."""
        start = self._fmt_date(start_date)
        end = self._fmt_date(end_date)
        postgres_dates = self._postgres_trade_dates(start, end)
        if not postgres_dates:
            self.data_warnings.append("PostgreSQL 中没有覆盖该区间的日线数据。")
        return postgres_dates

    def load_snapshot(self, date_ymd: str) -> List[SnapshotRow]:
        date_iso = self._fmt_date(date_ymd)
        if date_iso in self._snapshot_cache:
            return self._snapshot_cache[date_iso]

        ymd = date_iso.replace("-", "")
        snapshot_rows = self._postgres_snapshot_rows(ymd)
        if snapshot_rows:
            rows = []
            for raw in snapshot_rows:
                if not raw.get("ts_code"):
                    continue
                try:
                    daily = json.loads(str(raw.get("daily_payload_json") or "{}"))
                except (TypeError, json.JSONDecodeError):
                    daily = {}
                try:
                    adj = json.loads(str(raw.get("adj_payload_json") or "{}"))
                except (TypeError, json.JSONDecodeError):
                    adj = {}
                close = float(raw.get("close") or daily.get("close") or 0)
                symbol = str(raw.get("ts_code") or "").split(".", 1)[0].zfill(6)
                adj_factor = float(raw.get("adj_factor") or adj.get("adj_factor") or 1.0)
                base_factor = self._replay_adjustment_bases.setdefault(symbol, adj_factor)
                price_factor = adj_factor / base_factor if base_factor > 0 else 1.0
                rows.append(SnapshotRow(
                    symbol=symbol,
                    date=self._fmt_date(str(raw.get("trade_date") or ymd)),
                    open=float(raw.get("daily_open") or daily.get("open") or close) * price_factor,
                    high=float(raw.get("daily_high") or daily.get("high") or close) * price_factor,
                    low=float(raw.get("daily_low") or daily.get("low") or close) * price_factor,
                    close=close * price_factor,
                    prev_close=float(raw.get("daily_pre_close") or daily.get("pre_close") or close) * price_factor,
                    change_pct=float(raw.get("change_pct") or 0),
                    volume=float(raw.get("volume") or 0), turnover=float(raw.get("turnover") or 0),
                    name=str(raw.get("name") or ""), adj_factor=adj_factor,
                ))
            self._snapshot_cache[date_iso] = rows
            return rows
        # A normalized snapshot is the contract consumed by formal replay.
        # Reconstructing it from raw daily bars omits the independently frozen
        # daily_basic/limit/factor dependencies and lets an incomplete snapshot
        # appear usable.  Leave the date empty and make the PIT audit fail.
        self._record_pit_failure("snapshot.daily_structured", "normalized_snapshot_version_missing")
        self.data_warnings.append(f"{ymd} PostgreSQL 快照不可用。")
        self._snapshot_cache[date_iso] = []
        return []

    def get_symbol_history(self, symbol: str) -> List[HistBar]:
        code = str(symbol).strip().zfill(6)
        if code in self._hist_cache:
            return self._hist_cache[code]
        if code in self._history_load_failed:
            return []

        try:
            suffix = ".SH" if code.startswith(("6", "9")) else ".SZ" if code.startswith(("0", "3")) else ".BJ"
            versioned = self._versioned_snapshot_history_rows([f"{code}{suffix}"])
            if versioned is None:
                if _is_postgres() or self._as_of_time:
                    self._record_pit_failure("snapshot.symbol_history", "observation_version_table_unavailable")
                    self._hist_cache[code] = []
                    return []
                with _read_data_conn() as conn:
                    rows = conn.execute(
                        f"""SELECT trade_date,close,volume,turnover,observed_at AS available_time,observed_at,adj_factor
                           FROM dc_stock_daily_snapshot
                           WHERE ts_code=? AND adj_factor IS NOT NULL
                           {' AND observed_at<=?' if self._as_of_time else ''}
                           ORDER BY trade_date""",
                        (f"{code}{suffix}",) + ((self._as_of_time,) if self._as_of_time else ()),
                    ).fetchall()
            else:
                rows = [row for row in versioned if row.get("adj_factor") is not None]
            self._record_pit_read("snapshot.symbol_history", [dict(row) for row in rows])
            postgres_bars = []
            for raw in rows:
                item = dict(raw)
                adjusted_close = float(item.get("close") or 0) * float(item.get("adj_factor") or 1.0)
                postgres_bars.append(HistBar(
                    date=self._fmt_date(str(item.get("trade_date") or "")),
                    open=adjusted_close, high=adjusted_close,
                    low=adjusted_close, close=adjusted_close,
                    volume=float(item.get("volume") or 0), turnover=float(item.get("turnover") or 0),
                ))
            if postgres_bars:
                self._hist_cache[code] = postgres_bars
                self._hist_cache_through[code] = postgres_bars[-1].date.replace("-", "")
                return postgres_bars
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest history read failed: %s", str(exc)[:160])
            self._record_pit_failure("snapshot.symbol_history", "immutable_snapshot_history_read_failed")

        self._hist_cache[code] = []
        return []

    def get_hist_window(self, symbol: str, end_date_iso: str, lookback: int) -> List[HistBar]:
        bars = self.get_symbol_history(symbol)
        if not bars:
            return []
        d = end_date_iso[:10]
        vals = [b for b in bars if b.date <= d]
        if lookback <= 0:
            return vals[-1:] if vals else []
        return vals[-lookback:]

    def get_price_on_date(self, symbol: str, date_iso: str, field: str = "close") -> Optional[float]:
        ts_code = str(symbol or "").strip().upper()
        if "." in ts_code:
            ymd = str(date_iso or "").replace("-", "")[:8]
            try:
                versioned = self._versioned_raw_rows("index_daily", ymd, ts_codes=[ts_code])
                if versioned is None:
                    if _is_postgres() or self._as_of_time:
                        self._record_pit_failure("canonical_market.index_daily", "observation_version_table_unavailable")
                        return None
                    sql = """SELECT payload_json,observed_at AS available_time,observed_at FROM dc_tushare_raw_records
                             WHERE api_name='index_daily' AND ts_code=? AND trade_date=?"""
                    params: tuple = (ts_code, ymd)
                    if self._as_of_time:
                        sql += " AND observed_at<=?"
                        params += (self._as_of_time,)
                    with _read_data_conn() as conn:
                        raw = conn.execute(sql, params).fetchone()
                    versioned = [dict(raw)] if raw else []
                if versioned:
                    item = dict(versioned[-1])
                    self._record_pit_read("canonical_market.index_daily", [item])
                    payload = self._payload_fields(item)
                    value = float(payload.get(field) or 0)
                    return value if value > 0 else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("PostgreSQL benchmark read failed: %s", str(exc)[:160])
                self._record_pit_failure("canonical_market.index_daily", "immutable_index_read_failed")
        bars = self.get_symbol_history(symbol)
        if not bars:
            return None
        for b in reversed(bars):
            if b.date == date_iso[:10]:
                return float(getattr(b, field, 0) or 0)
            if b.date < date_iso[:10]:
                return None
        return None

    def get_index_return(self, symbol: str, as_of_date: str, *, lookback: int = 20) -> Optional[float]:
        returns = self.get_index_returns(symbol, as_of_date, lookback=lookback)
        if not returns:
            return None
        compound = 1.0
        for value in returns:
            compound *= 1.0 + value
        return compound - 1.0

    def get_index_returns(self, symbol: str, as_of_date: str, *, lookback: int = 60) -> List[float]:
        """Return point-in-time daily benchmark returns in chronological order."""
        ts_code = str(symbol or "").strip().upper()
        ymd = str(as_of_date or "").replace("-", "")[:8]
        try:
            versioned = self._versioned_raw_rows(
                "index_daily", ts_codes=[ts_code], date_clause="trade_date<=?", date_params=(ymd,),
            )
            if versioned is None:
                if _is_postgres() or self._as_of_time:
                    self._record_pit_failure("canonical_market.index_daily_trend", "observation_version_table_unavailable")
                    return []
                sql = """SELECT trade_date,payload_json,observed_at AS available_time,observed_at FROM dc_tushare_raw_records
                         WHERE api_name='index_daily' AND ts_code=? AND trade_date<=?"""
                params: tuple = (ts_code, ymd)
                if self._as_of_time:
                    sql += " AND observed_at<=?"
                    params += (self._as_of_time,)
                sql += " ORDER BY trade_date DESC LIMIT ?"
                params += (max(2, min(int(lookback) + 1, 251)),)
                with _read_data_conn() as conn:
                    raw_rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            else:
                raw_rows = sorted(versioned, key=lambda row: str(row.get("trade_date") or ""), reverse=True)[:max(2, min(int(lookback) + 1, 251))]
            self._record_pit_read("canonical_market.index_daily_trend", raw_rows)
            closes = [
                float(self._payload_fields(row).get("close") or 0)
                for row in reversed(raw_rows)
            ]
            closes = [value for value in closes if value > 0]
            return [
                closes[index] / closes[index - 1] - 1.0
                for index in range(1, len(closes))
                if closes[index - 1] > 0
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL benchmark trend read failed: %s", str(exc)[:160])
            self._record_pit_failure("canonical_market.index_daily_trend", "immutable_index_history_read_failed")
            return []

    def get_daily_basic_map(self, date_ymd: str) -> Dict[str, Dict[str, float]]:
        """
        TuShare daily_basic 截面缓存，键为 6 位 symbol。
        """
        date_iso = self._fmt_date(date_ymd)
        ymd = date_iso.replace("-", "")
        if ymd in self._daily_basic_cache:
            return self._daily_basic_cache[ymd]

        try:
            versioned = self._versioned_snapshot_rows(ymd)
            if versioned is None:
                if _is_postgres() or self._as_of_time:
                    self._record_pit_failure("snapshot.daily_basic", "observation_version_table_unavailable")
                    self._daily_basic_cache[ymd] = {}
                    return {}
                with _read_data_conn() as conn:
                    rows = conn.execute(
                        f"""SELECT ts_code,total_mv,circ_mv,pe_ttm,pb,turnover_rate,observed_at AS available_time,observed_at
                             FROM dc_stock_daily_snapshot WHERE trade_date=?{' AND observed_at<=?' if self._as_of_time else ''}""",
                        (ymd,) + ((self._as_of_time,) if self._as_of_time else ()),
                    ).fetchall()
            else:
                rows = versioned
            self._record_pit_read("snapshot.daily_basic", [dict(row) for row in rows])
            postgres_payload: Dict[str, Dict[str, float]] = {}
            for raw in rows:
                item = dict(raw)
                symbol = str(item.get("ts_code") or "").split(".", 1)[0].zfill(6)
                if symbol:
                    postgres_payload[symbol] = {
                        "total_mv": float(item.get("total_mv") or 0),
                        "float_mv": float(item.get("circ_mv") or 0),
                        "pe_ttm": float(item.get("pe_ttm") or 0),
                        "pb": float(item.get("pb") or 0),
                        "turnover_rate": float(item.get("turnover_rate") or 0),
                    }
            if postgres_payload:
                self._daily_basic_cache[ymd] = postgres_payload
                return postgres_payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("PostgreSQL backtest daily_basic read failed: %s", str(exc)[:160])
            self._record_pit_failure("snapshot.daily_basic", "immutable_snapshot_daily_basic_read_failed")
        self.data_warnings.append(f"{ymd} PostgreSQL daily_basic 不可用。")
        self._daily_basic_cache[ymd] = {}
        return {}

    @staticmethod
    def should_rebalance(freq: str, trade_date_iso: str, prev_rebalance_date: Optional[str]) -> bool:
        if prev_rebalance_date is None:
            return True
        if freq == "daily":
            return True
        cur = datetime.fromisoformat(trade_date_iso[:10])
        prev = datetime.fromisoformat(prev_rebalance_date[:10])
        if freq == "weekly":
            return cur.isocalendar()[1] != prev.isocalendar()[1] or cur.year != prev.year
        if freq == "monthly":
            return (cur.year, cur.month) != (prev.year, prev.month)
        return True
