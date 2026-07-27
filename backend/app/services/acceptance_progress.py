from __future__ import annotations

from typing import Any

from app.db.store import _conn
from app.services.execution_cost_calibration import (
    MINIMUM_ACTIVATION_OPERATIONS,
    MINIMUM_ACTIVATION_OPERATIONS_PER_SIDE,
    MINIMUM_CANDIDATE_OPERATIONS,
    MINIMUM_CANDIDATE_OPERATIONS_PER_SIDE,
    _calibration_metrics,
)
from app.services.system_autonomy import AUTONOMY_SLO_WINDOW_SESSIONS, build_autonomy_slo


FACTOR_EVALUATION_TARGET = 20


def _progress(current: int, target: int) -> float:
    return round(min(1.0, max(0.0, current / max(1, target))), 4)


def _factor_progress() -> dict[str, Any]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT d.factor_key,COUNT(e.id) AS evaluation_count
               FROM factor_definitions d
               LEFT JOIN factor_evaluations e ON e.factor_definition_id=d.id
               WHERE d.status='candidate'
               GROUP BY d.id,d.factor_key
               ORDER BY d.factor_key"""
        ).fetchall()
    items = [
        {
            "factor_key": str(dict(row).get("factor_key") or ""),
            "name": str(dict(row).get("factor_key") or ""),
            "current": int(dict(row).get("evaluation_count") or 0),
        }
        for row in rows
    ]
    minimum = min((item["current"] for item in items), default=0)
    ready = sum(item["current"] >= FACTOR_EVALUATION_TARGET for item in items)
    return {
        "key": "candidate_factors",
        "label": "候选因子评价",
        "status": "passed" if items and ready == len(items) else "observing",
        "current": minimum,
        "target": FACTOR_EVALUATION_TARGET,
        "remaining": max(0, FACTOR_EVALUATION_TARGET - minimum),
        "unit": "未来截面",
        "progress": _progress(minimum, FACTOR_EVALUATION_TARGET),
        "detail": f"{ready}/{len(items)} 个候选达到治理门；按最慢候选计",
        "items": [{**item, "remaining": max(0, FACTOR_EVALUATION_TARGET - item["current"])} for item in items],
    }


def _cost_stage(metrics: dict[str, Any], *, key: str, label: str, total_target: int, side_target: int) -> dict[str, Any]:
    total = int(metrics.get("operation_count") or 0)
    buy = int(metrics.get("buy_count") or 0)
    sell = int(metrics.get("sell_count") or 0)
    remaining = max(0, total_target - total, side_target - buy, side_target - sell)
    progress = min(
        _progress(total, total_target),
        _progress(buy, side_target),
        _progress(sell, side_target),
    )
    return {
        "key": key,
        "label": label,
        "status": "passed" if remaining == 0 else "observing",
        "current": total,
        "target": total_target,
        "remaining": remaining,
        "unit": "成交",
        "progress": progress,
        "detail": f"总计 {total}/{total_target} · 买 {buy}/{side_target} · 卖 {sell}/{side_target}",
        "dimensions": {"total": total, "buy": buy, "sell": sell},
    }


def _cost_progress() -> dict[str, Any]:
    with _conn() as conn:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT side,price,quantity,commission,transfer_fee,stamp_tax,slippage_bps
                   FROM operations WHERE quantity>0 AND price>0 ORDER BY id DESC LIMIT 5000"""
            ).fetchall()
        ]
    metrics = _calibration_metrics(rows)
    candidate = _cost_stage(
        metrics, key="execution_cost_candidate", label="成本候选生成",
        total_target=MINIMUM_CANDIDATE_OPERATIONS,
        side_target=MINIMUM_CANDIDATE_OPERATIONS_PER_SIDE,
    )
    activation = _cost_stage(
        metrics, key="execution_cost_activation", label="成本自动激活",
        total_target=MINIMUM_ACTIVATION_OPERATIONS,
        side_target=MINIMUM_ACTIVATION_OPERATIONS_PER_SIDE,
    )
    return {"metrics": metrics, "stages": [candidate, activation]}


def _autonomy_progress() -> dict[str, Any]:
    slo = build_autonomy_slo()
    current = int(slo.get("observed_sessions") or 0)
    target = int(slo.get("window_sessions") or AUTONOMY_SLO_WINDOW_SESSIONS)
    status = str(slo.get("status") or "observing")
    return {
        "key": "cross_session_autonomy",
        "label": "跨交易日自治链",
        "status": status,
        "current": current,
        "target": target,
        "remaining": max(0, target - current),
        "unit": "交易日",
        "progress": _progress(current, target),
        "detail": "样本满窗后校验完成率、关键数据门、零漂移、恢复率和防重复",
        "metrics": slo.get("metrics") or {},
        "gates": slo.get("gates") or {},
        "window_start": slo.get("window_start"),
        "window_end": slo.get("window_end"),
    }


def build_acceptance_progress() -> dict[str, Any]:
    factor = _factor_progress()
    cost = _cost_progress()
    autonomy = _autonomy_progress()
    stages = [factor, *cost["stages"], autonomy]
    return {
        "schema_version": "alis_acceptance_progress_v1",
        "status": "passed" if all(item["status"] == "passed" for item in stages) else "observing",
        "stages": stages,
        "summary": {
            "passed": sum(item["status"] == "passed" for item in stages),
            "total": len(stages),
        },
    }
