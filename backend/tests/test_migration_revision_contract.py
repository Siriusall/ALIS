from __future__ import annotations

import ast
from pathlib import Path

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
