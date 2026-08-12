"""Add raw-slice timing evidence to the OOS hard API daily statistics."""

from alembic import op


revision = "data_0010_oos_api_timing"
down_revision = "data_0009_oos_api_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dc_oos_hard_api_stats_live ADD COLUMN IF NOT EXISTS raw_rows BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE dc_oos_hard_api_stats_live ADD COLUMN IF NOT EXISTS first_available_time VARCHAR(64) NOT NULL DEFAULT ''")


def downgrade() -> None:
    raise RuntimeError("OOS hard API timing evidence requires a forward migration")
