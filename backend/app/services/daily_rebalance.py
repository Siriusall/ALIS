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
import threading
from typing import Any

from app.data_platform.db import _conn as data_conn
from app.db.store import get_account_balance, get_account_baseline, get_positions
from app.services.market_context import live_market_context
from app.services.operations_overview import build_live_portfolio_risk
from app.services.position_valuation import build_position_market_series_cache
from app.services.quant_run_engine import list_consolidated_trade_dates
from app.services.model_registry import get_model_status
from app.services.thesis_store import list_theses
from app.services.decision_store import list_pool_members
from app.core.a_share_fees import is_exchange_etf
from app.services.defensive_assets import defensive_asset_bar, defensive_asset_bars
from app.services.point_in_time import signal_available_time


def _current_price(symbol: str, trade_date: str | None = None) -> float | None:
    """Get latest close price from snapshot."""
    code = symbol.strip().upper()
    compact_date = str(trade_date or "").replace("-", "")[:8]
    cutoff = signal_available_time(compact_date) if compact_date else ""
    if is_exchange_etf(code):
        ts_code = code if code.endswith((".SH", ".SZ")) else f"{code}.SH"
        bar = defensive_asset_bar(ts_code, trade_date or datetime.now().strftime("%Y%m%d"))
        if not bar and trade_date:
            with data_conn() as c:
                row = c.execute(
                    """SELECT trade_date FROM dc_tushare_raw_records
                       WHERE api_name='fund_daily' AND ts_code=? AND trade_date<=?
                       ORDER BY trade_date DESC LIMIT 1""",
                    (ts_code, str(trade_date).replace("-", "")),
                ).fetchone()
            if row:
                bar = defensive_asset_bar(ts_code, str(dict(row).get("trade_date") or ""))
        if bar:
            return float(bar.get("close") or 0) or None
    if not code.endswith(".SH") and not code.endswith(".SZ"):
        for suffix in (".SH", ".SZ"):
            with data_conn() as c:
                sql = "SELECT close FROM dc_stock_daily_snapshot WHERE ts_code=?"
                params: tuple[Any, ...] = (f"{code}{suffix}",)
                if compact_date:
                    sql += " AND trade_date<=? AND available_time<=?"
                    params += (compact_date, cutoff)
                row = c.execute(sql + " ORDER BY trade_date DESC LIMIT 1", params).fetchone()
                if row:
                    return float(dict(row).get("close") or 0) or None
        return None
    with data_conn() as c:
        sql = "SELECT close FROM dc_stock_daily_snapshot WHERE ts_code=?"
        params = (code,)
        if compact_date:
            sql += " AND trade_date<=? AND available_time<=?"
            params += (compact_date, cutoff)
        row = c.execute(sql + " ORDER BY trade_date DESC LIMIT 1", params).fetchone()
        return float(dict(row).get("close") or 0) if row else None


def _stock_name(ts_code: str) -> str:
    with data_conn() as c:
        row = c.execute("SELECT name FROM dc_stock_master WHERE ts_code=?", (ts_code,)).fetchone()
        return str(dict(row).get("name") or "") if row else ""


_REPORT_CACHE_TTL_SECONDS = 30.0
_report_cache_lock = threading.Lock()
_report_cache: tuple[float, dict[str, Any]] | None = None


def build_daily_rebalance_summary() -> dict[str, Any]:
    """Return the bounded report contract used by evidence-page first paint."""
    positions = get_positions()
    balance = get_account_balance()
    baseline = get_account_baseline()
    trade_dates = list_consolidated_trade_dates()
    trade_date = trade_dates[-1] if trade_dates else ""
    total_equity = float(balance) + sum(
        float(item.get("quantity") or 0) * float(item.get("price") or 0)
        for item in positions
    )
    return {
        "ok": True,
        "status": "available",
        "data_available": True,
        "compact": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "sections": [],
        "summary": {
            "total_equity": round(total_equity, 2),
            "cash_balance": round(float(balance), 2),
            "position_count": len(positions),
            "total_return_pct": round((total_equity - float(baseline)) / float(baseline) * 100, 2) if float(baseline) > 0 else 0.0,
            "detail_policy": "完整日报仅在需要时读取；首屏不执行组合风险和候选池历史聚合。",
        },
    }


def _batch_quotes_and_names(symbols: list[str], trade_date: str) -> tuple[dict[str, float], dict[str, str]]:
    """Resolve the small read-only report universe with bounded SQL calls."""
    normalized = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    if not normalized:
        return {}, {}
    compact_date = str(trade_date or "").replace("-", "")[:8]
    cutoff = signal_available_time(compact_date) if compact_date else "9999-12-31T23:59:59"
    prices: dict[str, float] = {}
    names: dict[str, str] = {}
    etf_codes: set[str] = set()
    stock_codes: set[str] = set()
    for symbol in normalized:
        if is_exchange_etf(symbol):
            etf_codes.add(symbol if symbol.endswith((".SH", ".SZ")) else f"{symbol}.SH")
        elif symbol.endswith((".SH", ".SZ")):
            stock_codes.add(symbol)
        else:
            stock_codes.update({f"{symbol}.SH", f"{symbol}.SZ"})
    if etf_codes:
        for code, bar in defensive_asset_bars(etf_codes, compact_date or datetime.now().strftime("%Y%m%d")).items():
            close = float(bar.get("close") or 0)
            if close > 0:
                prices[code] = close
    if stock_codes:
        sorted_codes = tuple(sorted(stock_codes))
        value_placeholders = ",".join("(?)" for _ in sorted_codes)
        params: tuple[Any, ...] = sorted_codes
        date_clause = ""
        if compact_date:
            date_clause = " AND snapshot.trade_date<=? AND snapshot.available_time<=?"
            params += (compact_date, cutoff)
        with data_conn() as c:
            rows = c.execute(
                f"""SELECT requested.ts_code,latest.close
                    FROM (VALUES {value_placeholders}) AS requested(ts_code)
                    LEFT JOIN LATERAL (
                        SELECT snapshot.close
                        FROM dc_stock_daily_snapshot snapshot
                        WHERE snapshot.ts_code=requested.ts_code{date_clause}
                        ORDER BY snapshot.trade_date DESC,snapshot.available_time DESC
                        LIMIT 1
                    ) latest ON TRUE""",
                params,
            ).fetchall()
            for row in rows:
                item = dict(row)
                close = float(item.get("close") or 0)
                if close > 0:
                    prices[str(item.get("ts_code") or "").upper()] = close
            name_placeholders = ",".join("?" for _ in sorted_codes)
            name_rows = c.execute(
                f"SELECT ts_code,name FROM dc_stock_master WHERE ts_code IN ({name_placeholders})",
                sorted_codes,
            ).fetchall()
            names.update({
                str(dict(row).get("ts_code") or "").upper(): str(dict(row).get("name") or "")
                for row in name_rows
            })
    price_by_symbol = {
        symbol: prices.get(symbol) or prices.get(f"{symbol}.SH") or prices.get(f"{symbol}.SZ")
        for symbol in normalized
        if prices.get(symbol) or prices.get(f"{symbol}.SH") or prices.get(f"{symbol}.SZ")
    }
    name_by_symbol = {
        symbol: names.get(symbol) or names.get(f"{symbol}.SH") or names.get(f"{symbol}.SZ") or ""
        for symbol in normalized
    }
    return price_by_symbol, name_by_symbol


def _build_daily_rebalance_report() -> dict[str, Any]:
    """Generate P3-7 daily rebalancing report with specific trade recommendations."""
    now = datetime.now().isoformat(timespec="seconds")
    market = live_market_context(persist=True)
    positions_raw = get_positions()
    report_trade_date = str(market.get("trade_date") or "").replace("-", "")[:8]
    if len(report_trade_date) != 8 or not report_trade_date.isdigit():
        trade_dates = list_consolidated_trade_dates()
        report_trade_date = trade_dates[-1] if trade_dates else ""
    valuation_cache = build_position_market_series_cache(
        positions_raw,
        as_of=report_trade_date,
        lookback=120,
    )
    portfolio_risk = build_live_portfolio_risk(
        valuation_cache=valuation_cache,
        positions=positions_raw,
        as_of=report_trade_date,
    )
    report_trade_date = str(portfolio_risk.get("as_of") or report_trade_date or "").replace("-", "")
    model_status = get_model_status()
    balance = get_account_balance()
    baseline = get_account_baseline()

    # Build the bounded report universe first so position, target and candidate
    # rows share one price/name lookup instead of an N+1 query loop.
    pool_members = [
        {**member, "pool": pool_type}
        for pool_type in ("candidate", "watch", "core")
        for member in list_pool_members(pool_type, limit=20)
    ]
    report_symbols = [str(p.get("symbol") or "") for p in positions_raw]
    report_symbols += [str(symbol) for symbol in (portfolio_risk.get("target_weights") or {}).keys()]
    report_symbols += [str(member.get("ts_code") or "") for member in pool_members]
    prices, names = _batch_quotes_and_names(report_symbols, report_trade_date)

    # Enrich positions with current prices
    positions = []
    for p in positions_raw:
        sym = str(p.get("symbol", ""))
        price = prices.get(sym)
        qty = float(p.get("quantity", 0) or 0)
        avg_price = float(p.get("price", 0) or 0)
        current_value = price * qty if price else None
        cost_basis = avg_price * qty
        pnl = (current_value - cost_basis) if current_value and cost_basis else None
        pnl_pct = ((price - avg_price) / avg_price * 100) if price and avg_price else None
        positions.append({
            "symbol": sym,
            "name": names.get(sym) or p.get("name", ""),
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
        "market_profile": (market.get("market_profile") or {}).get("label", "待识别"),
        "market_profile_dimensions": (market.get("market_profile") or {}).get("dimensions", []),
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
            "name": current_pos["name"] if current_pos else names.get(sym, ""),
            "action": action,
            "target_weight_pct": round(target_pct * 100, 1),
            "current_value": round(current_value, 2) if current_value else 0,
            "target_value": round(target_value, 2),
            "difference": round(diff, 2),
            "current_price": current_pos["current_price"] if current_pos else prices.get(sym),
            "position_state": position_state.get("recommended_state", "normal") if position_state else "normal",
            "state_reason": position_state.get("reason", "") if position_state else "",
        })

    # Candidates from pool (suggestions for new buys)
    candidates = []
    for m in pool_members:
        code = str(m.get("ts_code") or "")
        candidates.append({
            "ts_code": code,
            "name": names.get(code) or m.get("name", ""),
            "pool": m.get("pool", ""),
            "current_price": prices.get(code),
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
        for t in list_theses(limit=100, include_hypotheses=False):
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


def build_daily_rebalance_report() -> dict[str, Any]:
    """Return a short-lived read-only report snapshot for concurrent page reads."""
    global _report_cache
    now = datetime.now().timestamp()
    with _report_cache_lock:
        if _report_cache and now - _report_cache[0] < _REPORT_CACHE_TTL_SECONDS:
            return _report_cache[1]
    result = _build_daily_rebalance_report()
    with _report_cache_lock:
        _report_cache = (datetime.now().timestamp(), result)
    return result
