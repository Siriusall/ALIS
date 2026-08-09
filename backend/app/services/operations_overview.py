from __future__ import annotations

from typing import Any

from app.data_platform.db import _conn as _data_conn
from app.data_platform.services.observability import get_observability
from app.data_platform.services.readiness import readiness_overview
from app.db.store import (
    get_account_balance,
    get_account_baseline,
    get_account_high_water_mark,
    get_positions,
    list_model_versions,
    list_quant_run_tasks,
)
from app.services.backtest_data_source import BacktestDataSource
from app.services.data_health import build_data_health_report
from app.services.decision_store import list_decision_snapshots, list_position_states, list_reviews, summarize_decision_audit
from app.services.model_registry import get_model_status
from app.services.quant_risk import (
    constrained_cvar_weights,
    historical_cvar,
    hrp_fallback_weights,
    ledoit_wolf_covariance,
)
from app.services.quant_run_engine import list_consolidated_trade_dates
from app.services.rebalance_allocator import correlated_industry_group
from app.services.five_bucket_contract import build_target_weight_contract, summarize_bucket_contracts
from app.services.position_valuation import point_in_time_position_market_series
from app.services.research_engine import capability_registry
from app.services.risk_limit_contract import (
    DEFAULT_CORRELATED_INDUSTRY_LIMIT,
    DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT,
    DEFAULT_INDUSTRY_LIMIT,
)
from app.services.thesis_store import list_theses_for_symbols


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_overview_call(name: str, fn, fallback: Any) -> tuple[Any, dict[str, str] | None]:
    """Keep the governance page useful when one data domain is unavailable."""
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001
        return fallback, {"domain": name, "error": str(exc)[:200]}


def _cached_readiness_overview() -> dict[str, Any]:
    """Return a governance-safe readiness snapshot without scanning raw history."""
    try:
        from app.data_platform.redis_store import get_json

        cached = get_json("alis:data-platform:readiness:v2")
        if isinstance(cached, dict):
            return cached
    except Exception:
        pass
    return {
        "ok": True,
        "scope": {},
        "consumers": [],
        "summary_only": True,
        "status_definition": {
            "ready": "满足正式运行要求",
            "degraded": "可运行但必须标注数据风险",
            "blocked": "关键数据不足，应禁止正式运行",
            "unknown": "尚无足够证据",
        },
    }


def _is_actionable_paused_task(task: dict[str, Any]) -> bool:
    """Exclude normal autonomous waiting states from the exception queue."""

    if str(task.get("status") or "") != "paused":
        return False
    if str(task.get("error") or "").strip():
        return True
    replay_status = str(task.get("replay_status") or "")
    if replay_status in {"completed", "waiting_formal_plan", "no_gap"}:
        return False
    if str(task.get("replay_mode") or "") == "autonomous_daily":
        message = str(task.get("last_message") or "")
        if message in {
            "自动任务参数已与当前 Champion 同步",
            "自治任务等待下一份到期正式调仓计划",
        }:
            return False
    return True


def _stress_tests(exposures: list[dict[str, Any]], equity: float) -> list[dict[str, Any]]:
    """Deterministic, disclosure-friendly portfolio stress scenarios."""
    market_value = sum(_f(item.get("market_value")) for item in exposures)
    if equity <= 0 or market_value <= 0:
        return []
    largest = max(exposures, key=lambda item: _f(item.get("market_value")), default={})
    by_industry: dict[str, float] = {}
    for item in exposures:
        industry = str(item.get("industry") or "unclassified")
        by_industry[industry] = by_industry.get(industry, 0.0) + _f(item.get("market_value"))
    largest_industry, industry_value = max(by_industry.items(), key=lambda item: item[1], default=("unclassified", 0.0))
    definitions = [
        ("broad_market_down_8pct", "全市场下跌 8%", market_value * -.08, "全部权益持仓同步冲击"),
        ("largest_position_down_20pct", "最大单仓下跌 20%", _f(largest.get("market_value")) * -.20, f"{largest.get('symbol') or '—'} 单仓冲击"),
        ("largest_industry_down_12pct", "最大行业下跌 12%", industry_value * -.12, f"{largest_industry} 行业集中冲击"),
    ]
    return [{"code": code, "name": name, "pnl": round(pnl, 2), "equity_after": round(equity + pnl, 2), "loss_pct": round(pnl / equity, 6), "assumption": assumption, "severity": "error" if pnl / equity <= -.10 else "warning" if pnl / equity <= -.05 else "info"} for code, name, pnl, assumption in definitions]


def _deleveraging_actions(exposures: list[dict[str, Any]], breaches: list[dict[str, Any]], stress_tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for exposure in exposures:
        limit, weight = _f(exposure.get("single_name_limit")), _f(exposure.get("weight"))
        if limit > 0 and weight > limit:
            actions.append({"action": "reduce", "symbol": exposure.get("symbol"), "priority": "high", "target_weight": round(limit, 6), "reason": "single_name_limit"})
    for breach in breaches:
        if breach.get("code") == "industry_limit":
            actions.append({"action": "reduce_industry", "industry": breach.get("industry"), "priority": "high", "target_weight": breach.get("limit"), "reason": "industry_concentration_limit"})
    if any(item.get("code") == "drawdown_limit" for item in breaches):
        actions.append({"action": "freeze_new_exposure", "priority": "critical", "reason": "drawdown_limit"})
    if any(item.get("severity") == "error" for item in stress_tests):
        actions.append({"action": "reduce_risk_budget", "priority": "high", "risk_budget_multiplier": .5, "reason": "stress_loss_over_10pct"})
    return actions


def build_live_portfolio_risk(
    lookback: int = 120,
    *,
    valuation_cache: dict[str, dict[str, Any]] | None = None,
    positions: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    positions = [x for x in (positions if positions is not None else get_positions()) if int(x.get("quantity") or 0) > 0]
    if as_of is None:
        dates = list_consolidated_trade_dates()
        as_of = dates[-1] if dates else ""
    ds = BacktestDataSource()
    returns_by_symbol: dict[str, list[float]] = {}
    exposures: list[dict[str, Any]] = []
    cash = _f(get_account_balance())
    baseline = _f(get_account_baseline())
    market_value = 0.0
    for item in positions:
        symbol = str(item.get("symbol") or "").strip()
        valuation = (valuation_cache or {}).get(symbol)
        if valuation is None:
            valuation = point_in_time_position_market_series(
                symbol=symbol,
                as_of=as_of,
                fallback_price=_f(item.get("price")),
                lookback=lookback,
                data_source=ds,
            )
        price = _f(valuation.get("price"))
        returns = list(valuation.get("returns") or [])
        price_source = str(valuation.get("price_source") or "")
        if returns:
            returns_by_symbol[symbol] = returns
        value = price * int(item.get("quantity") or 0)
        market_value += value
        exposures.append({
            "symbol": symbol,
            "name": str(item.get("name") or ""),
            "market_price": round(price, 6),
            "market_value": round(value, 2),
            "price_source": price_source,
            "observations": len(returns),
        })
    equity = cash + market_value
    for item in exposures:
        item["weight"] = round(item["market_value"] / equity, 6) if equity > 0 else 0.0
    exposures.sort(key=lambda x: x["weight"], reverse=True)
    if exposures:
        codes = [f"{item['symbol']}.SH" if str(item["symbol"]).startswith(("6", "9")) else f"{item['symbol']}.SZ" for item in exposures]
        placeholders = ",".join("?" for _ in codes)
        try:
            with _data_conn() as c:
                rows = c.execute(f"SELECT ts_code,industry FROM dc_stock_master WHERE ts_code IN ({placeholders})", tuple(codes)).fetchall()
            industry_by_symbol = {str(dict(row).get("ts_code") or "").split(".")[0]: str(dict(row).get("industry") or "unclassified") for row in rows}
        except Exception:  # noqa: BLE001
            industry_by_symbol = {}
        try:
            from app.services.defensive_assets import DEFENSIVE_ASSETS
            industry_by_symbol.update({
                str(item["ts_code"]).split(".")[0]: f"defensive_{item['asset_class']}"
                for item in DEFENSIVE_ASSETS
            })
        except Exception:  # noqa: BLE001
            pass
    for item in exposures:
        item["industry"] = industry_by_symbol.get(str(item["symbol"]), "unclassified")
    weights = constrained_cvar_weights(returns_by_symbol, max_single=DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)
    optimizer = "ledoit_wolf_shrinkage_plus_constrained_cvar_v1"
    if returns_by_symbol and not weights:
        weights = hrp_fallback_weights(returns_by_symbol, max_single=DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT)
        optimizer = "hrp_inverse_volatility_fallback_v1"
    black_litterman = {
        "status": "research_only_not_applied",
        "production_eligible": False,
        "method": "black_litterman_diagonal_v1",
        "reason": (
            "当前实现缺少完整P/Q/Omega与后验分布；正式目标权重只读取"
            "统一组合方法竞赛赢家，不再叠加第二次观点倾斜。"
        ),
    }
    min_len = min((len(x) for x in returns_by_symbol.values()), default=0)
    portfolio_returns = []
    if min_len:
        equal_weight = 1 / len(returns_by_symbol)
        portfolio_returns = [sum(values[-min_len + i] * equal_weight for values in returns_by_symbol.values()) for i in range(min_len)]
    tail = historical_cvar(portfolio_returns)
    peak = max(baseline, _f(get_account_high_water_mark()), equity)
    drawdown = equity / peak - 1 if peak > 0 else 0.0
    breaches: list[dict[str, Any]] = []
    if drawdown <= -.10:
        breaches.append({"code": "drawdown_limit", "severity": "error", "message": f"高水位回撤 {drawdown:.1%} 达到10%风险门"})
    if len(exposures) < 5 and market_value > 0:
        breaches.append({"code": "diversification", "severity": "warning", "message": "权益持仓少于5只，需说明集中配置原因"})
    theses = list_theses_for_symbols([str(item.get("symbol") or "") for item in exposures])
    thesis_by_symbol = {str(x.get("ts_code") or "").split(".")[0]: x for x in theses}
    for item in exposures:
        thesis = thesis_by_symbol.get(item["symbol"])
        item["thesis_status"] = thesis.get("derived_status") if thesis else "missing"
        item["thesis_confidence"] = _f(thesis.get("derived_confidence")) if thesis else 0.0
        is_core = bool(
            thesis
            and item["thesis_confidence"] >= 75.0
            and item["thesis_status"] in {"active", "confirmed", "valid"}
        )
        item["position_class"] = "core" if is_core else "standard"
        item["single_name_limit"] = DEFAULT_GLOBAL_SINGLE_ASSET_LIMIT
        if item["weight"] > item["single_name_limit"]:
            breaches.append({
                "code": "single_name_limit",
                "symbol": item["symbol"],
                "severity": "error",
                "message": f"单票权重 {item['weight']:.1%} 超过 {item['single_name_limit']:.0%} 上限",
            })
        if not thesis:
            breaches.append({"code": "missing_thesis", "symbol": item["symbol"], "severity": "warning", "message": "持仓缺少投资逻辑卡片"})
    industry_exposures: list[dict[str, Any]] = []
    by_industry: dict[str, float] = {}
    for item in exposures:
        industry = str(item.get("industry") or "unclassified")
        by_industry[industry] = by_industry.get(industry, 0.0) + _f(item.get("weight"))
    industry_limit = DEFAULT_INDUSTRY_LIMIT
    for industry, weight in sorted(by_industry.items(), key=lambda value: value[1], reverse=True):
        industry_exposures.append({"industry": industry, "weight": round(weight, 6), "limit": industry_limit, "status": "breach" if weight > industry_limit else "within_limit"})
        if weight > industry_limit:
            breaches.append({"code": "industry_limit", "industry": industry, "severity": "error", "limit": industry_limit, "message": f"行业权重 {weight:.1%} 超过 {industry_limit:.0%} 上限"})
    by_correlated_group: dict[str, float] = {}
    for industry, weight in by_industry.items():
        group = correlated_industry_group(industry)
        by_correlated_group[group] = by_correlated_group.get(group, 0.0) + weight
    correlated_industry_exposures = []
    for group, weight in sorted(by_correlated_group.items(), key=lambda value: value[1], reverse=True):
        status = "breach" if weight > DEFAULT_CORRELATED_INDUSTRY_LIMIT else "within_limit"
        correlated_industry_exposures.append({"group": group, "weight": round(weight, 6), "limit": DEFAULT_CORRELATED_INDUSTRY_LIMIT, "status": status})
        if status == "breach":
            breaches.append({"code": "correlated_industry_limit", "group": group, "severity": "error", "limit": DEFAULT_CORRELATED_INDUSTRY_LIMIT, "message": f"高度相关行业组权重 {weight:.1%} 超过 {DEFAULT_CORRELATED_INDUSTRY_LIMIT:.0%} 上限"})
    weight_explanations = []
    target_weight_contracts = []
    for item in exposures:
        target = _f(weights.get(item["symbol"]))
        reasons = []
        if target <= 0:
            reasons.append("历史收益观测不足，未生成优化目标")
        else:
            reasons.append("基于收缩协方差与约束 CVaR 分配")
        if target > _f(item.get("single_name_limit")):
            reasons.append("受单票上限裁剪")
        if any(x.get("industry") == item.get("industry") and x.get("status") == "breach" for x in industry_exposures):
            reasons.append("所在行业超过集中度上限，需整体降权")
        contract = build_target_weight_contract(
            symbol=str(item["symbol"]),
            current_weight=_f(item.get("weight")),
            target_weight=target,
            score=_f(item.get("thesis_confidence")),
            evidence={
                "return_source": "基于收缩协方差与约束CVaR的目标权重",
                "exit_conditions": ["单票/行业/CVaR约束触发", "投资逻辑失效", "组合回撤触发降风险"],
            },
            data_status="ready" if int(item.get("observations") or 0) > 0 else "insufficient",
        )
        target_weight_contracts.append(contract)
        weight_explanations.append({"symbol": item["symbol"], "current_weight": item["weight"], "target_weight": round(target, 6), "delta": round(target - _f(item["weight"]), 6), "reason": "；".join(reasons), "target_weight_contract": contract})
    stress_tests = _stress_tests(exposures, equity)
    deleveraging_actions = _deleveraging_actions(exposures, breaches, stress_tests)
    states_by_symbol = {str(item.get("ts_code") or "").split(".")[0]: item for item in list_position_states()}
    position_state_recommendations: list[dict[str, Any]] = []
    breach_codes_by_symbol: dict[str, set[str]] = {}
    for breach in breaches:
        symbol = str(breach.get("symbol") or "")
        if symbol:
            breach_codes_by_symbol.setdefault(symbol, set()).add(str(breach.get("code") or ""))
    for item in exposures:
        symbol = str(item["symbol"])
        current = states_by_symbol.get(symbol)
        current_state = str((current or {}).get("state") or "uninitialized")
        codes = breach_codes_by_symbol.get(symbol, set())
        thesis_status = str(item.get("thesis_status") or "missing")
        thesis_confidence = _f(item.get("thesis_confidence"))
        if thesis_status == "invalidated":
            recommended, reason = "reduce", "investment_thesis_invalidated"
        elif "single_name_limit" in codes:
            recommended, reason = "reduce", "single_name_risk_limit_breached"
        elif thesis_status in {"missing", "warning", "draft"} or thesis_confidence < 75.0:
            recommended, reason = "warning", "thesis_evidence_missing_or_below_core_threshold"
        else:
            continue
        position_state_recommendations.append({
            "symbol": symbol, "current_state": current_state, "recommended_state": recommended,
            "reason": reason,
            "evidence": {"thesis_status": thesis_status, "thesis_confidence": thesis_confidence, "risk_breaches": sorted(codes)},
            "requires_confirmation": True,
        })
    return {
        "ok": not any(x["severity"] == "error" for x in breaches),
        "status": "blocked" if any(x["severity"] == "error" for x in breaches) else "warning" if breaches else "ready",
        "as_of": as_of,
        "account": {"cash": round(cash, 2), "market_value": round(market_value, 2), "equity": round(equity, 2), "high_water_mark": round(peak, 2), "drawdown": round(drawdown, 6)},
        "tail_risk": tail,
        "optimizer": optimizer if returns_by_symbol else "unavailable",
        "black_litterman": black_litterman,
        "target_weights": weights,
        "target_weight_contracts": target_weight_contracts,
        "five_bucket_summary": summarize_bucket_contracts(target_weight_contracts),
        "weight_explanations": weight_explanations,
        "industry_exposures": industry_exposures,
        "correlated_industry_exposures": correlated_industry_exposures,
        "covariance": ledoit_wolf_covariance(returns_by_symbol) if returns_by_symbol else {},
        "exposures": exposures,
        "breaches": breaches,
        "stress_tests": stress_tests,
        "deleveraging_actions": deleveraging_actions,
        "position_state_recommendations": position_state_recommendations,
        "limitations": [] if returns_by_symbol else ["持仓历史收益不足，无法计算组合CVaR和目标风险权重"],
    }


def build_governance_overview() -> dict[str, Any]:
    health, health_error = _safe_overview_call("data_health", lambda: build_data_health_report(limit_days=30), {"ok": False, "issues": []})
    readiness, readiness_error = _safe_overview_call("data_readiness", _cached_readiness_overview, {"consumers": []})
    observability, observability_error = _safe_overview_call("observability", lambda: get_observability(limit_logs=20), {"logs": []})
    model, model_error = _safe_overview_call("model", get_model_status, {})
    capabilities, capabilities_error = _safe_overview_call("capabilities", capability_registry, [])
    tasks, tasks_error = _safe_overview_call("tasks", lambda: list_quant_run_tasks(limit=20), [])
    versions, versions_error = _safe_overview_call("versions", lambda: list_model_versions(limit=20), [])
    errors = [item for item in (health_error, readiness_error, observability_error, model_error, capabilities_error, tasks_error, versions_error) if item]
    failed_tasks = [
        x for x in tasks
        if str(x.get("status") or "") == "failed"
        or _is_actionable_paused_task(x)
    ]
    unresolved_data = [x for x in observability.get("logs", []) if str(x.get("status") or "") == "failed"]
    readiness_states = [str(x.get("status") or "unknown") for x in readiness.get("consumers", [])]
    readiness_status = "blocked" if "blocked" in readiness_states else "degraded" if "degraded" in readiness_states else "ready" if readiness_states else "unknown"
    readiness_detail = "消费者数据就绪度"
    if readiness.get("summary_only"):
        readiness_detail = "深度就绪度后台计算中"
    gates = [
        {"key": "data_health", "status": "unknown" if health_error else "ready" if health.get("ok") else "blocked", "detail": f"{len(health.get('issues') or [])}项问题"},
        {"key": "data_readiness", "status": "unknown" if readiness_error else readiness_status, "detail": readiness_detail},
        {"key": "model", "status": "unknown" if model_error else "ready" if model.get("active_version") else "blocked", "detail": "生产模型版本"},
        {"key": "task_failures", "status": "unknown" if tasks_error else "ready" if not failed_tasks else "degraded", "detail": f"{len(failed_tasks)}个异常任务"},
    ]
    return {
        "ok": not errors and all(x["status"] == "ready" for x in gates),
        "errors": errors,
        "gates": gates,
        "capabilities": {status: sum(1 for x in capabilities if x["status"] == status) for status in ("ready", "partial", "building", "missing")},
        "model": {"active_version": model.get("active_version"), "latest_train_run": model.get("latest_train_run"), "version_count": len(versions)},
        "tasks": {"recent": tasks, "failed_or_paused": failed_tasks},
        "data": {"health": health, "readiness": readiness, "failed_runs": unresolved_data},
    }


def build_decision_audit(limit: int = 100, *, compact: bool = False) -> dict[str, Any]:
    if compact:
        return summarize_decision_audit(limit=limit)
    decisions = list_decision_snapshots(limit=limit)
    reviews = list_reviews(limit=limit)
    reviewed_ids = {int(x["decision_snapshot_id"]) for x in reviews if x.get("decision_snapshot_id")}
    missing = [x for x in decisions if int(x.get("id") or 0) not in reviewed_ids]
    future_violations = [x for x in decisions if str(x.get("data_as_of_time") or "") > str(x.get("decision_time") or "")]
    missing_evidence = [x for x in decisions if not x.get("market_state") or not x.get("research_state") or not x.get("model_version")]
    return {
        "ok": not future_violations and not missing_evidence,
        "counts": {"decisions": len(decisions), "reviews": len(reviews), "awaiting_review": len(missing)},
        "quality": {"future_data_violations": len(future_violations), "missing_evidence": len(missing_evidence)},
        "decisions": decisions,
        "reviews": reviews,
    }
