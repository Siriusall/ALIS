"""Bound publication probes to one active row per worker pool."""

from alembic import op


revision = "sim_0017_pub_probe_unique"
down_revision = "sim_0016_snapshot_worker_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep one winner per pool before installing the database-level guard.
    # Running work wins, then retry_wait, then the newest queued row.  All
    # losers remain as cancelled audit records; no task row is deleted.
    op.execute(
        """WITH ranked AS (
               SELECT id,
                      ROW_NUMBER() OVER (
                          PARTITION BY worker_pool
                          ORDER BY CASE status
                              WHEN 'running' THEN 1
                              WHEN 'retry_wait' THEN 2
                              WHEN 'queued' THEN 3
                              ELSE 4
                          END, id DESC
                      ) AS rn
                 FROM evolution_tasks
                WHERE task_type='research_data_publication_probe'
                  AND status IN ('queued','running','retry_wait')
             )
             UPDATE evolution_tasks
                SET status='cancelled', phase='superseded', progress=1,
                    message='历史重复 publication probe 已由 sim_0017 隔离',
                    stop_requested=1, finished_at=CURRENT_TIMESTAMP,
                    lease_owner=NULL, lease_expires_at=NULL,
                    heartbeat_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
              WHERE id IN (SELECT id FROM ranked WHERE rn > 1)"""
    )
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS
            uq_evolution_tasks_active_publication_probe_pool
           ON evolution_tasks (task_type, worker_pool)
         WHERE task_type='research_data_publication_probe'
           AND status IN ('queued','running','retry_wait')"""
    )


def downgrade() -> None:
    raise RuntimeError("publication probe uniqueness is a forward-only operational contract")
