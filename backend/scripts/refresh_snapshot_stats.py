#!/usr/bin/env python3
"""Explicit resumable rebuild of normalized daily snapshot statistics."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data_platform.db import _conn


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
    size = max(1, min(int(batch_size), 60))
    for offset in range(0, len(dates), size):
        batch = dates[offset:offset + size]
        placeholders = ",".join("?" for _ in batch)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with _conn() as conn:
            rows = conn.execute(
                f"""SELECT trade_date,COUNT(*) AS rows_total,
                           COUNT(*) FILTER (WHERE close IS NOT NULL AND volume IS NOT NULL AND turnover IS NOT NULL) AS market_rows,
                           COALESCE(MIN(observed_at), '') AS min_available_time,
                           COALESCE(MAX(observed_at), '') AS max_available_time
                      FROM dc_stock_daily_snapshot
                     WHERE trade_date IN ({placeholders})
                       AND observed_at<>'' AND content_hash<>''
                       AND LOWER(COALESCE(observation_provenance,'')) NOT LIKE 'legacy_%'
                     GROUP BY trade_date""",
                tuple(batch),
            ).fetchall()
            values = {str(dict(row).get("trade_date") or ""): dict(row) for row in rows}
            for trade_date in batch:
                item = values.get(trade_date) or {}
                conn.execute(
                    """INSERT INTO dc_stock_daily_snapshot_stats_live
                           (trade_date,rows_total,market_rows,min_available_time,max_available_time,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(trade_date) DO UPDATE SET
                           rows_total=excluded.rows_total,market_rows=excluded.market_rows,
                           min_available_time=excluded.min_available_time,
                           max_available_time=excluded.max_available_time,updated_at=excluded.updated_at""",
                    (
                        trade_date, int(item.get("rows_total") or 0), int(item.get("market_rows") or 0),
                        str(item.get("min_available_time") or ""), str(item.get("max_available_time") or ""), now,
                    ),
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
