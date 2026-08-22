"""Verify restricted runtime access to durable console snapshots."""

from alembic import op


revision = "sim_0012_snapshot_priv"
down_revision = "sim_0011_model_research_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing production tables were created by the database owner while
    # Alembic normally runs as the restricted application role. A GRANT in
    # this migration would therefore fail before it could repair that legacy
    # ownership boundary. The deployment DBA preflight grants the minimum
    # DML rights; this migration makes their absence a clear, fail-closed
    # release error instead of allowing the worker to retry indefinitely.
    op.execute(
        """DO $$
        BEGIN
            IF to_regclass('public.console_page_snapshots') IS NULL THEN
                RAISE EXCEPTION 'console_page_snapshots is missing; apply the snapshot schema migration first';
            END IF;
            IF NOT has_schema_privilege(current_user, 'public', 'USAGE') THEN
                RAISE EXCEPTION 'runtime role lacks USAGE on public schema for console snapshots';
            END IF;
            IF NOT has_table_privilege(current_user, 'public.console_page_snapshots', 'SELECT')
               OR NOT has_table_privilege(current_user, 'public.console_page_snapshots', 'INSERT')
               OR NOT has_table_privilege(current_user, 'public.console_page_snapshots', 'UPDATE') THEN
                RAISE EXCEPTION 'runtime role lacks SELECT, INSERT, or UPDATE on console_page_snapshots';
            END IF;
        END $$"""
    )


def downgrade() -> None:
    raise RuntimeError("console snapshot access is a forward-only operational contract")
