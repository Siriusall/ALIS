"""Add authoritative application runtime projections.

Revision ID: sim_0015_app_runtime
Revises: sim_0014_snapshot_compression
"""

import sqlalchemy as sa
from alembic import op

revision = "sim_0015_app_runtime"
down_revision = "sim_0014_snapshot_compression"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_runtime_projections",
        sa.Column("projection_kind", sa.String(length=32), primary_key=True),
        sa.Column("source_role", sa.String(length=32), nullable=False),
        sa.Column("source_instance", sa.String(length=160), nullable=False),
        sa.Column("observed_at", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("application_runtime_projections")
