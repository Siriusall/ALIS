"""Persist the database-local console snapshot worker runtime projection.

Revision ID: sim_0016_snapshot_worker_runtime
Revises: sim_0015_app_runtime
"""

import sqlalchemy as sa
from alembic import op


revision = "sim_0016_snapshot_worker_runtime"
down_revision = "sim_0015_app_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "console_snapshot_worker_runtime",
        sa.Column("worker_key", sa.String(length=64), primary_key=True),
        sa.Column("worker_id", sa.String(length=160), nullable=False),
        sa.Column("node_name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.String(length=64), nullable=False),
        sa.Column("heartbeat_at", sa.String(length=64), nullable=False),
        sa.Column("cycle_started_at", sa.String(length=64), nullable=True),
        sa.Column("cycle_completed_at", sa.String(length=64), nullable=True),
        sa.Column("current_page", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("last_success_page", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("last_success_at", sa.String(length=64), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("last_error_at", sa.String(length=64), nullable=True),
        sa.Column("pages_completed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.String(length=64), nullable=False),
    )
    op.create_index(
        "idx_console_snapshot_worker_runtime_heartbeat",
        "console_snapshot_worker_runtime",
        ["heartbeat_at"],
    )
    # The worker runs with the restricted runtime role.  Fail the release
    # migration immediately when the DBA has not granted its DML rights;
    # otherwise a disabled-looking heartbeat would be indistinguishable from
    # a crashed process and the worker would retry forever.
    op.execute(
        """DO $$
        BEGIN
            IF NOT has_schema_privilege(current_user, 'public', 'USAGE') THEN
                RAISE EXCEPTION 'runtime role lacks USAGE on public schema for snapshot worker runtime';
            END IF;
            IF NOT has_table_privilege(current_user, 'public.console_snapshot_worker_runtime', 'SELECT')
               OR NOT has_table_privilege(current_user, 'public.console_snapshot_worker_runtime', 'INSERT')
               OR NOT has_table_privilege(current_user, 'public.console_snapshot_worker_runtime', 'UPDATE') THEN
                RAISE EXCEPTION 'runtime role lacks SELECT, INSERT, or UPDATE on console_snapshot_worker_runtime';
            END IF;
        END $$"""
    )


def downgrade() -> None:
    raise RuntimeError("console snapshot worker runtime is a forward-only operational contract")
