from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, time as dtime, timedelta
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
from app.data_platform.services.market_calendar import target_trade_date

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "phase": "idle", "message": "等待更新"}
_scheduler_lock = threading.Lock()
_scheduler_started = False
_DAILY_UPDATE_ENABLED = str(os.getenv("ALIS_DAILY_UPDATE_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}
_DAILY_UPDATE_INTERVAL_SEC = max(60, min(int(os.getenv("ALIS_DAILY_UPDATE_INTERVAL_SEC", "300")), 3600))
_DAILY_UPDATE_START_TIME = dtime(
    max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_HOUR", "16")), 23)),
    max(0, min(int(os.getenv("ALIS_DAILY_UPDATE_MINUTE", "5")), 59)),
)
logger = logging.getLogger("quant.daily_update")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _latest_trade_date() -> str:
    with _conn() as c:
        return target_trade_date(c)


def _latest_usable_snapshot_date(c: Any) -> str:
    stock_count = int(dict(c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone()).get("cnt") or 0)
    threshold = max(1, int(stock_count * 0.8)) if stock_count else 1
    row = c.execute(
        """
        SELECT trade_date value
        FROM dc_stock_daily_snapshot
        GROUP BY trade_date
        HAVING SUM(CASE WHEN close IS NOT NULL THEN 1 ELSE 0 END) >= ?
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (threshold,),
    ).fetchone()
    value = str(dict(row).get("value") or "") if row else ""
    if value:
        return value
    fallback = c.execute("SELECT MAX(trade_date) value FROM dc_stock_daily_snapshot").fetchone()
    return str(dict(fallback).get("value") or "") if fallback else ""


def _latest_decision_ready_trade_date(c: Any) -> str:
    stock_count = int(dict(c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone()).get("cnt") or 0)
    threshold = max(1, int(stock_count * 0.8)) if stock_count else 1
    row = c.execute(
        """
        SELECT s.trade_date value
        FROM dc_stock_daily_snapshot s
        WHERE EXISTS (SELECT 1 FROM dc_tushare_raw_records r WHERE r.trade_date=s.trade_date AND r.api_name='daily')
          AND EXISTS (SELECT 1 FROM dc_tushare_raw_records r WHERE r.trade_date=s.trade_date AND r.api_name='daily_basic')
          AND EXISTS (SELECT 1 FROM dc_tushare_raw_records r WHERE r.trade_date=s.trade_date AND r.api_name='stk_limit')
        GROUP BY s.trade_date
        HAVING SUM(CASE WHEN s.close IS NOT NULL THEN 1 ELSE 0 END) >= ?
           AND SUM(CASE WHEN s.total_mv IS NOT NULL THEN 1 ELSE 0 END) >= ?
           AND SUM(CASE WHEN s.up_limit IS NOT NULL AND s.down_limit IS NOT NULL THEN 1 ELSE 0 END) >= ?
        ORDER BY s.trade_date DESC
        LIMIT 1
        """,
        (threshold, threshold, threshold),
    ).fetchone()
    return str(dict(row).get("value") or "") if row else ""


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
        market_result = sync_market_bundle(trade_date)
        steps.append({"step": "market", "result": market_result})
        with _conn() as c:
            ready_date = _latest_decision_ready_trade_date(c)
        if not bool(market_result.get("ok")) or ready_date != trade_date:
            raise RuntimeError(f"上游尚未发布 {trade_date} 完整收盘数据，自动日更将在下一轮重试")

        _set_state(phase="announcements", message="正在更新最近公告", progress=75)
        ann_start = (date.today() - timedelta(days=3)).strftime("%Y%m%d")
        ann_end = date.today().strftime("%Y%m%d")
        steps.append({"step": "announcements", "result": sync_cninfo_announcements(
            start_date=ann_start, end_date=ann_end, page_size=30, max_pages=3,
        )})

        _set_state(phase="quality", message="正在检查数据完整性", progress=90)
        steps.append({"step": "quality", "result": run_data_quality_checks(trade_date)})

        # Research maintenance is downstream of a durable market snapshot. It
        # is deliberately non-fatal: a research failure must not roll back or
        # misreport a successfully completed market-data update.
        _set_state(phase="research", message="正在更新候选池和核心池研究", progress=95)
        try:
            from app.services.stock_screener import run_daily_screener
            from app.services.research_workflow import process_watch_pool

            research_limit = max(1, min(int(os.getenv("ALIS_DAILY_RESEARCH_LIMIT", "100")), 500))
            screen = run_daily_screener(top_n=max(100, research_limit * 3), research_limit=research_limit)
            promote = process_watch_pool(limit=research_limit)
            steps.append({"step": "research_pool", "result": {"screen": screen, "promote": promote}})
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily research maintenance deferred: %s", str(exc)[:300])
            steps.append({"step": "research_pool", "result": {"ok": False, "deferred": True, "error": str(exc)[:300]}})
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


def start_daily_update_scheduler() -> bool:
    """Start an independent close-of-day updater, even with no quant task."""
    global _scheduler_started
    if not _DAILY_UPDATE_ENABLED:
        return False
    with _scheduler_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True

    def _loop() -> None:
        while True:
            try:
                now = datetime.now()
                if now.weekday() < 5 and now.time() >= _DAILY_UPDATE_START_TIME:
                    with _conn() as c:
                        target = target_trade_date(c, now=now)
                        ready = _latest_decision_ready_trade_date(c)
                    if target and ready != target:
                        result = start_daily_update()
                        if result.get("ok"):
                            logger.info("自动收盘日更已启动 target=%s previous_ready=%s", target, ready)
                        elif result.get("reason") != "already_running":
                            logger.warning("自动收盘日更启动失败 target=%s result=%s", target, result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("自动收盘日更检查失败: %s", str(exc)[:300])
            threading.Event().wait(_DAILY_UPDATE_INTERVAL_SEC)

    threading.Thread(target=_loop, name="data-platform-daily-update", daemon=True).start()
    logger.info("独立收盘日更调度已启动，%s 后每 %ss 检查", _DAILY_UPDATE_START_TIME.strftime("%H:%M"), _DAILY_UPDATE_INTERVAL_SEC)
    return True


def daily_update_status() -> dict[str, Any]:
    with _lock:
        state = {"ok": True, **_state}
    try:
        with _conn() as c:
            state["latest_usable_snapshot_date"] = _latest_usable_snapshot_date(c)
            state["latest_decision_ready_trade_date"] = _latest_decision_ready_trade_date(c)
    except Exception as exc:  # noqa: BLE001
        state["status_error"] = str(exc)[:200]
    return state


def simple_health() -> dict[str, Any]:
    with _conn() as c:
        values = {
            "stock_count": c.execute("SELECT COUNT(*) cnt FROM dc_stock_master").fetchone(),
            "snapshot_count": c.execute("SELECT COUNT(*) cnt FROM dc_stock_daily_snapshot").fetchone(),
            "latest_trade_date": {"value": _latest_usable_snapshot_date(c)},
            "latest_decision_ready_trade_date": {"value": _latest_decision_ready_trade_date(c)},
            "latest_announcement": c.execute("SELECT MAX(publish_time) value FROM dc_announcement_raw").fetchone(),
            "failed_runs": c.execute("SELECT COUNT(*) cnt FROM data_ingestion_runs WHERE status='failed'").fetchone(),
        }
    data = {key: dict(row) for key, row in values.items()}
    latest = str(data["latest_trade_date"].get("value") or "")
    decision_ready = str(data["latest_decision_ready_trade_date"].get("value") or "")
    stock_count = int(data["stock_count"].get("cnt") or 0)
    snapshot_count = int(data["snapshot_count"].get("cnt") or 0)
    with _conn() as c:
        target = target_trade_date(c)
    if stock_count == 0 or snapshot_count == 0:
        level, summary = "needs_setup", "尚未完成首次数据更新"
    elif decision_ready == target:
        level, summary = "healthy", "数据状态正常"
    else:
        level, summary = "blocked", f"目标交易日 {target} 尚未完成决策数据更新"
    return {
        "ok": level == "healthy", "level": level, "summary": summary, "target_trade_date": target, "latest_trade_date": latest,
        "latest_decision_ready_trade_date": decision_ready,
        "stock_count": stock_count, "snapshot_count": snapshot_count,
        "latest_announcement": data["latest_announcement"].get("value"),
        "failed_runs": int(data["failed_runs"].get("cnt") or 0),
    }
