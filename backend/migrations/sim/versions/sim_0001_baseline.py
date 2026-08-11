"""Existing ALIS simulation schema baseline."""

revision = "sim_0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing installations are stamped here after the legacy schema audit.
    pass


def downgrade() -> None:
    raise RuntimeError("ALIS baseline downgrade is intentionally unsupported")
