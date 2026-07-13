from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.data_center import (
    ensure_data_center_seeded,
    run_data_quality_checks,
    sync_cninfo_announcements,
    sync_market_bundle,
    sync_stock_basic,
    sync_trade_calendar,
)

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "phase": "idle", "message": "等待更新"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _latest_trade_date() -> str:
    today = date.today().strftime("%Y%m%d")
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(cal_date) AS value FROM dc_trade_calendar WHERE is_open=1 AND cal_date<=?", (today,)
        ).fetchone()
    return str(dict(row).get("value") or today)


def _set_state(**values: Any) -> None:
    with _lock:
        _state.update(values)


def _worker(parent_run_id: int) -> None:
    steps: list[dict[str, Any]] = []
    error = ""
    trade_date = ""
    try:
        _set_state(phase="preparing", message="正在检查数据目录", progress=10)
        ensure_data_center_seeded()

        with _conn() as c:
            stock_count = int(dict(c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone()).get("cnt") or 0)
        if stock_count == 0:
            _set_state(phase="stock_basic", message="首次使用，正在建立股票名单", progress=20)
            steps.append({"step": "stock_basic", "result": sync_stock_basic()})

        _set_state(phase="calendar", message="正在确认最近交易日", progress=35)
        start = (date.today() - timedelta(days=35)).strftime("%Y%m%d")
        end = (date.today() + timedelta(days=7)).strftime("%Y%m%d")
        steps.append({"step": "trade_calendar", "result": sync_trade_calendar(start, end)})
        trade_date = _latest_trade_date()

        _set_state(phase="market", message=f"正在更新 {trade_date} 行情和估值", progress=50, trade_date=trade_date)
        steps.append({"step": "market", "result": sync_market_bundle(trade_date)})

        _set_state(phase="announcements", message="正在更新最近公告", progress=75)
        ann_start = (date.today() - timedelta(days=3)).strftime("%Y%m%d")
        ann_end = date.today().strftime("%Y%m%d")
        steps.append({"step": "announcements", "result": sync_cninfo_announcements(
            start_date=ann_start, end_date=ann_end, page_size=30, max_pages=3,
        )})

        _set_state(phase="quality", message="正在检查数据完整性", progress=90)
        steps.append({"step": "quality", "result": run_data_quality_checks(trade_date)})
        final_status = "completed"
        _set_state(running=False, phase="completed", message=f"数据已更新到 {trade_date}", progress=100,
                   finished_at=_now(), steps=steps)
    except Exception as exc:  # noqa: BLE001
        final_status, error = "failed", str(exc)[:1000]
        _set_state(running=False, phase="failed", message=f"更新失败：{error}", error=error,
                   finished_at=_now(), steps=steps)
    with _conn() as c:
        c.execute(
            """UPDATE data_ingestion_runs SET status=?,finished_at=?,rows_read=?,rows_written=?,error=?,meta_json=?
            WHERE id=?""",
            (final_status, _now(), len(steps), len(steps), error,
             json.dumps({"trade_date": trade_date, "steps": steps}, ensure_ascii=False, default=str), parent_run_id),
        )


def start_daily_update() -> dict[str, Any]:
    with _lock:
        if _state.get("running"):
            return {"ok": False, "reason": "already_running", **_state}
        with _conn() as c:
            c.execute(
                """INSERT INTO data_ingestion_runs
                (job_name,source_code,status,started_at,rows_read,rows_written,meta_json)
                VALUES (?,?,?,?,?,?,?)""",
                ("data_platform.daily_update", "system", "running", _now(), 0, 0, "{}"),
            )
            run_id = c.last_insert_id()
        _state.clear()
        _state.update({"running": True, "run_id": run_id, "phase": "starting", "message": "正在启动更新", "progress": 0,
                       "started_at": _now()})
        threading.Thread(target=_worker, args=(run_id,), daemon=True, name=f"daily-update-{run_id}").start()
        return {"ok": True, **_state}


def daily_update_status() -> dict[str, Any]:
    with _lock:
        return {"ok": True, **_state}


def simple_health() -> dict[str, Any]:
    today = date.today().strftime("%Y%m%d")
    with _conn() as c:
        values = {
            "stock_count": c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone(),
            "snapshot_count": c.execute("SELECT COUNT(*) cnt FROM dc_stock_daily_snapshot").fetchone(),
            "latest_trade_date": c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone(),
            "latest_announcement": c.execute("SELECT MAX(publish_time) value FROM dc_announcement_raw").fetchone(),
            "failed_runs": c.execute("SELECT COUNT(*) cnt FROM data_ingestion_runs WHERE status='failed'").fetchone(),
        }
    data = {key: dict(row) for key, row in values.items()}
    latest = str(data["latest_trade_date"].get("value") or "")
    stock_count = int(data["stock_count"].get("cnt") or 0)
    snapshot_count = int(data["snapshot_count"].get("cnt") or 0)
    if stock_count == 0 or snapshot_count == 0:
        level, summary = "needs_setup", "尚未完成首次数据更新"
    elif latest and (datetime.strptime(today, "%Y%m%d") - datetime.strptime(latest, "%Y%m%d")).days <= 4:
        level, summary = "healthy", "数据状态正常"
    else:
        level, summary = "stale", "行情数据需要更新"
    return {
        "ok": True, "level": level, "summary": summary, "latest_trade_date": latest,
        "stock_count": stock_count, "snapshot_count": snapshot_count,
        "latest_announcement": data["latest_announcement"].get("value"),
        "failed_runs": int(data["failed_runs"].get("cnt") or 0),
    }
