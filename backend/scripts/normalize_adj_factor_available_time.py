#!/usr/bin/env python3
"""Audit adj_factor observation times for point-in-time replay.

The historical ``--execute`` flag is retained only for CLI compatibility.  It
is intentionally refused: an observed response that arrived after a decision
cutoff must never be rewritten as if it had been available earlier.
"""

from __future__ import annotations

import argparse
import json

from app.data_platform.db import _conn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="deprecated; refuse instead of rewriting immutable observation times",
    )
    parser.add_argument("--start-date", default="20200320")
    parser.add_argument("--end-date", default="20260722")
    args = parser.parse_args()
    changed_dates: list[dict[str, object]] = []
    with _conn() as conn:
        dates = [
            str(dict(row).get("cal_date") or "")
            for row in conn.execute(
                """SELECT cal_date FROM dc_trade_calendar
                   WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN ? AND ?
                   ORDER BY cal_date""",
                (args.start_date, args.end_date),
            ).fetchall()
        ]
        for trade_date in dates:
            cutoff = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00"
            row = conn.execute(
                """SELECT COUNT(*) AS count FROM dc_tushare_raw_records
                   WHERE api_name='adj_factor' AND trade_date=? AND available_time>?""",
                (trade_date, cutoff),
            ).fetchone()
            count = int(dict(row).get("count") or 0) if row else 0
            if count <= 0:
                continue
            changed_dates.append({"trade_date": trade_date, "rows": count})
    result = {
        "status": "refused_immutable_rewrite" if args.execute else "audit",
        "date_count": len(changed_dates),
        "row_count": sum(int(item["rows"]) for item in changed_dates),
        "dates": changed_dates,
    }
    if args.execute:
        result["reason"] = "adj_factor_available_time_is_immutable"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.execute:
        return 3
    return 0 if not changed_dates else 2


if __name__ == "__main__":
    raise SystemExit(main())
