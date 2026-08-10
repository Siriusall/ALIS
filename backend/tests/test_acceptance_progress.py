from app.services import acceptance_progress


class _Rows:
    def __init__(self, factor_rows, operation_rows):
        self.factor_rows = factor_rows
        self.operation_rows = operation_rows

    def execute(self, sql, _params=()):
        return _Cursor(self.factor_rows if "factor_definitions" in sql else self.operation_rows)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Context:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *_args):
        return False


def test_acceptance_progress_uses_slowest_factor_and_balanced_cost_gates(monkeypatch):
    factors = [
        {"factor_key": "a", "evaluation_count": 12},
        {"factor_key": "b", "evaluation_count": 20},
    ]
    operations = [
        {"side": "BUY" if index < 11 else "SELL", "price": 10, "quantity": 100,
         "commission": 1, "transfer_fee": 0, "stamp_tax": 0, "slippage_bps": 5}
        for index in range(16)
    ]
    monkeypatch.setattr(acceptance_progress, "_conn", lambda: _Context(_Rows(factors, operations)))
    monkeypatch.setattr(acceptance_progress, "build_autonomy_slo", lambda: {
        "status": "observing", "observed_sessions": 7, "window_sessions": 20,
        "metrics": {}, "gates": {},
    })

    result = acceptance_progress.build_acceptance_progress()

    factor, candidate, activation, autonomy = result["stages"]
    assert factor["current"] == 12
    assert factor["remaining"] == 8
    assert "1/2" in factor["detail"]
    assert candidate["remaining"] == 4
    assert candidate["progress"] == 0.625
    assert activation["remaining"] == 34
    assert autonomy["remaining"] == 13


def test_acceptance_progress_passes_only_when_every_stage_passes(monkeypatch):
    monkeypatch.setattr(acceptance_progress, "_factor_progress", lambda: {"status": "passed"})
    monkeypatch.setattr(acceptance_progress, "_cost_progress", lambda: {"stages": [{"status": "passed"}, {"status": "passed"}]})
    monkeypatch.setattr(acceptance_progress, "_autonomy_progress", lambda: {"status": "passed"})

    result = acceptance_progress.build_acceptance_progress()

    assert result["status"] == "passed"
    assert result["summary"] == {"passed": 4, "total": 4}
