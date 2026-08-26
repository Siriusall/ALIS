"""
分析师一致预期融合引擎

从 report_rc (券商研报) 数据中提取分析师评级、目标价，
计算一致预期并融合到预期变化引擎。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.data_platform.db import _read_conn
from app.data_platform.services.observation_clock import load_raw_record_versions
from app.services.event_impact_features import (
    ANALYST_CONSENSUS_CONTRACT,
    ANALYST_MULTI_METRIC_CONSENSUS_CONTRACT,
    build_analyst_consensus_index,
    payload_dict,
    point_in_time_analyst_consensus,
    point_in_time_analyst_consensus_bundle,
)


def _yyyymmdd(value: str | None) -> str:
    return str(value or "")[:10].replace("-", "")


def _shift_yyyymmdd(value: str, days: int) -> str:
    return (datetime.strptime(value, "%Y%m%d").date() + timedelta(days=days)).strftime("%Y%m%d")


def build_analyst_consensus(ts_code: str, *, as_of_time: str | None = None) -> dict[str, Any]:
    """Build point-in-time independent-broker consensus from ``report_rc``."""
    cutoff = _yyyymmdd(as_of_time)
    cutoff_time = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:]}T23:59:59"
        if cutoff else ""
    )
    cutoff_clause = " AND trade_date<=? AND available_time<=?" if cutoff else ""
    params: tuple[Any, ...] = (cutoff, cutoff_time) if cutoff else ()

    with _read_conn() as c:
        rows = None
        if cutoff:
            versioned = load_raw_record_versions(
                c, "report_rc", cutoff=cutoff_time, ts_codes=[ts_code],
                trade_date_end=cutoff,
            )
            if versioned is not None:
                rows = [
                    {**dict(row), "available_time": row.get("observed_at")}
                    for row in versioned
                ]
        if rows is None and bool(getattr(c, "_is_postgres", False)):
            return {"ok": False, "reason": "observation_clock_unavailable", "ts_code": ts_code}
        if rows is None:
            rows = [dict(row) for row in c.execute(
                f"""SELECT ts_code,payload_json,trade_date,available_time,id
                    FROM dc_tushare_raw_records
                    WHERE api_name='report_rc' AND ts_code=?
                    {cutoff_clause}
                    ORDER BY trade_date DESC,id DESC LIMIT 4000""",
                (ts_code,) + params
            ).fetchall()]

    if not rows:
        return {"ok": False, "reason": "no_report_data", "ts_code": ts_code}

    report_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        payload = payload_dict(row.get("payload_json"))
        report_date = str(payload.get("report_date") or row.get("trade_date") or "")[:8]
        organization = str(payload.get("org_name") or "").strip()
        title = str(payload.get("report_title") or "")
        if not report_date or not organization:
            continue
        prices = [
            float(payload[key])
            for key in ("min_price", "max_price")
            if payload.get(key) is not None
        ]
        report_identity[(organization, report_date, title)] = {
            "trade_date": row.get("trade_date", ""),
            "rating": str(payload.get("rating", "") or ""),
            "target_price": sum(prices) / len(prices) if prices else None,
            "min_price": payload.get("min_price"),
            "max_price": payload.get("max_price"),
            "report_title": title[:60],
            "org_name": organization,
        }
    reports = sorted(
        report_identity.values(),
        key=lambda item: (str(item.get("trade_date") or ""), str(item.get("org_name") or "")),
        reverse=True,
    )

    # Rating mapping
    rating_scores = {"买入": 5, "增持": 4, "中性": 3, "减持": 2, "卖出": 1}

    anchor_date = cutoff or max((str(r.get("trade_date") or "") for r in reports), default="")
    stale_start = _shift_yyyymmdd(anchor_date, -179) if anchor_date else ""

    def latest_by_org(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for item in sorted(items, key=lambda value: str(value.get("trade_date") or "")):
            org = str(item.get("org_name") or "")
            if org:
                latest[org] = item
        return list(latest.values())

    current_reports = latest_by_org([
        report for report in reports
        if stale_start and stale_start <= str(report.get("trade_date") or "") <= anchor_date
    ])
    valid_ratings = [r for r in current_reports if r["rating"] in rating_scores]
    valid_targets = [r for r in current_reports if r.get("target_price") is not None]

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

    # Recent trend uses independent brokers inside two adjacent 4-week windows.
    recent_start = _shift_yyyymmdd(anchor_date, -27) if anchor_date else ""
    prior_start = _shift_yyyymmdd(anchor_date, -55) if anchor_date else ""
    recent = latest_by_org([
        r for r in reports
        if r["rating"] in rating_scores
        and recent_start and recent_start <= r["trade_date"] <= anchor_date
    ])
    prior = latest_by_org([
        r for r in reports
        if r["rating"] in rating_scores
        and prior_start and prior_start <= r["trade_date"] < recent_start
    ])
    recent_avg = sum(rating_scores[r["rating"]] for r in recent) / len(recent) if recent else None
    prior_avg = sum(rating_scores[r["rating"]] for r in prior) / len(prior) if prior else None
    revision = None
    if recent_avg is not None and prior_avg is not None:
        revision = round(recent_avg - prior_avg, 2)

    consensus_index = build_analyst_consensus_index(rows)
    earnings_consensus = []
    financial_consensus = []
    for symbol, period in sorted(consensus_index, key=lambda key: key[1], reverse=True):
        if symbol != ts_code:
            continue
        item = point_in_time_analyst_consensus(
            consensus_index,
            symbol=ts_code,
            period=period,
            as_of_date=anchor_date,
        )
        if int(item.get("organization_count") or 0) > 0:
            earnings_consensus.append(item)
        bundle = point_in_time_analyst_consensus_bundle(
            consensus_index,
            symbol=ts_code,
            period=period,
            as_of_date=anchor_date,
        )
        if any(
            int(metric.get("organization_count") or 0) > 0
            for metric in (bundle.get("metrics") or {}).values()
        ):
            financial_consensus.append(bundle)
        if len(earnings_consensus) >= 8:
            break

    confidence = min(0.90, 0.25 + 0.08 * len(valid_ratings))

    return {
        "ok": True,
        "ts_code": ts_code,
        "consensus_rating": consensus_rating,
        "consensus_score": consensus_score,
        "target_price_avg": target_price_avg,
        "target_price_min": target_price_min,
        "target_price_max": target_price_max,
        "report_count": len(current_reports),
        "analyst_count": len(current_reports),
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
        "earnings_consensus": earnings_consensus,
        "earnings_consensus_contract": ANALYST_CONSENSUS_CONTRACT,
        "financial_consensus": financial_consensus,
        "financial_consensus_contract": ANALYST_MULTI_METRIC_CONSENSUS_CONTRACT,
        "target_price_field_contract": "min_price_max_price_only_tp_is_total_profit_v2",
        "model_version": "analyst_consensus_v3",
    }


def batch_analyst_consensus(*, limit: int = 100, as_of_time: str | None = None) -> dict[str, Any]:
    """Batch build analyst consensus for all stocks with report data."""
    cutoff = str(as_of_time or "")[:10].replace("-", "")
    requested_limit = max(0, int(limit))
    if requested_limit == 0:
        return {
            "ok": True,
            "total_stocks": 0,
            "with_consensus": 0,
            "with_target_price": 0,
            "with_multi_metric_consensus": 0,
            "model_version": "analyst_consensus_v3",
            "samples": [],
        }
    cutoff_time = (
        f"{cutoff[:4]}-{cutoff[4:6]}-{cutoff[6:]}T23:59:59"
        if len(cutoff) == 8 else ""
    )
    cutoff_clause = ""
    cutoff_params: list[Any] = []
    if cutoff_time:
        # The master table is a small, indexed universe. A semi-join keeps
        # discovery bounded without DISTINCT/sorting the append-only archive.
        cutoff_clause = " AND r.trade_date<=? AND r.available_time<=?"
        cutoff_params.extend((cutoff, cutoff_time))
    with _read_conn() as c:
        rows = [dict(row) for row in c.execute(
            f"""SELECT m.ts_code
                 FROM dc_stock_master m
                WHERE COALESCE(m.ts_code,'')<>''
                  AND EXISTS (
                      SELECT 1
                        FROM dc_tushare_raw_records r
                       WHERE r.api_name='report_rc'
                         AND r.ts_code=m.ts_code
                         AND COALESCE(r.available_time,'')<>''
                         {cutoff_clause}
                  )
                ORDER BY m.ts_code
                LIMIT ?""",
            tuple(cutoff_params + [requested_limit]),
        ).fetchall()]

    codes = [str(r.get("ts_code", "")) for r in rows]
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
        "with_multi_metric_consensus": sum(
            1 for result in results
            if any(
                int(item.get("observed_metric_count") or 0) > 0
                for item in result.get("financial_consensus") or []
            )
        ),
        "model_version": "analyst_consensus_v3",
        "samples": results[:5],
    }
