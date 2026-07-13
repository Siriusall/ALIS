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

INDUSTRY_METRIC_HINTS = {
    "demand_orders_sales": ("需求", "订单", "销量", "销售", "出货"),
    "product_price": ("价格", "售价", "价差"),
    "inventory_cycle": ("库存", "库销"),
    "capacity_utilization": ("产能", "开工", "利用率"),
    "profit_diffusion": ("利润", "毛利", "盈利"),
    "policy_supply": ("政策", "供给", "产量"),
    "industry_valuation": ("估值", "市盈", "市净"),
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


def _metric_component(definition: dict[str, Any]) -> str | None:
    text = " ".join(str(definition.get(key) or "") for key in ("metric_code", "metric_name", "description"))
    return next((component for component, hints in INDUSTRY_METRIC_HINTS.items() if any(hint in text for hint in hints)), None)


def _metric_signal(definition: dict[str, Any], values: list[dict[str, Any]]) -> float | None:
    numeric = []
    for row in reversed(values):
        try:
            numeric.append(float(row.get("value")))
        except (TypeError, ValueError):
            continue
    if len(numeric) < 3:
        return None
    baseline = sum(numeric[:-1]) / len(numeric[:-1])
    scale = max(abs(baseline), max(numeric) - min(numeric), 1e-9)
    change = (numeric[-1] - baseline) / scale
    component = _metric_component(definition)
    inverse = component in {"inventory_cycle", "industry_valuation"}
    normalized = 50 - change * 50 if inverse else 50 + change * 50
    return _clip(normalized)


def _industry_state_from_series(series: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    history_points = 0
    for item in series:
        definition, values = item["definition"], item["values"]
        history_points += len(values)
        component = _metric_component(definition)
        signal = _metric_signal(definition, values)
        if component and signal is not None:
            grouped.setdefault(component, []).append(signal)
            evidence.setdefault(component, []).append({"metric_code": definition.get("metric_code"), "metric_name": definition.get("metric_name"), "latest_date": values[0].get("data_date") if values else None, "points": len(values), "signal": round(signal, 4)})
    observations = {key: sum(values) / len(values) for key, values in grouped.items()}
    return score_industry(observations, history_points=history_points), evidence


def list_industries() -> dict[str, Any]:
    with _conn() as c:
        definitions = [dict(row) for row in c.execute(
            """SELECT d.*,COUNT(v.metric_code) value_count,MIN(v.data_date) first_date,MAX(v.data_date) last_date
               FROM dc_industry_metric_definition d LEFT JOIN dc_industry_metric_value v ON v.metric_code=d.metric_code
               GROUP BY d.metric_code,d.industry_code,d.industry_name,d.metric_name,d.unit,d.frequency,d.source_code,d.description,d.created_at,d.updated_at
               ORDER BY d.industry_name,d.metric_name"""
        ).fetchall()]
        values = [dict(row) for row in c.execute(
            """SELECT * FROM (
                   SELECT v.*, ROW_NUMBER() OVER (
                       PARTITION BY v.metric_code ORDER BY v.data_date DESC
                   ) AS row_num
                   FROM dc_industry_metric_value v
               ) ranked WHERE row_num <= 60
               ORDER BY metric_code,data_date DESC"""
        ).fetchall()]
        company_rows = [dict(row) for row in c.execute(
            """SELECT industry,ts_code,name FROM dc_stock_master
               WHERE COALESCE(industry,'')<>'' ORDER BY industry,ts_code"""
        ).fetchall()]
    values_by_metric: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        value.pop("row_num", None)
        values_by_metric.setdefault(str(value.get("metric_code") or ""), []).append(value)
    grouped: dict[str, dict[str, Any]] = {}
    for item in definitions:
        key = str(item.get("industry_code") or item.get("industry_name") or "unknown")
        group = grouped.setdefault(key, {"industry_code": key, "industry_name": item.get("industry_name"), "metrics": [], "total_evidence_points": 0})
        group["metrics"].append(item)
        group["total_evidence_points"] += int(item.get("value_count") or 0)
    companies_by_industry: dict[str, list[dict[str, str]]] = {}
    for company in company_rows:
        industry = str(company.get("industry") or "").strip()
        if industry:
            companies_by_industry.setdefault(industry, []).append({
                "ts_code": str(company.get("ts_code") or ""),
                "name": str(company.get("name") or ""),
            })
    known_names = {str(item.get("industry_name") or "").strip() for item in grouped.values()}
    for industry_name in companies_by_industry:
        if industry_name not in known_names:
            key = f"master:{industry_name}"
            grouped[key] = {"industry_code": key, "industry_name": industry_name, "metrics": [], "total_evidence_points": 0}
    for group in grouped.values():
        series = [
            {"definition": definition, "values": values_by_metric.get(str(definition.get("metric_code") or ""), [])}
            for definition in group["metrics"]
        ]
        state, evidence = _industry_state_from_series(series)
        unmapped = [x["definition"].get("metric_name") for x in series if not _metric_component(x["definition"])]
        group["state"] = state
        group["evidence"] = evidence
        members = companies_by_industry.get(str(group.get("industry_name") or "").strip(), [])
        group["company_count"] = len(members)
        group["companies"] = members[:20]
        group["limitations"] = [f"{len(unmapped)}个指标未映射，只展示事实"] if unmapped else []
    return {"ok": True, "items": list(grouped.values()), "count": len(grouped)}


def industry_detail(industry_code: str) -> dict[str, Any]:
    with _conn() as c:
        definitions = [dict(row) for row in c.execute("SELECT * FROM dc_industry_metric_definition WHERE industry_code=? ORDER BY metric_name", (industry_code,)).fetchall()]
        series: list[dict[str, Any]] = []
        for definition in definitions:
            values = [dict(row) for row in c.execute("SELECT * FROM dc_industry_metric_value WHERE metric_code=? ORDER BY data_date DESC LIMIT 60", (definition["metric_code"],)).fetchall()]
            series.append({"definition": definition, "values": values})
    state, evidence = _industry_state_from_series(series)
    unmapped = [x["definition"].get("metric_name") for x in series if not _metric_component(x["definition"])]
    return {"ok": bool(definitions), "industry_code": industry_code, "series": series, "state": state, "evidence": evidence, "limitations": [f"{len(unmapped)}个指标未映射，只展示事实"] if unmapped else []}


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
    try:
        from app.services.thesis_store import list_theses
        thesis_by_code = {str(x.get("ts_code") or ""): x for x in list_theses(limit=500)}
    except Exception:
        thesis_by_code = {}
    for item in rows:
        item["entities"] = json.loads(item.pop("entities_json") or "[]")
        item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
        item["evidence"] = {"title": item.get("title"), "url": item.get("url"), "available_time": item.get("available_time"), "excerpt": str(item.pop("raw_text", "") or "")[:240]}
        item["impact"] = score_event(item)
        thesis = thesis_by_code.get(str(item.get("ts_code") or ""))
        decayed = float(item["impact"].get("decayed_score") or 0)
        item["thesis_link"] = {"thesis_id": thesis.get("id"), "title": thesis.get("title"), "status": thesis.get("derived_status")} if thesis else None
        item["follow_up"] = {
            "status": "urgent" if item.get("impact_direction") == "negative" and decayed >= 60 else "monitor" if decayed >= 25 else "expired",
            "next_action": "立即复核投资逻辑必要条件" if item.get("impact_direction") == "negative" and decayed >= 60 else "下个披露节点复核" if decayed >= 25 else "保留历史证据",
            "requires_thesis_review": bool(thesis and item.get("impact_direction") == "negative" and decayed >= 40),
        }
    return {"ok": True, "items": rows, "count": len(rows)}


def review_linked_event_theses(*, limit: int = 100) -> dict[str, Any]:
    """Recheck only theses hit by actionable negative announcement events.

    Announcement ingestion remains evidence-only.  This explicit action is the
    controlled bridge into investment logic: it records the existing
    point-in-time research review without treating a keyword classifier as an
    automatic thesis invalidation.
    """
    events = list_events(limit=limit).get("items", [])
    candidates: dict[int, dict[str, Any]] = {}
    for event in events:
        link = event.get("thesis_link") or {}
        thesis_id = link.get("thesis_id") if isinstance(link, dict) else None
        if thesis_id and bool((event.get("follow_up") or {}).get("requires_thesis_review")):
            candidates[int(thesis_id)] = event
    from app.services.thesis_store import review_thesis

    reviewed, failed = [], []
    for thesis_id, event in candidates.items():
        try:
            thesis = review_thesis(thesis_id)
            if thesis:
                reviewed.append({
                    "thesis_id": thesis_id,
                    "ts_code": thesis.get("ts_code"),
                    "event_id": event.get("id"),
                    "event_type": event.get("event_type"),
                    "status": thesis.get("derived_status"),
                    "confidence": thesis.get("derived_confidence"),
                })
        except Exception as exc:  # noqa: BLE001
            failed.append({"thesis_id": thesis_id, "event_id": event.get("id"), "error": str(exc)[:200]})
    return {"ok": not failed, "candidates": len(candidates), "reviewed": reviewed, "failed": failed}
