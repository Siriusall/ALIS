"""Add immutable observation-clock evidence for raw and normalized market data.

The legacy ``available_time`` column is retained for compatibility, but new
rows must use the actual observation timestamp there as well as in the
explicit ``observed_at`` column.  Historical rows are deliberately marked
``legacy_unknown`` and therefore cannot pass strict PIT gates.
"""

from alembic import op


revision = "data_0011_pit_observation_clock"
down_revision = "data_0010_oos_api_timing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("dc_tushare_raw_records", "dc_stock_daily_snapshot"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS economic_available_time VARCHAR(64) NOT NULL DEFAULT ''")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS observed_at VARCHAR(64) NOT NULL DEFAULT ''")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS first_observed_at VARCHAR(64) NOT NULL DEFAULT ''")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS observation_provenance VARCHAR(128) NOT NULL DEFAULT 'legacy_unknown'")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128) NOT NULL DEFAULT ''")

    op.execute("""CREATE TABLE IF NOT EXISTS dc_tushare_raw_record_version (
        id BIGSERIAL PRIMARY KEY,
        api_name VARCHAR(64) NOT NULL,
        record_key VARCHAR(255) NOT NULL,
        ts_code VARCHAR(32) NOT NULL DEFAULT '',
        trade_date VARCHAR(16) NOT NULL DEFAULT '',
        period VARCHAR(32) NOT NULL DEFAULT '',
        ann_date VARCHAR(16) NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        source_code VARCHAR(128) NOT NULL,
        economic_available_time VARCHAR(64) NOT NULL DEFAULT '',
        observed_at VARCHAR(64) NOT NULL DEFAULT '',
        first_observed_at VARCHAR(64) NOT NULL DEFAULT '',
        observation_provenance VARCHAR(128) NOT NULL DEFAULT '',
        content_hash VARCHAR(128) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        UNIQUE (api_name, record_key, content_hash))""")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_stock_daily_snapshot_version (
        id BIGSERIAL PRIMARY KEY,
        ts_code VARCHAR(32) NOT NULL,
        trade_date VARCHAR(16) NOT NULL,
        payload_json TEXT NOT NULL,
        source_code VARCHAR(128) NOT NULL,
        economic_available_time VARCHAR(64) NOT NULL DEFAULT '',
        observed_at VARCHAR(64) NOT NULL DEFAULT '',
        first_observed_at VARCHAR(64) NOT NULL DEFAULT '',
        observation_provenance VARCHAR(128) NOT NULL DEFAULT '',
        content_hash VARCHAR(128) NOT NULL,
        created_at VARCHAR(64) NOT NULL,
        UNIQUE (ts_code, trade_date, content_hash))""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dc_raw_version_pit ON dc_tushare_raw_record_version(api_name,record_key,observed_at,id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dc_raw_version_date ON dc_tushare_raw_record_version(api_name,trade_date,observed_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dc_snapshot_version_pit ON dc_stock_daily_snapshot_version(ts_code,trade_date,observed_at,id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dc_snapshot_version_date ON dc_stock_daily_snapshot_version(trade_date,observed_at)")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_pit_snapshot_stats_live (
        trade_date VARCHAR(16) PRIMARY KEY,
        current_rows BIGINT NOT NULL DEFAULT 0,
        current_market_rows BIGINT NOT NULL DEFAULT 0,
        observation_rows BIGINT NOT NULL DEFAULT 0,
        point_in_time_rows BIGINT NOT NULL DEFAULT 0,
        min_observed_at VARCHAR(64) NOT NULL DEFAULT '',
        max_observed_at VARCHAR(64) NOT NULL DEFAULT '',
        updated_at VARCHAR(64) NOT NULL)""")
    # All pre-clock OOS summaries are only economic-time estimates. Reset
    # their PIT count so no retained T15 value can survive as strict proof.
    op.execute("UPDATE dc_oos_hard_api_stats_live SET point_in_time_rows=0, first_available_time=''" )


def downgrade() -> None:
    raise RuntimeError("Observation-clock evidence is append-only and cannot be downgraded")
