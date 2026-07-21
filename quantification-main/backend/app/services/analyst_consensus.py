"""
分析师一致预期融合引擎

从 report_rc (券商研报) 数据中提取分析师评级、目标价，
计算一致预期并融合到预期变化引擎。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.data_platform.db import _conn


def _yyyymmdd(value: str | None) -> str:
    return str(value or "")[:10].replace("-", "")


def _shift_yyyymmdd(value: str, days: int) -> str:
    return (datetime.strptime(value, "%Y%m%d").date() + timedelta(days=days)).strftime("%Y%m%d")


def build_analyst_consensus(ts_code: str, *, as_of_time: str | None = None) -> dict[str, Any]:
    """Build analyst consensus from research reports.

    Returns consensus rating, target price, and estimate revisions.
    """
    cutoff = _yyyymmdd(as_of_time)
    cutoff_clause = " AND trade_date<=?" if cutoff else ""
    params: tuple[Any, ...] = (cutoff,) if cutoff else ()

    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT payload_json, trade_date, id
                FROM dc_tushare_raw_records
                WHERE api_name='report_rc' AND ts_code=?
                {cutoff_clause}
                ORDER BY trade_date DESC LIMIT 100""",
            (ts_code,) + params
        ).fetchall()]

    if not rows:
        return {"ok": False, "reason": "no_report_data", "ts_code": ts_code}

    reports = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        reports.append({
            "trade_date": row.get("trade_date", ""),
            "rating": str(payload.get("rating", "") or ""),
            "target_price": payload.get("max_price") or payload.get("min_price"),
            "min_price": payload.get("min_price"),
            "max_price": payload.get("max_price"),
            "report_title": str(payload.get("report_title", "") or "")[:60],
            "org_name": str(payload.get("org_name", "") or ""),
        })

    # Rating mapping
    rating_scores = {"买入": 5, "增持": 4, "中性": 3, "减持": 2, "卖出": 1}

    # Compute consensus
    valid_ratings = [r for r in reports if r["rating"] in rating_scores]
    valid_targets = [r for r in reports if r.get("target_price") is not None]

    consensus_rating = None
    consensus_score = None
    if valid_ratings:
        scores = [rating_scores[r["rating"]] for r in valid_ratings]
        consensus_score = round(sum(scores) / len(scores), 2)
        avg_score = sum(scores) / len(scores)
        if avg_score >= 4.5:
            consensus_rating = "买入"
        elif avg_score >= 3.5:
            consensus_rating = "增持"
        elif avg_score >= 2.5:
            consensus_rating = "中性"
        elif avg_score >= 1.5:
            consensus_rating = "减持"
        else:
            consensus_rating = "卖出"

    target_price_avg = None
    target_price_min = None
    target_price_max = None
    if valid_targets:
        prices = [float(r["target_price"]) for r in valid_targets if r["target_price"]]
        if prices:
            target_price_avg = round(sum(prices) / len(prices), 2)
            target_price_min = round(min(prices), 2)
            target_price_max = round(max(prices), 2)

    # Recent trend uses a rolling 4-week window anchored to as_of_time when
    # provided, otherwise to the latest available report date for this stock.
    anchor_date = cutoff or max((str(r.get("trade_date") or "") for r in valid_ratings), default="")
    recent_start = _shift_yyyymmdd(anchor_date, -27) if anchor_date else ""
    prior_start = _shift_yyyymmdd(anchor_date, -55) if anchor_date else ""
    recent = [r for r in valid_ratings if recent_start and recent_start <= r["trade_date"] <= anchor_date]
    prior = [r for r in valid_ratings if prior_start and prior_start <= r["trade_date"] < recent_start]
    recent_avg = sum(rating_scores[r["rating"]] for r in recent) / len(recent) if recent else None
    prior_avg = sum(rating_scores[r["rating"]] for r in prior) / len(prior) if prior else None
    revision = None
    if recent_avg is not None and prior_avg is not None:
        revision = round(recent_avg - prior_avg, 2)

    # Confidence based on number of reports
    confidence = min(0.85, 0.3 + 0.05 * len(valid_ratings))

    return {
        "ok": True,
        "ts_code": ts_code,
        "consensus_rating": consensus_rating,
        "consensus_score": consensus_score,
        "target_price_avg": target_price_avg,
        "target_price_min": target_price_min,
        "target_price_max": target_price_max,
        "report_count": len(valid_ratings),
        "analyst_count": len(set(r["org_name"] for r in valid_ratings if r["org_name"])),
        "rating_revision": revision,
        "revision_window": {
            "anchor_date": anchor_date,
            "recent_start": recent_start,
            "prior_start": prior_start,
            "recent_reports": len(recent),
            "prior_reports": len(prior),
            "method": "rolling_4w_vs_prior_4w",
        },
        "confidence": round(confidence, 4),
        "latest_reports": [{"date": r["trade_date"], "rating": r["rating"], "org": r["org_name"], "title": r["report_title"]} for r in reports[:5]],
        "model_version": "analyst_consensus_v1",
    }


def batch_analyst_consensus(*, limit: int = 100, as_of_time: str | None = None) -> dict[str, Any]:
    """Batch build analyst consensus for all stocks with report data."""
    cutoff = str(as_of_time or "")[:10].replace("-", "")
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            "SELECT DISTINCT ts_code FROM dc_tushare_raw_records WHERE api_name='report_rc' ORDER BY ts_code"
        ).fetchall()]

    codes = [str(r.get("ts_code", "")) for r in rows[:limit]]
    results = []
    for code in codes:
        try:
            result = build_analyst_consensus(code, as_of_time=as_of_time)
            if result.get("ok"):
                results.append(result)
        except Exception:
            pass

    with_consensus = [r for r in results if r.get("report_count", 0) >= 3]
    return {
        "ok": True,
        "total_stocks": len(results),
        "with_consensus": len(with_consensus),
        "with_target_price": sum(1 for r in results if r.get("target_price_avg")),
        "model_version": "analyst_consensus_v1",
        "samples": results[:5],
    }
