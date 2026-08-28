"""Add compressed durable console snapshot payloads.

Revision ID: sim_0014_snapshot_compression
Revises: sim_0013_factor_values_maint
"""

import sqlalchemy as sa
from alembic import op

revision = "sim_0014_snapshot_compression"
down_revision = "sim_0013_factor_values_maint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("console_page_snapshots", sa.Column("payload_gzip", sa.LargeBinary(), nullable=True))
    op.add_column(
        "console_page_snapshots",
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "console_page_snapshots",
        sa.Column("payload_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "console_page_snapshots",
        sa.Column("compressed_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE console_page_snapshots "
        "SET payload_size_bytes=octet_length(convert_to(payload_json, 'UTF8'))"
    )


def downgrade() -> None:
    op.drop_column("console_page_snapshots", "compressed_size_bytes")
    op.drop_column("console_page_snapshots", "payload_size_bytes")
    op.drop_column("console_page_snapshots", "content_hash")
    op.drop_column("console_page_snapshots", "payload_gzip")
