from fastapi import HTTPException
import pytest

from app.api.routes import account, execute, positions


@pytest.mark.parametrize(
    ("call", "args"),
    [
        (positions.add_position, (positions.PositionAdd(symbol="000001", quantity=100, price=10),)),
        (positions.update_position_api, (1, positions.PositionUpdate(quantity=200))),
        (positions.delete_position_api, (1,)),
        (positions.sell_position_api, (1, positions.PositionSell(quantity=100, price=10))),
    ],
)
def test_manual_position_mutations_are_disabled(call, args) -> None:
    with pytest.raises(HTTPException) as exc:
        call(*args)
    assert exc.value.status_code == 410


def test_withdraw_is_retired_before_balance_validation() -> None:
    with pytest.raises(HTTPException) as exc:
        account.withdraw(account.WithdrawRequest(amount=101.0))
    assert exc.value.status_code == 410


def test_direct_balance_reset_is_disabled() -> None:
    with pytest.raises(HTTPException) as exc:
        account.set_balance(account.SetBalanceRequest(balance=10_000.0))
    assert exc.value.status_code == 410


@pytest.mark.parametrize("call", [account.top_up, account.withdraw])
def test_direct_cash_mutations_are_disabled(call) -> None:
    body = account.TopUpRequest(amount=100.0) if call is account.top_up else account.WithdrawRequest(amount=100.0)
    with pytest.raises(HTTPException) as exc:
        call(body)
    assert exc.value.status_code == 410


def test_manual_open_execution_is_disabled() -> None:
    request = execute.ExecuteRequest(
        candidates=[execute._CandidateItem(symbol="000001", price=10.0)],
    )
    with pytest.raises(HTTPException) as exc:
        execute.execute_open(request)
    assert exc.value.status_code == 410
