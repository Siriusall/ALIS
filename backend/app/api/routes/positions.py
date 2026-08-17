from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.db.store import (
    add_position as add_position_store,
    count_executions,
    count_operations,
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
from app.integrations.market_data_client import fetch_symbol_names
from app.schemas.execution_list import ExecutionListItem, ExecutionOrderItem
from app.schemas.position import OperationResponse, PositionsListResponse, PositionResponse
from app.services.strategy_config import parse_strategy_content
from app.data_platform.db import _conn as data_conn
from app.data_platform.services.market_calendar import TradeCalendarNotReady, target_trade_date

router = APIRouter()
_BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


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


def _etf_ts_code(symbol: str) -> str:
    """Resolve the exchange for a bare exchange-traded fund code."""
    value = str(symbol or "").strip().upper()
    if value.endswith((".SH", ".SZ")):
        return value
    code = value.split(".", 1)[0].zfill(6)
    return f"{code}.SH" if code.startswith(("50", "51", "52", "56", "58")) else f"{code}.SZ"


def _strategy_next_for_position(bought_at: Optional[str], hold_days: Optional[int], rebalance_freq: str) -> Optional[str]:
    """根据持仓买入日和策略 hold_days/rebalance 生成「策略下一步」文案。"""
    if bought_at and hold_days and hold_days > 0:
        try:
            buy = date.fromisoformat(bought_at[:10])
            sell_by = buy + timedelta(days=hold_days)
            today = datetime.now(_BUSINESS_TZ).date()
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
    trade_date = ""

    # Current valuation must use the exact target session from the canonical
    # exchange calendar and the PostgreSQL data-platform projection only.
    try:
        with data_conn() as c:
            trade_date = target_trade_date(c)
        from app.services.position_console import resolve_position_prices

        prices_by_symbol, source_time = resolve_position_prices(symbols, trade_date)
        data_source_time = source_time or f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T15:00:00+08:00"
    except TradeCalendarNotReady as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:  # noqa: BLE001 - data readiness must be visible to the caller
        raise HTTPException(
            status_code=503,
            detail={"code": "position_market_data_not_ready", "trade_date": trade_date, "message": str(exc)[:300]},
        ) from exc

    missing_symbols = [s for s in symbols if s not in prices_by_symbol]
    if missing_symbols:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "position_market_data_not_ready",
                "trade_date": trade_date,
                "missing_symbols": missing_symbols,
                "message": "持仓标的缺少目标交易日精确收盘数据，未使用前日、成本价或实时行情冒充",
            },
        )

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
    raise HTTPException(status_code=410, detail="手动交易已关闭，持仓只能由模拟执行产生")


@router.put("/positions/{position_id}", response_model=PositionResponse)
def update_position_api(position_id: int, body: PositionUpdate) -> PositionResponse:
    """调整持仓数量或成本价；数量改为 0 即删除。"""
    raise HTTPException(status_code=410, detail="手动交易已关闭，禁止直接修改持仓")


@router.delete("/positions/{position_id}")
def delete_position_api(position_id: int) -> Response:
    """删除一条持仓。"""
    raise HTTPException(status_code=410, detail="手动交易已关闭，禁止直接删除持仓")


@router.post("/positions/{position_id}/sell")
def sell_position_api(position_id: int, body: PositionSell):
    raise HTTPException(status_code=410, detail="手动交易已关闭，卖出只能由自动模拟执行产生")


@router.get("/operations", response_model=list[OperationResponse])
def get_operations(
    execution_id: Optional[int] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
) -> list[OperationResponse]:
    """Operation records (trades), optionally filtered by execution_id."""
    rows = list_operations(execution_id=execution_id, limit=limit)
    return [OperationResponse(**r) for r in rows]


@router.get("/operations/page")
def get_operations_page(
    execution_id: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=100_000),
) -> dict:
    """Read a bounded operation page without exposing an unbounded list read."""
    total = count_operations(execution_id=execution_id)
    rows = list_operations(execution_id=execution_id, limit=limit, offset=offset)
    items = [OperationResponse(**r).model_dump() for r in rows]
    return {
        "ok": True,
        "items": items,
        "page": offset // limit + 1,
        "page_size": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(items) < total,
    }


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


@router.get("/executions/page")
def get_executions_page(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0, le=100_000),
) -> dict:
    """Read a bounded execution page without exposing an unbounded list read."""
    total = count_executions()
    rows = list_executions(limit=limit, offset=offset)
    items = [
        ExecutionListItem(
            id=r["id"],
            strategy_id=r["strategy_id"],
            executed_at=r["executed_at"],
            budget=r["budget"],
            top_n=r["top_n"],
            orders=[ExecutionOrderItem(**o) for o in r.get("orders", [])],
        ).model_dump()
        for r in rows
    ]
    return {
        "ok": True,
        "items": items,
        "page": offset // limit + 1,
        "page_size": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(items) < total,
    }
