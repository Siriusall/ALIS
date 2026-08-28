"""Add performance indices and runtime log retention maintenance.

Revision ID: data_0016_perf_indices
Revises: data_0015_churn_maint
"""

from alembic import op

revision = "data_0016_perf_indices"
down_revision = "data_0015_churn_maint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dc_runs_status_started ON data_ingestion_runs(status, started_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dc_runs_job_status ON data_ingestion_runs(job_name, status, started_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dc_runs_job_status;")
    op.execute("DROP INDEX IF EXISTS idx_dc_runs_status_started;")
