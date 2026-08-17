"""Enforce one scheduler-owned autonomous simulation task globally."""

from alembic import op


revision = "sim_0007_autonomous_singleton"
down_revision = "sim_0006_candidate_ledger_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reconcile duplicates before creating the partial unique index.  The
    # running task wins, then a stop request, then the newest queued task;
    # every other row remains visible as stopped historical evidence.
    op.execute(
        """WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                ORDER BY CASE status
                    WHEN 'running' THEN 1
                    WHEN 'stopping' THEN 2
                    WHEN 'pending' THEN 3
                    WHEN 'paused' THEN 4
                    ELSE 5
                END, id DESC
            ) AS rn
            FROM quant_run_tasks
            WHERE replay_mode = 'autonomous_daily'
              AND status IN ('pending', 'running', 'paused', 'stopping')
        )
        UPDATE quant_run_tasks
           SET status = 'stopped',
               stop_requested = 1,
               finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP::text),
               updated_at = CURRENT_TIMESTAMP::text,
               last_message = '历史重复自治任务已由 sim_0007 隔离'
         WHERE id IN (SELECT id FROM ranked WHERE rn > 1)"""
    )
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS
            uq_quant_run_tasks_one_active_autonomous_daily
        ON quant_run_tasks (replay_mode)
        WHERE replay_mode = 'autonomous_daily'
          AND status IN ('pending', 'running', 'paused', 'stopping')"""
    )


def downgrade() -> None:
    raise RuntimeError("Autonomous singleton downgrade requires an explicit forward migration")
