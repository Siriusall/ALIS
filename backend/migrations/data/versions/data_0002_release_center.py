"""Data-platform revision synchronized with release center V1."""

from alembic import op

revision = "data_0002_release_center"
down_revision = "data_0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS dc_market_provider_performance (
        dataset TEXT NOT NULL, provider TEXT NOT NULL, sample_date TEXT NOT NULL,
        probe_latency_ms INTEGER NOT NULL DEFAULT 0, bulk_latency_ms INTEGER NOT NULL DEFAULT 0,
        coverage DOUBLE PRECISION NOT NULL DEFAULT 0, success INTEGER NOT NULL DEFAULT 0,
        failure_kind TEXT NOT NULL DEFAULT '', completed_at TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(dataset,provider,sample_date))""")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_market_provider_field_audit (
        dataset TEXT NOT NULL, trade_date TEXT NOT NULL, primary_provider TEXT NOT NULL,
        fallback_provider TEXT NOT NULL, field_name TEXT NOT NULL, compared_count INTEGER NOT NULL DEFAULT 0,
        mismatch_count INTEGER NOT NULL DEFAULT 0, mean_abs_diff DOUBLE PRECISION NOT NULL DEFAULT 0,
        max_abs_diff DOUBLE PRECISION NOT NULL DEFAULT 0, mean_relative_diff DOUBLE PRECISION NOT NULL DEFAULT 0,
        max_relative_diff DOUBLE PRECISION NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'observing',
        checked_at TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(dataset,trade_date,primary_provider,fallback_provider,field_name))""")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_market_bulk_execution (
        trade_date TEXT NOT NULL, provider TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL,
        total_batches INTEGER NOT NULL DEFAULT 0, completed_batches INTEGER NOT NULL DEFAULT 0,
        rows_received INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, last_progress_at TEXT NOT NULL,
        finished_at TEXT, elapsed_ms INTEGER NOT NULL DEFAULT 0, eta_ms INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0, evidence_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(trade_date,provider,scope))""")
    op.execute("""CREATE TABLE IF NOT EXISTS dc_market_bulk_batch_checkpoint (
        trade_date TEXT NOT NULL, provider TEXT NOT NULL, scope TEXT NOT NULL, batch_index INTEGER NOT NULL,
        status TEXT NOT NULL, code_count INTEGER NOT NULL DEFAULT 0, rows_received INTEGER NOT NULL DEFAULT 0,
        codes_hash TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', completed_at TEXT NOT NULL,
        PRIMARY KEY(trade_date,provider,scope,batch_index))""")


def downgrade() -> None:
    raise RuntimeError("Data-platform downgrade requires an explicit forward migration")
