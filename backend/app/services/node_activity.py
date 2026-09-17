"""Multi-node daily activity and health aggregation for system overview."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
import logging

from app.db.store import _read_conn

logger = logging.getLogger(__name__)


def get_node_daily_activities(limit_days: int = 7) -> dict[str, Any]:
    """Aggregate daily tasks, executions, and errors across the 3 nodes."""
    safe_limit = max(1, min(int(limit_days or 7), 30))
    days_map: Dict[str, Dict[str, Any]] = {}

    try:
        with _read_conn() as conn:
            # 1. Evolution tasks grouped by date, pool, task_type and status
            sql_tasks = """
            SELECT 
                DATE(created_at::timestamptz) AS day,
                COALESCE(worker_pool, 'control') AS worker_pool,
                task_type,
                status,
                COUNT(*) AS cnt,
                MIN(started_at) AS first_start,
                MAX(finished_at) AS last_finish,
                MAX(error) AS sample_error
            FROM evolution_tasks
            WHERE created_at::timestamptz >= NOW() - make_interval(days => ?)
            GROUP BY DATE(created_at::timestamptz), COALESCE(worker_pool, 'control'), task_type, status
            ORDER BY day DESC, worker_pool, task_type;
            """
            cur = conn.execute(sql_tasks, (safe_limit,))
            task_rows = [dict(r) for r in cur.fetchall()]

            # 2. Data ingestion / DB node runs
            data_rows = []
            try:
                sql_data = """
                SELECT 
                    DATE(started_at::timestamptz) AS day,
                    job_name,
                    source_code,
                    status,
                    COUNT(*) AS cnt,
                    SUM(COALESCE(rows_written, 0)) AS total_rows,
                    MIN(started_at) AS first_start,
                    MAX(finished_at) AS last_finish,
                    MAX(error) AS sample_error
                FROM data_ingestion_runs
                WHERE started_at::timestamptz >= NOW() - make_interval(days => ?)
                GROUP BY DATE(started_at::timestamptz), job_name, source_code, status
                ORDER BY day DESC;
                """
                cur_data = conn.execute(sql_data, (safe_limit,))
                data_rows = [dict(r) for r in cur_data.fetchall()]
            except Exception as exc:
                logger.debug("Failed querying data_ingestion_runs for node activity: %s", exc)

    except Exception as exc:
        logger.warning("Failed retrieving node daily activities: %s", exc)
        return {
            "ok": False,
            "status": "degraded",
            "days": [],
            "error": str(exc),
        }

    for r in task_rows:
        d_str = str(r["day"])
        if d_str not in days_map:
            days_map[d_str] = {
                "date": d_str,
                "db_node": {
                    "name": "数据库与数据中台 (122.51.159.195)",
                    "role": "PostgreSQL 双库 · 数据集发布 · 快照生成 · Egress 流量守卫",
                    "tasks": [],
                    "completed": 0,
                    "failed": 0,
                    "queued": 0,
                    "running": 0,
                },
                "compute_node": {
                    "name": "计算/研究集群 (124.223.225.117)",
                    "role": "重型回测 · 分层重放 · 因子与多模型学习 · 离线数据集优先",
                    "tasks": [],
                    "completed": 0,
                    "failed": 0,
                    "queued": 0,
                    "running": 0,
                },
                "app_node": {
                    "name": "应用/调度网关 (124.221.86.123)",
                    "role": "FastAPI · 调仓决策 · 模拟账本 · 周期探测与调度心跳",
                    "tasks": [],
                    "completed": 0,
                    "failed": 0,
                    "queued": 0,
                    "running": 0,
                },
            }

        pool = str(r.get("worker_pool") or "control").lower()
        target = days_map[d_str]["compute_node"] if pool == "research" else days_map[d_str]["app_node"]
        st = str(r.get("status") or "").lower()
        cnt = int(r.get("cnt") or 0)

        if st == "completed":
            target["completed"] += cnt
        elif st in {"failed", "cancelled"}:
            target["failed"] += cnt
        elif st == "running":
            target["running"] += cnt
        elif st in {"queued", "pending"}:
            target["queued"] += cnt

        target["tasks"].append({
            "task_type": r.get("task_type"),
            "status": st,
            "count": cnt,
            "error": r.get("sample_error"),
            "first_start": r.get("first_start"),
            "last_finish": r.get("last_finish"),
        })

    for r in data_rows:
        d_str = str(r["day"])
        if d_str in days_map:
            st = str(r.get("status") or "").lower()
            cnt = int(r.get("cnt") or 0)
            db_target = days_map[d_str]["db_node"]
            if st == "completed":
                db_target["completed"] += cnt
            elif st == "failed":
                db_target["failed"] += cnt
            db_target["tasks"].append({
                "task_type": r.get("job_name"),
                "status": st,
                "count": cnt,
                "rows_written": int(r.get("total_rows") or 0),
                "error": r.get("sample_error"),
                "first_start": r.get("first_start"),
                "last_finish": r.get("last_finish"),
            })

    return {
        "ok": True,
        "schema_version": "alis_node_daily_activities_v1",
        "days": list(days_map.values()),
    }
