"""Teach PostgreSQL the strong API/date correlation in immutable raw data.

Current-session readers filter one API and one trade date.  The immutable
version table contains only a small set of recent dates, but the default
independent-column estimate treated each API/date pair as roughly one row
instead of the actual full-market slice (about 5,500 rows).  Under concurrent
daily/research reads that chose thousands of random projection lookups and
repeatedly crossed the statement timeout.
"""

from alembic import op


revision = "data_0014_raw_date_stats"
down_revision = "data_0013_strict_snapshot_idx"
branch_labels = None
depends_on = None


_TABLES = (
    "dc_tushare_raw_record_version",
    "dc_tushare_raw_records",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"ALTER TABLE public.{table} ALTER COLUMN api_name SET STATISTICS 1000"
        )
        op.execute(
            f"ALTER TABLE public.{table} ALTER COLUMN trade_date SET STATISTICS 1000"
        )
        op.execute(
            f"CREATE STATISTICS IF NOT EXISTS stx_{table}_api_trade_date "
            f"(dependencies, mcv, ndistinct) ON api_name, trade_date "
            f"FROM public.{table}"
        )
        op.execute(
            f"ANALYZE public.{table} (api_name, trade_date)"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"DROP STATISTICS IF EXISTS public.stx_{table}_api_trade_date"
        )
        op.execute(
            f"ALTER TABLE public.{table} ALTER COLUMN api_name SET STATISTICS -1"
        )
        op.execute(
            f"ALTER TABLE public.{table} ALTER COLUMN trade_date SET STATISTICS -1"
        )
