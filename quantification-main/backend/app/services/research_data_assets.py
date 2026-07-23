"""Point-in-time research-data depth audit.

This is intentionally an audit, not a synthetic-data generator.  A missing
industry feed or labelled announcement set remains visible as a hard research
limitation instead of being silently replaced by an optimistic proxy.
"""

from __future__ import annotations

from typing import Any

from app.data_platform.db import _conn
from app.services.industry_event_engine import _metric_component
from app.services.announcement_label_benchmark import announcement_label_benchmark


def _value(row: Any, key: str, default: Any = 0) -> Any:
    return dict(row).get(key, default) if row else default


def audit_research_data_assets() -> dict[str, Any]:
    with _conn() as conn:
        metric_rows = [dict(row) for row in conn.execute(
            """SELECT d.metric_code,d.metric_name,d.description,d.source_code,COUNT(v.metric_code) value_count,
                      MAX(v.available_time) latest_available_time
                 FROM dc_industry_metric_definition d LEFT JOIN dc_industry_metric_value v ON v.metric_code=d.metric_code
                 GROUP BY d.metric_code,d.metric_name,d.description"""
        ).fetchall()]
        report = conn.execute("""SELECT COUNT(*) records,COUNT(DISTINCT ts_code) symbols,
                                      MAX(available_time) latest_available_time,
                                      SUM(CASE WHEN COALESCE(available_time,'')='' THEN 1 ELSE 0 END) missing_pit
                               FROM dc_tushare_raw_records WHERE api_name='report_rc'""").fetchone()
        theme = conn.execute("""SELECT COUNT(*) mappings,COUNT(DISTINCT as_of_date) snapshot_dates,
                                     MAX(as_of_date) latest_date
                              FROM dc_theme_company_mapping""").fetchone()
        revenue_theme = conn.execute("""SELECT COUNT(*) exposures,COUNT(DISTINCT ts_code) companies,
                                             COUNT(DISTINCT theme_code) themes,MAX(as_of_date) latest_date
                                      FROM dc_theme_revenue_exposure""").fetchone()
        announcements = conn.execute("""SELECT COUNT(*) raw_records,MAX(available_time) latest_available_time
                                        FROM dc_announcement_raw""").fetchone()
        events = conn.execute("SELECT COUNT(*) events,AVG(confidence) avg_confidence FROM dc_announcement_event").fetchone()

    components = {key: [] for key in ("demand_orders_sales", "product_price", "inventory_cycle", "capacity_utilization", "profit_diffusion", "policy_supply", "industry_valuation")}
    for row in metric_rows:
        component = _metric_component(row)
        if component:
            components[component].append(row)
    industry = {}
    for key, items in components.items():
        sources = sorted({str(item.get("source_code") or "") for item in items if item.get("source_code")})
        proxy_only = bool(items) and all("proxy" in source for source in sources)
        value_count = sum(int(item.get("value_count") or 0) for item in items)
        component_status = "missing" if not items else "configured" if not value_count else "proxy_ready" if proxy_only else "ready"
        industry[key] = {
            "status": component_status,
            "metric_count": len(items),
            "value_count": value_count,
            "latest_available_time": max((str(item.get("latest_available_time") or "") for item in items), default=""),
            "sources": sources,
        }
    report_data = {"records": int(_value(report, "records")), "symbols": int(_value(report, "symbols")), "latest_available_time": str(_value(report, "latest_available_time", "")), "missing_point_in_time_records": int(_value(report, "missing_pit"))}
    theme_data = {"mappings": int(_value(theme, "mappings")), "snapshot_dates": int(_value(theme, "snapshot_dates")), "latest_date": str(_value(theme, "latest_date", "")), "revenue_exposures": int(_value(revenue_theme, "exposures")), "revenue_companies": int(_value(revenue_theme, "companies")), "revenue_themes": int(_value(revenue_theme, "themes")), "revenue_latest_date": str(_value(revenue_theme, "latest_date", ""))}
    benchmark = announcement_label_benchmark()
    announcement_data = {"raw_records": int(_value(announcements, "raw_records")), "events": int(_value(events, "events")), "latest_available_time": str(_value(announcements, "latest_available_time", "")), "avg_rule_confidence": round(float(_value(events, "avg_confidence", 0) or 0), 4), **benchmark}
    gaps = []
    for key, item in industry.items():
        if item["status"] in {"missing", "configured"}: gaps.append(f"industry:{key}")
        if item["status"] == "proxy_ready": gaps.append(f"industry:{key}:direct_source_not_integrated")
    if not report_data["records"] or report_data["missing_point_in_time_records"]:
        gaps.append("analyst_consensus:point_in_time_coverage")
    # Existing theme mappings are evidence-based narrative exposures, not a
    # revenue-segment dataset; retain that distinction in the contract.
    if not theme_data["revenue_exposures"]:
        gaps.append("theme_revenue_exposure:source_not_materialized")
    if benchmark["status"] != "ready": gaps.append("announcement_extraction_accuracy:labelled_benchmark_not_integrated")
    return {
        "ok": True, "schema_version": "alis_research_data_assets_v1", "status": "ready" if not gaps else "partial",
        "industry_components": industry,
        "analyst_consensus_pit": {**report_data, "status": "ready" if report_data["records"] and not report_data["missing_point_in_time_records"] else "partial"},
        "theme_exposure": {**theme_data, "status": "ready" if theme_data["revenue_exposures"] else "proxy_only", "limitation": "价格/新闻主题映射与主营业务收入暴露分开保存；收入暴露只使用点时主营业务分部。"},
        "announcement_extraction": {**announcement_data, "status": benchmark["status"], "limitation": "规则分类置信度不等于抽取准确率；需以人工标注基准验收。"},
        "gaps": gaps,
        "policy": "only_point_in_time_sources_are_production_eligible; proxies_and_unmeasured_extraction_cannot_satisfy_research_depth_gate",
    }
