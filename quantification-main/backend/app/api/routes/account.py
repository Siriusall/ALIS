from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.state import add_log
from app.db.store import add_account_balance, get_account_balance, get_positions, set_account_balance

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
    """持仓总金额（按成本价：quantity * price）。"""
    positions = get_positions()
    return sum(float(p.get("quantity", 0)) * float(p.get("price", 0)) for p in positions)


@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    """获取模拟账户：余额、持仓总金额、资产（余额+持仓总金额）。"""
    balance = get_account_balance()
    position_value = round(_position_total_value(), 2)
    assets = round(balance + position_value, 2)
    return AccountResponse(balance=balance, position_value=position_value, assets=assets)


@router.post("/account/topup", response_model=AccountResponse)
def top_up(body: TopUpRequest) -> AccountResponse:
    """充值。"""
    new_balance = add_account_balance(body.amount)
    add_log("INFO", f"账户充值: +{body.amount}, 余额={new_balance}")
    pv = round(_position_total_value(), 2)
    return AccountResponse(balance=new_balance, position_value=pv, assets=round(new_balance + pv, 2))


@router.post("/account/withdraw", response_model=AccountResponse)
def withdraw(body: WithdrawRequest) -> AccountResponse:
    """提现。余额不足时只提空余额。"""
    current = get_account_balance()
    deduct = min(body.amount, current)
    if deduct <= 0:
        raise HTTPException(status_code=400, detail="余额不足，无法提现")
    new_balance = add_account_balance(-deduct)
    add_log("INFO", f"账户提现: -{deduct}, 余额={new_balance}")
    pv = round(_position_total_value(), 2)
    return AccountResponse(balance=new_balance, position_value=pv, assets=round(new_balance + pv, 2))


@router.put("/account", response_model=AccountResponse)
def set_balance(body: SetBalanceRequest) -> AccountResponse:
    """自定义设置账户余额（模拟初始资金等）。"""
    new_balance = set_account_balance(body.balance)
    add_log("INFO", f"账户设置余额: {new_balance}")
    pv = round(_position_total_value(), 2)
    return AccountResponse(balance=new_balance, position_value=pv, assets=round(new_balance + pv, 2))
