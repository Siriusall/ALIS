"""Incremental theme exposure and bounded research-source discovery caches."""

from alembic import op


revision = "data_0004_research_sources"
down_revision = "data_0003_market_provider_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_theme_revenue_exposure (
        as_of_date TEXT NOT NULL,theme_code TEXT NOT NULL,ts_code TEXT NOT NULL,
        revenue_share REAL NOT NULL,matched_sales REAL NOT NULL,total_segment_sales REAL NOT NULL,
        source_period TEXT NOT NULL,source_available_time TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,
        PRIMARY KEY(as_of_date,theme_code,ts_code))""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_theme_revenue_exposure_theme ON dc_theme_revenue_exposure(theme_code,as_of_date)")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_theme_revenue_exposure_current (
        theme_code TEXT NOT NULL,ts_code TEXT NOT NULL,as_of_date TEXT NOT NULL,
        revenue_share REAL NOT NULL,matched_sales REAL NOT NULL,total_segment_sales REAL NOT NULL,
        source_period TEXT NOT NULL,source_available_time TEXT NOT NULL,source_fingerprint TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,
        PRIMARY KEY(theme_code,ts_code))""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_theme_revenue_current_code ON dc_theme_revenue_exposure_current(ts_code)")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_theme_revenue_exposure_runs (
        as_of_date TEXT PRIMARY KEY,status TEXT NOT NULL,source_updated_at TEXT NOT NULL DEFAULT '',
        source_id INTEGER NOT NULL DEFAULT 0,companies_examined INTEGER NOT NULL DEFAULT 0,
        companies_changed INTEGER NOT NULL DEFAULT 0,exposures_written INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL)""")
    op.execute("""CREATE INDEX IF NOT EXISTS idx_dc_raw_fina_mainbz_update_cursor
        ON dc_tushare_raw_records(updated_at,id) WHERE api_name='fina_mainbz'""")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_research_provider_probe (
        dataset TEXT NOT NULL,target_key TEXT NOT NULL,provider TEXT NOT NULL,status TEXT NOT NULL,
        probed_at TEXT NOT NULL,next_probe_at TEXT NOT NULL,latency_ms INTEGER NOT NULL DEFAULT 0,
        evidence_json TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(dataset,target_key,provider))""")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_futures_mapping_cache (
        root_contract TEXT NOT NULL,trade_date TEXT NOT NULL,mapped_contract TEXT NOT NULL,
        provider TEXT NOT NULL,available_time TEXT NOT NULL,updated_at TEXT NOT NULL,
        PRIMARY KEY(root_contract,trade_date))""")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_futures_price_cache (
        mapped_contract TEXT NOT NULL,trade_date TEXT NOT NULL,price REAL NOT NULL,
        provider TEXT NOT NULL,available_time TEXT NOT NULL,updated_at TEXT NOT NULL,
        PRIMARY KEY(mapped_contract,trade_date))""")


def downgrade() -> None:
    raise RuntimeError("Research source downgrade requires an explicit forward migration")
