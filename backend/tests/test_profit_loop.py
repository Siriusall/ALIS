from contextlib import contextmanager

from app.services import profit_loop


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_missing_live_metrics_do_not_degrade_production_model(monkeypatch):
    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "model_family_status", lambda: {"items": [{
        "model_key": "m1", "name": "M1", "status": "production",
        "active_version": {"version": "v1", "metrics": {}},
    }]})
    monkeypatch.setattr(profit_loop, "model_feedback_gate", lambda *_: {
        "weighted_accuracy": None, "samples": 0, "should_degrade": False,
    })

    @contextmanager
    def fake_conn():
        class Conn:
            def execute(self, *_args, **_kwargs):
                return _Rows([])
        yield Conn()

    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)
    result = profit_loop.build_model_live_monitor()
    assert result["degraded_count"] == 0
    assert result["items"][0]["status"] == "watch"
    assert result["items"][0]["weight_multiplier"] == 1.0


def test_model_contribution_refuses_to_invent_portfolio_pnl(monkeypatch):
    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "model_family_status", lambda: {"items": [{
        "model_key": "m1", "name": "M1", "active_version": {"version": "v1"},
    }]})

    @contextmanager
    def fake_conn():
        class Conn:
            def execute(self, *_args, **_kwargs):
                return _Rows([{
                    "model_version": "v1", "horizon_sessions": 20, "samples": 8,
                    "avg_directional_return": .03, "avg_benchmark_return": .01,
                    "avg_excess_return": .02, "hit_rate": .625,
                }])
        yield Conn()

    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)
    result = profit_loop.build_model_return_contribution()
    item = result["items"][0]
    assert item["preferred_20d_evidence"]["avg_excess_return_pct"] == 2.0
    assert item["portfolio_return_contribution_pct"] is None
    assert item["contribution_status"] == "insufficient_samples"
    assert result["methodology"]["no_fabrication"] is True


def test_planned_execution_time_uses_operational_scheduler_clock(monkeypatch):
    monkeypatch.setenv("QUANT_AUTO_SIMULATION_CLOSE_HOUR", "16")
    monkeypatch.setenv("QUANT_AUTO_SIMULATION_CLOSE_MINUTE", "5")
    monkeypatch.setattr(profit_loop, "remembered_first_rebalance_clock", lambda: None)

    assert profit_loop._planned_execute_at("20260722") == "2026-07-22T16:05:00+08:00"


def test_planned_execution_time_remembers_first_actual_rebalance(monkeypatch):
    monkeypatch.setattr(profit_loop, "remembered_first_rebalance_clock", lambda: profit_loop.dtime(16, 37))

    assert profit_loop._planned_execute_at("20260723") == "2026-07-23T16:37:00+08:00"


def test_formal_plan_can_only_be_claimed_once(monkeypatch):
    state = {"status": "planned", "token": ""}
    plan = {
        "id": 7, "trade_date": "20260724", "planned_execute_date": "20260727", "version": 1,
        "input_hash": "abc", "proposal_json": '{"proposal":[],"risk_budget":{},"decision_version_manifest":{}}',
    }

    class Result(_Rows):
        rowcount = 1

    @contextmanager
    def fake_conn():
        class Conn:
            _is_postgres = False
            _is_mysql = False

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("SELECT * FROM daily_rebalance_plans"):
                    return Result([{**plan, "status": state["status"]}] if state["status"] == "planned" else [])
                if "SET status='executing'" in sql and state["status"] == "planned":
                    state.update(status="executing", token=str(params[0]))
                    return Result([])
                return Result([])
        yield Conn()

    monkeypatch.setattr(profit_loop, "_ensure_tables", lambda: None)
    monkeypatch.setattr(profit_loop, "business_conn", fake_conn)

    first = profit_loop.claim_daily_rebalance_execution_input("20260724", "20260727", claim_token="task-1")
    second = profit_loop.claim_daily_rebalance_execution_input("20260724", "20260727", claim_token="task-2")

    assert first and first["execution_claim_token"] == "task-1"
    assert second is None
    assert state == {"status": "executing", "token": "task-1"}
