"""Ensure candidate ledger tables after an interrupted revision update."""

from alembic import op


revision = "sim_0006_candidate_ledger_repair"
down_revision = "sim_0005_candidate_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Re-declare the idempotent structure so an instance whose Alembic marker
    # advanced during an interrupted 0005 attempt cannot remain falsely green.
    from migrations.sim.versions.sim_0005_candidate_simulation_ledger import upgrade as ensure_ledger

    ensure_ledger()
    connection = op.get_bind()
    required = {
        "candidate_simulation_accounts",
        "candidate_simulation_daily_equity",
        "candidate_simulation_decisions",
        "candidate_simulation_operations",
        "candidate_simulation_blocked_events",
    }
    rows = connection.exec_driver_sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'candidate_simulation_%%'"
    ).fetchall()
    present = {str(row[0]) for row in rows}
    missing = sorted(required - present)
    if missing:
        raise RuntimeError("candidate simulation ledger tables missing after migration: " + ",".join(missing))


def downgrade() -> None:
    raise RuntimeError("Candidate ledger repair downgrade requires an explicit forward migration")
