"""Keep the large point-in-time factor table statistically fresh.

``factor_values`` is updated in daily batches and is large enough that the
default PostgreSQL churn thresholds can leave stale statistics for days.
Bounded maintenance keeps research plans predictable without changing any
factor values or query semantics.
"""

from alembic import op


revision = "sim_0013_factor_values_maint"
down_revision = "sim_0012_snapshot_priv"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.factor_values SET ("
        "autovacuum_vacuum_scale_factor = 0.005, "
        "autovacuum_vacuum_threshold = 10000, "
        "autovacuum_analyze_scale_factor = 0.001, "
        "autovacuum_analyze_threshold = 10000"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.factor_values RESET ("
        "autovacuum_vacuum_scale_factor, "
        "autovacuum_vacuum_threshold, "
        "autovacuum_analyze_scale_factor, "
        "autovacuum_analyze_threshold"
        ")"
    )
