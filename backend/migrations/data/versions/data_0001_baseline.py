"""Existing ALIS data-platform schema baseline."""

revision = "data_0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("ALIS baseline downgrade is intentionally unsupported")
