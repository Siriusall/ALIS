"""Persist point-in-time coverage of the permanent OOS hard API slices."""

from alembic import op


revision = "data_0009_oos_api_stats"
down_revision = "data_0008_snapshot_stats_live"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_oos_hard_api_stats_live (
        api_name VARCHAR(64) NOT NULL,
        trade_date VARCHAR(8) NOT NULL,
        point_in_time_rows BIGINT NOT NULL DEFAULT 0,
        updated_at VARCHAR(64) NOT NULL,
        PRIMARY KEY (api_name, trade_date))""")


def downgrade() -> None:
    raise RuntimeError("OOS hard API statistics require a forward migration")
