"""Move queued model-autonomy tasks off the control-plane worker."""

from alembic import op


revision = "sim_0011_model_research_route"
down_revision = "sim_0010_snapshot_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only work that has not started may be rerouted.  Running tasks retain
    # their lease/worker ownership, and terminal rows remain historical
    # evidence.  The conditional update is idempotent for rolling releases.
    op.execute(
        "UPDATE evolution_tasks SET worker_pool='research' "
        "WHERE task_type='plan_model_autonomy' "
        "AND status IN ('queued','retry_wait')"
    )


def downgrade() -> None:
    raise RuntimeError("model autonomy research routing is a forward-only operational contract")
