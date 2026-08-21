"""Index strict accepted snapshots for the hot usable-session probe.

The availability probe starts from the compact trade-date index and asks for
one accepted snapshot with immutable evidence.  Without this partial index,
PostgreSQL scans thousands of rows for every historical date in the 7 GB
snapshot projection.  The index contains only auditable current projections
and is built concurrently so production ingestion is not blocked.
"""

from alembic import op


revision = "data_0013_strict_snapshot_idx"
down_revision = "data_0012_large_table_autovacuum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """CREATE INDEX CONCURRENTLY IF NOT EXISTS
                   idx_dc_snapshot_strict_date_hash
                   ON public.dc_stock_daily_snapshot
                   (trade_date, ts_code, content_hash)
                 WHERE observed_at<>''
                   AND content_hash<>''
                   AND LOWER(COALESCE(observation_provenance,'')) NOT LIKE 'legacy_%'"""
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS public.idx_dc_snapshot_strict_date_hash"
        )
