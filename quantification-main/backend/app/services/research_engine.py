from __future__ import annotations

from dataclasses import dataclass
import json
from statistics import pstdev
from datetime import datetime
from typing import Any

from app.data_platform.db import _conn
from app.db.store import _conn as _business_conn


def _clip(value: Any, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


@dataclass(frozen=True)
class DimensionContract:
    weights: dict[str, float]
    minimum_coverage: float = 0.60


CONTRACTS: dict[str, DimensionContract] = {
    "quality": DimensionContract({"profitability": .20, "capital_efficiency": .20, "cash_conversion": .20, "balance_sheet_safety": .15, "stability": .15, "shareholder_return": .10}),
    "growth": DimensionContract({"revenue_growth": .25, "core_profit_growth": .25, "three_year_cagr": .15, "growth_acceleration": .15, "growth_quality": .20}),
    "cashflow": DimensionContract({"ocf_to_profit": .25, "free_cashflow_yield": .20, "cashflow_stability": .20, "working_capital_quality": .20, "cash_debt_coverage": .15}),
    "governance": DimensionContract({"audit_regulatory": .25, "pledge": .20, "insider_transactions": .15, "related_party_control": .15, "capital_allocation": .15, "disclosure_quality": .10}),
    "industry": DimensionContract({"demand_orders_sales": .25, "product_price": .15, "inventory_cycle": .15, "capacity_utilization": .10, "profit_diffusion": .15, "policy_supply": .10, "industry_valuation": .10}),
    "expectation": DimensionContract({"forecast_revision": .25, "earnings_surprise": .20, "growth_acceleration": .20, "margin_trend": .15, "industry_confirmation": .10, "guidance_breadth": .10}),
    "valuation": DimensionContract({"historical_percentile": .20, "industry_relative": .15, "normalized_yield": .20, "scenario_value_gap": .25, "growth_adjusted": .10, "downside_protection": .10}),
}


HARD_RISK_KEYS = {
    "is_st": "ST或退市风险",
    "untradable": "无法交易",
    "fraud": "重大财务造假",
    "debt_default": "重大债务违约",
    "major_investigation": "重大立案调查",
    "adverse_audit": "严重非标审计意见",
    "critical_data_missing": "关键数据可信度不足",
    "illiquid": "流动性不足",
}

WARNING_RISK_KEYS = {
    "high_pledge": "高比例质押",
    "repeated_reduction": "连续减持",
    "goodwill_abnormal": "商誉占比异常",
    "receivable_inventory_abnormal": "应收或存货异常",
    "profit_cashflow_divergence": "利润与现金流背离",
    "cross_industry_ma": "频繁跨界并购",
    "large_related_party": "大额关联交易",
    "customer_concentration": "客户集中度高",
}


def evaluate_risk(flags: dict[str, Any]) -> dict[str, Any]:
    hard = [label for key, label in HARD_RISK_KEYS.items() if bool(flags.get(key))]
    warnings = [label for key, label in WARNING_RISK_KEYS.items() if bool(flags.get(key))]
    eligible = not hard
    risk_level = "blocked" if hard else "high" if len(warnings) >= 2 else "warning" if warnings else "normal"
    max_position = 0.0 if hard else 0.05 if len(warnings) >= 2 else 0.10 if warnings else 0.15
    return {
        "eligible": eligible,
        "risk_level": risk_level,
        "hard_reasons": hard,
        "warnings": warnings,
        "risk_budget_multiplier": 0.0 if hard else 0.5 if len(warnings) >= 2 else 0.75 if warnings else 1.0,
        "max_position": max_position,
        "manual_review_required": bool(hard or warnings),
    }


def score_dimension(name: str, observations: dict[str, Any]) -> dict[str, Any]:
    contract = CONTRACTS[name]
    available = {key: _clip(observations[key]) for key in contract.weights if observations.get(key) is not None}
    used_weight = sum(contract.weights[key] for key in available)
    contributions = {
        key: {"value": value, "weight": contract.weights[key], "weighted": round(value * contract.weights[key], 4)}
        for key, value in available.items()
    }
    score = sum(item["weighted"] for item in contributions.values()) / used_weight if used_weight else 0.0
    coverage = used_weight / sum(contract.weights.values())
    confidence = min(1.0, coverage) * _clip(observations.get("evidence_confidence", 100.0)) / 100.0
    return {
        "score": round(score, 4),
        "coverage": round(coverage, 4),
        "confidence": round(confidence, 4),
        "status": "ready" if coverage >= contract.minimum_coverage else "insufficient_data",
        "components": contributions,
        "missing": [key for key in contract.weights if key not in available],
    }


def classify_company(*, industry: str, metrics: dict[str, Any]) -> dict[str, str]:
    text = str(industry or "未分类")
    if any(x in text for x in ("银行", "保险", "证券")):
        business_model = "金融杠杆型"
    elif any(x in text for x in ("煤炭", "石油", "有色", "钢铁", "化工")):
        business_model = "资源周期型"
    elif any(x in text for x in ("软件", "互联网", "传媒")):
        business_model = "平台或订阅型"
    elif any(x in text for x in ("食品", "饮料", "家电", "医药")):
        business_model = "品牌或渠道驱动型"
    else:
        business_model = "规模制造或项目制"
    growth = float(metrics.get("revenue_growth") or 0)
    profit = float(metrics.get("core_profit_growth") or 0)
    if growth >= 25 and profit >= 25:
        lifecycle = "高速成长"
    elif growth >= 8 and profit >= 5:
        lifecycle = "稳定成长"
    elif growth < -10 and profit < -10:
        lifecycle = "衰退"
    elif profit > 10 and growth <= 5:
        lifecycle = "困境反转"
    else:
        lifecycle = "成熟或周期观察"
    return {"industry": text, "business_model": business_model, "lifecycle": lifecycle}


def research_state(dimensions: dict[str, dict[str, Any]], risk: dict[str, Any]) -> dict[str, Any]:
    scores = {key: float(value.get("score") or 0) for key, value in dimensions.items()}
    governance_ready = dimensions.get("governance", {}).get("status") == "ready"
    if not risk.get("eligible") or (governance_ready and scores.get("governance", 100) < 40):
        label = "风险恶化"
    elif scores.get("quality", 0) >= 75 and scores.get("valuation", 0) >= 70:
        label = "高质量低估"
    elif scores.get("quality", 0) >= 75 and scores.get("valuation", 0) < 40:
        label = "高质量但过贵"
    elif scores.get("expectation", 0) >= 70:
        label = "预期上调"
    elif scores.get("industry", 0) >= 70 and scores.get("growth", 0) >= 55:
        label = "景气反转"
    else:
        label = "研究观察"
    # Missing optional dimensions must reduce confidence, not force it to zero.
    # The previous minimum made a real quality/growth assessment indistinguishable
    # from an assessment with no observations whenever one planned dimension had
    # not yet been ingested.  Average over the complete contract so missing
    # evidence remains an explicit penalty while observed evidence is retained.
    confidence_values = [float(v.get("confidence") or 0) for v in dimensions.values()]
    confidence = sum(confidence_values) / len(CONTRACTS) if confidence_values else 0.0
    covered_dimensions = sum(1 for value in dimensions.values() if float(value.get("coverage") or 0) > 0)
    return {
        "classification": label,
        "scores": scores,
        "confidence": round(confidence, 4),
        "covered_dimensions": covered_dimensions,
        "total_dimensions": len(CONTRACTS),
        "eligible": bool(risk.get("eligible")),
    }


def capability_registry() -> list[dict[str, Any]]:
    return [
        {"key": "market", "name": "市场总览", "status": "partial", "api": "/api/decision/summary", "missing": ["真实HSMM状态概率", "主题摘要"]},
        {"key": "data", "name": "数据中台", "status": "ready", "api": "/api/data-center/status", "missing": []},
        {"key": "stock_research", "name": "股票研究中心", "status": "building", "api": "/api/research/evaluate", "missing": ["实际财务字段映射", "历史证据快照"]},
        {"key": "industry", "name": "行业研究中心", "status": "partial", "api": "/api/data-center/assets", "missing": ["行业状态机", "公司映射"]},
        {"key": "events", "name": "公告事件中心", "status": "partial", "api": "/api/data-center/status", "missing": ["全事件校验", "跟进状态"]},
        {"key": "sentiment", "name": "市场情绪中心", "status": "partial", "api": "/api/model-strategy/market-context", "missing": ["真实观测持久化"]},
        {"key": "themes", "name": "主题叙事中心", "status": "partial", "api": "/api/model-strategy/market-context", "missing": ["自动发现", "业务暴露证据"]},
        {"key": "expectation", "name": "预期变化中心", "status": "building", "api": "/api/research/evaluate", "missing": ["版本化预测基线"]},
        {"key": "valuation", "name": "估值中心", "status": "building", "api": "/api/research/evaluate", "missing": ["三情景价值"]},
        {"key": "pool", "name": "股票池与筛选中心", "status": "partial", "api": "/api/candidates", "missing": ["三级股票池"]},
        {"key": "thesis", "name": "投资逻辑中心", "status": "partial", "api": "/api/research/theses", "missing": ["自动证据采集与定时复核"]},
        {"key": "portfolio", "name": "组合管理中心", "status": "partial", "api": "/api/positions", "missing": ["目标权重解释"]},
        {"key": "risk", "name": "持仓风险中心", "status": "partial", "api": "/api/model-strategy/portfolio-risk", "missing": ["逐仓逻辑风险"]},
        {"key": "validation", "name": "回测与模型验证", "status": "partial", "api": "/api/model-strategy/status", "missing": ["多周期统一验收"]},
        {"key": "decisions", "name": "决策日志", "status": "partial", "api": "/api/logs", "missing": ["证据版本"]},
        {"key": "review", "name": "复盘中心", "status": "missing", "api": None, "missing": ["决策回放", "归因"]},
        {"key": "governance", "name": "系统任务与模型治理", "status": "partial", "api": "/api/model-strategy/status", "missing": ["SLO统一视图"]},
        {"key": "strategy", "name": "策略工作台", "status": "partial", "api": "/api/model-strategy/compare", "missing": ["版本回退流程"]},
    ]


def _linear(value: Any, bad: float, good: float) -> float | None:
    if value is None:
        return None
    number = float(value)
    if good == bad:
        return 50.0
    return _clip((number - bad) / (good - bad) * 100.0)


def _payloads(ts_code: str, api: str, limit: int = 12) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT period,available_time,payload_json FROM dc_tushare_raw_records WHERE api_name=? AND ts_code=? ORDER BY period DESC,available_time DESC LIMIT ?",
            (api, ts_code, limit),
        ).fetchall()
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        period = str(item.get("period") or "")
        if period in seen:
            continue
        seen.add(period)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        values.append({"period": period, "available_time": item.get("available_time"), "payload": payload})
    return values


def _forecast_history(ts_code: str, limit: int = 16) -> list[dict[str, Any]]:
    """Return point-in-time company forecast notices with their announcement dates.

    Tushare ``forecast`` is a company performance notice, not analyst
    consensus.  Keeping it separate prevents the UI and gates from presenting
    a management notice as a sell-side estimate.
    """
    with _conn() as c:
        rows = c.execute(
            """SELECT period,ann_date,available_time,payload_json FROM dc_tushare_raw_records
               WHERE api_name='forecast' AND ts_code=?
               ORDER BY ann_date DESC, available_time DESC LIMIT ?""",
            (ts_code, limit),
        ).fetchall()
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        item = dict(row)
        key = (str(item.get("period") or ""), str(item.get("ann_date") or ""))
        if key in seen:
            continue
        seen.add(key)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        lower, upper = payload.get("p_change_min"), payload.get("p_change_max")
        try:
            change_mid = (float(lower) + float(upper)) / 2 if lower is not None and upper is not None else None
        except (TypeError, ValueError):
            change_mid = None
        values.append({
            "period": key[0], "ann_date": key[1], "available_time": item.get("available_time"),
            "type": payload.get("type"), "p_change_min": lower, "p_change_max": upper,
            "net_profit_min": payload.get("net_profit_min"), "net_profit_max": payload.get("net_profit_max"),
            "change_mid": change_mid,
        })
    return values


def _yoy(rows: list[dict[str, Any]], field: str) -> float | None:
    if not rows:
        return None
    latest = rows[0]
    period = str(latest.get("period") or "")
    if len(period) != 8:
        return None
    prior_period = str(int(period[:4]) - 1) + period[4:]
    prior = next((row for row in rows if row.get("period") == prior_period), None)
    current_value = latest.get("payload", {}).get(field)
    prior_value = (prior or {}).get("payload", {}).get(field)
    if current_value is None or prior_value in (None, 0):
        return None
    return (float(current_value) / abs(float(prior_value)) - (1.0 if float(prior_value) > 0 else 0.0)) * 100.0


def build_stock_research(ts_code: str, *, persist: bool = True) -> dict[str, Any]:
    code = str(ts_code).strip().upper()
    with _conn() as c:
        master_row = c.execute("SELECT * FROM dc_stock_master WHERE ts_code=?", (code,)).fetchone()
        snapshot_row = c.execute("SELECT * FROM dc_stock_daily_snapshot WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
    if not master_row:
        return {"ok": False, "reason": "stock_not_found", "ts_code": code}
    master, snapshot = dict(master_row), dict(snapshot_row) if snapshot_row else {}
    income = _payloads(code, "income")
    balance = _payloads(code, "balancesheet")
    cashflow = _payloads(code, "cashflow")
    indicator = _payloads(code, "fina_indicator")
    latest_income = (income[0]["payload"] if income else {})
    latest_balance = (balance[0]["payload"] if balance else {})
    latest_cashflow = (cashflow[0]["payload"] if cashflow else {})
    latest_indicator = (indicator[0]["payload"] if indicator else {})
    revenue_growth = _yoy(income, "total_revenue")
    profit_growth = _yoy(income, "n_income")
    net_income = latest_income.get("n_income")
    operating_cash = latest_cashflow.get("n_cashflow_act")
    cash_conversion = None if net_income in (None, 0) or operating_cash is None else float(operating_cash) / abs(float(net_income))
    debt_ratio = latest_indicator.get("debt_to_assets")
    revenue_growth_series = [value for value in (_yoy(income[index:], "total_revenue") for index in range(len(income))) if value is not None]
    stability = None if len(revenue_growth_series) < 2 else _linear(pstdev(revenue_growth_series), 60, 5)
    dimensions_input = {
        "quality": {
            "profitability": _linear(latest_indicator.get("netprofit_margin"), -15, 20),
            "capital_efficiency": _linear(latest_indicator.get("roe"), -10, 25),
            "cash_conversion": _linear(cash_conversion, 0, 1.2),
            "balance_sheet_safety": _linear(debt_ratio, 90, 25),
            "stability": stability,
        },
        "growth": {
            "revenue_growth": _linear(revenue_growth, -20, 35),
            "core_profit_growth": _linear(profit_growth, -30, 40),
            "growth_acceleration": None,
            "growth_quality": _linear(cash_conversion, 0, 1.2),
        },
        "cashflow": {
            "ocf_to_profit": _linear(cash_conversion, 0, 1.2),
            "cashflow_stability": None,
            "cash_debt_coverage": None,
        },
        "governance": {"disclosure_quality": _linear(len(indicator) + len(income) + len(balance) + len(cashflow), 4, 28)},
        "industry": {},
        "expectation": {"growth_acceleration": None, "margin_trend": None},
        "valuation": {
            "historical_percentile": None,
            "industry_relative": None,
            "normalized_yield": _linear(snapshot.get("pe_ttm"), 60, 8) if snapshot.get("pe_ttm") and float(snapshot["pe_ttm"]) > 0 else None,
            "downside_protection": _linear(snapshot.get("pb"), 8, .8) if snapshot.get("pb") and float(snapshot["pb"]) > 0 else None,
        },
    }
    name = str(master.get("name") or "")
    risk_flags = {
        "is_st": "ST" in name.upper(),
        "untradable": bool(snapshot.get("is_suspended")),
        "critical_data_missing": not (income and balance and cashflow and indicator),
        "receivable_inventory_abnormal": False,
        "profit_cashflow_divergence": cash_conversion is not None and cash_conversion < 0,
        "debt_default": False,
    }
    risk = evaluate_risk(risk_flags)
    dimensions = {name: score_dimension(name, values) for name, values in dimensions_input.items()}
    raw_metrics = {"revenue_growth": revenue_growth, "core_profit_growth": profit_growth, "cash_conversion": cash_conversion, "debt_to_assets": debt_ratio}
    evidence = [
        {"api": api, "period": rows[0]["period"], "available_time": rows[0]["available_time"]}
        for api, rows in (("income", income), ("balancesheet", balance), ("cashflow", cashflow), ("fina_indicator", indicator)) if rows
    ]
    result = {
        "ok": True,
        "ts_code": code,
        "profile": {"name": name, "industry": master.get("industry"), "list_date": master.get("list_date"), "trade_date": snapshot.get("trade_date")},
        "classification": classify_company(industry=str(master.get("industry") or ""), metrics=raw_metrics),
        "risk": risk,
        "raw_metrics": raw_metrics,
        "dimensions": dimensions,
        "research_state": research_state(dimensions, risk),
        "evidence": evidence,
        "limitations": ["扣非利润、ROIC、自由现金流、质押与监管事件尚未完成统一字段映射", "估值历史分位和行业相对值尚未计算"]
    }
    if persist:
        as_of = max((str(x.get("available_time") or "") for x in evidence), default=str(snapshot.get("available_time") or datetime.now().isoformat(timespec="seconds")))
        confidence = float(result["research_state"].get("confidence") or 0)
        now = datetime.now().isoformat(timespec="seconds")
        with _business_conn() as c:
            c.execute(
                """INSERT INTO research_score_snapshots(ts_code,as_of_time,model_version,dimensions_json,evidence_json,confidence,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (code, as_of, "research_contract_v1", json.dumps(dimensions, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), confidence, now),
            )
    return result


def _prior_year(rows: list[dict[str, Any]], period: str) -> dict[str, Any] | None:
    if len(period) != 8:
        return None
    wanted = str(int(period[:4]) - 1) + period[4:]
    return next((row for row in rows if row.get("period") == wanted), None)


def build_expectation_state(ts_code: str, *, persist_baseline: bool = True) -> dict[str, Any]:
    code = str(ts_code).strip().upper()
    income, indicator = _payloads(code, "income", 16), _payloads(code, "fina_indicator", 16)
    forecast_history = _forecast_history(code, 16)
    if not income:
        if forecast_history:
            return {
                "ok": True, "ts_code": code, "status": "insufficient_financial_data",
                "observations": {}, "dimension": score_dimension("expectation", {}), "baseline": None,
                "external_forecast": {
                    "kind": "company_performance_notice_not_consensus", "status": "unscorable",
                    "latest": forecast_history[0], "history": forecast_history[:8],
                },
                "limitations": ["已同步公司业绩预告，但缺少同期财务报表，不能计算超预期、趋势或预测基线", "公司业绩预告不等同于分析师一致预期"],
                "evidence": [{"api": "forecast", "period": x["period"], "available_time": x["available_time"]} for x in forecast_history[:4]],
            }
        return {"ok": False, "reason": "financial_data_missing", "ts_code": code}
    latest_period = str(income[0]["period"])
    revenue_yoy, profit_yoy = _yoy(income, "total_revenue"), _yoy(income, "n_income")
    previous_revenue_yoy = _yoy(income[1:], "total_revenue") if len(income) > 1 else None
    previous_profit_yoy = _yoy(income[1:], "n_income") if len(income) > 1 else None
    acceleration_values = [x for x in (
        None if revenue_yoy is None or previous_revenue_yoy is None else revenue_yoy - previous_revenue_yoy,
        None if profit_yoy is None or previous_profit_yoy is None else profit_yoy - previous_profit_yoy,
    ) if x is not None]
    acceleration = sum(acceleration_values) / len(acceleration_values) if acceleration_values else None
    latest_indicator = indicator[0]["payload"] if indicator else {}
    prior_indicator = (_prior_year(indicator, str(indicator[0]["period"])) or {}).get("payload", {}) if indicator else {}
    current_margin, prior_margin = latest_indicator.get("netprofit_margin"), prior_indicator.get("netprofit_margin")
    margin_change = None if current_margin is None or prior_margin is None else float(current_margin) - float(prior_margin)
    latest_forecast = forecast_history[0] if forecast_history else None
    forecast_revision = None
    if len(forecast_history) >= 2:
        current_mid, prior_mid = forecast_history[0].get("change_mid"), forecast_history[1].get("change_mid")
        if current_mid is not None and prior_mid is not None:
            forecast_revision = float(current_mid) - float(prior_mid)
    # A notice that predates the latest reported period has no admission value
    # for the current decision.  It remains visible as historical evidence.
    forecast_current = bool(latest_forecast and str(latest_forecast.get("period") or "") >= latest_period)
    dimension = score_dimension("expectation", {
        "forecast_revision": _linear(forecast_revision, -20, 20) if forecast_current else None,
        "growth_acceleration": _linear(acceleration, -20, 20),
        "margin_trend": _linear(margin_change, -5, 5),
    })
    suffix = latest_period[4:]
    next_suffix = {"0331": "0630", "0630": "0930", "0930": "1231", "1231": "0331"}.get(suffix, "")
    next_year = int(latest_period[:4]) + (1 if suffix == "1231" else 0)
    target_period = f"{next_year}{next_suffix}" if next_suffix else ""
    prior_comparable = _prior_year(income, latest_period)
    baseline = None
    if target_period and prior_comparable and revenue_yoy is not None and profit_yoy is not None:
        latest_payload, prior_payload = income[0]["payload"], prior_comparable["payload"]
        revenue_forecast = float(latest_payload.get("total_revenue") or 0) * (1 + max(-.5, min(.5, revenue_yoy / 100)) / 4)
        profit_forecast = float(latest_payload.get("n_income") or 0) * (1 + max(-.8, min(.8, profit_yoy / 100)) / 4)
        confidence = min(.55, .25 + .05 * min(6, len(income)))
        now = datetime.now().isoformat(timespec="seconds")
        baseline = {"target_period": target_period, "revenue_forecast": revenue_forecast, "profit_forecast": profit_forecast, "confidence": confidence, "model_version": "seasonal_yoy_baseline_v1", "available_time": now}
        if persist_baseline:
            with _business_conn() as c:
                c.execute(
                    """INSERT INTO earnings_forecast_baselines(ts_code,target_period,model_version,revenue_forecast,profit_forecast,confidence,inputs_json,available_time,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(ts_code,target_period,model_version) DO UPDATE SET revenue_forecast=excluded.revenue_forecast,
                       profit_forecast=excluded.profit_forecast,confidence=excluded.confidence,inputs_json=excluded.inputs_json,available_time=excluded.available_time""",
                    (code, target_period, baseline["model_version"], revenue_forecast, profit_forecast, confidence, json.dumps({"latest_period": latest_period, "revenue_yoy": revenue_yoy, "profit_yoy": profit_yoy}, ensure_ascii=False), now, now),
                )
    return {
        "ok": True, "ts_code": code, "latest_period": latest_period,
        "observations": {"revenue_yoy": revenue_yoy, "profit_yoy": profit_yoy, "previous_revenue_yoy": previous_revenue_yoy, "previous_profit_yoy": previous_profit_yoy, "growth_acceleration": acceleration, "net_margin_change": margin_change, "forecast_revision": forecast_revision},
        "dimension": dimension, "baseline": baseline,
        "status": "ready" if dimension["status"] == "ready" and baseline else "insufficient_data",
        "external_forecast": {
            "kind": "company_performance_notice_not_consensus",
            "status": "current" if forecast_current else "stale" if latest_forecast else "missing",
            "latest": latest_forecast,
            "history": forecast_history[:8],
        },
        "limitations": ["尚无可靠一致预期，系统基线置信度最高0.55", "公司业绩预告不等同于分析师一致预期", "行业确认和管理层指引尚未映射"],
        "evidence": ([{"api": "income", "period": x["period"], "available_time": x["available_time"]} for x in income[:4]] + [{"api": "forecast", "period": x["period"], "available_time": x["available_time"]} for x in forecast_history[:4]]),
    }


def _percentile(values: list[float], current: float) -> float | None:
    clean = sorted(float(x) for x in values if x is not None and float(x) > 0)
    if not clean:
        return None
    return sum(1 for x in clean if x <= current) / len(clean)


def build_valuation_state(ts_code: str, *, persist: bool = True) -> dict[str, Any]:
    code = str(ts_code).strip().upper()
    with _conn() as c:
        master_row = c.execute("SELECT * FROM dc_stock_master WHERE ts_code=?", (code,)).fetchone()
        rows = [dict(row) for row in c.execute("SELECT trade_date,close,pe_ttm,pb,available_time FROM dc_stock_daily_snapshot WHERE ts_code=? ORDER BY trade_date DESC LIMIT 500", (code,)).fetchall()]
        if not master_row or not rows:
            return {"ok": False, "reason": "valuation_data_missing", "ts_code": code}
        master = dict(master_row)
        latest = rows[0]
        peer_rows = [dict(row) for row in c.execute(
            """SELECT s.pe_ttm,s.pb FROM dc_stock_daily_snapshot s JOIN dc_stock_master m ON m.ts_code=s.ts_code
               WHERE s.trade_date=? AND m.industry=? AND s.ts_code<>?""", (latest["trade_date"], master.get("industry") or "", code)
        ).fetchall()]
    financial = any(x in str(master.get("industry") or "") for x in ("银行", "保险", "证券"))
    multiple_key = "pb" if financial else "pe_ttm"
    current_multiple = latest.get(multiple_key)
    history = [float(x[multiple_key]) for x in rows if x.get(multiple_key) and float(x[multiple_key]) > 0]
    peers = [float(x[multiple_key]) for x in peer_rows if x.get(multiple_key) and float(x[multiple_key]) > 0]
    if not current_multiple or float(current_multiple) <= 0 or len(history) < 20:
        return {"ok": True, "ts_code": code, "status": "insufficient_data", "method": "PB-ROE" if financial else "PE-relative", "reason": "正估值倍数历史不足20个交易日", "history_points": len(history)}
    current_multiple = float(current_multiple)
    sorted_history = sorted(history)
    quantile = lambda p: sorted_history[min(len(sorted_history) - 1, int((len(sorted_history) - 1) * p))]
    bear_m, base_m, bull_m = quantile(.25), quantile(.50), quantile(.75)
    price = float(latest.get("close") or 0)
    scenarios = {"bear": price * bear_m / current_multiple, "base": price * base_m / current_multiple, "bull": price * bull_m / current_multiple}
    history_percentile = _percentile(history, current_multiple)
    industry_percentile = _percentile(peers, current_multiple)
    dimension = score_dimension("valuation", {
        "historical_percentile": None if history_percentile is None else (1 - history_percentile) * 100,
        "industry_relative": None if industry_percentile is None else (1 - industry_percentile) * 100,
        "normalized_yield": _linear(current_multiple, 60 if not financial else 6, 8 if not financial else .7),
        "scenario_value_gap": _linear(scenarios["base"] / price - 1 if price else None, -.3, .5),
        "downside_protection": _linear(scenarios["bear"] / price - 1 if price else None, -.5, .1),
    })
    now = datetime.now().isoformat(timespec="seconds")
    if persist:
        with _business_conn() as c:
            c.execute(
                """INSERT INTO valuation_snapshots(ts_code,as_of_time,method,bear_value,base_value,bull_value,current_price,assumptions_json,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (code, latest["available_time"], "PB-ROE-relative-v1" if financial else "PE-relative-v1", scenarios["bear"], scenarios["base"], scenarios["bull"], price,
                 json.dumps({"history_points": len(history), "peer_count": len(peers), "multiple": multiple_key}, ensure_ascii=False), json.dumps({"trade_date": latest["trade_date"], "source": "dc_stock_daily_snapshot"}, ensure_ascii=False), now),
            )
    return {"ok": True, "ts_code": code, "status": dimension["status"], "method": "PB-ROE-relative-v1" if financial else "PE-relative-v1", "trade_date": latest["trade_date"], "current_price": price, "current_multiple": current_multiple, "history_percentile": history_percentile, "industry_percentile": industry_percentile, "scenarios": scenarios, "dimension": dimension, "evidence": {"history_points": len(history), "peer_count": len(peers), "available_time": latest["available_time"]}, "limitations": ["V1为相对估值情景，DCF/NAV和正常化盈利尚待现金流预测接入"]}
