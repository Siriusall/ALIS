"""Route persistent evolution tasks to an explicit worker pool."""

from alembic import op


revision = "sim_0008_worker_pools"
down_revision = "sim_0007_autonomous_singleton"
branch_labels = None
depends_on = None


_RESEARCH_TASK_TYPES = (
    "strategy_evolution_cycle",
    "strategy_controller_oos",
    "factor_research_cycle",
    "strategy_candidate_generation",
    "strategy_environment_backtest",
    "strategy_cluster_distillation",
    "strategy_portfolio_research",
    "strategy_failure_model_training",
    "model_candidate_research",
    "model_strategy_impact_validation",
    "hsmm_shadow_training",
    "controller_candidate_observation",
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE evolution_tasks "
        "ADD COLUMN IF NOT EXISTS worker_pool VARCHAR(32) NOT NULL DEFAULT 'control'"
    )
    placeholders = ",".join(f"'{task_type}'" for task_type in _RESEARCH_TASK_TYPES)
    op.execute(
        "UPDATE evolution_tasks SET worker_pool='research' "
        f"WHERE task_type IN ({placeholders}) "
        "AND status IN ('queued','retry_wait')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_evolution_tasks_pool_status_next "
        "ON evolution_tasks(worker_pool,status,next_run_at,priority,created_at)"
    )


def downgrade() -> None:
    raise RuntimeError("worker pool routing is a forward-only production contract")
