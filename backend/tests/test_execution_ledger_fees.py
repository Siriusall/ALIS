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
    connection_count = 0

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
        nonlocal connection_count
        connection_count += 1
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    result = store.sell_position(7, quantity=100, price=12.0, trade_date="2026-02-02", record_operation=False)
    assert result is None
    assert connection_count == 1
    assert any("DELETE FROM positions" in sql for sql in statements)
    assert any("UPDATE account SET balance=ROUND(balance+?" in sql for sql in statements)
    assert not any("INSERT INTO operations" in sql for sql in statements)


def test_sell_rejects_missing_or_zero_price_before_mutating_position(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []

    class _Conn:
        def execute(self, sql, params=()):
            statements.append(" ".join(sql.split()))
            if "FROM positions WHERE id" in sql:
                return _Cursor([{"id": 7, "symbol": "000001.SZ", "quantity": 100, "price": 10.0,
                                 "name": "平安银行", "bought_at": "2026-01-02"}])
            return _Cursor([])

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    with pytest.raises(ValueError, match="禁止以 0 元成交"):
        store.sell_position(7, quantity=100, price=0, trade_date="2026-02-02")
    assert not any("DELETE FROM positions" in sql for sql in statements)
    assert not any("UPDATE positions" in sql for sql in statements)


def test_quant_etf_sell_persists_projection_and_exact_fee_ledger_together(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[tuple[str, tuple]] = []

    class _Conn:
        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            statements.append((normalized, params))
            if "FROM positions WHERE id" in sql:
                return _Cursor([{"id": 7, "symbol": "511880", "quantity": 100, "price": 100.0,
                                 "name": "银华日利ETF", "bought_at": "2026-01-02"}])
            if "SELECT balance FROM account" in sql:
                return _Cursor([{"balance": 1000.0}])
            return _Cursor([])

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    fee = sell_cost_breakdown(100.0, 100, "511880")
    result = store.sell_position(
        7,
        quantity=100,
        price=float(fee["executed_price"]),
        trade_date="2026-02-02",
        record_operation=True,
        operation_at="2026-02-02T09:30:00",
        operation_remark="量化任务卖出（关联执行）",
        fee_breakdown=fee,
    )

    assert result is None
    insert = next(params for sql, params in statements if "INSERT INTO operations" in sql)
    assert insert[5] == "2026-02-02T09:30:00"
    assert insert[8:11] == (fee["commission"], 0.0, 0.0)
    assert insert[12] == ETF_FEE_SCHEDULE_VERSION


def test_buy_execution_uses_one_transaction_for_cash_operation_and_position(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []
    connection_count = 0

    class _Conn:
        _is_postgres = True
        _is_mysql = True

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            statements.append(normalized)
            if "SELECT balance FROM account" in sql:
                return _Cursor([{"balance": 100_000.0}])
            if "SELECT id FROM positions" in sql:
                return _Cursor([{"id": 9}])
            return _Cursor([])

        def last_insert_id(self):
            return 42

    @contextmanager
    def fake_conn():
        nonlocal connection_count
        connection_count += 1
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    execution_id = store.create_and_apply_buy_execution(
        1,
        "2026-02-02T09:30:00",
        1005.0,
        1,
        [{
            "symbol": "000001", "side": "BUY", "price": 10.0, "quantity": 100,
            "commission": 5.0, "transfer_fee": 0.0, "stamp_tax": 0.0,
            "fee_schedule_version": "cn_a_share_fee_v1",
        }],
    )

    assert execution_id == 42
    assert connection_count == 1
    assert any("INSERT INTO executions" in sql for sql in statements)
    assert any("INSERT INTO operations" in sql for sql in statements)
    assert any("INSERT INTO positions" in sql for sql in statements)
    assert any("UPDATE account SET balance" in sql for sql in statements)


@pytest.mark.parametrize(
    ("order", "error"),
    [
        ({"symbol": "000001", "side": "BUY", "price": 10, "quantity": 100.5}, "invalid_operation"),
        ({"symbol": "000001", "side": "BUY", "price": float("nan"), "quantity": 100}, "invalid_operation"),
        ({"symbol": "000001", "side": "BUY", "price": 10, "quantity": 100, "commission": -1}, "invalid_operation_fees"),
    ],
)
def test_compatibility_operation_writers_reject_invalid_orders_before_writing(
    monkeypatch: pytest.MonkeyPatch, order: dict, error: str,
) -> None:
    statements: list[str] = []

    class _Conn:
        def execute(self, sql, _params=()):
            statements.append(sql)
            return _Cursor([])

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    with pytest.raises(ValueError, match=error):
        store.create_execution(1, "2026-02-02T09:30:00", 0, 1, [order])
    assert statements == []


def test_compatibility_execution_writer_rejects_invalid_budget_before_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []

    class _Conn:
        def execute(self, sql, _params=()):
            statements.append(sql)
            return _Cursor([])

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    with pytest.raises(ValueError, match="invalid_execution_budget"):
        store.create_execution(1, "2026-02-02T09:30:00", float("inf"), 1, [])
    assert statements == []


def test_sell_operation_link_requires_every_row_to_bind_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Update:
        rowcount = 1

    class _Conn:
        def execute(self, _sql, _params=()):
            return _Update()

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    linked = store.link_recorded_operations_to_execution(7, [{
        "symbol": "000001", "side": "SELL", "price": 10, "quantity": 100,
        "at": "2026-02-02T09:30:00",
    }])
    assert linked == 1


def test_sell_operation_link_rejects_missing_or_partial_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Update:
        rowcount = 0

    class _Conn:
        def execute(self, _sql, _params=()):
            return _Update()

    @contextmanager
    def fake_conn():
        yield _Conn()

    monkeypatch.setattr(store, "_conn", fake_conn)
    with pytest.raises(RuntimeError, match="绑定失败"):
        store.link_recorded_operations_to_execution(7, [{
            "symbol": "000001", "side": "SELL", "price": 10, "quantity": 100,
            "at": "2026-02-02T09:30:00",
        }])
    with pytest.raises(ValueError, match="invalid_execution_id"):
        store.link_recorded_operations_to_execution(0, [])


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
