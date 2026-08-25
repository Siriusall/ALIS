"""Keep high-churn data-platform tables analyzed before plans drift.

The raw table has tens of millions of rows.  A five-percent autovacuum
threshold therefore waits for millions of dead rows, while the shared 4 GB
database is already sensitive to stale row-count and date distributions.
Use a bounded threshold for the tables that are updated by every daily run.
"""

from alembic import op


revision = "data_0015_churn_maint"
down_revision = "data_0014_raw_date_stats"
branch_labels = None
depends_on = None


_CHURN_TABLES = (
    "dc_tushare_raw_records",
    "dc_stock_daily_snapshot",
    "dc_theme_company_mapping",
    "dc_theme_snapshot",
    "dc_tushare_request_coverage",
    "data_ingestion_runs",
)


def upgrade() -> None:
    for table in _CHURN_TABLES:
        op.execute(
            f"ALTER TABLE public.{table} SET ("
            "autovacuum_vacuum_scale_factor = 0.005, "
            "autovacuum_vacuum_threshold = 10000, "
            "autovacuum_analyze_scale_factor = 0.001, "
            "autovacuum_analyze_threshold = 10000"
            ")"
        )


def downgrade() -> None:
    for table in _CHURN_TABLES:
        op.execute(
            f"ALTER TABLE public.{table} RESET ("
            "autovacuum_vacuum_scale_factor, "
            "autovacuum_vacuum_threshold, "
            "autovacuum_analyze_scale_factor, "
            "autovacuum_analyze_threshold"
            ")"
        )
