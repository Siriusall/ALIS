"""Cross-process leases for bounded autonomous steps."""

from alembic import op


revision = "sim_0003_autonomy_step_lease"
down_revision = "sim_0002_release_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS autonomy_step_leases (
        step_name VARCHAR(160) PRIMARY KEY,
        lease_token VARCHAR(64) NOT NULL,
        lease_owner VARCHAR(255) NOT NULL,
        acquired_at VARCHAR(64) NOT NULL,
        expires_at VARCHAR(64) NOT NULL)""")


def downgrade() -> None:
    raise RuntimeError("Autonomy lease downgrade requires an explicit forward migration")
