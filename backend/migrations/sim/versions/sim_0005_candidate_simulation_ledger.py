"""Persistent isolated simulation ledgers for every candidate version."""

from alembic import op


revision = "sim_0005_candidate_ledger"
down_revision = "sim_0004_candidate_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS candidate_simulation_accounts (
        id BIGSERIAL PRIMARY KEY, model_version_id BIGINT NOT NULL REFERENCES model_versions(id),
        contract_hash VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL,
        initial_cash DOUBLE PRECISION NOT NULL, latest_cash DOUBLE PRECISION NOT NULL,
        latest_equity DOUBLE PRECISION NOT NULL, latest_trade_date VARCHAR(16) NOT NULL DEFAULT '',
        data_contract_json TEXT NOT NULL DEFAULT '{}', created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL, UNIQUE(model_version_id,contract_hash))""")
    op.execute("""CREATE TABLE IF NOT EXISTS candidate_simulation_daily_equity (
        account_id BIGINT NOT NULL REFERENCES candidate_simulation_accounts(id),
        trade_date VARCHAR(16) NOT NULL, cash DOUBLE PRECISION NOT NULL,
        stock_value DOUBLE PRECISION NOT NULL, total_value DOUBLE PRECISION NOT NULL,
        benchmark_value DOUBLE PRECISION, positions_json TEXT NOT NULL DEFAULT '{}',
        point_in_time_passed INTEGER NOT NULL DEFAULT 0, created_at VARCHAR(64) NOT NULL,
        PRIMARY KEY(account_id,trade_date))""")
    op.execute("""CREATE TABLE IF NOT EXISTS candidate_simulation_decisions (
        account_id BIGINT NOT NULL REFERENCES candidate_simulation_accounts(id),
        signal_date VARCHAR(16) NOT NULL, execute_date VARCHAR(16) NOT NULL,
        status VARCHAR(64) NOT NULL, decision_json TEXT NOT NULL,
        created_at VARCHAR(64) NOT NULL, PRIMARY KEY(account_id,signal_date))""")
    op.execute("""CREATE TABLE IF NOT EXISTS candidate_simulation_operations (
        account_id BIGINT NOT NULL REFERENCES candidate_simulation_accounts(id),
        operation_uid VARCHAR(160) NOT NULL, trade_date VARCHAR(16) NOT NULL,
        symbol VARCHAR(32) NOT NULL, side VARCHAR(16) NOT NULL,
        price DOUBLE PRECISION NOT NULL, quantity INTEGER NOT NULL,
        fees DOUBLE PRECISION NOT NULL DEFAULT 0, operation_json TEXT NOT NULL,
        created_at VARCHAR(64) NOT NULL, PRIMARY KEY(account_id,operation_uid))""")
    op.execute("""CREATE TABLE IF NOT EXISTS candidate_simulation_blocked_events (
        account_id BIGINT NOT NULL REFERENCES candidate_simulation_accounts(id),
        event_uid VARCHAR(160) NOT NULL, trade_date VARCHAR(16) NOT NULL,
        code VARCHAR(64) NOT NULL, event_json TEXT NOT NULL,
        created_at VARCHAR(64) NOT NULL, PRIMARY KEY(account_id,event_uid))""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_candidate_sim_equity_date ON candidate_simulation_daily_equity(trade_date,account_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_candidate_sim_operations_date ON candidate_simulation_operations(trade_date,account_id)")


def downgrade() -> None:
    raise RuntimeError("Candidate simulation ledger downgrade requires an explicit forward migration")
