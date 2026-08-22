"""Index accepted raw evidence joins used by decision and console readers."""

from alembic import op


revision = "data_0014_raw_accept_idx"
down_revision = "data_0013_strict_snapshot_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The accepted projection is filtered by market date before its exact
    # immutable content-hash join. Build this online so ingestion continues.
    with op.get_context().autocommit_block():
        op.execute(
            """CREATE INDEX CONCURRENTLY IF NOT EXISTS
                   idx_dc_raw_accepted_date_join
                   ON public.dc_tushare_raw_records
                   (trade_date, api_name, record_key, content_hash)
                 WHERE trade_date<>'' AND content_hash<>''"""
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS public.idx_dc_raw_accepted_date_join"
        )
