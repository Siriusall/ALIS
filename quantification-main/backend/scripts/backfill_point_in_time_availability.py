from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data_platform.db import _conn
from app.data_platform.services.data_center import _DISCLOSURE_APIS, _point_in_time_available_time


def main() -> None:
    with _conn() as c:
        if c._is_postgres:
            raw_cursor = c.execute(
                """WITH disclosure AS (
                       SELECT id,COALESCE(NULLIF(payload_json::jsonb->>'actual_date',''),NULLIF(payload_json::jsonb->>'f_ann_date',''),NULLIF(payload_json::jsonb->>'ann_date','')) AS disclosed
                       FROM dc_tushare_raw_records WHERE api_name = ANY(?)
                   ), valid AS (
                       SELECT id,SUBSTRING(disclosed,1,4)||'-'||SUBSTRING(disclosed,5,2)||'-'||SUBSTRING(disclosed,7,2)||'T23:59:59' AS available
                       FROM disclosure WHERE disclosed ~ '^[0-9]{8}$'
                   )
                   UPDATE dc_tushare_raw_records r SET available_time=v.available FROM valid v
                   WHERE r.id=v.id AND r.available_time<>v.available""",
                (list(sorted(_DISCLOSURE_APIS)),),
            )
            snapshot_cursor = c.execute(
                """UPDATE dc_stock_daily_snapshot SET available_time=
                       SUBSTRING(trade_date,1,4)||'-'||SUBSTRING(trade_date,5,2)||'-'||SUBSTRING(trade_date,7,2)||'T15:00:00'
                   WHERE trade_date ~ '^[0-9]{8}$' AND available_time<>
                       SUBSTRING(trade_date,1,4)||'-'||SUBSTRING(trade_date,5,2)||'-'||SUBSTRING(trade_date,7,2)||'T15:00:00'"""
            )
            print(json.dumps({"ok": True, "raw_records_updated": raw_cursor.rowcount, "snapshots_updated": snapshot_cursor.rowcount}, ensure_ascii=False))
            return

        updated_raw = 0
        placeholders = ",".join("?" for _ in _DISCLOSURE_APIS)
        rows = c.execute(
            f"SELECT id,api_name,payload_json,available_time FROM dc_tushare_raw_records WHERE api_name IN ({placeholders})",
            tuple(sorted(_DISCLOSURE_APIS)),
        ).fetchall()
        for row in rows:
            item = dict(row)
            try:
                payload = json.loads(item.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            available = _point_in_time_available_time(str(item.get("api_name") or ""), payload, fallback=str(item.get("available_time") or ""))
            if available and available != item.get("available_time"):
                c.execute("UPDATE dc_tushare_raw_records SET available_time=? WHERE id=?", (available, item["id"]))
                updated_raw += 1
        snapshot_rows = c.execute("SELECT ts_code,trade_date,available_time FROM dc_stock_daily_snapshot").fetchall()
        updated_snapshots = 0
        for row in snapshot_rows:
            item = dict(row)
            available = _point_in_time_available_time("daily", {"trade_date": item.get("trade_date")}, fallback=str(item.get("available_time") or ""))
            if available and available != item.get("available_time"):
                c.execute("UPDATE dc_stock_daily_snapshot SET available_time=? WHERE ts_code=? AND trade_date=?", (available, item["ts_code"], item["trade_date"]))
                updated_snapshots += 1
    print(json.dumps({"ok": True, "raw_records_updated": updated_raw, "snapshots_updated": updated_snapshots}, ensure_ascii=False))


if __name__ == "__main__":
    main()
