"""P3-7 每日调仓报告

Plan book P3-7:
一、今日市场环境及相对昨日的变化
二、组合收益、回撤、风险预算和防御资产状态
三、逐笔模拟调仓：标的、方向、目标仓位、计划价格、模拟成交价格
四、调仓理由：新事实、评分变化、逻辑变化、估值、风险和交易成本
五、未调仓持仓：继续持有的理由和下一触发条件
六、被拒绝信号：因数据、风险、流动性或置信度未执行的机会
七、明日需要观察的数据、公告、价格和失效条件
八、策略引擎今日进行了哪些学习、校准或Challenger试验
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.data_platform.db import _conn as data_conn
from app.db.store import get_account_balance, get_account_baseline, get_positions
from app.services.market_context import live_market_context
from app.services.operations_overview import build_live_portfolio_risk
from app.services.model_registry import get_model_status
from app.services.thesis_store import list_theses
from app.services.decision_store import list_pool_members


def _current_price(symbol: str) -> float | None:
    """Get latest close price from snapshot."""
    code = symbol.strip().upper()
    if not code.endswith(".SH") and not code.endswith(".SZ"):
        for suffix in (".SH", ".SZ"):
            with data_conn() as c:
                row = c.execute(
                    "SELECT close FROM dc_stock_daily_snapshot WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                    (f"{code}{suffix}",)
                ).fetchone()
                if row:
                    return float(dict(row).get("close") or 0) or None
        return None
    with data_conn() as c:
        row = c.execute(
            "SELECT close FROM dc_stock_daily_snapshot WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
            (code,)
        ).fetchone()
        return float(dict(row).get("close") or 0) if row else None


def _stock_name(ts_code: str) -> str:
    with data_conn() as c:
        row = c.execute("SELECT name FROM dc_stock_master WHERE ts_code=?", (ts_code,)).fetchone()
        return str(dict(row).get("name") or "") if row else ""


def build_daily_rebalance_report() -> dict[str, Any]:
    """Generate P3-7 daily rebalancing report with specific trade recommendations."""
    now = datetime.now().isoformat(timespec="seconds")
    market = live_market_context()
    portfolio_risk = build_live_portfolio_risk()
    model_status = get_model_status()
    positions_raw = get_positions()
    balance = get_account_balance()
    baseline = get_account_baseline()

    # Enrich positions with current prices
    positions = []
    for p in positions_raw:
        sym = str(p.get("symbol", ""))
        price = _current_price(sym)
        qty = float(p.get("quantity", 0) or 0)
        avg_price = float(p.get("price", 0) or 0)
        current_value = price * qty if price else None
        cost_basis = avg_price * qty
        pnl = (current_value - cost_basis) if current_value and cost_basis else None
        pnl_pct = ((price - avg_price) / avg_price * 100) if price and avg_price else None
        positions.append({
            "symbol": sym,
            "name": _stock_name(sym) or p.get("name", ""),
            "quantity": int(qty),
            "avg_price": round(avg_price, 4),
            "current_price": price,
            "current_value": round(current_value, 2) if current_value else None,
            "cost_basis": round(cost_basis, 2),
            "profit_loss": round(pnl, 2) if pnl else None,
            "profit_loss_pct": round(pnl_pct, 2) if pnl_pct else None,
            "strategy_next": p.get("strategy_next"),
        })

    total_equity = balance + sum(float(p.get("current_value", 0) or 0) for p in positions)
    total_return_pct = round((total_equity - baseline) / baseline * 100, 2) if baseline > 0 else 0.0

    # Section 1: Market environment
    ms = (market.get("market_state") or {})
    sent = (market.get("sentiment") or {})
    section1 = {
        "title": "一、今日市场环境",
        "market_state": ms.get("label", ms.get("state", "未知")),
        "market_confidence": ms.get("confidence"),
        "risk_budget_multiplier": ms.get("risk_budget_multiplier", 1.0),
        "sentiment_state": sent.get("state", "未知"),
        "sentiment_score": round(sent.get("score", 0), 2),
        "trade_date": market.get("trade_date", "未知"),
        "change_from_yesterday": market.get("change_alarm"),
        "limitations": market.get("limitations", []),
    }

    # Section 2: Portfolio performance
    section2 = {
        "title": "二、组合收益与风险",
        "total_equity": round(total_equity, 2),
        "cash_balance": round(balance, 2),
        "position_count": len(positions),
        "total_return_pct": total_return_pct,
        "risk_status": portfolio_risk.get("status"),
        "breaches": [b.get("message", "") for b in (portfolio_risk.get("breaches") or [])],
        "defensive_assets": [p for p in positions if "ETF" in str(p.get("name", "") or "").upper()],
        "target_weights": portfolio_risk.get("target_weights", {}),
    }

    # Section 3: Rebalancing recommendations
    target_weights = portfolio_risk.get("target_weights", {})
    total_value = total_equity
    rebalance_orders = []
    current_symbols = {p["symbol"]: p for p in positions}

    for sym, target_pct in target_weights.items():
        target_value = total_value * target_pct
        current_pos = current_symbols.get(sym)
        current_value = current_pos["current_value"] if current_pos else 0
        diff = (target_value - (current_value or 0))
        position_state = next((r for r in (portfolio_risk.get("position_state_recommendations") or []) if r.get("symbol") == sym), None)

        if abs(diff) < 100:  # < 100元差异忽略
            action = "持有"
        elif diff > 0:
            action = "买入"
        else:
            action = "卖出"

        rebalance_orders.append({
            "symbol": sym,
            "name": current_pos["name"] if current_pos else _stock_name(sym),
            "action": action,
            "target_weight_pct": round(target_pct * 100, 1),
            "current_value": round(current_value, 2) if current_value else 0,
            "target_value": round(target_value, 2),
            "difference": round(diff, 2),
            "current_price": current_pos["current_price"] if current_pos else _current_price(sym),
            "position_state": position_state.get("recommended_state", "normal") if position_state else "normal",
            "state_reason": position_state.get("reason", "") if position_state else "",
        })

    # Candidates from pool (suggestions for new buys)
    candidates = []
    for pool_type in ("candidate", "watch", "core"):
        for m in list_pool_members(pool_type):
            code = m.get("ts_code", "")
            price = _current_price(code)
            candidates.append({
                "ts_code": code,
                "name": _stock_name(code) or m.get("name", ""),
                "pool": pool_type,
                "current_price": price,
                "reason": m.get("reason", ""),
            })

    # Section 4: Rejected signals
    rejected = []
    for b in (portfolio_risk.get("breaches") or []):
        rejected.append({
            "reason": b.get("message", ""),
            "severity": b.get("severity", "warning"),
            "code": b.get("code", ""),
        })

    # Section 5: Theses
    thesis_items = []
    try:
        theses = list_theses()
        for t in theses.get("items", []):
            thesis_items.append({
                "ts_code": t.get("ts_code", ""),
                "thesis_type": t.get("thesis_type"),
                "confidence": t.get("confidence"),
                "status": t.get("status"),
            })
    except Exception:
        thesis_items = []

    # Section 6: Tomorrow's watch
    section6 = {
        "title": "七、明日观察事项",
        "watch_items": [
            "收盘后数据更新（daily + daily_basic + stk_limit）是否完成",
            "持仓股票公告和事件",
            "市场状态变化（BOCPD突变报警）",
            "模型Champion/Challenger运行状态",
        ],
    }

    # Section 7: Model learning
    av = (model_status or {}).get("active_version") or {}
    section7 = {
        "title": "八、策略引擎动态",
        "active_model": str(av.get("version", "无候选版本")),
        "model_versions": len((model_status or {}).get("versions", [])),
        "latest_train": (model_status or {}).get("latest_train_run"),
    }

    # Generate decision text
    decision_parts = []
    actions = [o for o in rebalance_orders if o["action"] in ("买入", "卖出")]
    if actions:
        decision_parts.append("建议调仓：")
        for o in actions:
            decision_parts.append("  " + str(o["action"]) + " " + str(o["symbol"]) + "（" + str(o["name"]) + "）目标仓位 " + str(o["target_weight_pct"]) + "%")
    else:
        decision_parts.append("建议：维持现有持仓，无需调仓")
    if candidates:
        decision_parts.append("候选池推荐（" + str(len(candidates)) + "只可关注）：")
        for c in sorted(candidates, key=lambda x: x.get("pool", "") == "candidate", reverse=True)[:5]:
            decision_parts.append("  " + str(c["ts_code"]) + "（" + str(c["name"]) + "）现价" + str(c.get("current_price","?")))
            sym = c.get("symbol", "?")
            nm = c.get("name", "?")
            pr = c.get("current_price", 0)
            decision_parts.append("  " + str(sym) + "(" + str(nm) + ") 现价" + str(pr))

    return {
        "ok": True,
        "generated_at": now,
        "trade_date": market.get("trade_date", "?"),
        "sections": [
            section1,
            section2,
            {"title": "三、逐笔调仓建议", "orders": rebalance_orders},
            {"title": "四、当前持仓详情", "positions": positions},
            {"title": "五、候选股票池", "candidates": candidates[:20]},
            {"title": "六、被拒绝信号", "rejected": rejected},
            section6,
            section7,
        ],
        "summary": {
            "decision": "\n".join(decision_parts),
            "tone": "caution" if any(b.get("severity") == "error" for b in (portfolio_risk.get("breaches") or [])) else "proceed",
            "total_equity": round(total_equity, 2),
            "total_return_pct": total_return_pct,
            "position_count": len(positions),
            "cash_balance": round(balance, 2),
            "risk_status": portfolio_risk.get("status"),
        },
    }
