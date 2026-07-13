from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.getenv("DATA_PLATFORM_DB_PATH", str(Path(__file__).resolve().parents[1] / "data" / "data-platform.db")))

SCHEMA_SQL = r"""CREATE TABLE data_ingestion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name TEXT NOT NULL,
            source_code TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            rows_read INTEGER NOT NULL DEFAULT 0,
            rows_written INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            meta_json TEXT NOT NULL
        );
CREATE TABLE data_sources (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'active',
            description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE dc_announcement_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            announcement_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            impact_direction TEXT NOT NULL DEFAULT 'neutral',
            severity TEXT NOT NULL DEFAULT 'medium',
            confidence REAL NOT NULL DEFAULT 0,
            event_date TEXT NOT NULL DEFAULT '',
            entities_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
CREATE TABLE dc_announcement_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_code TEXT NOT NULL,
            announcement_id TEXT NOT NULL,
            ts_code TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            publish_time TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL,
            raw_text TEXT,
            file_path TEXT,
            content_hash TEXT NOT NULL DEFAULT '',
            available_time TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (source_code, announcement_id)
        );
CREATE TABLE dc_data_quality_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_name TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            affected_count INTEGER NOT NULL DEFAULT 0,
            checked_at TEXT NOT NULL,
            meta_json TEXT NOT NULL
        );
CREATE TABLE dc_industry_metric_definition (
            metric_code TEXT PRIMARY KEY,
            industry_code TEXT NOT NULL,
            industry_name TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT '',
            frequency TEXT NOT NULL DEFAULT '',
            source_code TEXT NOT NULL DEFAULT '',
            description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE dc_industry_metric_value (
            metric_code TEXT NOT NULL,
            data_date TEXT NOT NULL,
            value REAL,
            raw_value TEXT NOT NULL DEFAULT '',
            source_code TEXT NOT NULL DEFAULT '',
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (metric_code, data_date)
        );
CREATE TABLE dc_stock_daily_snapshot (
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            close REAL,
            total_mv REAL,
            circ_mv REAL,
            pe_ttm REAL,
            pb REAL,
            turnover_rate REAL,
            up_limit REAL,
            down_limit REAL,
            is_suspended INTEGER NOT NULL DEFAULT 0,
            data_quality_score REAL NOT NULL DEFAULT 0,
            quality_flags_json TEXT NOT NULL,
            source_code TEXT NOT NULL,
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ts_code, trade_date)
        );
CREATE TABLE dc_stock_master (
            ts_code TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            market TEXT NOT NULL DEFAULT '',
            list_date TEXT NOT NULL DEFAULT '',
            source_code TEXT NOT NULL,
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE dc_trade_calendar (
            exchange TEXT NOT NULL,
            cal_date TEXT NOT NULL,
            is_open INTEGER NOT NULL,
            pretrade_date TEXT NOT NULL DEFAULT '',
            source_code TEXT NOT NULL,
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (exchange, cal_date)
        );
CREATE TABLE dc_tushare_raw_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_name TEXT NOT NULL,
            record_key TEXT NOT NULL,
            ts_code TEXT NOT NULL DEFAULT '',
            trade_date TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            ann_date TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            source_code TEXT NOT NULL,
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (api_name, record_key)
        );
CREATE TABLE dc_tushare_failed_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_key TEXT NOT NULL UNIQUE,
            api_name TEXT NOT NULL,
            params_json TEXT NOT NULL,
            limit_rows INTEGER,
            error_category TEXT NOT NULL,
            last_error TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 1,
            first_failed_at TEXT NOT NULL,
            last_failed_at TEXT NOT NULL,
            resumed_at TEXT,
            resolved_at TEXT
        );
CREATE TABLE dc_tushare_request_coverage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_key TEXT NOT NULL UNIQUE,
            api_name TEXT NOT NULL,
            ts_code TEXT NOT NULL DEFAULT '',
            trade_date TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            params_json TEXT NOT NULL,
            rows_read INTEGER NOT NULL DEFAULT 0,
            checked_at TEXT NOT NULL
        );
CREATE TABLE dc_runtime_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL,
            logger TEXT NOT NULL,
            message TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
CREATE TABLE dc_web_factor_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(16) NOT NULL,
            ts_code VARCHAR(32) NOT NULL,
            name VARCHAR(255) NOT NULL DEFAULT '',
            industry VARCHAR(128) NOT NULL DEFAULT '',
            source_code VARCHAR(64) NOT NULL,
            source_run_key VARCHAR(128) NOT NULL,
            crawled_at VARCHAR(64) NOT NULL,
            available_time VARCHAR(64) NOT NULL,
            factors_json TEXT NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        );
CREATE TABLE dc_market_context_snapshot (
            trade_date TEXT PRIMARY KEY,
            market_state TEXT NOT NULL,
            state_confidence REAL NOT NULL DEFAULT 0,
            sentiment_score REAL NOT NULL DEFAULT 0,
            observations_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            model_version TEXT NOT NULL,
            available_time TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE TABLE tushare_api_catalog (
            api_name TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            display_name TEXT NOT NULL,
            use_case TEXT NOT NULL,
            recommended_frequency TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_probe_status TEXT NOT NULL DEFAULT 'unknown',
            last_probe_rows INTEGER NOT NULL DEFAULT 0,
            last_probe_error TEXT,
            last_probe_at TEXT,
            fields TEXT NOT NULL,
            params_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
CREATE INDEX idx_dc_announcement_ts_time ON dc_announcement_raw (ts_code, publish_time);
CREATE INDEX idx_dc_event_date ON dc_announcement_event (event_date);
CREATE INDEX idx_dc_event_type ON dc_announcement_event (event_type);
CREATE INDEX idx_dc_industry_metric_date ON dc_industry_metric_value (data_date);
CREATE INDEX idx_dc_industry_metric_industry ON dc_industry_metric_definition (industry_code);
CREATE INDEX idx_dc_quality_checked ON dc_data_quality_checks (checked_at);
CREATE INDEX idx_dc_quality_status ON dc_data_quality_checks (status);
CREATE INDEX idx_dc_raw_api_date ON dc_tushare_raw_records (api_name, trade_date);
CREATE INDEX idx_dc_raw_api_period ON dc_tushare_raw_records (api_name, period);
CREATE INDEX idx_dc_raw_ts_code ON dc_tushare_raw_records (ts_code);
CREATE INDEX idx_dc_raw_trade_date ON dc_tushare_raw_records (trade_date);
CREATE INDEX idx_dc_raw_api_period_ts ON dc_tushare_raw_records (api_name, period, ts_code);
CREATE INDEX idx_dc_raw_api_date_ts ON dc_tushare_raw_records (api_name, trade_date, ts_code);
CREATE INDEX idx_dc_tushare_failed_status ON dc_tushare_failed_requests (status, last_failed_at);
CREATE INDEX idx_dc_request_coverage_api_symbol ON dc_tushare_request_coverage (api_name,ts_code,period);
CREATE INDEX idx_dc_request_coverage_api_date ON dc_tushare_request_coverage (api_name,trade_date);
CREATE INDEX idx_dc_runtime_logs_created ON dc_runtime_logs (created_at);
CREATE INDEX idx_dc_calendar_open_date ON dc_trade_calendar (is_open, cal_date);
CREATE INDEX idx_dc_snapshot_date ON dc_stock_daily_snapshot (trade_date);
CREATE INDEX idx_dc_snapshot_symbol ON dc_stock_daily_snapshot (symbol);
CREATE INDEX idx_dc_snapshot_date_symbol ON dc_stock_daily_snapshot (trade_date, symbol);
CREATE INDEX idx_dc_market_context_state ON dc_market_context_snapshot (market_state,trade_date);
CREATE INDEX idx_dc_stock_industry ON dc_stock_master (industry);
CREATE INDEX idx_dc_stock_symbol ON dc_stock_master (symbol);
CREATE INDEX idx_dc_stock_ts_code ON dc_stock_master (ts_code);
CREATE INDEX idx_dc_web_factor_run ON dc_web_factor_snapshot (source_run_key);
CREATE INDEX idx_dc_web_factor_symbol_time ON dc_web_factor_snapshot (symbol, crawled_at);
CREATE INDEX idx_dc_runs_started ON data_ingestion_runs (started_at);
CREATE INDEX idx_dc_runs_job_started ON data_ingestion_runs (job_name, started_at);
CREATE INDEX idx_dc_announcement_publish ON dc_announcement_raw (publish_time);
"""

class Connection:
    def __init__(self, raw, *, is_postgres: bool = False):
        self.raw = raw
        self._last = None
        self._is_postgres = is_postgres
        # Keep dialect flags truthful. PostgreSQL must take the ON CONFLICT
        # branches; aliasing PostgreSQL as MySQL caused every status request
        # that seeds catalog rows to execute invalid ON DUPLICATE KEY SQL.
        self._is_mysql = False

    def execute(self, sql: str, params: tuple | None = None):
        if self._is_postgres:
            self._last = self.raw.cursor()
            self._last.execute(sql.replace("?", "%s"), params or ())
            return self._last
        self._last = self.raw.execute(sql, params or ())
        return self._last

    def executescript(self, sql: str):
        if self._is_postgres:
            for stmt in sql.split(";"):
                if stmt.strip():
                    self.execute(stmt)
            return None
        return self.raw.executescript(sql)

    def last_insert_id(self) -> int:
        if self._is_postgres:
            row = self.raw.execute("SELECT lastval()").fetchone()
            return int(row["lastval"] if isinstance(row, dict) else row[0])
        return int(self.raw.execute("SELECT last_insert_rowid()").fetchone()[0])


def _is_mysql() -> bool:
    return False


def _is_postgres() -> bool:
    # Unit tests may replace DB_PATH with a temporary file.  That explicit
    # override is the only remaining SQLite path; production defaults to PG.
    default_path = Path(__file__).resolve().parents[1] / "data" / "data-platform.db"
    if DB_PATH != default_path:
        return False
    value = os.getenv(
        "DATA_PLATFORM_DATABASE_URL",
        "postgresql://sirius@127.0.0.1:5432/alis_data",
    ).strip().lower()
    return value.startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))


@contextmanager
def _conn():
    if _is_postgres():
        import psycopg
        from psycopg.rows import dict_row
        url = os.getenv(
            "DATA_PLATFORM_DATABASE_URL",
            "postgresql://sirius@127.0.0.1:5432/alis_data",
        )
        if url.startswith("postgresql+psycopg://"):
            url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        raw = psycopg.connect(url, row_factory=dict_row)
        try:
            conn = Connection(raw, is_postgres=True)
            yield conn
            raw.commit()
        finally:
            raw.close()
        return
    if DB_PATH == Path(__file__).resolve().parents[1] / "data" / "data-platform.db":
        raise RuntimeError(
            "DATA_PLATFORM_DATABASE_URL 未配置为 PostgreSQL；SQLite 已禁用。"
        )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(DB_PATH), timeout=30)
    raw.row_factory = sqlite3.Row
    try:
        conn = Connection(raw)
        yield conn
        raw.commit()
    finally:
        raw.close()
def init_db() -> None:
    with _conn() as conn:
        if _is_postgres():
            # PostgreSQL is populated by the explicit SQLite migration tool;
            # do not silently create a second, incomplete schema at startup.
            # A copied table can contain ids far ahead of its SERIAL sequence.
            # Keep every migrated sequence aligned so inserts cannot reuse an
            # existing primary key after a restart.
            serial_tables = (
                "data_ingestion_runs",
                "dc_announcement_event",
                "dc_announcement_raw",
                "dc_data_quality_checks",
                "dc_runtime_logs",
                "dc_tushare_failed_requests",
                "dc_tushare_raw_records",
                "dc_tushare_request_coverage",
                "dc_web_factor_snapshot",
            )
            for table in serial_tables:
                conn.execute(
                    f"""SELECT setval(
                        pg_get_serial_sequence('{table}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {table}), 1),
                        EXISTS (SELECT 1 FROM {table})
                    )"""
                )
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_market_context_snapshot (
                trade_date VARCHAR(8) PRIMARY KEY, market_state VARCHAR(32) NOT NULL,
                state_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                sentiment_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                observations_json TEXT NOT NULL, result_json TEXT NOT NULL,
                model_version VARCHAR(64) NOT NULL, available_time VARCHAR(64) NOT NULL,
                created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_market_context_state ON dc_market_context_snapshot (market_state,trade_date)")
            return
        schema = SCHEMA_SQL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
        schema = schema.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        conn.executescript(schema)
