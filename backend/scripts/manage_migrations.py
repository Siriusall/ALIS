#!/usr/bin/env python3
"""Explicit dual-database migration command; production startup never upgrades."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATABASES = {
    "sim": (ROOT / "alembic_sim.ini", "sim_0001_baseline"),
    "data": (ROOT / "alembic_data.ini", "data_0001_baseline"),
}
BASELINE_REQUIRED_TABLES = {
    "sim": {"users", "executions", "model_versions", "factor_definitions", "decision_snapshots"},
    "data": {"data_ingestion_runs", "data_sources", "dc_stock_daily_snapshot"},
}


def _verify_legacy_baseline(name: str) -> None:
    if name == "sim":
        from app.db.store import _conn
    else:
        from app.data_platform.db import _conn
    with _conn() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        ).fetchall()
    present = {str(dict(row).get("table_name") or "") for row in rows}
    missing = sorted(BASELINE_REQUIRED_TABLES[name] - present)
    if missing:
        raise RuntimeError(
            f"refusing to stamp {name} baseline; legacy schema is incomplete: {', '.join(missing)}"
        )


def _config(path: Path) -> Config:
    return Config(str(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("current", "heads", "baseline", "upgrade"))
    parser.add_argument("--database", choices=("sim", "data", "all"), default="all")
    args = parser.parse_args()
    selected = DATABASES if args.database == "all" else {args.database: DATABASES[args.database]}
    for name, (path, baseline) in selected.items():
        print(f"[{name}] {args.action}")
        config = _config(path)
        if args.action == "current":
            command.current(config, verbose=True)
        elif args.action == "heads":
            command.heads(config, verbose=True)
        elif args.action == "baseline":
            _verify_legacy_baseline(name)
            command.stamp(config, baseline)
        else:
            command.upgrade(config, "head")


if __name__ == "__main__":
    main()
