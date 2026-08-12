#!/usr/bin/env python3
"""Explicit resumable rebuild of permanent OOS hard-API daily statistics."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data_platform.db import _conn
from app.data_platform.services.oos_hard_data import HARD_APIS


def _dates(start: str, end: str) -> list[str]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT cal_date FROM dc_trade_calendar
                 WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN ? AND ?
                 ORDER BY cal_date""",
            (start, end),
        ).fetchall()
    return [str(dict(row).get("cal_date") or "") for row in rows if dict(row).get("cal_date")]


def refresh(start: str, end: str, *, batch_size: int = 20) -> dict[str, int]:
    dates = _dates(start, end)
    written = 0
    for offset in range(0, len(dates), max(1, min(batch_size, 60))):
        batch = dates[offset:offset + max(1, min(batch_size, 60))]
        placeholders = ",".join("?" for _ in batch)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with _conn() as conn:
            for api_name in HARD_APIS:
                rows = conn.execute(
                    f"""SELECT trade_date,COUNT(DISTINCT ts_code) AS point_in_time_rows
                          FROM dc_tushare_raw_records
                         WHERE api_name=? AND trade_date IN ({placeholders}) AND ts_code<>''
                           AND available_time<=
                               (SUBSTRING(trade_date,1,4)||'-'||SUBSTRING(trade_date,5,2)||'-'||SUBSTRING(trade_date,7,2)||'T15:00:00')
                         GROUP BY trade_date""",
                    (api_name, *batch),
                ).fetchall()
                counts = {str(dict(row).get("trade_date") or ""): int(dict(row).get("point_in_time_rows") or 0) for row in rows}
                for trade_date in batch:
                    conn.execute(
                        """INSERT INTO dc_oos_hard_api_stats_live
                               (api_name,trade_date,point_in_time_rows,updated_at)
                           VALUES(?,?,?,?)
                           ON CONFLICT(api_name,trade_date) DO UPDATE SET
                               point_in_time_rows=excluded.point_in_time_rows,
                               updated_at=excluded.updated_at""",
                        (api_name, trade_date, counts.get(trade_date, 0), now),
                    )
                    written += 1
        print(f"refreshed {batch[0]}..{batch[-1]} ({offset + len(batch)}/{len(dates)})")
    return {"dates": len(dates), "rows": written}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    print(refresh(args.start.replace("-", ""), args.end.replace("-", ""), batch_size=args.batch_size))


if __name__ == "__main__":
    main()
