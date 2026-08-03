from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable


EVENT_FEATURE_CONTRACT = "alis_point_in_time_structured_event_features_v3"
ANALYST_CONSENSUS_CONTRACT = "alis_pit_independent_analyst_consensus_v2"
ANALYST_MULTI_METRIC_CONSENSUS_CONTRACT = (
    "alis_pit_independent_analyst_multi_metric_consensus_v1"
)
ANALYST_CONSENSUS_LOOKBACK_DAYS = 180
ANALYST_CONSENSUS_MIN_ORGS = 3
ANALYST_CONSENSUS_METRICS = {
    "net_profit_cny": {"field": "np", "scale": 10_000.0, "unit": "CNY"},
    "revenue_cny": {"field": "op_rt", "scale": 10_000.0, "unit": "CNY"},
    "operating_profit_cny": {"field": "op_pr", "scale": 10_000.0, "unit": "CNY"},
    "eps_cny_per_share": {"field": "eps", "scale": 1.0, "unit": "CNY/share"},
}
STRUCTURED_DISCLOSURE_APIS = (
    "forecast",
    "express",
    "dividend",
    "repurchase",
    "stk_holdertrade",
    "pledge_stat",
    "fina_audit",
    "namechange",
)
EVENT_TYPE_GROUPS = (
    "earnings",
    "capital_return",
    "contract_operation",
    "governance_risk",
    "other",
)

_TYPE_HINTS = {
    "earnings": (
        "earnings",
        "earnings_forecast",
        "earnings_flash",
        "forecast",
        "express",
        "业绩",
        "利润",
        "营收",
        "预增",
        "预减",
        "扭亏",
        "亏损",
    ),
    "capital_return": (
        "repurchase",
        "dividend",
        "shareholder_increase",
        "shareholder_reduction",
        "回购",
        "分红",
        "增持",
        "减持",
    ),
    "contract_operation": (
        "major_contract",
        "ma_restructuring",
        "private_placement",
        "capital_operation",
        "订单",
        "中标",
        "合同",
        "重组",
        "并购",
        "定增",
        "获批",
    ),
    "governance_risk": (
        "regulatory_risk",
        "investigation",
        "inquiry",
        "pledge",
        "audit_opinion",
        "name_change",
        "立案",
        "处罚",
        "问询",
        "质押",
        "审计",
        "诉讼",
        "违约",
        "退市",
    ),
}
_API_EVENT_GROUP = {
    "forecast": "earnings",
    "express": "earnings",
    "dividend": "capital_return",
    "repurchase": "capital_return",
    "stk_holdertrade": "capital_return",
    "pledge_stat": "governance_risk",
    "fina_audit": "governance_risk",
    "namechange": "governance_risk",
}
_SOURCE_PRIORITY = {
    "structured_disclosure": 4,
    "announcement": 3,
    "major_news": 2,
}
_DIRECTION_SCORE = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
_SEVERITY_SCORE = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}
_AMOUNT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(万亿元|亿元|万元|亿|万)(?![\d.])")
_PERCENT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")


def report_period_key(value: Any) -> str:
    """Normalize a report end date or ``YYYYQn`` into one period key."""
    raw = str(value or "").strip().upper().replace("-", "").replace(" ", "")
    direct = re.fullmatch(r"(?:FY)?(\d{4})Q([1-4])E?", raw)
    if direct:
        return f"{direct.group(1)}Q{direct.group(2)}"
    annual = re.fullmatch(r"(?:FY)?(\d{4})(?:E|A)", raw)
    if annual:
        return f"{annual.group(1)}Q4"
    if re.fullmatch(r"\d{8}", raw):
        month = raw[4:6]
        quarter = {"03": "1", "06": "2", "09": "3", "12": "4"}.get(month)
        if quarter:
            return f"{raw[:4]}Q{quarter}"
    chinese_year = re.search(r"((?:19|20)\d{2})年", raw)
    if chinese_year:
        quarter = (
            "1" if any(hint in raw for hint in ("一季", "1季", "第一季")) else
            "2" if any(hint in raw for hint in ("半年", "中报", "二季", "2季", "第二季")) else
            "3" if any(hint in raw for hint in ("三季", "3季", "第三季")) else
            "4" if any(hint in raw for hint in ("年报", "全年", "年度", "四季", "4季", "第四季")) else
            ""
        )
        if quarter:
            return f"{chinese_year.group(1)}Q{quarter}"
    return ""


def report_period_end_date(value: Any) -> str:
    key = report_period_key(value)
    match = re.fullmatch(r"(\d{4})Q([1-4])", key)
    if not match:
        return ""
    suffix = {"1": "0331", "2": "0630", "3": "0930", "4": "1231"}[match.group(2)]
    return f"{match.group(1)}{suffix}"


def analyst_net_profit_cny(payload: dict[str, Any] | str | None) -> float | None:
    """Return the point-in-time report_rc net-profit estimate in CNY.

    TuShare ``report_rc.np`` is expressed in ten-thousand CNY.  Keeping this
    conversion in the shared event feature module prevents training and live
    fusion from silently comparing it with ``express.n_income`` in CNY.
    """
    values = payload_dict(payload)
    estimate = safe_float(values.get("np"))
    if estimate is None or estimate == 0:
        return None
    return estimate * 10_000.0


def analyst_estimate_values(
    payload: dict[str, Any] | str | None,
) -> dict[str, float]:
    """Normalize official report_rc forecast fields without imputing gaps."""
    values = payload_dict(payload)
    estimates: dict[str, float] = {}
    for metric, contract in ANALYST_CONSENSUS_METRICS.items():
        raw = safe_float(values.get(str(contract["field"])))
        if raw is None:
            continue
        estimate = float(raw) * float(contract["scale"])
        if math.isfinite(estimate):
            estimates[metric] = estimate
    return estimates


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip()[:10].replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        return ""
    try:
        datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return ""
    return raw


def build_analyst_consensus_index(
    rows: Iterable[Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Normalize retained ``report_rc`` rows into a point-in-time index.

    A broker is an independent vote. Reports without an institution, a valid
    forecast period, a publication date, or a numeric net-profit estimate are
    excluded instead of being treated as zero-valued consensus evidence.
    """
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw_row in rows:
        item = dict(raw_row)
        payload = payload_dict(item.get("payload_json"))
        symbol = str(item.get("ts_code") or payload.get("ts_code") or "").strip()
        period = report_period_key(
            payload.get("quarter") or payload.get("end_date") or item.get("period")
        )
        estimates = analyst_estimate_values(payload)
        estimate = estimates.get("net_profit_cny")
        organization = str(payload.get("org_name") or "").strip()
        report_date = _compact_date(
            payload.get("report_date") or item.get("trade_date")
        )
        available_date = _compact_date(item.get("available_time")) or report_date
        if not all((symbol, period, organization, report_date, available_date)):
            continue
        if not estimates or available_date < report_date:
            continue
        index.setdefault((symbol, period), []).append({
            "organization": organization,
            "report_date": report_date,
            "available_date": available_date,
            "estimate_cny": float(estimate) if estimate is not None else None,
            "estimates": estimates,
            "report_title": str(payload.get("report_title") or ""),
        })
    for values in index.values():
        values.sort(key=lambda value: (
            str(value.get("available_date") or ""),
            str(value.get("report_date") or ""),
            str(value.get("organization") or ""),
            str(value.get("report_title") or ""),
        ))
    return index


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def point_in_time_analyst_consensus(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    symbol: str,
    period: Any,
    as_of_date: Any,
    lookback_days: int = ANALYST_CONSENSUS_LOOKBACK_DAYS,
    minimum_organizations: int = ANALYST_CONSENSUS_MIN_ORGS,
) -> dict[str, Any]:
    """Return an independent-institution consensus known by ``as_of_date``.

    Repeated reports from one broker never receive extra voting weight: only
    that broker's latest available estimate inside the stale-data window is
    retained. At least three independent brokers are required before an
    estimate may become an observed event surprise.
    """
    period_key = report_period_key(period)
    cutoff = _compact_date(as_of_date)
    empty = {
        "contract": ANALYST_CONSENSUS_CONTRACT,
        "period": period_key,
        "as_of_date": cutoff,
        "observed": False,
        "estimate_cny": None,
        "organization_count": 0,
        "dispersion": None,
        "freshness_days": None,
        "minimum_organizations": int(minimum_organizations),
        "lookback_days": int(lookback_days),
    }
    if not period_key or not cutoff:
        return empty
    cutoff_date = datetime.strptime(cutoff, "%Y%m%d").date()
    start = (cutoff_date - timedelta(days=max(1, int(lookback_days)))).strftime("%Y%m%d")
    latest_by_org: dict[str, dict[str, Any]] = {}
    for row in index.get((str(symbol), period_key), []):
        available_date = str(row.get("available_date") or "")
        report_date = str(row.get("report_date") or "")
        organization = str(row.get("organization") or "")
        if not organization or not (start <= report_date <= cutoff):
            continue
        if not (report_date <= available_date <= cutoff):
            continue
        previous = latest_by_org.get(organization)
        rank = (available_date, report_date, str(row.get("report_title") or ""))
        previous_rank = (
            str(previous.get("available_date") or ""),
            str(previous.get("report_date") or ""),
            str(previous.get("report_title") or ""),
        ) if previous else ("", "", "")
        if previous is None or rank >= previous_rank:
            latest_by_org[organization] = row
    estimates = [
        float(row["estimate_cny"])
        for row in latest_by_org.values()
        if safe_float(row.get("estimate_cny")) is not None
    ]
    if not estimates:
        return empty
    estimate = float(median(estimates))
    q25 = _percentile(estimates, 0.25)
    q75 = _percentile(estimates, 0.75)
    dispersion = (q75 - q25) / abs(estimate) if estimate else None
    latest_date = max(str(row.get("report_date") or "") for row in latest_by_org.values())
    freshness = (cutoff_date - datetime.strptime(latest_date, "%Y%m%d").date()).days
    observed = len(estimates) >= max(1, int(minimum_organizations)) and estimate != 0
    return {
        **empty,
        "observed": observed,
        "estimate_cny": estimate if observed else None,
        "raw_median_estimate_cny": estimate,
        "organization_count": len(estimates),
        "dispersion": dispersion,
        "freshness_days": max(0, freshness),
        "latest_report_date": latest_date,
    }


def _point_in_time_metric_consensus(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    symbol: str,
    period: str,
    as_of_date: str,
    metric: str,
    lookback_days: int,
    minimum_organizations: int,
) -> dict[str, Any]:
    contract = ANALYST_CONSENSUS_METRICS[metric]
    empty = {
        "metric": metric,
        "unit": str(contract["unit"]),
        "observed": False,
        "estimate": None,
        "raw_median_estimate": None,
        "organization_count": 0,
        "dispersion": None,
        "freshness_days": None,
        "latest_report_date": None,
    }
    cutoff_date = datetime.strptime(as_of_date, "%Y%m%d").date()
    start = (
        cutoff_date - timedelta(days=max(1, int(lookback_days)))
    ).strftime("%Y%m%d")
    latest_by_org: dict[str, dict[str, Any]] = {}
    for row in index.get((str(symbol), period), []):
        estimate = safe_float((row.get("estimates") or {}).get(metric))
        available_date = str(row.get("available_date") or "")
        report_date = str(row.get("report_date") or "")
        organization = str(row.get("organization") or "")
        if estimate is None or not organization or not (start <= report_date <= as_of_date):
            continue
        if not (report_date <= available_date <= as_of_date):
            continue
        previous = latest_by_org.get(organization)
        rank = (available_date, report_date, str(row.get("report_title") or ""))
        previous_rank = (
            str(previous.get("available_date") or ""),
            str(previous.get("report_date") or ""),
            str(previous.get("report_title") or ""),
        ) if previous else ("", "", "")
        if previous is None or rank >= previous_rank:
            latest_by_org[organization] = row
    estimates = [
        float((row.get("estimates") or {})[metric])
        for row in latest_by_org.values()
        if safe_float((row.get("estimates") or {}).get(metric)) is not None
    ]
    if not estimates:
        return empty
    estimate = float(median(estimates))
    q25 = _percentile(estimates, 0.25)
    q75 = _percentile(estimates, 0.75)
    latest_date = max(
        str(row.get("report_date") or "") for row in latest_by_org.values()
    )
    observed = (
        len(estimates) >= max(1, int(minimum_organizations))
        and estimate != 0
    )
    return {
        **empty,
        "observed": observed,
        "estimate": estimate if observed else None,
        "raw_median_estimate": estimate,
        "organization_count": len(estimates),
        "dispersion": (q75 - q25) / abs(estimate) if estimate else None,
        "freshness_days": max(
            0,
            (cutoff_date - datetime.strptime(latest_date, "%Y%m%d").date()).days,
        ),
        "latest_report_date": latest_date,
    }


def point_in_time_analyst_consensus_bundle(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    symbol: str,
    period: Any,
    as_of_date: Any,
    lookback_days: int = ANALYST_CONSENSUS_LOOKBACK_DAYS,
    minimum_organizations: int = ANALYST_CONSENSUS_MIN_ORGS,
    revision_days: int = 28,
) -> dict[str, Any]:
    """Return PIT net-profit, revenue, operating-profit and EPS consensus.

    Each metric keeps one latest in-window estimate per independent broker.
    A 28-calendar-day revision is emitted only when both snapshots independently
    meet the same three-broker observation rule.
    """
    period_key = report_period_key(period)
    cutoff = _compact_date(as_of_date)
    result = {
        "contract": ANALYST_MULTI_METRIC_CONSENSUS_CONTRACT,
        "legacy_net_profit_contract": ANALYST_CONSENSUS_CONTRACT,
        "symbol": str(symbol),
        "period": period_key,
        "as_of_date": cutoff,
        "lookback_days": int(lookback_days),
        "minimum_organizations": int(minimum_organizations),
        "revision_days": int(revision_days),
        "metrics": {},
    }
    if not period_key or not cutoff:
        return result
    prior_cutoff = (
        datetime.strptime(cutoff, "%Y%m%d").date()
        - timedelta(days=max(1, int(revision_days)))
    ).strftime("%Y%m%d")
    for metric in ANALYST_CONSENSUS_METRICS:
        current = _point_in_time_metric_consensus(
            index,
            symbol=str(symbol),
            period=period_key,
            as_of_date=cutoff,
            metric=metric,
            lookback_days=lookback_days,
            minimum_organizations=minimum_organizations,
        )
        prior = _point_in_time_metric_consensus(
            index,
            symbol=str(symbol),
            period=period_key,
            as_of_date=prior_cutoff,
            metric=metric,
            lookback_days=lookback_days,
            minimum_organizations=minimum_organizations,
        )
        current_estimate = safe_float(current.get("estimate"))
        prior_estimate = safe_float(prior.get("estimate"))
        absolute_revision = (
            current_estimate - prior_estimate
            if current_estimate is not None and prior_estimate is not None
            else None
        )
        current["revision"] = {
            "prior_as_of_date": prior_cutoff,
            "prior_observed": bool(prior.get("observed")),
            "prior_estimate": prior_estimate,
            "absolute": absolute_revision,
            "relative": (
                absolute_revision / abs(prior_estimate)
                if absolute_revision is not None and prior_estimate
                else None
            ),
        }
        result["metrics"][metric] = current
    result["observed_metric_count"] = sum(
        bool(item.get("observed")) for item in result["metrics"].values()
    )
    return result


def disclosure_expectation_gap(
    *,
    api_name: str,
    payload: dict[str, Any] | str | None,
    consensus: dict[str, Any] | None,
    prior_guidance: dict[str, float | None] | None = None,
) -> tuple[float | None, str, dict[str, float | None]]:
    """Compare a disclosure with only strictly comparable point-in-time data."""
    values = payload_dict(payload)
    consensus = consensus or {}
    consensus_estimate = (
        safe_float(consensus.get("estimate_cny"))
        if bool(consensus.get("observed")) else None
    )
    changes = [
        float(value)
        for key in ("p_change_min", "p_change_max")
        if (value := safe_float(values.get(key))) is not None
    ]
    profits = [
        float(value) * 10_000.0
        for key in ("net_profit_min", "net_profit_max")
        if (value := safe_float(values.get(key))) is not None
    ]
    current_change = sum(changes) / len(changes) if changes else None
    current_profit_cny = sum(profits) / len(profits) if profits else None
    current = {"change_pct": current_change, "profit_cny": current_profit_cny}
    if api_name == "forecast":
        if current_profit_cny is not None and consensus_estimate not in (None, 0):
            return (
                (current_profit_cny - consensus_estimate) / abs(consensus_estimate),
                "company_guidance_vs_pit_independent_analyst_consensus_np_v2",
                current,
            )
        previous_change = safe_float((prior_guidance or {}).get("change_pct"))
        if current_change is not None and previous_change is not None:
            return (
                (current_change - previous_change) / 100.0,
                "company_guidance_revision_same_normalized_period_v2",
                current,
            )
    if api_name == "express":
        actual_profit = safe_float(values.get("n_income"))
        if actual_profit is not None and consensus_estimate not in (None, 0):
            return (
                (actual_profit - consensus_estimate) / abs(consensus_estimate),
                "earnings_flash_vs_pit_independent_analyst_consensus_np_v2",
                current,
            )
        previous_profit = safe_float((prior_guidance or {}).get("profit_cny"))
        if actual_profit is not None and previous_profit not in (None, 0):
            return (
                (actual_profit - previous_profit) / abs(previous_profit),
                "earnings_flash_vs_prior_company_guidance_same_normalized_period_v2",
                current,
            )
    return None, "", current


def safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def clip(value: Any, low: float, high: float, default: float = 0.0) -> float:
    numeric = safe_float(value)
    if numeric is None:
        return float(default)
    return max(float(low), min(float(high), numeric))


def event_type_group(
    *,
    event_type: str = "",
    api_name: str = "",
    text: str = "",
) -> str:
    if str(api_name or "") in _API_EVENT_GROUP:
        return _API_EVENT_GROUP[str(api_name)]
    normalized = " ".join((str(event_type), str(api_name), str(text))).lower()
    if re.search(r"(?<![a-z])\*?st(?![a-z])", normalized):
        return "governance_risk"
    for group, hints in _TYPE_HINTS.items():
        if any(hint in normalized for hint in hints):
            return group
    return "other"


def extract_text_amount_cny(text: str) -> float | None:
    multipliers = {
        "万": 10_000.0,
        "万元": 10_000.0,
        "亿": 100_000_000.0,
        "亿元": 100_000_000.0,
        "万亿元": 1_000_000_000_000.0,
    }
    values = [
        float(number) * multipliers[unit]
        for number, unit in _AMOUNT_RE.findall(str(text or ""))
    ]
    return max(values) if values else None


def extract_text_percentage(text: str) -> float | None:
    values = [safe_float(value) for value in _PERCENT_RE.findall(str(text or ""))]
    finite = [float(value) for value in values if value is not None]
    if not finite:
        return None
    return max(finite, key=abs) / 100.0


def _first_numeric(payload: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = safe_float(payload.get(key))
        if value is not None:
            return value
    return None


def structured_event_candidate(
    *,
    source: str,
    api_name: str = "",
    event_type: str = "",
    direction: str = "neutral",
    severity: str = "medium",
    confidence: float = 0.0,
    title: str = "",
    text: str = "",
    payload: dict[str, Any] | str | None = None,
    metrics: dict[str, Any] | str | None = None,
    expectation_gap: float | None = None,
    expectation_basis: str = "",
    analyst_consensus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_values = payload_dict(payload)
    metric_values = payload_dict(metrics)
    values = {**metric_values, **payload_values}
    combined_text = " ".join((str(title or ""), str(text or ""))).strip()
    api = str(api_name or "")
    event_name = str(event_type or api or "other")
    group = event_type_group(
        event_type=event_name,
        api_name=api,
        text=combined_text,
    )
    inferred_direction = str(direction or "neutral").lower()
    inferred_severity = str(severity or "medium").lower()
    magnitude: float | None = None
    amount_cny = extract_text_amount_cny(combined_text)
    percentage = extract_text_percentage(combined_text)
    direct_materiality: float | None = None

    if api == "forecast" or event_name == "earnings_forecast":
        changes = [
            value
            for value in (
                safe_float(values.get("p_change_min")),
                safe_float(values.get("p_change_max")),
                safe_float(values.get("profit_change_pct")),
            )
            if value is not None
        ]
        change = sum(changes) / len(changes) if changes else None
        if change is not None:
            inferred_direction = "positive" if change > 10 else "negative" if change < -10 else "neutral"
            inferred_severity = "high" if abs(change) >= 50 else "medium"
            magnitude = min(3.0, abs(change) / 50.0)
            percentage = change / 100.0
        forecast_amount = _first_numeric(values, ("net_profit_max", "net_profit_min"))
        # TuShare forecast profit fields are in ten-thousand CNY.
        amount_cny = (
            forecast_amount * 10_000.0
            if forecast_amount is not None
            else amount_cny
        )
    elif api == "express" or event_name == "earnings_flash":
        net_income = safe_float(values.get("n_income"))
        if net_income is not None:
            inferred_direction = "positive" if net_income > 0 else "negative" if net_income < 0 else "neutral"
            amount_cny = abs(net_income)
    elif api == "dividend" or event_name == "dividend":
        cash_per_ten = safe_float(values.get("cash_div_tax"))
        stock_per_ten = safe_float(values.get("stk_div"))
        inferred_direction = "positive"
        inferred_severity = "low"
        if cash_per_ten is not None or stock_per_ten is not None:
            magnitude = min(
                3.0,
                abs(cash_per_ten or 0.0) / 5.0 + abs(stock_per_ten or 0.0),
            )
    elif api == "repurchase" or event_name == "repurchase":
        process = str(values.get("proc") or "")
        repurchase_amount = safe_float(values.get("amount"))
        repurchase_volume = safe_float(values.get("vol"))
        inferred_direction = "positive"
        inferred_severity = "medium" if process in {"实施", "完成"} else "low"
        amount_cny = repurchase_amount if repurchase_amount is not None else amount_cny
        if repurchase_amount is not None or repurchase_volume is not None:
            magnitude = min(
                3.0,
                math.log1p(max(0.0, repurchase_amount or 0.0) / 10_000_000.0)
                + math.log1p(max(0.0, repurchase_volume or 0.0) / 1_000_000.0) / 2.0,
            )
    elif api == "stk_holdertrade" or event_name == "shareholder_trade":
        direction_code = str(values.get("in_de") or "").upper()
        change_ratio_pct = safe_float(values.get("change_ratio"))
        change_volume = safe_float(values.get("change_vol"))
        inferred_direction = (
            "positive" if direction_code == "IN" else
            "negative" if direction_code == "DE" else
            "neutral"
        )
        inferred_severity = (
            "high" if change_ratio_pct is not None and abs(change_ratio_pct) >= 1.0
            else "medium"
        )
        event_name = (
            "shareholder_increase" if direction_code == "IN" else
            "shareholder_reduction" if direction_code == "DE" else
            "shareholder_trade"
        )
        if change_ratio_pct is not None:
            percentage = change_ratio_pct / 100.0
            direct_materiality = min(1.0, abs(change_ratio_pct) / 5.0)
            magnitude = min(3.0, abs(change_ratio_pct))
        elif change_volume is not None:
            magnitude = min(3.0, math.log1p(abs(change_volume) / 1_000_000.0))
    elif api == "pledge_stat" or event_name == "pledge":
        pledge_ratio = safe_float(values.get("pledge_ratio"))
        if pledge_ratio is not None:
            inferred_direction = "negative" if pledge_ratio >= 30 else "neutral"
            inferred_severity = "high" if pledge_ratio >= 50 else "medium" if pledge_ratio >= 30 else "low"
            magnitude = min(3.0, max(0.0, pledge_ratio) / 30.0)
            direct_materiality = min(1.0, max(0.0, pledge_ratio) / 100.0)
            percentage = pledge_ratio / 100.0
    elif api == "fina_audit" or event_name == "audit_opinion":
        audit_result = str(values.get("audit_result") or combined_text)
        standard = "标准无保留意见" in audit_result or audit_result.strip() == "无保留意见"
        inferred_direction, inferred_severity = (
            ("neutral", "low") if standard else ("negative", "critical")
        )
        magnitude = 0.25 if standard else 1.0
        direct_materiality = 0.0 if standard else 1.0
    elif api == "namechange" or event_name == "name_change":
        name = str(values.get("name") or combined_text).upper()
        is_st = "ST" in name
        inferred_direction, inferred_severity = (
            ("negative", "high") if is_st else ("neutral", "low")
        )
        magnitude = 0.75 if is_st else 0.25
        direct_materiality = 1.0 if is_st else 0.0

    if magnitude is None and percentage is not None:
        magnitude = min(3.0, abs(percentage) / 0.25)
    if amount_cny is None:
        amount_cny = _first_numeric(
            values,
            (
                "amount",
                "contract_amount",
                "repurchase_amount",
                "buyback_amount",
                "total_amount",
                "n_income",
            ),
        )
    direction_value = _DIRECTION_SCORE.get(inferred_direction, 0.0)
    severity_value = _SEVERITY_SCORE.get(inferred_severity, 0.5)
    confidence_value = clip(confidence, 0.0, 1.0)
    consensus = analyst_consensus or {}
    return {
        "source": str(source or "unknown"),
        "api_name": api,
        "event_type": event_name,
        "event_type_group": group,
        "event_type_groups": [group],
        "direction": inferred_direction,
        "severity": inferred_severity,
        "sentiment": direction_value * severity_value,
        "confidence": confidence_value,
        "magnitude": magnitude,
        "magnitude_observed": magnitude is not None,
        "amount_cny": amount_cny,
        "amount_observed": amount_cny is not None and amount_cny > 0,
        "direct_materiality": direct_materiality,
        "percentage": percentage,
        "expectation_gap": (
            clip(expectation_gap, -3.0, 3.0)
            if expectation_gap is not None else 0.0
        ),
        "expectation_observed": expectation_gap is not None,
        "expectation_basis": str(expectation_basis or "unobservable"),
        "analyst_consensus_contract": str(
            consensus.get("contract") or ANALYST_CONSENSUS_CONTRACT
        ),
        "analyst_consensus_observed": bool(consensus.get("observed")),
        "analyst_consensus_org_count": int(
            consensus.get("organization_count") or 0
        ),
        "analyst_consensus_dispersion": safe_float(consensus.get("dispersion")),
        "analyst_consensus_freshness_days": safe_float(
            consensus.get("freshness_days")
        ),
        "event_count": 1,
        "evidence_numeric_count": sum(
            value is not None
            for value in (magnitude, amount_cny, percentage, expectation_gap)
        ),
    }


def merge_event_candidates(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if not current:
        primary = dict(candidate)
        primary["_deterministic_primary_key"] = _event_candidate_primary_key(
            candidate
        )
        return primary
    merged_groups = sorted(
        set(current.get("event_type_groups") or [])
        | set(candidate.get("event_type_groups") or [])
    )
    current_rank = (
        bool(current.get("expectation_observed")),
        int(_SOURCE_PRIORITY.get(str(current.get("source") or ""), 0)),
        bool(current.get("amount_observed")),
        bool(current.get("magnitude_observed")),
        float(current.get("confidence") or 0.0),
    )
    candidate_rank = (
        bool(candidate.get("expectation_observed")),
        int(_SOURCE_PRIORITY.get(str(candidate.get("source") or ""), 0)),
        bool(candidate.get("amount_observed")),
        bool(candidate.get("magnitude_observed")),
        float(candidate.get("confidence") or 0.0),
    )
    current_key = str(
        current.get("_deterministic_primary_key")
        or _event_candidate_primary_key(current)
    )
    candidate_key = str(
        candidate.get("_deterministic_primary_key")
        or _event_candidate_primary_key(candidate)
    )
    primary = dict(
        candidate
        if (candidate_rank, candidate_key) > (current_rank, current_key)
        else current
    )
    primary["_deterministic_primary_key"] = (
        candidate_key
        if (candidate_rank, candidate_key) > (current_rank, current_key)
        else current_key
    )
    primary["event_type_groups"] = merged_groups or ["other"]
    primary["event_count"] = int(current.get("event_count") or 1) + int(
        candidate.get("event_count") or 1
    )
    primary["evidence_numeric_count"] = int(
        current.get("evidence_numeric_count") or 0
    ) + int(candidate.get("evidence_numeric_count") or 0)
    observed_sources = (
        set(current.get("observed_sources") or [current.get("source")])
        | set(candidate.get("observed_sources") or [candidate.get("source")])
    ) - {None, ""}
    primary["observed_sources"] = sorted(str(value) for value in observed_sources)
    return primary


def _event_candidate_primary_key(candidate: dict[str, Any]) -> str:
    """Return an order-independent tie breaker for equal-quality events."""

    stable = {
        str(key): value
        for key, value in candidate.items()
        if key not in {
            "_deterministic_primary_key",
            "event_count",
            "evidence_numeric_count",
            "event_type_groups",
            "observed_sources",
        }
    }
    raw = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def materialize_event_features(
    observation: dict[str, Any] | None,
    *,
    total_mv: Any,
    close: Any,
    momentum_20: Any,
    volatility_20: Any,
    market_reaction: Any,
    crowding: Any,
    analyst_revision: float | None = None,
    analyst_target_price: float | None = None,
) -> dict[str, float]:
    event = observation or {}
    market_cap_ten_thousand = safe_float(total_mv)
    market_cap_cny = (
        market_cap_ten_thousand * 10_000.0
        if market_cap_ten_thousand is not None and market_cap_ten_thousand > 0
        else None
    )
    company_size_log = (
        math.log1p(market_cap_ten_thousand / 10_000.0)
        if market_cap_ten_thousand is not None and market_cap_ten_thousand > 0
        else 0.0
    )
    amount_cny = safe_float(event.get("amount_cny"))
    amount_to_market_cap = (
        amount_cny / market_cap_cny
        if amount_cny is not None and amount_cny > 0 and market_cap_cny
        else None
    )
    direct_materiality = safe_float(event.get("direct_materiality"))
    materiality = (
        min(1.0, math.tanh(max(0.0, amount_to_market_cap) * 20.0))
        if amount_to_market_cap is not None
        else clip(direct_materiality, 0.0, 1.0)
        if direct_materiality is not None
        else 0.0
    )
    magnitude = safe_float(event.get("magnitude"))
    magnitude_value = clip(
        max(float(magnitude or 0.0), materiality),
        0.0,
        3.0,
    )
    direction_signal = clip(event.get("sentiment"), -1.0, 1.0)
    reaction = clip(market_reaction, -1.0, 1.0)
    pre_event_runup = direction_signal * clip(momentum_20, -2.0, 2.0)
    directional_reaction = direction_signal * reaction
    volatility = max(0.005, abs(clip(volatility_20, -3.0, 3.0)))
    pricing_pressure = (
        max(0.0, directional_reaction) / volatility
        + max(0.0, pre_event_runup) / 0.10
        + clip(crowding, 0.0, 3.0) / 3.0
    )
    price = safe_float(close)
    target_upside = (
        analyst_target_price / price - 1.0
        if analyst_target_price is not None and price is not None and price > 0
        else None
    )
    groups = set(event.get("event_type_groups") or ["other"])
    return {
        "event_type_earnings": float("earnings" in groups),
        "event_type_capital_return": float("capital_return" in groups),
        "event_type_contract_operation": float("contract_operation" in groups),
        "event_type_governance_risk": float("governance_risk" in groups),
        "event_type_other": float("other" in groups),
        "event_magnitude": magnitude_value,
        "event_magnitude_observed": float(bool(event.get("magnitude_observed"))),
        "event_materiality": materiality,
        "event_materiality_observed": float(
            amount_to_market_cap is not None or direct_materiality is not None
        ),
        "event_expectation_gap": clip(event.get("expectation_gap"), -3.0, 3.0),
        "event_expectation_observed": float(bool(event.get("expectation_observed"))),
        "event_consensus_org_count_log": math.log1p(
            max(0, int(event.get("analyst_consensus_org_count") or 0))
        ),
        "event_consensus_dispersion": clip(
            event.get("analyst_consensus_dispersion"), 0.0, 3.0
        ),
        "event_consensus_dispersion_observed": float(
            safe_float(event.get("analyst_consensus_dispersion")) is not None
        ),
        "event_consensus_freshness": (
            math.exp(-max(0.0, float(freshness)) / 90.0)
            if (freshness := safe_float(
                event.get("analyst_consensus_freshness_days")
            )) is not None else 0.0
        ),
        "event_consensus_freshness_observed": float(
            safe_float(event.get("analyst_consensus_freshness_days")) is not None
        ),
        "event_company_size_log": company_size_log,
        "event_company_size_observed": float(market_cap_cny is not None),
        "event_pre_event_runup": clip(pre_event_runup, -3.0, 3.0),
        "event_priced_in_proxy": math.tanh(max(0.0, pricing_pressure) / 3.0),
        "event_analyst_revision": clip(analyst_revision, -2.0, 2.0),
        "event_analyst_revision_observed": float(analyst_revision is not None),
        "event_target_price_upside": clip(target_upside, -2.0, 3.0),
        "event_target_price_observed": float(target_upside is not None),
        "event_count_log": math.log1p(max(0, int(event.get("event_count") or 0))),
        "event_numeric_evidence_log": math.log1p(
            max(0, int(event.get("evidence_numeric_count") or 0))
        ),
    }
