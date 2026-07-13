from app.services.decision_store import POOL_TYPES, POSITION_TRANSITIONS


def test_three_stock_pools_are_fixed() -> None: assert POOL_TYPES=={"watch","core","holding"}


def test_position_state_cannot_jump_from_watch_to_conviction() -> None:
    assert "conviction" not in POSITION_TRANSITIONS["watch"]
    assert "waiting_valuation" in POSITION_TRANSITIONS["watch"]


def test_warning_can_only_recover_reduce_or_exit() -> None: assert POSITION_TRANSITIONS["warning"]=={"holding","reduce","exit"}
