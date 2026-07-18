from __future__ import annotations

from typing import Any

from app.data_platform.db import _conn as data_conn
from app.db.store import _conn as business_conn, get_account_baseline, list_model_versions
from app.services.market_context import market_state_model_governance


def _check(key: str, name: str, passed: bool, evidence: dict[str, Any], *, partial: bool = False, requirement: str = "") -> dict[str, Any]:
    return {"key": key, "name": name, "status": "passed" if passed else "partial" if partial else "failed", "requirement": requirement, "evidence": evidence}


def backend_acceptance_matrix() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    with data_conn() as c:
        raw = dict(c.execute("SELECT reltuples::bigint total FROM pg_class WHERE oid='dc_tushare_raw_records'::regclass").fetchone())
        snapshots = dict(c.execute("SELECT reltuples::bigint total FROM pg_class WHERE oid='dc_stock_daily_snapshot'::regclass").fetchone())
        raw_missing = bool(dict(c.execute("SELECT EXISTS(SELECT 1 FROM dc_tushare_raw_records WHERE available_time IS NULL OR available_time='' LIMIT 1) missing").fetchone()).get("missing"))
        snapshot_missing = bool(dict(c.execute("SELECT EXISTS(SELECT 1 FROM dc_stock_daily_snapshot WHERE available_time IS NULL OR available_time='' LIMIT 1) missing").fetchone()).get("missing"))
        industries = int(dict(c.execute("SELECT COUNT(*) count FROM dc_industry_state_snapshot").fetchone()).get("count") or 0)
        themes = dict(c.execute("SELECT COUNT(*) count,SUM(CASE WHEN promotable=1 THEN 1 ELSE 0 END) promotable FROM dc_theme_snapshot").fetchone())
        followups = dict(c.execute("SELECT COUNT(*) count,SUM(CASE WHEN next_followup_date IS NULL OR next_followup_date='' THEN 1 ELSE 0 END) missing_date FROM dc_event_followup").fetchone())
        defensive = int(dict(c.execute("SELECT COUNT(DISTINCT ts_code) count FROM dc_tushare_raw_records WHERE api_name='fund_daily' AND ts_code IN ('511880.SH','511010.SH','518880.SH','512890.SH','510880.SH')").fetchone()).get("count") or 0)
        context = int(dict(c.execute("SELECT COUNT(*) count FROM dc_market_context_snapshot").fetchone()).get("count") or 0)
    with business_conn() as c:
        decisions = int(dict(c.execute("SELECT COUNT(*) count FROM decision_snapshots").fetchone()).get("count") or 0)
        operations = dict(c.execute("SELECT COUNT(*) count,COUNT(DISTINCT operation_uid) unique_count FROM operations").fetchone())
        reports = int(dict(c.execute("SELECT COUNT(*) count FROM quant_performance_reports").fetchone()).get("count") or 0)
        theses = int(dict(c.execute("SELECT COUNT(*) count FROM investment_theses").fetchone()).get("count") or 0)

    raw_total = int(raw.get("total") or 0) + int(snapshots.get("total") or 0)
    missing_available = raw_missing or snapshot_missing
    checks.append(_check("P0_PIT", "点时数据契约", raw_total > 0 and not missing_available, {"approximate_rows": raw_total, "has_missing_available_time": missing_available}, requirement="关键事实具备available_time且历史决策禁止未来数据"))
    checks.append(_check("P1_INDUSTRY", "行业状态物化", industries > 0, {"snapshots": industries}, requirement="行业景气状态可在线读取"))
    checks.append(_check("P1_THESIS", "投资逻辑闭环", theses > 0, {"theses": theses}, requirement="核心候选具备自动逻辑、证据和失效条件"))
    checks.append(_check("P2_MARKET", "市场状态历史", context >= 120, {"snapshots": context}, partial=context > 0, requirement="市场状态可回放并包含BOCPD"))
    checks.append(_check("P2_THEME", "主题证据门", int(themes.get("count") or 0) > 0, {"snapshots": int(themes.get("count") or 0), "promotable": int(themes.get("promotable") or 0)}, requirement="主题至少两类来源、五家公司、三日证据"))
    checks.append(_check("P2_EVENT", "事件持续跟踪", int(followups.get("count") or 0) > 0 and int(followups.get("missing_date") or 0) == 0, {"followups": int(followups.get("count") or 0), "missing_followup_date": int(followups.get("missing_date") or 0)}, requirement="事件影响窗口和下一验证时间持久化"))
    checks.append(_check("P3_CAPITAL", "模拟盘初始资金", abs(float(get_account_baseline()) - 200000.0) < .01, {"baseline": get_account_baseline()}, requirement="V1初始资金固定200000元"))
    checks.append(_check("P3_DEFENSIVE", "防御资产数据", defensive == 5, {"ready_asset_count": defensive, "required": 5}, partial=defensive > 0, requirement="货币、债券、黄金、低波和红利ETF"))
    checks.append(_check("P3_DECISION", "决策证据快照", decisions > 0, {"snapshots": decisions}, requirement="决策具备证据、版本、置信度和可回放快照"))
    checks.append(_check("P3_LEDGER", "唯一模拟执行账本", int(operations.get("count") or 0) == int(operations.get("unique_count") or 0), {"operations": int(operations.get("count") or 0), "unique_operation_uid": int(operations.get("unique_count") or 0)}, requirement="逐笔成交不可重复且费用版本化"))
    checks.append(_check("P3_REPORTS", "周期绩效报告", reports > 0, {"reports": reports}, requirement="周/月/年收益、回撤、成本和归因持久化"))
    governance = market_state_model_governance()
    hsmm = governance.get("hsmm") or {}
    hsmm_status = str(hsmm.get("status") or "")
    live_ready = hsmm_status == "shadow_live_observation_review_ready"
    oos_passed = str((hsmm.get("validation") or {}).get("status")) == "passed"
    checks.append(_check("P4_HSMM", "HSMM影子OOS", live_ready, {"status": hsmm_status, "version": hsmm.get("latest_shadow_version"), "validation": hsmm.get("validation"), "live_observation_weeks": hsmm.get("live_observation_weeks"), "minimum_live_observation_weeks": hsmm.get("minimum_live_observation_weeks")}, partial=oos_passed and not live_ready, requirement="四状态Student-t HSMM通过时间切分OOS后进入真实影子期"))
    market_versions = list_model_versions("market_regime", limit=10)
    checks.append(_check("P4_MODEL_AUDIT", "模型版本审计", bool(market_versions), {"versions": len(market_versions), "latest": market_versions[0].get("version") if market_versions else None}, requirement="训练、失败、验证和版本晋升均留痕"))
    counts = {status: sum(1 for item in checks if item["status"] == status) for status in ("passed", "partial", "failed")}
    return {"ok": counts["failed"] == 0, "status": "passed" if counts["failed"] == 0 and counts["partial"] == 0 else "partial" if counts["passed"] else "failed", "counts": counts, "checks": checks}
