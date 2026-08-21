"""Keep the two large data-platform projections on a practical vacuum cadence.

The raw projection and normalized daily snapshot are intentionally append-only
evidence consumers with overwrite projections.  Their update churn is much
higher than PostgreSQL's default 20% vacuum / 10% analyze scale factors can
handle on a small shared database node, leaving stale planner statistics and
millions of dead tuples between maintenance runs.
"""

from alembic import op


revision = "data_0012_large_table_autovacuum"
down_revision = "data_0011_pit_observation_clock"
branch_labels = None
depends_on = None


_LARGE_PROJECTIONS = (
    "dc_tushare_raw_records",
    "dc_stock_daily_snapshot",
)


def upgrade() -> None:
    for table in _LARGE_PROJECTIONS:
        op.execute(
            f"ALTER TABLE public.{table} SET ("
            "autovacuum_vacuum_scale_factor = 0.05, "
            "autovacuum_vacuum_threshold = 50000, "
            "autovacuum_analyze_scale_factor = 0.01, "
            "autovacuum_analyze_threshold = 50000"
            ")"
        )


def downgrade() -> None:
    for table in _LARGE_PROJECTIONS:
        op.execute(
            f"ALTER TABLE public.{table} RESET ("
            "autovacuum_vacuum_scale_factor, "
            "autovacuum_vacuum_threshold, "
            "autovacuum_analyze_scale_factor, "
            "autovacuum_analyze_threshold"
            ")"
        )
