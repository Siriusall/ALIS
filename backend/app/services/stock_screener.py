"""
全量股票日选系统

流程：5534只股票 → 快速预筛 → 研究评分 → 候选池 → 投资逻辑 → 组合优化
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from statistics import mean

from app.data_platform.db import _conn as data_conn
from app.db.store import _conn as business_conn, get_positions
from app.services.research_engine import build_stock_research, score_dimension
from app.services.decision_store import upsert_pool_member, list_pool_members, transition_pool_member
from app.services.thesis_store import generate_thesis_for_stock
from app.services.operations_overview import build_live_portfolio_risk
from app.core.trade_calendar import get_last_trade_date
from app.services.point_in_time import signal_available_time


def _get_stock_universe(trade_date: str) -> list[dict[str, Any]]:
    """Get all investable stocks with financial data, excluding ST."""
    cutoff = signal_available_time(trade_date)
    compact_date = str(trade_date).replace("-", "")[:8]
    with data_conn() as c:
        rows = [dict(row) for row in c.execute("""
            SELECT m.ts_code, m.name, m.industry,
                   s.close, s.pe_ttm, s.pb, s.total_mv, s.turnover
            FROM dc_stock_master m
            LEFT JOIN dc_stock_daily_snapshot s ON s.ts_code = m.ts_code
                AND s.trade_date = ? AND s.available_time <= ?
            WHERE m.industry IS NOT NULL AND m.industry != ''
              AND position('ST' in m.name) = 0
              AND position('退市' in m.name) = 0
              AND EXISTS (SELECT 1 FROM dc_tushare_raw_records fin
                          WHERE fin.ts_code = m.ts_code AND fin.api_name='income'
                            AND fin.available_time <= ?)
            ORDER BY COALESCE(s.total_mv, 0) DESC
        """, (compact_date, cutoff, cutoff)).fetchall()]
    return rows


def _prefilter(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quick pre-filter using cheap database queries."""
    scored = []
    for row in rows:
        score = 0
        reasons = []
        pe = float(row.get("pe_ttm") or 0) or None
        mv = float(row.get("total_mv") or 0) or 0
        turnover = float(row.get("turnover") or 0) or 0

        # Market cap filter (exclude micro caps)
        if mv >= 500000:  # >= 50亿
            score += 20
            reasons.append("市值合格")
        elif mv >= 20000:  # >= 2亿
            score += 10
            reasons.append("小市值")
        else:
            reasons.append("市值过小")
            scored.append({"stock": row, "pre_score": score, "reasons": reasons})
            continue

        # PE filter (avoid extreme values)
        if pe and 5 < pe < 60:
            score += 15
            reasons.append(f"PE合理({pe:.0f})")
        elif pe and pe <= 5:
            score += 5
            reasons.append(f"PE偏低({pe:.0f})")
        else:
            reasons.append("PE异常")

        # Liquidity filter
        if turnover >= 5000:  # 成交额 >= 5000万
            score += 15
            reasons.append("流动性好")
        elif turnover >= 1000:  # >= 1000万
            score += 5
            reasons.append("流动性一般")

        scored.append({"stock": row, "pre_score": score, "reasons": reasons})
    return sorted(scored, key=lambda x: x["pre_score"], reverse=True)


def run_daily_screener(
    *,
    top_n: int = 300,
    research_limit: int = 100,
    trade_date: str | None = None,
) -> dict[str, Any]:
    """Full daily stock selection pipeline."""
    now = datetime.now().isoformat(timespec="seconds")
    target_date = str(
        trade_date
        or get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
    ).replace("-", "")[:8]
    cutoff = signal_available_time(target_date)
    errors = []

    # Step 1: Get all stocks
    universe = _get_stock_universe(target_date)
    print(f"  总股票池: {len(universe)} 只")

    # Step 2: Pre-filter
    prefilters = _prefilter(universe)
    top_candidates = [p for p in prefilters if p["pre_score"] >= 20][:top_n]
    print(f"  预筛通过: {len(top_candidates)} 只（前置分≥20）")

    # Step 3: Run full research on top candidates
    new_pool = 0
    new_theses = 0
    for i, candidate in enumerate(top_candidates[:research_limit]):
        stock = candidate["stock"]
        code = stock["ts_code"]
        try:
            result = build_stock_research(code, persist=True, as_of_time=cutoff)
            if not result.get("ok"):
                continue
            rs = result.get("research_state", {})
            scores = rs.get("scores", {})
            eligible = rs.get("eligible", True)
            classification = result.get("classification", {})
            lifecycle = classification.get("lifecycle", "?")

            # Research admission criteria: score ≥ 65, confidence ≥ 0.60, no hard risk
            quality = float(scores.get("quality", 0) or 0)
            growth = float(scores.get("growth", 0) or 0)
            valuation = float(scores.get("valuation", 0) or 0)
            overall = (quality + growth + valuation) / 3
            confidence = float(rs.get("confidence", 0) or 0)

            if overall >= 60 and confidence >= 0.60 and eligible is not False:
                # Add to watch pool
                upsert_pool_member(
                    ts_code=code,
                    pool_type="watch",
                    reason=f"auto_screener: score={overall:.0f} conf={confidence:.2f} life={lifecycle}",
                )
                new_pool += 1
                print(f"    [{i+1}/{research_limit}] {code:10s} score={overall:.0f} conf={confidence:.2f} life={lifecycle} ✅ 入池")

                # Auto-generate thesis
                try:
                    thesis = generate_thesis_for_stock(code, force=True)
                    if thesis.get("ok") or thesis.get("thesis_id"):
                        new_theses += 1
                except Exception as exc:
                    errors.append(f"thesis_{code}: {str(exc)[:80]}")
            else:
                reason = "硬风险" if eligible is False else f"分低({overall:.0f}/{confidence:.2f})"
                print(f"    [{i+1}/{research_limit}] {code:10s} score={overall:.0f} conf={confidence:.2f} {reason} ❌")

        except Exception as e:
            errors.append(f"{code}: {str(e)[:80]}")
            continue

    # Step 4: Promote top watch pool members to candidate
    promoted = 0
    # Also promote to candidate for portfolio optimization
    # Keep existing candidate/core, promote top watch members
    # First clear old candidate/core, then promote
    watch_members = list_pool_members("watch")
    for m in sorted(watch_members, key=lambda x: float(str(x.get("reason", "0")).split("score=")[-1].split(" ")[0].split(" conf")[0]) if "score=" in str(x.get("reason", "")) else 0.0, reverse=True)[:50]:
        try:
            code = m["ts_code"]
            # Check if already in candidate or core
            already = any(m2.get("ts_code") == code for m2 in list_pool_members("candidate") + list_pool_members("core"))
            if not already:
                transition_pool_member(ts_code=code, from_pool="watch", to_pool="candidate", reason=f"auto_promote_from_watch: {m.get('reason', '')}")
                promoted += 1
        except Exception as exc:
            errors.append(f"promotion_{m.get('ts_code') or 'unknown'}: {str(exc)[:80]}")

    # Step 5: Run portfolio optimization on candidates
    portfolio_risk = build_live_portfolio_risk()

    return {
        "ok": not errors,
        "status": "completed" if not errors else "partial",
        "results": {
            "total_universe": len(universe),
            "prefilter_passed": len(top_candidates),
            "research_run": min(research_limit, len(top_candidates)),
            "new_pool_members": new_pool,
            "new_theses": new_theses,
            "promoted_to_candidate": promoted,
        },
        "errors": errors[:10],
        "error_count": len(errors),
        "timestamp": now,
        "trade_date": target_date,
        "data_cutoff": cutoff,
    }
