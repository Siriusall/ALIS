from contextlib import contextmanager

import pytest

from app.core.a_share_fees import ETF_FEE_SCHEDULE_VERSION, apply_slippage_buy, sell_cost_breakdown
from app.services import decision_store
from app.db import store


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def test_sell_cost_breakdown_uses_executed_price_and_all_fees() -> None:
    fee = sell_cost_breakdown(12.0, 60)
    assert fee["executed_price"] == 11.99
    assert fee["slippage_bps"] < 0
    assert fee["commission"] == 5.0
    assert fee["transfer_fee"] > 0
    assert fee["stamp_tax"] > 0
    assert fee["net_amount"] < fee["gross_amount"]


def test_exchange_etf_uses_five_bp_slippage_and_no_transaction_taxes() -> None:
    assert apply_slippage_buy(1.0, "511880.SH") == 1.001
    fee = sell_cost_breakdown(1.0, 10_000, "511880.SH")
    assert fee["executed_price"] == 1.0
    assert fee["stamp_tax"] == 0
    assert fee["transfer_fee"] == 0
    assert fee["fee_schedule_version"] == ETF_FEE_SCHEDULE_VERSION


def test_fifo_review_subtracts_proportional_entry_and_exit_fees(monkeypatch: pytest.MonkeyPatch) -> None:
    operations = [
        {"id": 1, "execution_id": 10, "symbol": "000001.SZ", "side": "BUY", "price": 10.0,
         "quantity": 100, "at": "2026-01-02T09:30:00", "commission": 5.0, "transfer_fee": 0.01,
         "stamp_tax": 0.0, "fee_schedule_version": "cn_a_share_fee_v1"},
        {"id": 2, "execution_id": 11, "symbol": "000001.SZ", "side": "SELL", "price": 12.0,
         "quantity": 60, "at": "2026-02-02T09:30:00", "commission": 5.0, "transfer_fee": 0.01,
         "stamp_tax": 0.36, "fee_schedule_version": "cn_a_share_fee_v1"},
    ]

    class _Conn:
        def execute(self, sql, params):
            if "WHERE id=" in sql:
                return _Cursor([x for x in operations if x["id"] == int(params[0])])
            return _Cursor(operations)

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(decision_store, "_conn", fake_conn)
    outcome = decision_store._matched_execution_outcome({"operation_id": 1})
    assert outcome is not None
    assert outcome["status"] == "partially_realized"
    assert outcome["matched_quantity"] == 60
    assert outcome["remaining_quantity"] == 40
    assert outcome["realized_pnl"] == pytest.approx(111.624)
    assert outcome["realized_return"] == pytest.approx(0.185113)


def test_quant_sell_can_update_cash_without_writing_manual_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []

    class _Conn:
        def execute(self, sql, params=()):
            statements.append(" ".join(sql.split()))
            if "FROM positions WHERE id" in sql:
                return _Cursor([{"id": 7, "symbol": "000001.SZ", "quantity": 100, "price": 10.0,
                                 "name": "平安银行", "bought_at": "2026-01-02"}])
            if "SELECT balance FROM account" in sql:
                return _Cursor([{"balance": 1000.0}])
            return _Cursor([])

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    result = store.sell_position(7, quantity=100, price=12.0, trade_date="2026-02-02", record_operation=False)
    assert result is None
    assert any("DELETE FROM positions" in sql for sql in statements)
    assert any("UPDATE account SET balance" in sql for sql in statements)
    assert not any("INSERT INTO operations" in sql for sql in statements)


def test_list_operations_decodes_target_weight_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Conn:
        def execute(self, *_args):
            return _Cursor([{
                "id": 1, "execution_id": 2, "symbol": "000001", "side": "BUY", "price": 10.0,
                "quantity": 100, "at": "2026-01-02T09:30:00", "remark": "", "name": "平安银行",
                "commission": 5.0, "transfer_fee": 0.01, "stamp_tax": 0.0, "slippage_bps": 10,
                "fee_schedule_version": "cn_a_share_fee_v1", "operation_uid": "u1",
                "target_weight_contract_json": "{\"function_bucket\":\"leader\",\"target_weight\":0.08}",
            }])

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    rows = store.list_operations(limit=1)

    assert rows[0]["target_weight_contract"]["function_bucket"] == "leader"
    assert rows[0]["target_weight_contract"]["target_weight"] == .08


def test_fifo_attribution_reconciles_market_industry_selection_and_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_store, "_market_and_industry_returns", lambda *_args: (.10, .15, "银行"))
    outcome = {
        "matched_quantity": 60, "entry_operation_id": 1, "entry_price": 10.0,
        "entry_at": "2026-01-02T09:30:00", "entry_fees": 3.0, "exit_fees": 5.4,
        "realized_pnl": 111.6,
        "matched_exit_operations": [{"operation_id": 2, "quantity": 60, "price": 12.0, "at": "2026-02-02T09:30:00"}],
    }

    result = decision_store._execution_attribution(
        {"decision_type": "BUY", "ts_code": "000001.SZ"}, outcome,
    )

    assert result["market_attribution"] == pytest.approx(60.0)
    assert result["industry_allocation_attribution"] == pytest.approx(30.0)
    assert result["selection_attribution"] == pytest.approx(30.0)
    assert result["execution_attribution"] == pytest.approx(-8.4)
    assert result["attributed_net_pnl"] == pytest.approx(111.6)
    assert result["reconciliation_difference"] == pytest.approx(0.0)
