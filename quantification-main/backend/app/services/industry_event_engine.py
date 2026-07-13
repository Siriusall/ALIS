from __future__ import annotations

from datetime import date, datetime
import json
import math
from typing import Any

from app.data_platform.db import _conn


INDUSTRY_WEIGHTS = {
    "demand_orders_sales": .25,
    "product_price": .15,
    "inventory_cycle": .15,
    "capacity_utilization": .10,
    "profit_diffusion": .15,
    "policy_supply": .10,
    "industry_valuation": .10,
}

EVENT_WEIGHTS = {
    "source_reliability": .20,
    "quantified_materiality": .25,
    "direction": .15,
    "duration": .15,
    "thesis_impact": .15,
    "novelty": .10,
}

EVENT_HALF_LIFE_DAYS = {
    "regulatory_risk": 180,
    "shareholder_reduction": 60,
    "pledge": 90,
    "earnings": 45,
    "earnings_forecast": 45,
    "earnings_flash": 45,
    "repurchase": 60,
    "dividend": 30,
    "major_contract": 60,
    "ma_restructuring": 120,
    "private_placement": 90,
    "capital_operation": 60,
}


def _clip(value: Any, low: float = 0, high: float = 100) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def score_industry(observations: dict[str, Any], *, prior_score: float | None = None, history_points: int = 0) -> dict[str, Any]:
    available = {key: _clip(observations[key]) for key in INDUSTRY_WEIGHTS if observations.get(key) is not None}
    coverage = sum(INDUSTRY_WEIGHTS[key] for key in available)
    score = sum(available[key] * INDUSTRY_WEIGHTS[key] for key in available) / coverage if coverage else 0.0
    delta = None if prior_score is None else score - float(prior_score)
    if coverage < .60 or history_points < 3:
        state, status = "证据不足", "insufficient_data"
    elif score < 30 and (delta is None or delta <= 0):
        state, status = "衰退", "ready"
    elif score < 45 and delta is not None and delta > 0:
        state, status = "筑底", "ready"
    elif score < 60 and delta is not None and delta > 0:
        state, status = "复苏", "ready"
    elif score < 75 and (delta is None or delta >= -5):
        state, status = "扩张", "ready"
    elif score >= 75 and (delta is None or delta >= 0):
        state, status = "过热", "ready"
    else:
        state, status = "回落", "ready"
    return {
        "score": round(score, 4), "coverage": round(coverage, 4), "status": status,
        "lifecycle": state, "score_change": None if delta is None else round(delta, 4),
        "components": {key: {"value": value, "weight": INDUSTRY_WEIGHTS[key]} for key, value in available.items()},
        "missing": [key for key in INDUSTRY_WEIGHTS if key not in available],
        "evidence_points": history_points,
    }


def list_industries() -> dict[str, Any]:
    with _conn() as c:
        definitions = [dict(row) for row in c.execute(
            """SELECT d.*,COUNT(v.metric_code) value_count,MIN(v.data_date) first_date,MAX(v.data_date) last_date
               FROM dc_industry_metric_definition d LEFT JOIN dc_industry_metric_value v ON v.metric_code=d.metric_code
               GROUP BY d.metric_code,d.industry_code,d.industry_name,d.metric_name,d.unit,d.frequency,d.source_code,d.description,d.created_at,d.updated_at
               ORDER BY d.industry_name,d.metric_name"""
        ).fetchall()]
    grouped: dict[str, dict[str, Any]] = {}
    for item in definitions:
        key = str(item.get("industry_code") or item.get("industry_name") or "unknown")
        group = grouped.setdefault(key, {"industry_code": key, "industry_name": item.get("industry_name"), "metrics": [], "total_evidence_points": 0})
        group["metrics"].append(item)
        group["total_evidence_points"] += int(item.get("value_count") or 0)
    for group in grouped.values():
        group["state"] = score_industry({}, history_points=group["total_evidence_points"])
        group["limitations"] = ["指标方向和七类景气分量尚未建立映射"] if group["metrics"] else ["无行业指标"]
    return {"ok": True, "items": list(grouped.values()), "count": len(grouped)}


def industry_detail(industry_code: str) -> dict[str, Any]:
    with _conn() as c:
        definitions = [dict(row) for row in c.execute("SELECT * FROM dc_industry_metric_definition WHERE industry_code=? ORDER BY metric_name", (industry_code,)).fetchall()]
        series: list[dict[str, Any]] = []
        for definition in definitions:
            values = [dict(row) for row in c.execute("SELECT * FROM dc_industry_metric_value WHERE metric_code=? ORDER BY data_date DESC LIMIT 60", (definition["metric_code"],)).fetchall()]
            series.append({"definition": definition, "values": values})
    return {"ok": bool(definitions), "industry_code": industry_code, "series": series, "state": score_industry({}, history_points=sum(len(x["values"]) for x in series)), "limitations": ["未映射的指标只展示事实，不参与景气得分"]}


def _days_since(value: str) -> int:
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text).date()
    except ValueError:
        try:
            parsed = datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            return 0
    return max(0, (date.today() - parsed).days)


def score_event(item: dict[str, Any]) -> dict[str, Any]:
    severity = {"critical": 100, "high": 80, "medium": 55, "low": 30}.get(str(item.get("severity")), 50)
    direction_name = str(item.get("impact_direction") or "neutral")
    direction = {"positive": 100, "negative": 100, "neutral": 30}.get(direction_name, 30)
    confidence = _clip(float(item.get("confidence") or 0) * 100)
    source = 95 if str(item.get("source_code")) in {"cninfo", "sse", "szse"} else 70
    event_type = str(item.get("event_type") or "")
    half_life = EVENT_HALF_LIFE_DAYS.get(event_type, 45)
    duration = _clip(40 + min(60, half_life / 3))
    components = {
        "source_reliability": source,
        "quantified_materiality": severity,
        "direction": direction,
        "duration": duration,
        "thesis_impact": severity,
        "novelty": 50,
    }
    initial = sum(components[key] * EVENT_WEIGHTS[key] for key in EVENT_WEIGHTS)
    age_days = _days_since(str(item.get("event_date") or item.get("publish_time") or ""))
    decayed = initial * math.exp(-math.log(2) * age_days / half_life)
    return {
        "score": round(initial, 4), "decayed_score": round(decayed, 4), "direction": direction_name,
        "age_days": age_days, "half_life_days": half_life,
        "components": {key: {"value": components[key], "weight": EVENT_WEIGHTS[key]} for key in EVENT_WEIGHTS},
    }


def list_events(*, ts_code: str = "", limit: int = 100) -> dict[str, Any]:
    where, params = "", []
    if ts_code:
        where, params = "WHERE r.ts_code=?", [ts_code]
    with _conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT e.*,r.ts_code,r.title,r.publish_time,r.url,r.source_code,r.available_time,r.raw_text
                FROM dc_announcement_event e JOIN dc_announcement_raw r ON r.id=e.announcement_id
                {where} ORDER BY COALESCE(NULLIF(e.event_date,''),r.publish_time) DESC,e.id DESC LIMIT ?""",
            tuple(params + [limit]),
        ).fetchall()]
    for item in rows:
        item["entities"] = json.loads(item.pop("entities_json") or "[]")
        item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
        item["evidence"] = {"title": item.get("title"), "url": item.get("url"), "available_time": item.get("available_time"), "excerpt": str(item.pop("raw_text", "") or "")[:240]}
        item["impact"] = score_event(item)
    return {"ok": True, "items": rows, "count": len(rows)}
