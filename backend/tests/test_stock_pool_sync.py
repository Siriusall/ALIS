from app.services import stock_pool_sync


def test_stage_candidates_only_enters_watch_pool_with_score_evidence(monkeypatch):
    saved = []
    monkeypatch.setattr(stock_pool_sync, "upsert_pool_member", lambda **kwargs: saved.append(kwargs) or kwargs)

    result = stock_pool_sync.stage_candidates_in_watch_pool([
        {"symbol": "000001", "confidence": 88.5, "price": 10, "change_pct": 1.2},
        {"symbol": "600000", "confidence": 72, "price": 8, "change_pct": -1},
    ])

    assert result["staged"] == 2
    assert [item["ts_code"] for item in saved] == ["000001.SZ", "600000.SH"]
    assert all(item["pool_type"] == "watch" and "candidate_screen_v1" in item["reason"] for item in saved)


def test_confirmed_execution_enters_and_exits_holding_pool(monkeypatch):
    entered, exited = [], []
    monkeypatch.setattr(stock_pool_sync, "upsert_pool_member", lambda **kwargs: entered.append(kwargs) or kwargs)
    monkeypatch.setattr(stock_pool_sync, "set_pool_member_status", lambda **kwargs: exited.append(kwargs) or kwargs)
    monkeypatch.setattr(stock_pool_sync, "get_position_state", lambda _code: None)
    transitions = []
    monkeypatch.setattr(stock_pool_sync, "transition_position", lambda **kwargs: transitions.append(kwargs) or kwargs)

    result = stock_pool_sync.sync_pool_from_confirmed_execution(
        execution_id=12,
        executed_at="2026-07-13T09:30:00",
        buy_orders=[{"symbol": "000001", "exec_price": 10, "quantity": 100, "score": 81}],
        sell_orders=[{"symbol": "600000", "status": "executed", "message": "risk gate"}],
    )

    assert result["ok"] is True
    assert entered[0]["ts_code"] == "000001.SZ"
    assert entered[0]["pool_type"] == "holding"
    assert "confirmed_execution_v1" in entered[0]["reason"]
    assert exited[0]["ts_code"] == "600000.SH"
    assert exited[0]["status"] == "removed"
    assert [item["to_state"] for item in transitions[:4]] == ["watch", "waiting_valuation", "trial", "holding"]


def test_reconcile_missing_position_states_uses_immutable_operation_evidence(monkeypatch):
    states = {}
    writes = []
    monkeypatch.setattr(stock_pool_sync, "list_operations", lambda **_kwargs: [
        {"id": 1, "execution_id": 9, "symbol": "510880", "side": "BUY", "price": 3, "quantity": 100, "at": "2026-07-17T09:30:00"},
        {"id": 2, "execution_id": 10, "symbol": "510880", "side": "SELL", "price": 3.1, "quantity": 100, "at": "2026-07-18T09:30:00"},
    ])
    monkeypatch.setattr(stock_pool_sync, "get_position_state", lambda code: states.get(code))

    def transition(**kwargs):
        states[kwargs["ts_code"]] = {"state": kwargs["to_state"]}
        writes.append(kwargs)
        return states[kwargs["ts_code"]]

    monkeypatch.setattr(stock_pool_sync, "transition_position", transition)

    result = stock_pool_sync.reconcile_missing_position_states_from_confirmed_operations()

    assert result["operations_replayed"] == 2
    assert result["state_transitions_created"] == 5
    assert states["510880.SH"]["state"] == "exit"
    assert [item["to_state"] for item in writes] == ["watch", "waiting_valuation", "trial", "holding", "exit"]
    assert all(item["rule_version"] == "confirmed_execution_state_reconciliation_v1" for item in writes)


def test_reconcile_partial_sell_marks_reduce_not_exit(monkeypatch):
    states = {}
    writes = []
    monkeypatch.setattr(stock_pool_sync, "list_operations", lambda **_kwargs: [
        {"id": 1, "execution_id": 9, "symbol": "000001", "side": "BUY", "price": 10, "quantity": 200, "at": "2026-07-17T09:30:00"},
        {"id": 2, "execution_id": 10, "symbol": "000001", "side": "SELL", "price": 11, "quantity": 100, "at": "2026-07-18T09:30:00"},
    ])
    monkeypatch.setattr(stock_pool_sync, "get_position_state", lambda code: states.get(code))
    monkeypatch.setattr(
        stock_pool_sync,
        "transition_position",
        lambda **kwargs: writes.append(kwargs) or states.setdefault(kwargs["ts_code"], {}).update(state=kwargs["to_state"]) or states[kwargs["ts_code"]],
    )

    result = stock_pool_sync.reconcile_missing_position_states_from_confirmed_operations()

    assert result["operations_replayed"] == 2
    assert states["000001.SZ"]["state"] == "reduce"
    assert writes[-1]["to_state"] == "reduce"


def test_reconcile_is_noop_for_existing_state_evidence(monkeypatch):
    monkeypatch.setattr(stock_pool_sync, "list_operations", lambda **_kwargs: [
        {"id": 1, "execution_id": 9, "symbol": "000001", "side": "BUY", "price": 10, "quantity": 100, "at": "2026-07-17T09:30:00"},
    ])
    monkeypatch.setattr(stock_pool_sync, "get_position_state", lambda _code: {"state": "holding"})
    monkeypatch.setattr(stock_pool_sync, "transition_position", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not rewrite state")))

    result = stock_pool_sync.reconcile_missing_position_states_from_confirmed_operations()

    assert result["operations_replayed"] == 0
    assert result["state_transitions_created"] == 0
