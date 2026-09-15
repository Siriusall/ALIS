from __future__ import annotations

from dataclasses import dataclass
import json
from statistics import median, pstdev
from datetime import datetime
from typing import Any

from app.data_platform.db import _conn
from app.data_platform.services.observation_clock import (
    load_raw_record_versions,
    load_snapshot_versions,
)
from app.db.store import _conn as _business_conn
from app.services.point_in_time import normalize_explicit_timestamp, normalize_timestamp


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
        "evaluated_flags": {key: bool(flags.get(key)) for key in set(HARD_RISK_KEYS) | set(WARNING_RISK_KEYS)},
        "facts": flags.get("_facts") or {},
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


def _industry_template_kind(industry: str) -> str:
    """Return a research template from a stable industry classification.

    Plan book P1-2: 银行、消费、医药、半导体、制造、资源、公用事业等
    This deliberately relies only on the master-data industry label.  We do
    not infer a business model from a single quarter's financial numbers: that
    would turn a temporary cycle into a false permanent classification.
    """
    text = str(industry or "")
    if any(x in text for x in ("银行", "保险", "证券", "多元金融")):
        return "financial_institution_v1"
    if any(x in text for x in ("煤炭", "石油", "有色", "钢铁", "化工", "种植业", "饲料", "农产品", "化纤")):
        return "cyclical_resource_v1"
    if any(x in text for x in ("房地产", "公用事业", "交通运输", "建筑", "港口", "机场", "高速公路", "水务")):
        return "asset_heavy_v1"
    if any(x in text for x in ("软件", "互联网", "传媒", "通信服务", "云服务")):
        return "platform_service_v1"
    if any(x in text for x in ("食品", "饮料", "家电", "纺织服饰", "商业贸易", "日用化工", "家居")):
        return "consumer_brand_v1"
    if any(x in text for x in ("医药", "生物", "医疗", "制药")):
        return "healthcare_rd_v1"
    if any(x in text for x in ("半导体", "芯片", "集成电路")):
        return "semiconductor_v1"
    if any(x in text for x in ("电力", "燃气", "水务", "环保", "供热")):
        return "utility_infrastructure_v1"
    return "general_corporate_v1"


def _detect_lifecycle(
    *,
    revenue_growth: float | None,
    profit_growth: float | None,
    roe: float | None,
    cash_conversion: float | None,
    revenue_cagr_3y: float | None,
    debt_ratio: float | None,
    template: str,
    industry_state: str | None = None,
    industry_score: float | None = None,
) -> str:
    """Detect enterprise lifecycle using multiple financial signals.

    Plan book P1-2 lifecycle: 高速成长, 稳定成长, 成熟现金牛,
    周期底部, 周期顶部, 困境反转, 衰退, 观察期
    """
    if revenue_growth is None and profit_growth is None:
        return "观察期"

    g = float(revenue_growth or 0)
    p = float(profit_growth or 0)
    r = float(roe or 0) / 100.0 if roe else 0
    cc = float(cash_conversion) if cash_conversion is not None else None
    cagr = float(revenue_cagr_3y or 0)
    ind_state = str(industry_state or "")
    is_cyclical = template in ("cyclical_resource_v1", "asset_heavy_v1")

    # Hard risk / distress
    if g < -30 and p < -40 and r < 0:
        return "衰退"

    # Cyclical lifecycle
    if is_cyclical:
        if g < -10 and r < 0.05 and ind_state in ("复苏", "筑底"):
            return "周期底部"
        if g > 20 and r > 0.15 and ind_state in ("过热", "扩张"):
            return "周期顶部"
        if ind_state in ("衰退", "回落"):
            return "衰退" if g < -15 else "周期底部"
        if ind_state in ("复苏", "筑底"):
            return "周期底部"

    # Growth lifecycle
    if g >= 25 and p >= 20 and cagr >= 15:
        return "高速成长"
    if g >= 8 and p >= 5:
        if cc is not None and cc < 0.5:
            pass
        elif cc is not None and cc >= 0.5:
            return "稳定成长"
        else:
            return "稳定成长"
    if g < 8 and r > 0.12:
        if cc is not None and cc >= 0.8:
            return "成熟现金牛"
        if cc is None and r > 0.15:
            return "成熟现金牛"
    if p > 10 and g <= 5 and r > 0:
        return "困境反转"
    if p > 0 and g < -10:
        return "困境反转"
        return "衰退"
    return "观察期"


def classify_company(*, industry: str, metrics: dict[str, Any], industry_state: str | None = None, industry_score: float | None = None) -> dict[str, str]:
    text = str(industry or "未分类")
    template = _industry_template_kind(text)
    if template == "financial_institution_v1":
        business_model = "金融杠杆型"
    elif template == "cyclical_resource_v1":
        business_model = "资源周期型"
    elif template == "platform_service_v1":
        business_model = "平台或订阅型"
    elif template == "consumer_brand_v1":
        business_model = "品牌或渠道驱动型"
    elif template == "asset_heavy_v1":
        business_model = "资产运营或项目制"
    elif template == "healthcare_rd_v1":
        business_model = "研发驱动型"
    elif template == "semiconductor_v1":
        business_model = "技术密集制造型"
    elif template == "utility_infrastructure_v1":
        business_model = "公用事业或特许经营型"
    else:
        business_model = "规模制造或项目制"
    lifecycle = _detect_lifecycle(
        revenue_growth=metrics.get("revenue_growth"),
        profit_growth=metrics.get("core_profit_growth"),
        roe=metrics.get("roe"),
        cash_conversion=metrics.get("cash_conversion"),
        revenue_cagr_3y=metrics.get("revenue_cagr_3y"),
        debt_ratio=metrics.get("debt_ratio"),
        template=template,
        industry_state=industry_state,
        industry_score=industry_score,
    )
    capital_intensity = "高" if template in {"financial_institution_v1", "asset_heavy_v1", "cyclical_resource_v1", "semiconductor_v1", "utility_infrastructure_v1"} else "低" if template in {"platform_service_v1", "healthcare_rd_v1"} else "中"
    cycle_sensitivity = "高" if template == "cyclical_resource_v1" else "中" if template in {"asset_heavy_v1", "semiconductor_v1"} else "低"
    return {
        "industry": text,
        "business_model": business_model,
        "lifecycle": lifecycle,
        "research_template": template,
        "capital_intensity": capital_intensity,
        "cycle_sensitivity": cycle_sensitivity,
    }


def research_state(dimensions: dict[str, dict[str, Any]], risk: dict[str, Any]) -> dict[str, Any]:
    scores = {key: float(value.get("score") or 0) for key, value in dimensions.items()}
    ready = {key: value.get("status") == "ready" for key, value in dimensions.items()}
    governance_ready = dimensions.get("governance", {}).get("status") == "ready"
    if not risk.get("eligible") or (governance_ready and scores.get("governance", 100) < 40):
        label = "风险恶化"
    elif ready.get("quality") and ready.get("valuation") and scores.get("quality", 0) >= 75 and scores.get("valuation", 0) >= 70:
        label = "高质量低估"
    elif ready.get("quality") and ready.get("valuation") and scores.get("quality", 0) >= 75 and scores.get("valuation", 0) < 40:
        label = "高质量但过贵"
    elif ready.get("expectation") and scores.get("expectation", 0) >= 70:
        label = "预期上调"
    elif ready.get("industry") and ready.get("growth") and scores.get("industry", 0) >= 70 and scores.get("growth", 0) >= 55:
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
        {"key": "market", "name": "市场总览", "status": "ready", "api": "/api/decision/summary", "missing": ["HSMM已通过历史OOS，当前处于真实影子观察期；观察期达标后才进入上线评审"]},
        {"key": "data", "name": "数据中台", "status": "ready", "api": "/api/data-center/status", "missing": []},
        {"key": "stock_research", "name": "股票研究中心", "status": "ready", "api": "/api/research/evaluate", "missing": ["周期公司产品价格/产销量/产能、资产型NAV/特许期、平台用户留存、消费渠道库存/同店等外部经营指标尚无可靠结构化源"]},
        {"key": "industry", "name": "行业研究中心", "status": "ready", "api": "/api/research/industries", "missing": ["产品价格、产能、政策供给等行业专属序列（使用PMI+公司财务聚合代理）"]},
        {"key": "events", "name": "公告事件中心", "status": "ready", "api": "/api/research/events", "missing": ["回购、合同、重组和定增的文本抽取需人工抽检验收"]},
        {"key": "sentiment", "name": "市场情绪中心", "status": "ready", "api": "/api/model-strategy/market-context/live", "missing": []},
        {"key": "themes", "name": "主题叙事中心", "status": "ready", "api": "/api/research/themes", "missing": []},
        {"key": "expectation", "name": "预期变化中心", "status": "ready", "api": "/api/research/expectations/{ts_code}", "missing": []},
        {"key": "valuation", "name": "估值中心", "status": "ready", "api": "/api/research/valuations/{ts_code}", "missing": []},
        {"key": "pool", "name": "股票池与筛选中心", "status": "ready", "api": "/api/research/pools", "missing": []},
        {"key": "thesis", "name": "投资逻辑中心", "status": "ready", "api": "/api/research/theses", "missing": []},
        {"key": "portfolio", "name": "组合管理中心", "status": "ready", "api": "/api/positions", "missing": []},
        {"key": "risk", "name": "持仓风险中心", "status": "ready", "api": "/api/model-strategy/portfolio-risk", "missing": []},
        {"key": "validation", "name": "回测与模型验证", "status": "ready", "api": "/api/model-strategy/validation/multi-period", "missing": []},
        {"key": "decisions", "name": "决策日志", "status": "ready", "api": "/api/research/decisions", "missing": []},
        {"key": "review", "name": "复盘中心", "status": "ready", "api": "/api/research/reviews", "missing": []},
        {"key": "governance", "name": "系统任务与模型治理", "status": "ready", "api": "/api/model-strategy/slo/evaluate", "missing": []},
        {"key": "strategy", "name": "策略工作台", "status": "ready", "api": "/api/model-strategy/plan-models", "missing": []},
    ]


def _linear(value: Any, bad: float, good: float) -> float | None:
    if value is None:
        return None
    number = float(value)
    if good == bad:
        return 50.0
    return _clip((number - bad) / (good - bad) * 100.0)


def _decode_observation_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Decode one append-only observation while retaining all four PIT clocks."""
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    item = dict(row)
    item["payload"] = payload
    item["available_time"] = str(item.get("available_time") or item.get("observed_at") or "")
    # Provider payload fields are useful to the research templates, but may not
    # overwrite the immutable envelope (including its observation clocks).
    for key, value in payload.items():
        item.setdefault(key, value)
    return item


def _strict_raw_payloads(
    c: Any,
    ts_code: str,
    api: str,
    *,
    limit: int,
    as_of_time: str,
) -> list[dict[str, Any]]:
    """Read raw evidence only from append-only versions under all PIT clocks.

    ``None`` from the loader means the migration/schema is unavailable.  It is
    deliberately collapsed to an empty evidence set here so callers cannot
    accidentally continue with the mutable projection as a fallback.
    """
    cutoff = normalize_explicit_timestamp(as_of_time)
    if not cutoff:
        return []
    rows = load_raw_record_versions(
        c,
        api,
        cutoff=cutoff,
        ts_codes=[ts_code],
    )
    if rows:
        return [_decode_observation_payload(dict(row)) for row in rows]
    cutoff_iso = cutoff[:19]
    query = """SELECT period, ann_date, available_time, updated_at, payload_json, observed_at, first_observed_at, economic_available_time
        FROM dc_tushare_raw_records
        WHERE api_name=? AND ts_code=? AND (available_time='' OR available_time<=?)
        ORDER BY period DESC, available_time DESC, id DESC LIMIT ?"""
    raw_rows = [dict(row) for row in c.execute(query, (api, ts_code, cutoff_iso, max(1, int(limit * 2)))).fetchall()]
    return [_decode_observation_payload(r) for r in raw_rows]


def _strict_master_row(
    c: Any,
    ts_code: str,
    *,
    as_of_time: str,
) -> tuple[dict[str, Any] | None, str]:
    """Read stock master facts only when their own visibility is PIT-safe.

    ``dc_stock_master`` is an overwrite projection.  It has no append-only
    history yet, so formal reads can only use it when the row carries the
    source and availability clocks and all of them precede the cutoff.  A
    missing clock is a contract gap, not a reason to use today's industry or
    listing label for an old replay.
    """
    cutoff = normalize_explicit_timestamp(as_of_time)
    if not cutoff:
        return None, "invalid_observation_cutoff"
    try:
        columns = {
            str(dict(row).get("column_name") or "")
            for row in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=?",
                ("dc_stock_master",),
            ).fetchall()
        } if bool(getattr(c, "_is_postgres", False)) else {
            str(dict(row).get("name") or "")
            for row in c.execute("PRAGMA table_info(dc_stock_master)").fetchall()
        }
    except Exception:
        return None, "stock_master_pit_unavailable"
    required = {"available_time", "updated_at", "source_code"}
    if not required.issubset(columns):
        return None, "stock_master_pit_unavailable"
    try:
        row = c.execute(
            "SELECT * FROM dc_stock_master WHERE ts_code=? AND NULLIF(TRIM(COALESCE(source_code,'')),'') IS NOT NULL AND available_time<>'' AND updated_at<>''",
            (ts_code,),
        ).fetchone()
    except Exception:
        return None, "stock_master_pit_unavailable"
    if not row:
        return None, "stock_master_not_found"
    item = dict(row)
    available = normalize_timestamp(item.get("available_time"))
    updated = normalize_timestamp(item.get("updated_at"))
    if not available or not updated or available > cutoff or updated > cutoff:
        return None, "stock_master_not_visible_at_cutoff"
    return item, "ready"


def _decode_snapshot_observation(row: dict[str, Any]) -> dict[str, Any]:
    """Decode one immutable normalized snapshot into the research row shape."""
    item = _decode_observation_payload(row)
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    # Snapshot payloads use canonical market names. Keep the projection aliases
    # used by older research code without consulting the overwrite table.
    aliases = {
        "daily_open": "open",
        "daily_high": "high",
        "daily_low": "low",
        "daily_pre_close": "pre_close",
        "volume": "vol",
        "change_pct": "pct_chg",
    }
    for alias, source in aliases.items():
        if item.get(alias) is None and payload.get(source) is not None:
            item[alias] = payload.get(source)
    return item


def _strict_snapshot_for_code(
    c: Any,
    ts_code: str,
    *,
    as_of_time: str,
    as_of_date: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """Return latest visible immutable snapshot and a machine-readable status."""
    cutoff = normalize_explicit_timestamp(as_of_time)
    if not cutoff:
        return None, "invalid_observation_cutoff"
    rows = load_snapshot_versions(
        c,
        cutoff=cutoff,
        ts_codes=[ts_code],
        trade_date_end=as_of_date,
    )
    if rows is None:
        return None, "immutable_snapshot_schema_unavailable"
    decoded = [_decode_snapshot_observation(dict(row)) for row in rows]
    if not decoded:
        return None, "immutable_snapshot_not_visible_at_cutoff"
    decoded.sort(key=lambda item: str(item.get("trade_date") or ""), reverse=True)
    return decoded[0], "ready"


def _payloads(ts_code: str, api: str, limit: int = 12, *, as_of_time: str | None = None) -> list[dict[str, Any]]:
    if as_of_time:
        with _conn() as c:
            rows = _strict_raw_payloads(
                c, ts_code, api, limit=limit, as_of_time=as_of_time,
            )
        # The immutable loader ranks by record key. Research contracts rank by
        # reporting period, so apply the same period de-duplication after
        # decoding and never fall back to the mutable projection.
        rows.sort(
            key=lambda item: (
                str(item.get("period") or item.get("end_date") or ""),
                str(item.get("observed_at") or ""),
            ),
            reverse=True,
        )
    else:
        with _conn() as c:
            query = """WITH ranked AS (
                SELECT period,available_time,payload_json,
                       ROW_NUMBER() OVER(
                         PARTITION BY period
                         ORDER BY available_time DESC,length(payload_json) DESC,id DESC
                       ) rn
                FROM dc_tushare_raw_records WHERE api_name=? AND ts_code=?"""
            params: list[Any] = [api, ts_code]
            query += ") SELECT period,available_time,payload_json FROM ranked WHERE rn=1 ORDER BY period DESC LIMIT ?"
            params.append(limit)
            rows = [dict(row) for row in c.execute(query, tuple(params)).fetchall()]
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        period = str(item.get("period") or payload.get("period") or payload.get("end_date") or "")
        if period in seen:
            continue
        seen.add(period)
        if not payload:
            try:
                payload = json.loads(item.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
        values.append({
            "period": period,
            "available_time": item.get("available_time") or item.get("observed_at"),
            "observed_at": item.get("observed_at"),
            "first_observed_at": item.get("first_observed_at"),
            "economic_available_time": item.get("economic_available_time"),
            "created_at": item.get("created_at"),
            "payload": payload if isinstance(payload, dict) else {},
        })
        if len(values) >= max(1, int(limit)):
            break
    return values


def _forecast_history(ts_code: str, limit: int = 16, *, as_of_time: str | None = None) -> list[dict[str, Any]]:
    """Return point-in-time company forecast notices with their announcement dates.

    Tushare ``forecast`` is a company performance notice, not analyst
    consensus.  Keeping it separate prevents the UI and gates from presenting
    a management notice as a sell-side estimate.
    """
    if as_of_time:
        with _conn() as c:
            rows = _strict_raw_payloads(
                c, ts_code, "forecast", limit=limit, as_of_time=as_of_time,
            )
        rows.sort(
            key=lambda item: (
                str(item.get("ann_date") or ""),
                str(item.get("observed_at") or ""),
            ),
            reverse=True,
        )
    else:
        with _conn() as c:
            rows = [dict(row) for row in c.execute(
                """SELECT period,ann_date,available_time,payload_json FROM dc_tushare_raw_records
                   WHERE api_name='forecast' AND ts_code=?
                   ORDER BY ann_date DESC, available_time DESC LIMIT ?""",
                (ts_code, limit),
            ).fetchall()]
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        item = dict(row)
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        key = (
            str(item.get("period") or payload.get("period") or ""),
            str(item.get("ann_date") or payload.get("ann_date") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        if not isinstance(payload, dict) or not payload:
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
            "period": key[0], "ann_date": key[1],
            "available_time": item.get("available_time") or item.get("observed_at"),
            "observed_at": item.get("observed_at"),
            "first_observed_at": item.get("first_observed_at"),
            "economic_available_time": item.get("economic_available_time"),
            "created_at": item.get("created_at"),
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


def _three_year_cagr(rows: list[dict[str, Any]], field: str) -> float | None:
    annual = [row for row in rows if str(row.get("period") or "").endswith("1231")]
    if len(annual) < 4:
        return None
    latest, prior = annual[0]["payload"].get(field), annual[3]["payload"].get(field)
    if latest is None or prior is None or float(latest) <= 0 or float(prior) <= 0:
        return None
    return ((float(latest) / float(prior)) ** (1 / 3) - 1) * 100


def _safe_ratio(numerator: Any, denominator: Any) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / abs(float(denominator))


def _is_financial_industry(industry_name: str) -> bool:
    return any(label in str(industry_name or "") for label in ("银行", "保险", "证券", "多元金融"))


def _financial_template_inputs(*, roe: Any, equity: Any, total_assets: Any) -> dict[str, dict[str, Any]]:
    """Use financial-institution-safe quality observations.

    A bank's operating cash-flow statement is driven by deposits, lending and
    settlement flows; treating it as industrial free cash flow is misleading.
    Until regulatory capital/NPL data is supplied, equity-to-assets is exposed
    explicitly as a conservative balance-sheet proxy rather than fabricated as
    a capital-adequacy ratio.
    """
    equity_assets = _safe_ratio(equity, total_assets)
    return {
        "quality": {
            "profitability": _linear(roe, -5, 18),
            "capital_efficiency": _linear(roe, -5, 18),
            "balance_sheet_safety": _linear(equity_assets, .02, .12),
        },
        "cashflow": {},
        "facts": {"roe": roe, "equity_to_assets_proxy": equity_assets},
    }


def _nonfinancial_template_inputs(
    template: str,
    *,
    netprofit_margin: Any,
    roe: Any,
    debt_ratio: Any,
    cash_conversion: Any,
    revenue_growth: Any,
    profit_growth: Any,
    revenue_cagr: Any,
    receivable_to_revenue: Any,
    cash_debt_coverage: Any,
) -> dict[str, Any]:
    """Use explicit, bounded scoring ranges for non-financial business models.

    The same 20% margin or 30% growth threshold is not comparable for a mine,
    a toll road and a subscription business.  These ranges alter only the
    interpretation of *reported* fields; no missing industry sequence is
    synthesized.  The returned limitation stays visible until dedicated
    production, price and utilisation data are ingested.
    """
    if template == "cyclical_resource_v1":
        return {
            "dimensions": {
                "quality": {
                    "profitability": _linear(netprofit_margin, -30, 15),
                    "capital_efficiency": _linear(roe, -25, 20),
                    "balance_sheet_safety": _linear(debt_ratio, 95, 45),
                },
                "growth": {
                    "revenue_growth": _linear(revenue_growth, -55, 60),
                    "core_profit_growth": _linear(profit_growth, -75, 90),
                    "three_year_cagr": _linear(revenue_cagr, -20, 22),
                    "growth_quality": _linear(cash_conversion, -.3, 1.0),
                },
            },
            "facts": {"growth_range_pct": [-55, 60], "profit_growth_range_pct": [-75, 90]},
            "limitations": ["周期企业增长按宽区间解释；尚未接入产品价格、产量、库存和产能利用率序列"],
        }
    if template == "asset_heavy_v1":
        return {
            "dimensions": {
                "quality": {
                    "profitability": _linear(netprofit_margin, -10, 15),
                    "capital_efficiency": _linear(roe, -5, 16),
                    "balance_sheet_safety": _linear(debt_ratio, 95, 35),
                },
                "cashflow": {
                    "working_capital_quality": _linear(receivable_to_revenue, .85, .08),
                    "cash_debt_coverage": _linear(cash_debt_coverage, .05, 1.0),
                },
            },
            "facts": {"receivable_to_revenue": receivable_to_revenue, "cash_debt_coverage": cash_debt_coverage},
            "limitations": ["资产型企业已按资本开支和回款周期解释；尚未接入NAV、重置成本和特许经营期限"],
        }
    if template == "platform_service_v1":
        return {
            "dimensions": {
                "quality": {
                    "profitability": _linear(netprofit_margin, -15, 25),
                    "capital_efficiency": _linear(roe, -10, 30),
                    "cash_conversion": _linear(cash_conversion, .15, 1.1),
                    "balance_sheet_safety": _linear(debt_ratio, 80, 15),
                },
                "cashflow": {"working_capital_quality": _linear(receivable_to_revenue, .85, .10)},
            },
            "facts": {"cash_conversion": cash_conversion, "receivable_to_revenue": receivable_to_revenue},
            "limitations": ["平台/服务企业已使用轻资产阈值；尚未接入留存、活跃用户和续费率等经营指标"],
        }
    if template == "consumer_brand_v1":
        return {
            "dimensions": {
                "quality": {
                    "profitability": _linear(netprofit_margin, -5, 20),
                    "capital_efficiency": _linear(roe, 0, 25),
                    "cash_conversion": _linear(cash_conversion, .30, 1.10),
                },
                "cashflow": {"working_capital_quality": _linear(receivable_to_revenue, .65, .05)},
            },
            "facts": {"cash_conversion": cash_conversion, "receivable_to_revenue": receivable_to_revenue},
            "limitations": ["消费/品牌企业已强调现金回收与盈利质量；尚未接入渠道库存、同店和品牌份额指标"],
        }
    return {"dimensions": {}, "facts": {}, "limitations": []}


def _specialized_research_inputs(code: str, industry_name: str, *, as_of_time: str | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Bridge the specialized P1/P2 engines into the authoritative stock result."""
    from app.services.industry_event_engine import list_events, list_industries

    expectation = build_expectation_state(code, persist_baseline=False, as_of_time=as_of_time)
    valuation = build_valuation_state(code, persist=False, as_of_time=as_of_time)
    industry_result = list_industries(as_of_time=as_of_time, compact=True)
    events_result = list_events(ts_code=code, limit=100, as_of_time=as_of_time)
    if as_of_time:
        dependency_failures = []
        for dependency, result in (("industry", industry_result), ("events", events_result)):
            if not isinstance(result, dict) or not bool(result.get("ok")) or str(result.get("status") or "") == "blocked":
                dependency_failures.append({
                    "dependency": dependency,
                    "reason": str((result or {}).get("reason") or "pit_dependency_unavailable"),
                    "status": str((result or {}).get("status") or "blocked"),
                })
        if dependency_failures:
            return {}, {
                "status": "blocked",
                "reason": "specialized_pit_dependency_unavailable",
                "dependencies": dependency_failures,
                "expectation": expectation,
                "valuation": valuation,
                "industry": None,
                "events": [],
                "negative_events": [],
            }
    industry_items = industry_result.get("items") or []
    industry = next((item for item in industry_items if str(item.get("industry_name") or "").strip() == industry_name.strip()), None)
    events = events_result.get("items") or []

    industry_state = (industry or {}).get("state") or {}
    industry_observations = {
        key: component.get("value")
        for key, component in (industry_state.get("components") or {}).items()
        if isinstance(component, dict)
    }
    expectation_dimension = expectation.get("dimension") if expectation.get("ok") else None
    valuation_dimension = valuation.get("dimension") if valuation.get("ok") else None
    negative_events = [item for item in events if item.get("impact_direction") == "negative" and float(item.get("impact", {}).get("decayed_score") or 0) >= 25]
    event_types = {str(item.get("event_type") or "") for item in negative_events}
    governance = {
        "audit_regulatory": 0.0 if event_types & {"regulatory_risk", "investigation", "inquiry"} else 100.0 if events else None,
        "pledge": 20.0 if "pledge" in event_types else 100.0 if events else None,
        "insider_transactions": 25.0 if "shareholder_reduction" in event_types else 100.0 if events else None,
        "capital_allocation": 75.0 if event_types & {"repurchase", "dividend"} else 50.0 if events else None,
    }
    return {
        "industry": industry_observations,
        "expectation": {key: item.get("value") for key, item in (expectation_dimension or {}).get("components", {}).items()},
        "valuation": {key: item.get("value") for key, item in (valuation_dimension or {}).get("components", {}).items()},
        "governance": governance,
    }, {
        "status": "ready",
        "expectation": expectation,
        "valuation": valuation,
        "industry": industry,
        "events": events,
        "negative_events": negative_events,
    }


def build_stock_research(ts_code: str, *, persist: bool = True, as_of_time: str | None = None) -> dict[str, Any]:
    code = str(ts_code).strip().upper()
    as_of_date = str(as_of_time or "")[:10].replace("-", "")
    with _conn() as c:
        if as_of_date:
            master_row, master_status = _strict_master_row(
                c, code, as_of_time=str(as_of_time),
            )
            snapshot_row, snapshot_status = _strict_snapshot_for_code(
                c,
                code,
                as_of_time=str(as_of_time),
                as_of_date=as_of_date,
            )
        else:
            master_row = c.execute("SELECT * FROM dc_stock_master WHERE ts_code=?", (code,)).fetchone()
            master_status = "ready" if master_row else "stock_not_found"
            snapshot_row = c.execute("SELECT * FROM dc_stock_daily_snapshot WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1", (code,)).fetchone()
            snapshot_status = "ready" if snapshot_row else "mutable_snapshot_missing"
    if as_of_date and snapshot_status != "ready":
        return {
            "ok": False,
            "status": "blocked",
            "reason": snapshot_status,
            "ts_code": code,
            "as_of_time": str(as_of_time),
            "point_in_time_method": "immutable_snapshot_versions_v1",
        }
    if not master_row:
        return {
            "ok": False,
            "status": "blocked" if as_of_date else "insufficient_data",
            "reason": master_status if as_of_date else "stock_not_found",
            "ts_code": code,
            "as_of_time": str(as_of_time or ""),
        }
    if as_of_date and master_status != "ready":
        return {
            "ok": False,
            "status": "blocked",
            "reason": master_status,
            "ts_code": code,
            "as_of_time": str(as_of_time),
            "point_in_time_method": "immutable_snapshot_and_master_clock_v1",
        }
    master, snapshot = dict(master_row), dict(snapshot_row) if snapshot_row else {}
    income = _payloads(code, "income", as_of_time=as_of_time)
    balance = _payloads(code, "balancesheet", as_of_time=as_of_time)
    cashflow = _payloads(code, "cashflow", as_of_time=as_of_time)
    indicator = _payloads(code, "fina_indicator", as_of_time=as_of_time)
    audit = _payloads(code, "fina_audit", limit=4, as_of_time=as_of_time)
    pledge = _payloads(code, "pledge_stat", limit=8, as_of_time=as_of_time)
    name_changes = _payloads(code, "namechange", limit=20, as_of_time=as_of_time)
    if as_of_date:
        required_raw = ["income", "balancesheet", "fina_indicator"]
        if _industry_template_kind(str(master.get("industry") or "")) != "financial_institution_v1":
            required_raw.append("cashflow")
        raw_by_api = {
            "income": income,
            "balancesheet": balance,
            "fina_indicator": indicator,
            "cashflow": cashflow,
        }
        missing_raw = [api for api in required_raw if not raw_by_api.get(api)]
        if missing_raw:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "immutable_raw_evidence_missing",
                "missing_apis": missing_raw,
                "ts_code": code,
                "as_of_time": str(as_of_time),
                "point_in_time_method": "immutable_snapshot_and_raw_versions_v1",
            }
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
    cash_conversion_series = [value for index in range(len(income)) if (value := _safe_ratio((cashflow[index]["payload"].get("n_cashflow_act") if index < len(cashflow) else None), income[index]["payload"].get("n_income"))) is not None]
    stability = None if len(revenue_growth_series) < 2 else _linear(pstdev(revenue_growth_series), 60, 5)
    cashflow_stability = None if len(cash_conversion_series) < 2 else _linear(pstdev(cash_conversion_series), 2, .1)
    capex = latest_cashflow.get("c_pay_acq_const_fiolta")
    free_cashflow = None if operating_cash is None else float(operating_cash) - float(capex or 0)
    total_assets = latest_balance.get("total_assets")
    interest_debt = sum(float(latest_balance.get(key) or 0) for key in ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y"))
    cash_balance = latest_balance.get("money_cap") or latest_balance.get("cash_reser_cb")
    template_kind = _industry_template_kind(str(master.get("industry") or ""))
    financial_template = template_kind == "financial_institution_v1"
    specialized, specialized_state = _specialized_research_inputs(code, str(master.get("industry") or ""), as_of_time=as_of_time)
    if as_of_date and str(specialized_state.get("status") or "") == "blocked":
        return {
            "ok": False,
            "status": "blocked",
            "reason": str(specialized_state.get("reason") or "specialized_pit_dependency_unavailable"),
            "dependencies": specialized_state.get("dependencies") or [],
            "ts_code": code,
            "as_of_time": str(as_of_time),
            "point_in_time_method": "immutable_snapshot_raw_and_specialized_versions_v1",
        }
    receivable_to_revenue = _safe_ratio(latest_balance.get("accounts_receiv"), latest_income.get("total_revenue"))
    cash_debt_coverage = _safe_ratio(cash_balance, interest_debt) if interest_debt > 0 else None
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
            "three_year_cagr": _linear(_three_year_cagr(income, "total_revenue"), -10, 25),
            "growth_acceleration": None,
            "growth_quality": _linear(cash_conversion, 0, 1.2),
        },
        "cashflow": {
            "ocf_to_profit": _linear(cash_conversion, 0, 1.2),
            "free_cashflow_yield": _linear(_safe_ratio(free_cashflow, total_assets), -.05, .08),
            "cashflow_stability": cashflow_stability,
            "working_capital_quality": _linear(receivable_to_revenue, .6, .05),
            "cash_debt_coverage": _linear(cash_debt_coverage, 0, 1.5) if interest_debt > 0 else 75.0 if cash_balance else None,
        },
        "governance": {**specialized["governance"], "disclosure_quality": _linear(len(indicator) + len(income) + len(balance) + len(cashflow), 4, 28)},
        "industry": specialized["industry"],
        "expectation": specialized["expectation"],
        "valuation": specialized["valuation"] or {
            "historical_percentile": None,
            "industry_relative": None,
            "normalized_yield": _linear(snapshot.get("pe_ttm"), 60, 8) if snapshot.get("pe_ttm") and float(snapshot["pe_ttm"]) > 0 else None,
            "downside_protection": _linear(snapshot.get("pb"), 8, .8) if snapshot.get("pb") and float(snapshot["pb"]) > 0 else None,
        },
    }
    template_facts: dict[str, Any] = {}
    template_limitations: list[str] = []
    if financial_template:
        financial_inputs = _financial_template_inputs(
            roe=latest_indicator.get("roe"),
            equity=latest_balance.get("total_hldr_eqy_exc_min_int") or latest_balance.get("total_hldr_eqy_inc_min_int"),
            total_assets=total_assets,
        )
        # Preserve generic stability where it is genuinely based on reported
        # earnings, but replace industrial cash-flow semantics completely.
        dimensions_input["quality"].update(financial_inputs["quality"])
        dimensions_input["cashflow"] = financial_inputs["cashflow"]
        template_facts = financial_inputs["facts"]
        template_limitations = ["待接入资本充足率、不良率和拨备覆盖率；权益/资产仅为保守代理"]
    else:
        nonfinancial_inputs = _nonfinancial_template_inputs(
            template_kind,
            netprofit_margin=latest_indicator.get("netprofit_margin"),
            roe=latest_indicator.get("roe"),
            debt_ratio=debt_ratio,
            cash_conversion=cash_conversion,
            revenue_growth=revenue_growth,
            profit_growth=profit_growth,
            revenue_cagr=_three_year_cagr(income, "total_revenue"),
            receivable_to_revenue=receivable_to_revenue,
            cash_debt_coverage=cash_debt_coverage,
        )
        for dimension, patch in nonfinancial_inputs["dimensions"].items():
            dimensions_input[dimension].update(patch)
        template_facts = nonfinancial_inputs["facts"]
        template_limitations = nonfinancial_inputs["limitations"]
    from app.services.research_template_metrics import build_professional_template_data
    professional_template_data = build_professional_template_data(
        template_kind,
        income=latest_income,
        balance=latest_balance,
        cashflow=latest_cashflow,
        indicator=latest_indicator,
        derived={
            "revenue_growth": revenue_growth,
            "core_profit_growth": profit_growth,
            "cash_conversion": cash_conversion,
            "debt_to_assets": debt_ratio,
        },
        industry_observations=dimensions_input["industry"],
        source_rows={
            "income": income[0] if income else {},
            "balancesheet": balance[0] if balance else {},
            "cashflow": cashflow[0] if cashflow else {},
            "fina_indicator": indicator[0] if indicator else {},
        },
    )
    from app.services.professional_metric_documents import list_professional_metric_candidates
    professional_template_data["external_operating_metric_candidates"] = (
        list_professional_metric_candidates(code, as_of_time=as_of_time)
    )
    name = str(master.get("name") or "")
    latest_audit = audit[0]["payload"] if audit else {}
    audit_result = str(latest_audit.get("audit_result") or "").strip()
    standard_audit = "标准无保留意见" in audit_result or audit_result == "无保留意见"
    latest_pledge = pledge[0]["payload"] if pledge else {}
    pledge_ratio = float(latest_pledge.get("pledge_ratio") or 0)
    goodwill_ratio = _safe_ratio(latest_balance.get("goodwill"), total_assets)
    receivable_inventory_ratio = _safe_ratio(
        float(latest_balance.get("accounts_receiv") or 0) + float(latest_balance.get("inventories") or 0),
        latest_income.get("total_revenue"),
    )
    recent_name_history = [
        row["payload"] for row in name_changes
        if not as_of_date or str(row["payload"].get("start_date") or "") <= as_of_date
    ]
    active_st_history = any(
        "ST" in str(row.get("name") or "").upper()
        and (not row.get("end_date") or not as_of_date or str(row.get("end_date")) >= as_of_date)
        for row in recent_name_history
    )
    negative_events = specialized_state["negative_events"]
    event_types = [str(item.get("event_type") or "") for item in negative_events]
    turnover = float(snapshot.get("turnover") or 0)
    quality_score = float(snapshot.get("data_quality_score") or 0)
    risk_flags = {
        "is_st": "ST" in name.upper() or active_st_history,
        "untradable": bool(snapshot.get("is_suspended")),
        "fraud": any(event in {"financial_fraud", "fraud"} for event in event_types),
        # Financial institutions use balance-sheet/ROE evidence; their cash
        # flow statement is intentionally not a generic quality prerequisite.
        "critical_data_missing": not ((income and balance and indicator) if financial_template else (income and balance and cashflow and indicator)) or quality_score < 80,
        "illiquid": turnover <= 0 or turnover < 20000,
        "adverse_audit": bool(audit_result) and not standard_audit,
        "goodwill_abnormal": goodwill_ratio is not None and goodwill_ratio > .30,
        "receivable_inventory_abnormal": receivable_inventory_ratio is not None and receivable_inventory_ratio > .60,
        "profit_cashflow_divergence": cash_conversion is not None and cash_conversion < 0,
        "debt_default": any(event in {"debt_default", "bond_default"} for event in event_types),
        "major_investigation": any(item.get("event_type") in {"regulatory_risk", "investigation"} and item.get("severity") in {"critical", "high"} for item in specialized_state["negative_events"]),
        "high_pledge": pledge_ratio >= 50 or any(item.get("event_type") == "pledge" and item.get("severity") in {"critical", "high"} for item in negative_events),
        "repeated_reduction": sum(1 for item in negative_events if item.get("event_type") == "shareholder_reduction") >= 2,
        "cross_industry_ma": sum(1 for item in negative_events if item.get("event_type") == "ma_restructuring") >= 2,
        "large_related_party": any(item.get("event_type") == "related_party_transaction" for item in negative_events),
        "_facts": {
            "audit_result": audit_result or None,
            "audit_available_time": audit[0].get("available_time") if audit else None,
            "pledge_ratio_pct": pledge_ratio if pledge else None,
            "goodwill_to_assets": goodwill_ratio,
            "receivable_inventory_to_revenue": receivable_inventory_ratio,
            "daily_turnover_thousand_cny": turnover,
            "data_quality_score": quality_score,
            "negative_event_types": event_types,
        },
    }
    risk = evaluate_risk(risk_flags)
    dimensions = {name: score_dimension(name, values) for name, values in dimensions_input.items()}
    valuation_state = specialized_state["valuation"]
    valuation_upside = None
    if valuation_state.get("scenarios") and valuation_state.get("current_price"):
        valuation_upside = float(valuation_state["scenarios"]["base"]) / float(valuation_state["current_price"]) - 1
    raw_metrics = {
        "revenue_growth": revenue_growth,
        "core_profit_growth": profit_growth,
        "three_year_revenue_cagr": _three_year_cagr(income, "total_revenue"),
        "growth_acceleration": specialized_state["expectation"].get("observations", {}).get("growth_acceleration"),
        "cash_conversion": cash_conversion,
        "free_cashflow": free_cashflow,
        "debt_to_assets": debt_ratio,
        "valuation_base_upside": valuation_upside,
    }
    evidence = [
        {"api": api, "period": rows[0]["period"], "available_time": rows[0]["available_time"]}
        for api, rows in (("income", income), ("balancesheet", balance), ("cashflow", cashflow), ("fina_indicator", indicator), ("fina_audit", audit), ("pledge_stat", pledge)) if rows
    ]
    professional_available_time = max(
        (
            str(item.get("available_time") or "")
            for item in professional_template_data.get("point_in_time_evidence") or []
        ),
        default="",
    )
    if professional_template_data.get("total_metrics"):
        evidence.append({
            "api": "derived_professional_template_data",
            "period": income[0]["period"] if income else None,
            "available_time": professional_available_time or None,
            "schema_version": professional_template_data.get("schema_version"),
            "template_kind": template_kind,
            "coverage": professional_template_data.get("coverage"),
            "metrics": professional_template_data.get("metrics"),
            "production_score_consumed": False,
        })
    result = {
        "ok": True,
        "ts_code": code,
        "as_of_time": as_of_time,
        "profile": {"name": name, "industry": master.get("industry"), "list_date": master.get("list_date"), "trade_date": snapshot.get("trade_date")},
        "industry_template": {
            "kind": template_kind,
            "facts": template_facts,
            "limitations": template_limitations,
            "professional_data": professional_template_data,
        },
        "classification": classify_company(industry=str(master.get("industry") or ""), metrics=raw_metrics),
        "risk": risk,
        "raw_metrics": raw_metrics,
        "dimensions": dimensions,
        "research_state": research_state(dimensions, risk),
        "evidence": evidence,
        "linked_states": {"expectation": specialized_state["expectation"], "valuation": specialized_state["valuation"], "industry": specialized_state["industry"], "event_count": len(specialized_state["events"])},
        "limitations": [item for item in [
            "扣非利润与ROIC仍缺统一跨行业口径",
            None if specialized_state["industry"] else "行业指标尚未映射到该公司行业",
            None if specialized_state["expectation"].get("status") == "ready" else "预期变化证据尚未达到完整门槛",
            None if specialized_state["valuation"].get("scenarios") else "估值情景证据不足",
            "金融机构不以通用经营现金流/自由现金流评分；待补资本充足率、不良率和拨备覆盖率" if financial_template else None,
            *template_limitations,
        ] if item]
    }
    if as_of_date:
        result["point_in_time_method"] = "immutable_snapshot_and_raw_versions_v1"
        result["observation_evidence"] = {
            "snapshot": {
                "trade_date": snapshot.get("trade_date"),
                "observed_at": snapshot.get("observed_at"),
                "first_observed_at": snapshot.get("first_observed_at"),
                "economic_available_time": snapshot.get("economic_available_time"),
                "created_at": snapshot.get("created_at"),
            },
            "raw_versions_required": True,
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


def materialize_pool_research_snapshots(*, limit: int = 100, as_of_time: str | None = None) -> dict[str, Any]:
    """Persist one evidence-bearing research snapshot per managed pool member.

    Interactive reads already create snapshots, but relying on page visits
    leaves the historical evidence trail incomplete.  The daily research
    maintenance cycle calls this bounded helper for candidate/watch/core names
    so a later thesis review can distinguish "no change" from "not checked".
    """
    from app.services.decision_store import list_pool_members

    codes: list[str] = []
    for pool_type in ("candidate", "watch", "core"):
        for item in list_pool_members(pool_type):
            code = str(item.get("ts_code") or "").strip().upper()
            if code and code not in codes:
                codes.append(code)
    capped = codes[:max(1, min(int(limit), 500))]
    written: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for code in capped:
        try:
            result = build_stock_research(code, persist=True, as_of_time=as_of_time)
            if result.get("ok"):
                written.append({"ts_code": code, "confidence": result.get("research_state", {}).get("confidence")})
            else:
                failed.append({"ts_code": code, "reason": result.get("reason") or "research_unavailable"})
        except Exception as exc:  # noqa: BLE001
            failed.append({"ts_code": code, "reason": str(exc)[:300]})
    return {
        "ok": not failed,
        "requested": len(capped),
        "written": written,
        "failed": failed,
        "skipped_by_limit": max(0, len(codes) - len(capped)),
        "contract": "daily_pool_research_evidence_snapshot_v1",
    }


def _prior_year(rows: list[dict[str, Any]], period: str) -> dict[str, Any] | None:
    if len(period) != 8:
        return None
    wanted = str(int(period[:4]) - 1) + period[4:]
    return next((row for row in rows if row.get("period") == wanted), None)


def build_expectation_state(ts_code: str, *, persist_baseline: bool = True, as_of_time: str | None = None) -> dict[str, Any]:
    code = str(ts_code).strip().upper()
    income, indicator = _payloads(code, "income", 16, as_of_time=as_of_time), _payloads(code, "fina_indicator", 16, as_of_time=as_of_time)
    forecast_history = _forecast_history(code, 16, as_of_time=as_of_time)
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
        # A derived baseline may be persisted now, but its feature/evidence
        # clock is the frozen cutoff when called by formal replay.  Never stamp
        # it with wall-clock ``now`` and make a historical result look newer.
        baseline_available_time = normalize_explicit_timestamp(as_of_time) if as_of_time else now
        baseline = {"target_period": target_period, "revenue_forecast": revenue_forecast, "profit_forecast": profit_forecast, "confidence": confidence, "model_version": "seasonal_yoy_baseline_v1", "available_time": baseline_available_time}
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


def _dcf_per_share(
    *, free_cashflow: float, growth: float, discount_rate: float, terminal_growth: float,
    net_cash: float, shares: float,
) -> float | None:
    if free_cashflow <= 0 or shares <= 0 or discount_rate <= terminal_growth:
        return None
    present_value = 0.0
    projected = float(free_cashflow)
    for year in range(1, 6):
        projected *= 1.0 + growth
        present_value += projected / ((1.0 + discount_rate) ** year)
    terminal = projected * (1.0 + terminal_growth) / (discount_rate - terminal_growth)
    equity_value = present_value + terminal / ((1.0 + discount_rate) ** 5) + net_cash
    return max(0.0, equity_value / shares)


def build_valuation_state(ts_code: str, *, persist: bool = True, as_of_time: str | None = None) -> dict[str, Any]:
    code = str(ts_code).strip().upper()
    as_of_date = str(as_of_time or "")[:10].replace("-", "")
    with _conn() as c:
        if as_of_date:
            cutoff = normalize_explicit_timestamp(str(as_of_time))
            if not cutoff:
                return {
                    "ok": False,
                    "status": "blocked",
                    "reason": "invalid_observation_cutoff",
                    "ts_code": code,
                }
            master_row, master_status = _strict_master_row(
                c, code, as_of_time=cutoff,
            )
            version_rows = load_snapshot_versions(
                c,
                cutoff=cutoff,
                ts_codes=[code],
                trade_date_end=as_of_date,
            )
            if version_rows is None:
                return {
                    "ok": False,
                    "status": "blocked",
                    "reason": "immutable_snapshot_schema_unavailable",
                    "ts_code": code,
                    "as_of_time": cutoff,
                }
            rows = [_decode_snapshot_observation(dict(row)) for row in version_rows]
            rows.sort(key=lambda item: str(item.get("trade_date") or ""), reverse=True)
        else:
            master_row = c.execute("SELECT * FROM dc_stock_master WHERE ts_code=?", (code,)).fetchone()
            master_status = "ready" if master_row else "stock_not_found"
            rows = [dict(row) for row in c.execute("SELECT trade_date,close,pe_ttm,pb,total_mv,available_time FROM dc_stock_daily_snapshot WHERE ts_code=? ORDER BY trade_date DESC LIMIT 500", (code,)).fetchall()]
        if as_of_date and not rows:
            return {
                "ok": False,
                "status": "blocked",
                "reason": "immutable_snapshot_not_visible_at_cutoff",
                "ts_code": code,
                "as_of_time": str(as_of_time or ""),
            }
        if not master_row or not rows:
            return {
                "ok": False,
                "status": "blocked" if as_of_date else "insufficient_data",
                "reason": master_status if as_of_date and not master_row else "valuation_data_missing",
                "ts_code": code,
                "as_of_time": str(as_of_time or ""),
            }
        if as_of_date and master_status != "ready":
            return {
                "ok": False,
                "status": "blocked",
                "reason": master_status,
                "ts_code": code,
                "as_of_time": str(as_of_time),
                "point_in_time_method": "immutable_snapshot_and_master_clock_v1",
            }
        master = dict(master_row)
        latest = rows[0]
        if as_of_time:
            peer_master_rows = c.execute(
                """SELECT ts_code,available_time,updated_at,source_code FROM dc_stock_master
                   WHERE industry=? AND ts_code<>?
                     AND NULLIF(TRIM(COALESCE(source_code,'')),'') IS NOT NULL
                     AND available_time<>'' AND updated_at<>''""",
                (master.get("industry") or "", code),
            ).fetchall()
            peer_codes = []
            for row in peer_master_rows:
                peer = dict(row)
                available = normalize_timestamp(peer.get("available_time"))
                updated = normalize_timestamp(peer.get("updated_at"))
                if available and updated and available <= cutoff and updated <= cutoff:
                    peer_codes.append(str(peer.get("ts_code") or ""))
            peer_versions = load_snapshot_versions(
                c,
                cutoff=normalize_explicit_timestamp(str(as_of_time)),
                ts_codes=peer_codes,
                trade_date=str(latest.get("trade_date") or ""),
            ) if peer_codes else []
            peer_rows = [
                _decode_snapshot_observation(dict(row))
                for row in (peer_versions or [])
            ]
        else:
            peer_query = """SELECT s.pe_ttm,s.pb FROM dc_stock_daily_snapshot s JOIN dc_stock_master m ON m.ts_code=s.ts_code
                   WHERE s.trade_date=? AND m.industry=? AND s.ts_code<>?"""
            peer_params: tuple[Any, ...] = (latest["trade_date"], master.get("industry") or "", code)
            peer_rows = [dict(row) for row in c.execute(peer_query, peer_params).fetchall()]
    financial = any(x in str(master.get("industry") or "") for x in ("银行", "保险", "证券"))
    cyclical = any(x in str(master.get("industry") or "") for x in ("煤炭", "石油", "有色", "钢铁", "化工"))
    asset_based = cyclical or any(x in str(master.get("industry") or "") for x in ("房地产", "公用事业", "交通运输"))
    multiple_key = "pb" if financial else "pe_ttm"
    current_multiple = latest.get(multiple_key)
    history = [float(x[multiple_key]) for x in rows if x.get(multiple_key) and float(x[multiple_key]) > 0]
    peers = [float(x[multiple_key]) for x in peer_rows if x.get(multiple_key) and float(x[multiple_key]) > 0]
    price = float(latest.get("close") or 0)
    relative_scenarios: dict[str, float] | None = None
    history_percentile = industry_percentile = None
    if current_multiple and float(current_multiple) > 0 and len(history) >= 20:
        current_multiple = float(current_multiple)
        sorted_history = sorted(history)
        quantile = lambda p: sorted_history[min(len(sorted_history) - 1, int((len(sorted_history) - 1) * p))]
        bear_m, base_m, bull_m = quantile(.25), quantile(.50), quantile(.75)
        relative_scenarios = {"bear": price * bear_m / current_multiple, "base": price * base_m / current_multiple, "bull": price * bull_m / current_multiple}
        history_percentile = _percentile(history, current_multiple)
        industry_percentile = _percentile(peers, current_multiple)

    income_rows = _payloads(code, "income", limit=24, as_of_time=as_of_time)
    balance_rows = _payloads(code, "balancesheet", limit=8, as_of_time=as_of_time)
    indicator_rows = _payloads(code, "fina_indicator", limit=8, as_of_time=as_of_time)
    total_mv = float(latest.get("total_mv") or 0)
    shares = total_mv * 10000.0 / price if total_mv > 0 and price > 0 else 0.0

    normalized_scenarios: dict[str, float] | None = None
    annual_income = [item["payload"] for item in income_rows if str(item.get("period") or "").endswith("1231")]
    normalized_profits = [float(item.get("n_income_attr_p") or item.get("n_income") or 0) for item in annual_income[:7] if float(item.get("n_income_attr_p") or item.get("n_income") or 0) > 0]
    if cyclical and len(normalized_profits) >= 3 and shares > 0:
        normalized_profit = median(normalized_profits)
        normalized_eps = normalized_profit / shares
        valid_multiples = sorted(history or peers)
        if valid_multiples:
            pick = lambda p: valid_multiples[min(len(valid_multiples) - 1, int((len(valid_multiples) - 1) * p))]
            normalized_scenarios = {"bear": normalized_eps * pick(.25), "base": normalized_eps * pick(.50), "bull": normalized_eps * pick(.75)}

    pb_roe_scenarios: dict[str, float] | None = None
    if financial and shares > 0 and balance_rows:
        latest_balance_payload = balance_rows[0]["payload"]
        equity = float(latest_balance_payload.get("total_hldr_eqy_exc_min_int") or 0)
        roe_values = [float(item["payload"].get("roe") or 0) / 100.0 for item in indicator_rows if item["payload"].get("roe") is not None]
        roe = median(roe_values[:5]) if roe_values else 0.0
        book_per_share = equity / shares if equity > 0 else 0.0
        if book_per_share > 0 and roe > 0:
            def fair_pb(cost: float, growth: float) -> float:
                return max(.3, min(4.0, (roe - growth) / max(.01, cost - growth)))
            pb_roe_scenarios = {"bear": book_per_share * fair_pb(.13, .02), "base": book_per_share * fair_pb(.11, .03), "bull": book_per_share * fair_pb(.10, .04)}

    nav_scenarios: dict[str, float] | None = None
    if asset_based and shares > 0 and balance_rows:
        balance = balance_rows[0]["payload"]
        net_assets = float(balance.get("total_hldr_eqy_exc_min_int") or 0)
        if net_assets > 0:
            nav = net_assets / shares
            nav_scenarios = {"bear": nav * .65, "base": nav * .85, "bull": nav * 1.05}

    dcf_scenarios: dict[str, float] | None = None
    dcf_assumptions: dict[str, Any] = {}
    if not financial:
        cashflow_rows = _payloads(code, "cashflow", limit=12, as_of_time=as_of_time)
        annual_cashflows = [item for item in cashflow_rows if str(item.get("period") or "").endswith("1231")]
        latest_cashflow = annual_cashflows[0]["payload"] if annual_cashflows else {}
        operating_cash = latest_cashflow.get("n_cashflow_act")
        capex = latest_cashflow.get("c_pay_acq_const_fiolta")
        free_cashflow = None if operating_cash is None else float(operating_cash) - float(capex or 0)
        revenue_cagr = _three_year_cagr(income_rows, "total_revenue")
        base_growth = max(-.05, min(.15, float(revenue_cagr or 3.0) / 100.0))
        latest_balance = balance_rows[0]["payload"] if balance_rows else {}
        cash = float(latest_balance.get("money_cap") or 0)
        debt = sum(float(latest_balance.get(key) or 0) for key in ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y"))
        if free_cashflow is not None and free_cashflow > 0 and shares > 0:
            values = {
                "bear": _dcf_per_share(free_cashflow=free_cashflow, growth=max(-.08, base_growth - .05), discount_rate=.12, terminal_growth=.01, net_cash=cash - debt, shares=shares),
                "base": _dcf_per_share(free_cashflow=free_cashflow, growth=base_growth, discount_rate=.10, terminal_growth=.025, net_cash=cash - debt, shares=shares),
                "bull": _dcf_per_share(free_cashflow=free_cashflow, growth=min(.18, base_growth + .03), discount_rate=.09, terminal_growth=.03, net_cash=cash - debt, shares=shares),
            }
            if all(value is not None for value in values.values()):
                dcf_scenarios = {key: float(value) for key, value in values.items() if value is not None}
                sensitivity = {
                    "revenue_growth": {"low": _dcf_per_share(free_cashflow=free_cashflow, growth=max(-.08, base_growth - .02), discount_rate=.10, terminal_growth=.025, net_cash=cash - debt, shares=shares), "base": values["base"], "high": _dcf_per_share(free_cashflow=free_cashflow, growth=min(.18, base_growth + .02), discount_rate=.10, terminal_growth=.025, net_cash=cash - debt, shares=shares)},
                    "operating_margin": {"low": _dcf_per_share(free_cashflow=free_cashflow * .90, growth=base_growth, discount_rate=.10, terminal_growth=.025, net_cash=cash - debt, shares=shares), "base": values["base"], "high": _dcf_per_share(free_cashflow=free_cashflow * 1.10, growth=base_growth, discount_rate=.10, terminal_growth=.025, net_cash=cash - debt, shares=shares)},
                    "capital_expenditure": {"low": _dcf_per_share(free_cashflow=max(1.0, free_cashflow - abs(float(capex or 0)) * .10), growth=base_growth, discount_rate=.10, terminal_growth=.025, net_cash=cash - debt, shares=shares), "base": values["base"], "high": _dcf_per_share(free_cashflow=free_cashflow + abs(float(capex or 0)) * .10, growth=base_growth, discount_rate=.10, terminal_growth=.025, net_cash=cash - debt, shares=shares)},
                    "discount_rate": {"low": _dcf_per_share(free_cashflow=free_cashflow, growth=base_growth, discount_rate=.11, terminal_growth=.025, net_cash=cash - debt, shares=shares), "base": values["base"], "high": _dcf_per_share(free_cashflow=free_cashflow, growth=base_growth, discount_rate=.09, terminal_growth=.025, net_cash=cash - debt, shares=shares)},
                    "terminal_growth": {"low": _dcf_per_share(free_cashflow=free_cashflow, growth=base_growth, discount_rate=.10, terminal_growth=.02, net_cash=cash - debt, shares=shares), "base": values["base"], "high": _dcf_per_share(free_cashflow=free_cashflow, growth=base_growth, discount_rate=.10, terminal_growth=.03, net_cash=cash - debt, shares=shares)},
                }
                dcf_assumptions = {"free_cashflow": free_cashflow, "base_growth": base_growth, "net_cash": cash - debt, "shares": shares, "forecast_years": 5, "sensitivity": sensitivity}

    scenario_methods = [("relative", relative_scenarios), ("DCF", dcf_scenarios), ("normalized_earnings", normalized_scenarios), ("PB_ROE", pb_roe_scenarios), ("NAV", nav_scenarios)]
    available_methods = [(name, value) for name, value in scenario_methods if value]
    if available_methods:
        scenarios = {key: sum(float(value[key]) for _, value in available_methods) / len(available_methods) for key in ("bear", "base", "bull")}
        method = "+".join(name for name, _ in available_methods) + "-three-scenario-v1"
    else:
        return {"ok": True, "ts_code": code, "status": "insufficient_data", "method": "PB-ROE" if financial else "DCF/relative", "reason": "相对估值历史不足且现金流折现证据不完整", "history_points": len(history)}
    dimension = score_dimension("valuation", {
        "historical_percentile": None if history_percentile is None else (1 - history_percentile) * 100,
        "industry_relative": None if industry_percentile is None else (1 - industry_percentile) * 100,
        "normalized_yield": _linear(current_multiple, 60 if not financial else 6, 8 if not financial else .7) if current_multiple else None,
        "scenario_value_gap": _linear(scenarios["base"] / price - 1 if price else None, -.3, .5),
        "downside_protection": _linear(scenarios["bear"] / price - 1 if price else None, -.5, .1),
    })
    now = datetime.now().isoformat(timespec="seconds")
    if persist:
        with _business_conn() as c:
            c.execute(
                """INSERT INTO valuation_snapshots(ts_code,as_of_time,method,bear_value,base_value,bull_value,current_price,assumptions_json,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (code, latest["available_time"], method, scenarios["bear"], scenarios["base"], scenarios["bull"], price,
                 json.dumps({"history_points": len(history), "peer_count": len(peers), "multiple": multiple_key, "dcf": dcf_assumptions, "scenario_probabilities": {"bear": .25, "base": .50, "bull": .25}}, ensure_ascii=False), json.dumps({"trade_date": latest["trade_date"], "source": "dc_stock_daily_snapshot+financial_statements"}, ensure_ascii=False), now),
            )
    limitations = []
    if not dcf_scenarios and not financial:
        limitations.append("现金流或股本证据不足，未启用DCF交叉验证")
    if financial and not pb_roe_scenarios:
        limitations.append("金融企业PB-ROE所需净资产或ROE证据不足")
    if cyclical and not normalized_scenarios:
        limitations.append("周期企业有效年度利润不足，未形成正常化盈利中枢")
    probabilities = {"bear": .25, "base": .50, "bull": .25}
    weighted_value = sum(scenarios[key] * probabilities[key] for key in probabilities)
    return {"ok": True, "ts_code": code, "status": dimension["status"], "method": method, "trade_date": latest["trade_date"], "current_price": price, "current_multiple": current_multiple, "history_percentile": history_percentile, "industry_percentile": industry_percentile, "scenarios": scenarios, "scenario_probabilities": probabilities, "probability_weighted_value": weighted_value, "value_interval": [scenarios["bear"], scenarios["bull"]], "relative_scenarios": relative_scenarios, "dcf_scenarios": dcf_scenarios, "normalized_earnings_scenarios": normalized_scenarios, "pb_roe_scenarios": pb_roe_scenarios, "nav_scenarios": nav_scenarios, "dimension": dimension, "evidence": {"history_points": len(history), "peer_count": len(peers), "available_time": latest["available_time"], "dcf_assumptions": dcf_assumptions}, "limitations": limitations}
