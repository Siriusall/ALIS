"""System release center and release lineage."""

from alembic import op

revision = "sim_0002_release_center"
down_revision = "sim_0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS system_components (
        component_key TEXT PRIMARY KEY, component_type TEXT NOT NULL, name TEXT NOT NULL,
        decision_critical INTEGER NOT NULL DEFAULT 0, native_registry TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    op.execute("""CREATE TABLE IF NOT EXISTS system_component_versions (
        id BIGSERIAL PRIMARY KEY, component_key TEXT NOT NULL REFERENCES system_components(component_key),
        version TEXT NOT NULL, content_hash TEXT NOT NULL, native_table TEXT NOT NULL DEFAULT '',
        native_id BIGINT, git_commit TEXT NOT NULL DEFAULT '', artifact_uri TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'registered', evidence_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, UNIQUE(component_key,version,content_hash))""")
    op.execute("""CREATE TABLE IF NOT EXISTS system_component_dependencies (
        id BIGSERIAL PRIMARY KEY, component_version_id BIGINT NOT NULL REFERENCES system_component_versions(id),
        dependency_component_key TEXT NOT NULL, constraint_type TEXT NOT NULL,
        constraint_json TEXT NOT NULL DEFAULT '{}', required INTEGER NOT NULL DEFAULT 1,
        reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
    op.execute("""CREATE TABLE IF NOT EXISTS system_releases (
        id BIGSERIAL PRIMARY KEY, release_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
        manifest_hash TEXT NOT NULL UNIQUE, manifest_json TEXT NOT NULL, git_commit TEXT NOT NULL DEFAULT '',
        git_dirty INTEGER NOT NULL DEFAULT 0, sim_db_revision TEXT NOT NULL DEFAULT '',
        data_db_revision TEXT NOT NULL DEFAULT '', config_hash TEXT NOT NULL DEFAULT '',
        validation_status TEXT NOT NULL DEFAULT 'pending', validation_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, activated_at TEXT, retired_at TEXT)""")
    op.execute("""CREATE TABLE IF NOT EXISTS system_release_components (
        release_id BIGINT NOT NULL REFERENCES system_releases(id), component_key TEXT NOT NULL,
        component_version_id BIGINT NOT NULL REFERENCES system_component_versions(id),
        role TEXT NOT NULL DEFAULT 'required', required INTEGER NOT NULL DEFAULT 1,
        frozen_payload_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(release_id,component_key))""")
    op.execute("""CREATE TABLE IF NOT EXISTS system_release_events (
        id BIGSERIAL PRIMARY KEY, release_id BIGINT NOT NULL REFERENCES system_releases(id),
        event_type TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_system_release_one_active ON system_releases(status) WHERE status='active'")
    op.execute("CREATE INDEX IF NOT EXISTS idx_system_release_events_release ON system_release_events(release_id,created_at)")
    op.execute("ALTER TABLE daily_rebalance_plans ADD COLUMN IF NOT EXISTS release_id BIGINT")
    op.execute("ALTER TABLE decision_snapshots ADD COLUMN IF NOT EXISTS release_id BIGINT")
    op.execute("ALTER TABLE executions ADD COLUMN IF NOT EXISTS release_id BIGINT")


def downgrade() -> None:
    raise RuntimeError("Release-center downgrade requires an explicit forward migration")
