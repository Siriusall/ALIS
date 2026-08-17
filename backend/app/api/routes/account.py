from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.data_platform.services.market_calendar import TradeCalendarNotReady
from app.db.store import get_account_balance, get_positions, set_account_balance
from app.services.position_valuation import (
    PositionMarketDataNotReadyError,
    build_position_market_series_cache,
    current_live_valuation_trade_date,
    missing_exact_position_valuations,
)

router = APIRouter()


class AccountResponse(BaseModel):
    balance: float
    position_value: float = 0.0
    assets: float = 0.0


class TopUpRequest(BaseModel):
    amount: float = Field(..., gt=0, description="充值金额（元）")


class WithdrawRequest(BaseModel):
    amount: float = Field(..., gt=0, description="提现金额（元）")


class SetBalanceRequest(BaseModel):
    balance: float = Field(..., ge=0, description="设置余额（元）")


def _position_total_value() -> float:
    """持仓当前市值（使用未复权点时收盘价，不使用成本价）。"""
    positions = get_positions()
    as_of = current_live_valuation_trade_date()
    valuation = build_position_market_series_cache(positions, as_of=as_of)
    missing = missing_exact_position_valuations(positions, valuation, as_of=as_of)
    if missing:
        raise PositionMarketDataNotReadyError(missing, as_of, missing)
    return sum(
        float(p.get("quantity", 0)) * float((valuation.get(str(p.get("symbol") or "")) or {}).get("price") or 0)
        for p in positions
    )


@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    """获取模拟账户：余额、持仓总金额、资产（余额+持仓总金额）。"""
    balance = get_account_balance()
    try:
        position_value = round(_position_total_value(), 2)
    except (PositionMarketDataNotReadyError, TradeCalendarNotReady) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": getattr(exc, "code", "position_market_data_not_ready"),
                "trade_date": getattr(exc, "as_of", ""),
                "missing_symbols": getattr(exc, "symbols", []),
                "message": str(exc),
            },
        ) from exc
    assets = round(balance + position_value, 2)
    return AccountResponse(balance=balance, position_value=position_value, assets=assets)


@router.post("/account/topup", response_model=AccountResponse)
def top_up(body: TopUpRequest) -> AccountResponse:
    """Retired: cash changes must be represented by an audited ledger adjustment."""
    raise HTTPException(status_code=410, detail="直接充值已关闭，请使用受审计的账户调整流程")


@router.post("/account/withdraw", response_model=AccountResponse)
def withdraw(body: WithdrawRequest) -> AccountResponse:
    """Retired: cash changes must be represented by an audited ledger adjustment."""
    raise HTTPException(status_code=410, detail="直接提现已关闭，请使用受审计的账户调整流程")


@router.put("/account", response_model=AccountResponse)
def set_balance(body: SetBalanceRequest) -> AccountResponse:
    raise HTTPException(status_code=410, detail="直接设置余额已关闭，请使用充值或受审计的账户调整流程")
