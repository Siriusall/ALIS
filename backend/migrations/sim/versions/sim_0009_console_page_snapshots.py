"""Persist authoritative console page snapshots."""

from alembic import op


revision = "sim_0009_console_page_snapshots"
down_revision = "sim_0008_worker_pools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS console_page_snapshots (
            page_key VARCHAR(32) PRIMARY KEY,
            version VARCHAR(64) NOT NULL,
            generated_at VARCHAR(64) NOT NULL,
            as_of VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            invalidated_at VARCHAR(64),
            payload_json TEXT NOT NULL,
            updated_at VARCHAR(64) NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_console_page_snapshots_updated "
        "ON console_page_snapshots(updated_at DESC)"
    )


def downgrade() -> None:
    raise RuntimeError("console snapshots are a forward-only operational contract")
