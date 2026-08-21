"""Fence cross-node console snapshot rebuilds by invalidation generation."""

from alembic import op


revision = "sim_0010_snapshot_generation"
down_revision = "sim_0009_console_page_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE console_page_snapshots "
        "ADD COLUMN IF NOT EXISTS refresh_generation BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE console_page_snapshots "
        "ADD COLUMN IF NOT EXISTS last_attempt_generation BIGINT NOT NULL DEFAULT -1"
    )
    op.execute(
        "ALTER TABLE console_page_snapshots "
        "ADD COLUMN IF NOT EXISTS last_attempt_at VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE console_page_snapshots "
        "ADD COLUMN IF NOT EXISTS last_error_code VARCHAR(64) NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    raise RuntimeError("console snapshot generations are a forward-only operational contract")
