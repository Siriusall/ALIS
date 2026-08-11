"""Add an app-owned live generation for normalized snapshot statistics."""

from alembic import op


revision = "data_0008_snapshot_stats_live"
down_revision = "data_0007_industry_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_stock_daily_snapshot_stats_live (
        trade_date VARCHAR(8) PRIMARY KEY,
        rows_total BIGINT NOT NULL DEFAULT 0,
        market_rows BIGINT NOT NULL DEFAULT 0,
        min_available_time VARCHAR(64) NOT NULL DEFAULT '',
        max_available_time VARCHAR(64) NOT NULL DEFAULT '',
        updated_at VARCHAR(64) NOT NULL)""")


def downgrade() -> None:
    raise RuntimeError("Snapshot statistics live generation requires a forward migration")
