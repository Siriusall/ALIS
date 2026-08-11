"""Point-in-time research-data depth audit.

This is intentionally an audit, not a synthetic-data generator.  A missing
industry feed or labelled announcement set remains visible as a hard research
limitation instead of being silently replaced by an optimistic proxy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.data_platform.db import _conn
from app.services.industry_event_engine import _metric_component
from app.services.announcement_label_benchmark import (
    CNINFO_CATEGORY_HOLDOUT_CONTRACT,
    announcement_label_benchmark,
    announcement_manual_review_queue_status,
    announcement_provider_reference_benchmark,
)
from app.services.professional_metric_documents import audit_professional_metric_candidates


def _value(row: Any, key: str, default: Any = 0) -> Any:
    return dict(row).get(key, default) if row else default


def _report_rc_summary(conn: Any) -> dict[str, Any]:
    """Read the maintained analyst-report summary; never scan raw payloads on a read path."""
    if not getattr(conn, "_is_postgres", False):
        row = conn.execute(
            """SELECT COUNT(*) records,COUNT(DISTINCT ts_code) symbols,
                      MAX(available_time) latest_available_time,
                      SUM(CASE WHEN COALESCE(available_time,'')='' THEN 1 ELSE 0 END) missing_pit
               FROM dc_tushare_raw_records WHERE api_name='report_rc'"""
        ).fetchone()
        return {**(dict(row) if row else {}), "summary_status": "exact_fixture_fallback"}
    table = conn.execute(
        "SELECT to_regclass(?) AS table_name",
        ("public.dc_research_asset_summary",),
    ).fetchone()
    if not _value(table, "table_name", ""):
        return {
            "records": 0, "symbols": 0, "latest_available_time": "",
            "missing_pit": 0, "measurement": "summary_missing",
            "summary_status": "unavailable",
        }
    row = conn.execute(
        """SELECT records,symbols,latest_available_time,missing_pit,measurement
             FROM dc_research_asset_summary WHERE asset_key=?""",
        ("report_rc",),
    ).fetchone()
    if row is None:
        return {
            "records": 0, "symbols": 0, "latest_available_time": "",
            "missing_pit": 0, "measurement": "summary_pending",
            "summary_status": "pending",
        }
    return {
        **dict(row),
        "summary_status": "ready",
    }


def audit_research_data_assets() -> dict[str, Any]:
    from app.services.theme_revenue_exposure import _ensure_tables as ensure_theme_revenue_tables

    ensure_theme_revenue_tables()
    with _conn() as conn:
        metric_rows = [dict(row) for row in conn.execute(
            """SELECT d.metric_code,d.metric_name,d.description,d.source_code,COUNT(v.metric_code) value_count,
                      MAX(v.available_time) latest_available_time
                 FROM dc_industry_metric_definition d LEFT JOIN dc_industry_metric_value v ON v.metric_code=d.metric_code
                 GROUP BY d.metric_code,d.metric_name,d.description"""
        ).fetchall()]
        report = _report_rc_summary(conn)
        # The mapping table contains millions of point-in-time rows.  A daily
        # audit must not rescan the whole history merely to display an
        # informational total.  PostgreSQL's labelled live-row estimate is
        # sufficient for depth reporting, while snapshot-date coverage remains
        # exact on the much smaller canonical theme snapshot table.
        theme_mapping = conn.execute(
            """SELECT COALESCE(n_live_tup,0) mappings
                 FROM pg_stat_user_tables
                WHERE relname='dc_theme_company_mapping'"""
        ).fetchone()
        theme_snapshot = conn.execute(
            """SELECT COUNT(DISTINCT as_of_date) snapshot_dates,
                      MAX(as_of_date) latest_date
                 FROM dc_theme_snapshot"""
        ).fetchone()
        revenue_theme = conn.execute("""SELECT COUNT(*) exposures,COUNT(DISTINCT ts_code) companies,
                                             COUNT(DISTINCT theme_code) themes,
                                             (SELECT MAX(as_of_date) FROM dc_theme_revenue_exposure_runs) latest_date
                                      FROM dc_theme_revenue_exposure_current""").fetchone()
        announcements = conn.execute("""SELECT COUNT(*) raw_records,MAX(available_time) latest_available_time
                                        FROM dc_announcement_raw""").fetchone()
        events = conn.execute("SELECT COUNT(*) events,AVG(confidence) avg_confidence FROM dc_announcement_event").fetchone()
        ocr = conn.execute(
            """SELECT (SELECT COUNT(*) FROM dc_research_ocr_observation) observations,
                      (SELECT COUNT(*) FROM dc_research_ocr_document) documents,
                      (SELECT COUNT(*) FROM dc_research_ocr_observation
                        WHERE validation_status='structured_reconciled') reconciled,
                      (SELECT MAX(available_time) FROM dc_research_ocr_observation) latest_available_time"""
        ).fetchone()

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
    report_data = {"records": int(_value(report, "records")), "symbols": int(_value(report, "symbols")), "latest_available_time": str(_value(report, "latest_available_time", "")), "missing_point_in_time_records": int(_value(report, "missing_pit")), "measurement": str(_value(report, "measurement", "summary_missing")), "summary_status": str(_value(report, "summary_status", "unavailable"))}
    theme_data = {"mappings": int(_value(theme_mapping, "mappings")), "mappings_count_method": "postgres_live_row_estimate", "snapshot_dates": int(_value(theme_snapshot, "snapshot_dates")), "latest_date": str(_value(theme_snapshot, "latest_date", "")), "revenue_exposures": int(_value(revenue_theme, "exposures")), "revenue_companies": int(_value(revenue_theme, "companies")), "revenue_themes": int(_value(revenue_theme, "themes")), "revenue_latest_date": str(_value(revenue_theme, "latest_date", ""))}
    # Compute the manual benchmark once.  The queue status normally embeds the
    # same benchmark, which previously caused this audit to repeat the join
    # and open another database connection under load.
    benchmark = announcement_label_benchmark()
    provider_reference = announcement_provider_reference_benchmark(
        reference_contract=CNINFO_CATEGORY_HOLDOUT_CONTRACT,
        benchmark_role="frozen_holdout",
    )
    manual_review_queue = announcement_manual_review_queue_status(
        include_items=False,
        include_benchmark=False,
    )
    manual_review_queue["manual_gold"] = benchmark
    professional_metrics = audit_professional_metric_candidates()
    announcement_data = {
        "raw_records": int(_value(announcements, "raw_records")),
        "events": int(_value(events, "events")),
        "latest_available_time": str(_value(announcements, "latest_available_time", "")),
        "avg_rule_confidence": round(float(_value(events, "avg_confidence", 0) or 0), 4),
        "manual_gold_benchmark": benchmark,
        "manual_review_queue": manual_review_queue,
        "provider_category_reference": provider_reference,
    }
    ocr_data = {
        "observations": int(_value(ocr, "observations")),
        "documents": int(_value(ocr, "documents")),
        "reconciled": int(_value(ocr, "reconciled")),
        "pending_reconciliation": max(0, int(_value(ocr, "observations")) - int(_value(ocr, "reconciled"))),
        "latest_available_time": str(_value(ocr, "latest_available_time", "")),
        "status": "reconciled" if int(_value(ocr, "observations")) and int(_value(ocr, "observations")) == int(_value(ocr, "reconciled")) else "candidate_only" if int(_value(ocr, "observations")) else "missing",
        "production_eligible": False,
        "contract": "alis_caam_macos_vision_ocr_candidate_v1",
    }
    gaps = []
    for key, item in industry.items():
        if item["status"] in {"missing", "configured"}: gaps.append(f"industry:{key}")
        if item["status"] == "proxy_ready": gaps.append(f"industry:{key}:direct_source_not_integrated")
    if report_data["summary_status"] != "ready":
        gaps.append(f"analyst_consensus:summary_{report_data['summary_status']}")
    elif not report_data["records"] or report_data["missing_point_in_time_records"]:
        gaps.append("analyst_consensus:point_in_time_coverage")
    # Existing theme mappings are evidence-based narrative exposures, not a
    # revenue-segment dataset; retain that distinction in the contract.
    if not theme_data["revenue_exposures"]:
        gaps.append("theme_revenue_exposure:source_not_materialized")
    if provider_reference["status"] != "covered_type_holdout_ready":
        gaps.append("announcement_classification:provider_category_reference_not_ready")
    if benchmark["status"] != "ready":
        gaps.append("announcement_extraction_accuracy:manual_gold_not_ready")
    if ocr_data["pending_reconciliation"]:
        gaps.append("industry_ocr:structured_reconciliation_pending")
    if professional_metrics["candidates"]:
        gaps.append("professional_operating_metrics:manual_verification_pending")
    return {
        "ok": True,
        "data_available": True,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema_version": "alis_research_data_assets_v1",
        "status": "ready" if not gaps else "partial",
        "industry_components": industry,
        "analyst_consensus_pit": {**report_data, "status": "ready" if report_data["records"] and not report_data["missing_point_in_time_records"] else "partial"},
        "theme_exposure": {**theme_data, "status": "ready" if theme_data["revenue_exposures"] else "proxy_only", "limitation": "价格/新闻主题映射与主营业务收入暴露分开保存；收入暴露只使用点时主营业务分部。"},
        "announcement_extraction": {
            **announcement_data,
            "status": "ready" if benchmark["status"] == "ready" else "partial",
            "limitation": "官方分类参考集只验证其覆盖类型；规则置信度不等于准确率，完整误报、漏检和正文数值字段仍需分层人工金标准。",
        },
        "industry_ocr_candidates": ocr_data,
        "professional_operating_metric_candidates": professional_metrics,
        "gaps": gaps,
        "policy": "only_point_in_time_sources_are_production_eligible; proxies_and_unmeasured_extraction_cannot_satisfy_research_depth_gate",
    }
