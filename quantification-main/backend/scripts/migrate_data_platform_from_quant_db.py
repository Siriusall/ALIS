from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from app.data_platform.db import DB_PATH, init_db

TABLES = (
    "data_sources", "data_ingestion_runs", "tushare_api_catalog", "dc_stock_master",
    "dc_trade_calendar", "dc_tushare_raw_records", "dc_stock_daily_snapshot",
    "dc_data_quality_checks", "dc_announcement_raw", "dc_announcement_event",
    "dc_industry_metric_definition", "dc_industry_metric_value", "dc_web_factor_snapshot",
)


def migrate(source: Path) -> dict[str, int]:
    if not source.exists():
        raise FileNotFoundError(source)
    init_db()
    target = sqlite3.connect(str(DB_PATH))
    try:
        target.execute("ATTACH DATABASE ? AS source_db", (str(source.resolve()),))
        counts: dict[str, int] = {}
        for table in TABLES:
            exists = target.execute(
                "SELECT 1 FROM source_db.sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            target.execute(f"DELETE FROM {table}")
            target.execute(f"INSERT INTO {table} SELECT * FROM source_db.{table}")
            counts[table] = int(target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        target.commit()
        return counts
    finally:
        target.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(migrate(args.source))
