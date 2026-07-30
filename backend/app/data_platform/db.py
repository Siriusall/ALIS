from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.core.project_env import load_project_env

load_project_env()

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
CREATE TABLE dc_announcement_label (
            source_code TEXT NOT NULL,
            announcement_id TEXT NOT NULL,
            label_version TEXT NOT NULL DEFAULT 'v1',
            is_event INTEGER NOT NULL DEFAULT 1,
            event_type TEXT NOT NULL DEFAULT '',
            impact_direction TEXT NOT NULL DEFAULT 'neutral',
            severity TEXT NOT NULL DEFAULT 'medium',
            annotator TEXT NOT NULL DEFAULT '',
            labelled_at TEXT NOT NULL,
            evidence_note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_code, announcement_id, label_version)
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
            change_pct REAL,
            volume REAL,
            turnover REAL,
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
CREATE TABLE dc_available_trade_dates (
            trade_date TEXT PRIMARY KEY,
            source_code TEXT NOT NULL DEFAULT '',
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
CREATE TABLE dc_etf_pcf_header (
            exchange TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            fund_name TEXT NOT NULL DEFAULT '',
            fund_manager TEXT NOT NULL DEFAULT '',
            index_code TEXT NOT NULL DEFAULT '',
            creation_redemption_unit REAL,
            component_count INTEGER NOT NULL DEFAULT 0,
            publish_iopv INTEGER NOT NULL DEFAULT 0,
            source_url TEXT NOT NULL,
            source_code TEXT NOT NULL,
            raw_text TEXT NOT NULL DEFAULT '',
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (exchange, etf_code, trade_date)
        );
CREATE TABLE dc_etf_pcf_component (
            exchange TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            component_code TEXT NOT NULL,
            component_name TEXT NOT NULL DEFAULT '',
            quantity REAL,
            substitution_flag TEXT NOT NULL DEFAULT '',
            creation_premium_rate REAL,
            redemption_discount_rate REAL,
            substitution_cash_amount REAL,
            listing_market TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            source_code TEXT NOT NULL,
            available_time TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (exchange, etf_code, trade_date, component_code)
        );
CREATE TABLE dc_cold_archive_manifest (
            archive_id TEXT PRIMARY KEY,
            dataset TEXT NOT NULL,
            api_names_json TEXT NOT NULL,
            date_start TEXT NOT NULL DEFAULT '',
            date_end TEXT NOT NULL DEFAULT '',
            row_count INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL
        );
CREATE INDEX idx_dc_cold_archive_dataset_created ON dc_cold_archive_manifest (dataset, created_at);
CREATE TABLE dc_asset_watermark (
            asset_key TEXT NOT NULL,
            slice_key TEXT NOT NULL DEFAULT 'overall',
            contract_tier TEXT NOT NULL DEFAULT 'core',
            expected_count INTEGER NOT NULL DEFAULT 0,
            available_count INTEGER NOT NULL DEFAULT 0,
            missing_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'unknown',
            terminal_reason TEXT NOT NULL DEFAULT '',
            repair_priority INTEGER NOT NULL DEFAULT 50,
            source_code TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            checked_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(asset_key,slice_key)
        );
CREATE INDEX idx_dc_asset_watermark_status_priority ON dc_asset_watermark (status,repair_priority,asset_key);
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
            result_status TEXT NOT NULL DEFAULT 'success',
            result_reason TEXT NOT NULL DEFAULT '',
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
CREATE TABLE dc_data_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_name TEXT NOT NULL,
            record_key TEXT NOT NULL,
            ts_code TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            quarantined_at TEXT NOT NULL,
            UNIQUE (api_name, record_key)
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
CREATE TABLE dc_app_state (
            state_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
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
CREATE INDEX idx_dc_raw_api_date_available ON dc_tushare_raw_records (api_name, trade_date) WHERE available_time<>'';
CREATE INDEX idx_dc_raw_api_period ON dc_tushare_raw_records (api_name, period);
CREATE INDEX idx_dc_raw_ts_code ON dc_tushare_raw_records (ts_code);
CREATE INDEX idx_dc_raw_trade_date ON dc_tushare_raw_records (trade_date);
CREATE INDEX idx_dc_raw_api_period_ts ON dc_tushare_raw_records (api_name, period, ts_code);
CREATE INDEX idx_dc_raw_api_date_ts ON dc_tushare_raw_records (api_name, trade_date, ts_code);
CREATE INDEX idx_dc_raw_api_available ON dc_tushare_raw_records (api_name, available_time, id);
CREATE INDEX idx_dc_etf_pcf_date ON dc_etf_pcf_header (trade_date, exchange);
CREATE INDEX idx_dc_etf_pcf_component_date ON dc_etf_pcf_component (trade_date, etf_code);
CREATE INDEX idx_dc_raw_api_ts_available ON dc_tushare_raw_records (api_name, ts_code, available_time, id);
CREATE INDEX idx_dc_tushare_failed_status ON dc_tushare_failed_requests (status, last_failed_at);
CREATE INDEX idx_dc_request_coverage_api_symbol ON dc_tushare_request_coverage (api_name,ts_code,period);
CREATE INDEX idx_dc_request_coverage_api_date ON dc_tushare_request_coverage (api_name,trade_date);
CREATE INDEX idx_dc_runtime_logs_created ON dc_runtime_logs (created_at);
CREATE INDEX idx_dc_calendar_open_date ON dc_trade_calendar (is_open, cal_date);
CREATE INDEX idx_dc_available_trade_dates_updated ON dc_available_trade_dates (updated_at);
CREATE INDEX idx_dc_snapshot_date ON dc_stock_daily_snapshot (trade_date);
CREATE INDEX idx_dc_snapshot_symbol ON dc_stock_daily_snapshot (symbol);
CREATE INDEX idx_dc_snapshot_date_symbol ON dc_stock_daily_snapshot (trade_date, symbol);
CREATE INDEX idx_dc_market_context_state ON dc_market_context_snapshot (market_state,trade_date);
CREATE INDEX idx_dc_app_state_updated ON dc_app_state (updated_at);
CREATE INDEX idx_dc_stock_industry ON dc_stock_master (industry);
CREATE INDEX idx_dc_stock_symbol ON dc_stock_master (symbol);
CREATE INDEX idx_dc_stock_ts_code ON dc_stock_master (ts_code);
CREATE INDEX idx_dc_web_factor_run ON dc_web_factor_snapshot (source_run_key);
CREATE INDEX idx_dc_web_factor_symbol_time ON dc_web_factor_snapshot (symbol, crawled_at);
CREATE INDEX idx_dc_runs_started ON data_ingestion_runs (started_at);
CREATE INDEX idx_dc_runs_job_started ON data_ingestion_runs (job_name, started_at);
CREATE INDEX idx_dc_announcement_publish ON dc_announcement_raw (publish_time);
CREATE INDEX idx_dc_announcement_label_version ON dc_announcement_label (label_version, labelled_at);
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
        "postgresql://alis_app@127.0.0.1:5432/alis_data",
    ).strip().lower()
    return value.startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))


@contextmanager
def _conn():
    if _is_postgres():
        import psycopg
        from psycopg.rows import dict_row
        url = os.getenv(
            "DATA_PLATFORM_DATABASE_URL",
            "postgresql://alis_app@127.0.0.1:5432/alis_data",
        )
        if url.startswith("postgresql+psycopg://"):
            url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        raw = psycopg.connect(
            url,
            row_factory=dict_row,
            connect_timeout=int(os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "5")),
        )
        try:
            raw.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (os.getenv("DATABASE_STATEMENT_TIMEOUT_MS", "30000"),),
            )
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
            schema = SCHEMA_SQL.replace(
                "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"
            )
            schema = schema.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
            schema = schema.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
            conn.executescript(schema)
            # Keep migrated sequences aligned so inserts cannot reuse an id.
            serial_tables = (
                "data_ingestion_runs",
                "dc_announcement_event",
                "dc_announcement_raw",
                "dc_data_quality_checks",
                "dc_data_quarantine",
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
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_available_trade_dates (
                trade_date VARCHAR(8) PRIMARY KEY,
                source_code VARCHAR(64) NOT NULL DEFAULT '',
                available_time VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_available_trade_dates_updated ON dc_available_trade_dates (updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_market_context_state ON dc_market_context_snapshot (market_state,trade_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_snapshot_ts_date_bars ON dc_stock_daily_snapshot (ts_code,trade_date) INCLUDE (close,volume,turnover)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_snapshot_ts_available_date ON dc_stock_daily_snapshot (ts_code,available_time,trade_date DESC) INCLUDE (pe_ttm)")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_industry_state_snapshot (
                as_of_date VARCHAR(8) NOT NULL, industry_name VARCHAR(255) NOT NULL,
                state_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                model_version VARCHAR(64) NOT NULL, available_time VARCHAR(64) NOT NULL,
                created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                PRIMARY KEY(as_of_date,industry_name))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_industry_state_name_date ON dc_industry_state_snapshot(industry_name,as_of_date)")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_theme_snapshot (
                as_of_date VARCHAR(8) NOT NULL, theme_code VARCHAR(255) NOT NULL,
                theme_name VARCHAR(255) NOT NULL, lifecycle VARCHAR(32) NOT NULL,
                score DOUBLE PRECISION NOT NULL DEFAULT 0, promotable INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL, model_version VARCHAR(64) NOT NULL,
                available_time VARCHAR(64) NOT NULL, created_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL, PRIMARY KEY(as_of_date,theme_code))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_theme_company_mapping (
                as_of_date VARCHAR(8) NOT NULL, theme_code VARCHAR(255) NOT NULL,
                ts_code VARCHAR(16) NOT NULL, exposure_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL, updated_at VARCHAR(64) NOT NULL,
                PRIMARY KEY(as_of_date,theme_code,ts_code))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_theme_score_date ON dc_theme_snapshot(promotable,score,as_of_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_theme_company_symbol ON dc_theme_company_mapping(ts_code,as_of_date)")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_event_followup (
                event_key VARCHAR(128) PRIMARY KEY, ts_code VARCHAR(16) NOT NULL DEFAULT '',
                event_type VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL,
                next_followup_date VARCHAR(16), next_action TEXT NOT NULL,
                thesis_id BIGINT, evidence_json TEXT NOT NULL,
                intervention_actor VARCHAR(128) NOT NULL DEFAULT '',
                created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_event_followup_status_date ON dc_event_followup(status,next_followup_date)")
            conn.execute("""CREATE TABLE IF NOT EXISTS model_training_history (
                feature_date VARCHAR(8) PRIMARY KEY, feature_set VARCHAR(64) NOT NULL,
                features_json TEXT NOT NULL, source_as_of_time VARCHAR(64) NOT NULL,
                created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_model_training_feature_set_date ON model_training_history(feature_set,feature_date)")
            for column in ("change_pct", "volume", "turnover"):
                conn.execute(f"ALTER TABLE dc_stock_daily_snapshot ADD COLUMN IF NOT EXISTS {column} DOUBLE PRECISION")
            conn.execute("ALTER TABLE dc_tushare_request_coverage ADD COLUMN IF NOT EXISTS result_status VARCHAR(32) NOT NULL DEFAULT 'success'")
            conn.execute("ALTER TABLE dc_tushare_request_coverage ADD COLUMN IF NOT EXISTS result_reason TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_request_coverage_result ON dc_tushare_request_coverage (result_status,api_name)")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_cold_archive_manifest (
                archive_id VARCHAR(96) PRIMARY KEY, dataset VARCHAR(64) NOT NULL,
                api_names_json TEXT NOT NULL, date_start VARCHAR(16) NOT NULL DEFAULT '',
                date_end VARCHAR(16) NOT NULL DEFAULT '', row_count BIGINT NOT NULL,
                file_path TEXT NOT NULL, file_sha256 VARCHAR(128) NOT NULL,
                schema_json TEXT NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'completed',
                created_at VARCHAR(64) NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_cold_archive_dataset_created ON dc_cold_archive_manifest (dataset,created_at)")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_asset_watermark (
                asset_key VARCHAR(128) NOT NULL, slice_key VARCHAR(128) NOT NULL DEFAULT 'overall',
                contract_tier VARCHAR(32) NOT NULL DEFAULT 'core', expected_count BIGINT NOT NULL DEFAULT 0,
                available_count BIGINT NOT NULL DEFAULT 0, missing_count BIGINT NOT NULL DEFAULT 0,
                status VARCHAR(32) NOT NULL DEFAULT 'unknown', terminal_reason TEXT NOT NULL DEFAULT '',
                repair_priority INTEGER NOT NULL DEFAULT 50, source_code VARCHAR(128) NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}', checked_at VARCHAR(64) NOT NULL,
                updated_at VARCHAR(64) NOT NULL, PRIMARY KEY(asset_key,slice_key))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_asset_watermark_status_priority ON dc_asset_watermark(status,repair_priority,asset_key)")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_etf_pcf_header (
                exchange VARCHAR(8) NOT NULL, etf_code VARCHAR(16) NOT NULL, trade_date VARCHAR(8) NOT NULL,
                fund_name TEXT NOT NULL DEFAULT '', fund_manager TEXT NOT NULL DEFAULT '', index_code VARCHAR(32) NOT NULL DEFAULT '',
                creation_redemption_unit DOUBLE PRECISION, component_count INTEGER NOT NULL DEFAULT 0,
                publish_iopv INTEGER NOT NULL DEFAULT 0, source_url TEXT NOT NULL, source_code VARCHAR(64) NOT NULL,
                raw_text TEXT NOT NULL DEFAULT '', available_time VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                PRIMARY KEY(exchange,etf_code,trade_date))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS dc_etf_pcf_component (
                exchange VARCHAR(8) NOT NULL, etf_code VARCHAR(16) NOT NULL, trade_date VARCHAR(8) NOT NULL,
                component_code VARCHAR(32) NOT NULL, component_name TEXT NOT NULL DEFAULT '', quantity DOUBLE PRECISION,
                substitution_flag VARCHAR(32) NOT NULL DEFAULT '', creation_premium_rate DOUBLE PRECISION,
                redemption_discount_rate DOUBLE PRECISION, substitution_cash_amount DOUBLE PRECISION,
                listing_market VARCHAR(32) NOT NULL DEFAULT '', payload_json TEXT NOT NULL,
                source_code VARCHAR(64) NOT NULL, available_time VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                PRIMARY KEY(exchange,etf_code,trade_date,component_code))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_etf_pcf_date ON dc_etf_pcf_header(trade_date,exchange)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_etf_pcf_component_date ON dc_etf_pcf_component(trade_date,etf_code)")
            return
        schema = SCHEMA_SQL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
        schema = schema.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        conn.executescript(schema)


def database_health() -> dict[str, Any]:
    """Run a cheap readiness probe against the data-platform database."""
    try:
        with _conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "engine": "postgresql"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:200]}
