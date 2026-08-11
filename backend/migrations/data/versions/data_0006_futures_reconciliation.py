"""Persist independent futures source reconciliation evidence."""

from alembic import op


revision = "data_0006_futures_reconcile"
down_revision = "data_0005_futures_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_futures_source_reconciliation (
        root_contract TEXT NOT NULL,trade_date TEXT NOT NULL,primary_provider TEXT NOT NULL,
        comparison_provider TEXT NOT NULL,primary_price REAL NOT NULL,comparison_price REAL NOT NULL,
        relative_diff REAL NOT NULL,status TEXT NOT NULL,evidence_json TEXT NOT NULL,checked_at TEXT NOT NULL,
        PRIMARY KEY(root_contract,trade_date,primary_provider,comparison_provider))""")


def downgrade() -> None:
    raise RuntimeError("Futures reconciliation downgrade requires a forward migration")
