from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.store import get_account_balance, get_positions, set_account_balance
from app.services.position_valuation import build_position_market_series_cache
from app.services.quant_run_engine import list_consolidated_trade_dates

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
    dates = list_consolidated_trade_dates()
    valuation = build_position_market_series_cache(positions, as_of=dates[-1] if dates else "")
    return sum(
        float(p.get("quantity", 0)) * float((valuation.get(str(p.get("symbol") or "")) or {}).get("price") or p.get("price", 0))
        for p in positions
    )


@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    """获取模拟账户：余额、持仓总金额、资产（余额+持仓总金额）。"""
    balance = get_account_balance()
    position_value = round(_position_total_value(), 2)
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
