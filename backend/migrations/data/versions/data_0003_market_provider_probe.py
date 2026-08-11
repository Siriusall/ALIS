"""Persist publication probes used by the multi-source daily refresh."""

from alembic import op


revision = "data_0003_market_provider_probe"
down_revision = "data_0002_release_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_market_provider_probe (
        dataset TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        probed_at TEXT NOT NULL,
        next_probe_at TEXT NOT NULL,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(dataset,trade_date,provider))""")


def downgrade() -> None:
    raise RuntimeError("Data-platform downgrade requires an explicit forward migration")
