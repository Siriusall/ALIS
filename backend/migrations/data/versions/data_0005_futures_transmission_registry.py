"""Persist the futures-to-industry transmission registry."""

from alembic import op


revision = "data_0005_futures_registry"
down_revision = "data_0004_research_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_futures_proxy_registry (
        proxy_key TEXT PRIMARY KEY,root_contract TEXT NOT NULL,name TEXT NOT NULL,
        revenue_industries_json TEXT NOT NULL,cost_industries_json TEXT NOT NULL,
        lag_trading_days INTEGER NOT NULL,market_role TEXT NOT NULL,
        contract_version TEXT NOT NULL,updated_at TEXT NOT NULL)""")


def downgrade() -> None:
    raise RuntimeError("Futures transmission registry downgrade requires a forward migration")
