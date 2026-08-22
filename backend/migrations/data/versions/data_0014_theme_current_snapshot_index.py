"""Index the bounded current theme materialization predicate."""

from alembic import op


revision = "data_0014_theme_current_idx"
down_revision = "data_0013_strict_snapshot_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """CREATE INDEX CONCURRENTLY IF NOT EXISTS
                   idx_dc_snapshot_theme_current_date_code
                   ON public.dc_stock_daily_snapshot (trade_date, ts_code)
                 WHERE industry<>'' AND close IS NOT NULL
                   AND observed_at<>'' AND first_observed_at<>''
                   AND economic_available_time<>'' AND available_time<>''
                   AND updated_at<>''"""
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS public.idx_dc_snapshot_theme_current_date_code")
