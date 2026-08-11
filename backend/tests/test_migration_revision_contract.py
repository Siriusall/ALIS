from __future__ import annotations

import ast
from pathlib import Path


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
