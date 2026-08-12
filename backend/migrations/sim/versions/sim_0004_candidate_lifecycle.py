"""Separate candidate observation runtime from production eligibility."""

from alembic import op


revision = "sim_0004_candidate_lifecycle"
down_revision = "sim_0003_autonomy_step_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS candidate_lifecycle_records (
        model_version_id BIGINT PRIMARY KEY REFERENCES model_versions(id),
        candidate_type VARCHAR(32) NOT NULL,
        runtime_status VARCHAR(32) NOT NULL,
        eligibility_status VARCHAR(32) NOT NULL,
        change_type VARCHAR(32) NOT NULL,
        root_experiment_id VARCHAR(128) NOT NULL,
        parent_model_version_id BIGINT,
        supersedes_model_version_id BIGINT,
        problem_family VARCHAR(160) NOT NULL,
        changed_fields_json TEXT NOT NULL DEFAULT '[]',
        observation_contract VARCHAR(128) NOT NULL,
        production_change_forbidden INTEGER NOT NULL DEFAULT 1,
        stop_reason VARCHAR(160) NOT NULL DEFAULT '',
        created_at VARCHAR(64) NOT NULL,
        updated_at VARCHAR(64) NOT NULL)""")
    op.execute("""CREATE TABLE IF NOT EXISTS candidate_lifecycle_events (
        id BIGSERIAL PRIMARY KEY,
        model_version_id BIGINT NOT NULL REFERENCES model_versions(id),
        event_type VARCHAR(64) NOT NULL,
        runtime_status VARCHAR(32) NOT NULL,
        eligibility_status VARCHAR(32) NOT NULL,
        reason VARCHAR(255) NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '{}',
        created_at VARCHAR(64) NOT NULL)""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_candidate_lifecycle_runtime ON candidate_lifecycle_records(runtime_status,candidate_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_candidate_lifecycle_events_version ON candidate_lifecycle_events(model_version_id,id)")


def downgrade() -> None:
    raise RuntimeError("Candidate lifecycle downgrade requires an explicit forward migration")
