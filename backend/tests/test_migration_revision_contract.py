from __future__ import annotations

import ast
from contextlib import nullcontext
from pathlib import Path

import pytest

from app.services.system_release_center import DATA_SCHEMA_HEAD, SIM_SCHEMA_HEAD


def _revision_graph(root: Path) -> tuple[dict[str, str | None], set[str]]:
    revisions: dict[str, str | None] = {}
    referenced: set[str] = set()
    for path in sorted(root.glob("versions/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, str | None] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        values[target.id] = node.value.value
                    elif isinstance(node.value, ast.Constant) and node.value.value is None:
                        values[target.id] = None
        revision = values.get("revision")
        if revision:
            revisions[revision] = values.get("down_revision")
            if values.get("down_revision"):
                referenced.add(str(values["down_revision"]))
    return revisions, referenced


def test_alembic_revision_ids_fit_version_table_contract() -> None:
    root = Path(__file__).resolve().parents[1] / "migrations"
    revisions: list[tuple[Path, str]] = []
    for path in sorted(root.glob("*/versions/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "revision" for target in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                revisions.append((path, node.value.value))
    assert revisions
    assert [(str(path), revision) for path, revision in revisions if len(revision) > 32] == []


def test_runtime_schema_heads_match_alembic_heads() -> None:
    root = Path(__file__).resolve().parents[1] / "migrations"
    sim_revisions, sim_referenced = _revision_graph(root / "sim")
    data_revisions, data_referenced = _revision_graph(root / "data")
    assert set(sim_revisions) - sim_referenced == {SIM_SCHEMA_HEAD}
    assert set(data_revisions) - data_referenced == {DATA_SCHEMA_HEAD}


def test_model_autonomy_route_migration_only_moves_pending_tasks(monkeypatch) -> None:
    from migrations.sim.versions import sim_0011_route_model_autonomy_research as migration

    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: statements.append(str(sql)))

    migration.upgrade()

    assert len(statements) == 1
    statement = statements[0].lower()
    assert "task_type='plan_model_autonomy'" in statement
    assert "status in ('queued','retry_wait')" in statement
    assert "worker_pool='research'" in statement
    assert "running" not in statement


def test_console_snapshot_privilege_migration_validates_minimum_runtime_access(monkeypatch) -> None:
    from migrations.sim.versions import sim_0012_console_snapshot_privileges as migration

    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: statements.append(str(sql)))

    migration.upgrade()

    normalized = " ".join(statement.lower() for statement in statements)
    assert "to_regclass('public.console_page_snapshots')" in normalized
    assert "has_schema_privilege(current_user, 'public', 'usage')" in normalized
    assert normalized.count("has_table_privilege") == 3
    assert "'select'" in normalized
    assert "'insert'" in normalized
    assert "'update'" in normalized
    assert "grant" not in normalized




def test_console_snapshot_privilege_migration_is_forward_only() -> None:
    from migrations.sim.versions import sim_0012_console_snapshot_privileges as migration

    with pytest.raises(RuntimeError, match="forward-only"):
        migration.downgrade()


def test_raw_date_statistics_migration_collects_correlated_planner_evidence(monkeypatch) -> None:
    from migrations.data.versions import data_0014_raw_date_stats as migration

    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda sql: statements.append(str(sql)))

    migration.upgrade()

    normalized = " ".join(statements).lower()
    assert normalized.count("create statistics if not exists") == 1
    assert "dependencies, mcv, ndistinct" in normalized
    assert normalized.count("set statistics 1000") == 2
    assert normalized.count("analyze public.") == 1
    assert "dc_tushare_raw_record_version" in normalized
