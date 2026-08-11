"""Cache PIT industry financial proxies by immutable source signature."""

from alembic import op


revision = "data_0007_industry_cache"
down_revision = "data_0006_futures_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_industry_financial_proxy_cache (
        source_signature VARCHAR(64) PRIMARY KEY,
        as_of_time VARCHAR(64) NOT NULL,
        proxies_json TEXT NOT NULL,
        industry_count INTEGER NOT NULL DEFAULT 0,
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL)""")


def downgrade() -> None:
    raise RuntimeError("Industry financial proxy cache downgrade requires a forward migration")
