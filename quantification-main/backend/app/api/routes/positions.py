from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.db.store import (
    add_position as add_position_store,
    delete_position,
    get_default_strategy_id,
    get_positions as get_positions_store,
    get_strategy,
    list_executions,
    list_operations,
    sell_position as sell_position_store,
    update_position,
)
from app.api.state import ensure_manual_trade_allowed
from app.integrations.market_data_client import fetch_symbol_names, fetch_symbol_prices_with_time
from app.schemas.execution_list import ExecutionListItem, ExecutionOrderItem
from app.schemas.position import OperationResponse, PositionsListResponse, PositionResponse
from app.core.trade_calendar import get_last_trade_date
from app.services.cache_consolidation import read_consolidated_cache
from app.services.tushare_snapshot import get_daily_snapshot
from app.services.strategy_config import parse_strategy_content
from app.core.a_share_fees import is_exchange_etf
from app.services.defensive_assets import defensive_asset_bar

router = APIRouter()


def _normalize_symbol_key(symbol: str) -> str:
    """统一 symbol 键，用于与 snapshot(6位代码) 对齐。"""
    s = str(symbol or "").strip()
    if "." in s:
        s = s.split(".", 1)[0]
    lower = s.lower()
    for prefix in ("sh", "sz", "bj"):
        if lower.startswith(prefix):
            s = s[len(prefix) :]
            break
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def _strategy_next_for_position(bought_at: Optional[str], hold_days: Optional[int], rebalance_freq: str) -> Optional[str]:
    """根据持仓买入日和策略 hold_days/rebalance 生成「策略下一步」文案。"""
    if bought_at and hold_days and hold_days > 0:
        try:
            buy = date.fromisoformat(bought_at[:10])
            sell_by = buy + timedelta(days=hold_days)
            today = date.today()
            if sell_by <= today:
                return "已到持仓期满，可卖出"
            n = (sell_by - today).days
            return f"{n} 日后自动卖出（预计 {sell_by.isoformat()}）"
        except (ValueError, TypeError):
            pass
    freq_text = {"daily": "每日", "weekly": "每周", "monthly": "每月"}.get(rebalance_freq, "每日")
    return f"下次调仓：{freq_text}"


class PositionAdd(BaseModel):
    symbol: str = Field(..., min_length=1)
    quantity: int = Field(..., ge=1)
    price: float = Field(..., ge=0)


class PositionUpdate(BaseModel):
    quantity: Optional[int] = Field(None, ge=0)
    price: Optional[float] = Field(None, ge=0)


class PositionSell(BaseModel):
    quantity: Optional[int] = Field(None, ge=1, description="卖出数量，不传则全部")
    price: Optional[float] = Field(None, ge=0, description="卖出单价，不传则用当前价")


@router.get("/stock_names")
def get_stock_names(symbols: str = Query("", description="逗号分隔的股票代码")) -> dict[str, str]:
    """批量查询股票名称，返回 { 代码: 名称 }，用于列表展示「名称 代码」。"""
    if not symbols or not symbols.strip():
        return {}
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    return fetch_symbol_names(sym_list)


@router.get("/positions", response_model=PositionsListResponse)
def get_positions() -> PositionsListResponse:
    """当前持仓（含用户自定义），可在此调整。补全名称与当前价、策略下一步、数据源时间。"""
    rows = get_positions_store()
    symbols = [r["symbol"] for r in rows]
    missing_name = [r["symbol"] for r in rows if not (r.get("name") or "").strip()]
    if missing_name:
        try:
            names = fetch_symbol_names(missing_name)
            for r in rows:
                if not (r.get("name") or "").strip() and names.get(r["symbol"]):
                    r["name"] = names[r["symbol"]]
        except Exception:
            pass
    data_source_time: Optional[str] = None
    prices_by_symbol: dict[str, float] = {}

    # 1) 优先使用 all_in_one（统一缓存入口）
    try:
        trade_date = get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
        payload = read_consolidated_cache(date=trade_date, symbols=symbols)
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data:
            day = data.get(trade_date, {})
            if isinstance(day, dict):
                for s in symbols:
                    row = day.get(_normalize_symbol_key(s))
                    if isinstance(row, dict):
                        p = float(row.get("price", row.get("close", 0)) or 0)
                        if p > 0:
                            prices_by_symbol[s] = p
                data_source_time = (
                    f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T15:00:00"
                )
    except Exception:
        pass

    # 2) 回退使用 TuShare snapshot（与折线图/日线口径一致）
    try:
        snap = get_daily_snapshot(cache_only=True) if len(prices_by_symbol) < len(symbols) else {}
        if snap:
            snap_prices_by_norm: dict[str, float] = {}
            snap_trade_date: Optional[str] = None
            for row in snap.values():
                norm = _normalize_symbol_key(getattr(row, "symbol", ""))
                close = float(getattr(row, "close", 0) or 0)
                if norm:
                    snap_prices_by_norm[norm] = close
                td = str(getattr(row, "trade_date", "") or "")
                if not snap_trade_date and len(td) == 8 and td.isdigit():
                    snap_trade_date = td
            for r in rows:
                norm = _normalize_symbol_key(r["symbol"])
                if norm in snap_prices_by_norm:
                    prices_by_symbol[r["symbol"]] = snap_prices_by_norm[norm]
            if snap_trade_date:
                data_source_time = (
                    f"{snap_trade_date[:4]}-{snap_trade_date[4:6]}-{snap_trade_date[6:8]}T15:00:00"
                )
    except Exception:
        pass

    # 3) snapshot 不全时，缺失标的再回退到 spot
    missing_symbols = [s for s in symbols if s not in prices_by_symbol]
    for s in list(missing_symbols):
        if not is_exchange_etf(s):
            continue
        ts_code = s if str(s).upper().endswith((".SH", ".SZ")) else f"{s}.SH"
        trade_date = (data_source_time or "")[:10].replace("-", "") or get_last_trade_date(for_data_availability=True).strftime("%Y%m%d")
        bar = defensive_asset_bar(ts_code, trade_date)
        if not bar:
            continue
        p = float(bar.get("close") or 0)
        if p > 0:
            prices_by_symbol[s] = p
            missing_symbols.remove(s)
    if missing_symbols:
        try:
            spot_prices, spot_time = fetch_symbol_prices_with_time(missing_symbols)
            for s in missing_symbols:
                p = float(spot_prices.get(s) or 0)
                if p > 0:
                    prices_by_symbol[s] = p
            if data_source_time is None:
                data_source_time = spot_time
        except Exception:
            pass

    for r in rows:
        sym = r["symbol"]
        cost = float(r["price"]) * int(r["quantity"])
        cur_p = float(prices_by_symbol.get(sym) or 0.0)
        cur_v = cur_p * int(r["quantity"])
        r["current_price"] = round(cur_p, 2) if cur_p else None
        r["current_value"] = round(cur_v, 2) if cur_p else None
        r["profit_loss"] = round(cur_v - cost, 2) if cur_p else None
        r["profit_loss_pct"] = round((cur_v - cost) / cost * 100, 2) if cost and cur_p else None
    hold_days: Optional[int] = None
    rebalance_freq = "daily"
    try:
        sid = get_default_strategy_id()
        if sid:
            strategy = get_strategy(sid)
            if strategy:
                config = parse_strategy_content(strategy.get("content"), strategy.get("type", "formula"))
                hold_days = config.hold_days
                rebalance_freq = (config.rebalance or {}).get("freq", "daily")
    except Exception:
        pass
    for r in rows:
        r["strategy_next"] = _strategy_next_for_position(
            r.get("bought_at"), hold_days, rebalance_freq
        )
    return PositionsListResponse(
        positions=[PositionResponse(**r) for r in rows],
        data_source_time=data_source_time,
    )


@router.post("/positions", response_model=PositionResponse)
def add_position(body: PositionAdd) -> PositionResponse:
    """添加或累加持仓（同代码累加数量、加权成本）。"""
    try:
        ensure_manual_trade_allowed("开仓")
    except RuntimeError as e:
        raise HTTPException(status_code=403, detail=str(e))
    p = add_position_store(body.symbol, body.quantity, body.price)
    if not p:
        raise HTTPException(status_code=400, detail="无效代码或数量")
    return PositionResponse(**p)


@router.put("/positions/{position_id}", response_model=PositionResponse)
def update_position_api(position_id: int, body: PositionUpdate) -> PositionResponse:
    """调整持仓数量或成本价；数量改为 0 即删除。"""
    try:
        ensure_manual_trade_allowed("改仓")
    except RuntimeError as e:
        raise HTTPException(status_code=403, detail=str(e))
    p = update_position(position_id, quantity=body.quantity, price=body.price)
    if p is None:
        raise HTTPException(status_code=404, detail="持仓不存在或已删除")
    return PositionResponse(**p)


@router.delete("/positions/{position_id}")
def delete_position_api(position_id: int) -> Response:
    """删除一条持仓。"""
    try:
        ensure_manual_trade_allowed("删仓")
    except RuntimeError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not delete_position(position_id):
        raise HTTPException(status_code=404, detail="持仓不存在")
    return Response(status_code=204)


@router.post("/positions/{position_id}/sell")
def sell_position_api(position_id: int, body: PositionSell):
    """手动卖出持仓（全部或部分）。未传 quantity 则全部卖出，未传 price 则使用当前价。全部卖出返回 204，部分卖出返回 200 及更新后持仓。"""
    rows = get_positions_store()
    pos = next((r for r in rows if r["id"] == position_id), None)
    if not pos:
        raise HTTPException(status_code=404, detail="持仓不存在")
    price = body.price
    if price is None:
        prices, _ = fetch_symbol_prices_with_time([pos["symbol"]])
        price = prices.get(pos["symbol"]) or 0.0
    try:
        result = sell_position_store(position_id, quantity=body.quantity, price=price)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        return Response(status_code=204)
    row = get_positions_store()
    updated = next((r for r in row if r["id"] == position_id), None)
    if not updated:
        return Response(status_code=204)
    for k in ("current_price", "current_value", "profit_loss", "profit_loss_pct", "strategy_next"):
        updated.setdefault(k, None)
    return PositionResponse(**updated)


@router.get("/operations", response_model=list[OperationResponse])
def get_operations(
    execution_id: Optional[int] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
) -> list[OperationResponse]:
    """Operation records (trades), optionally filtered by execution_id."""
    rows = list_operations(execution_id=execution_id, limit=limit)
    return [OperationResponse(**r) for r in rows]


@router.get("/executions", response_model=list[ExecutionListItem])
def get_executions(limit: int = Query(100, ge=1, le=200)) -> list[ExecutionListItem]:
    """List recent executions (for dropdown / equity curve selection)."""
    rows = list_executions(limit=limit)
    return [
        ExecutionListItem(
            id=r["id"],
            strategy_id=r["strategy_id"],
            executed_at=r["executed_at"],
            budget=r["budget"],
            top_n=r["top_n"],
            orders=[ExecutionOrderItem(**o) for o in r.get("orders", [])],
        )
        for r in rows
    ]
