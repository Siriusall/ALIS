from __future__ import annotations

import sys
import types
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_trading_constraints() -> None:
    from app.core.a_share_fees import (
        apply_slippage_buy,
        apply_slippage_sell,
        commission,
        cost_buy,
        stamp_tax,
        transfer_fee,
    )
    from app.core.trading_constraints import check_open_trade_constraints, normalize_lot_quantity

    _assert(normalize_lot_quantity(99) == 0, "A-share lot size must round 99 shares down to 0")
    _assert(normalize_lot_quantity(250) == 200, "A-share lot size must round 250 shares down to 200")

    ok = check_open_trade_constraints(
        side="BUY",
        symbol="600000",
        name="浦发银行",
        open_price=10.0,
        close_price=10.1,
        change_pct=1.0,
        volume=-1,
    )
    _assert(ok.ok and ok.code == "tradable", "unknown volume should not be treated as suspension")

    suspended = check_open_trade_constraints(
        side="BUY",
        symbol="600001",
        name="测试",
        open_price=10.0,
        close_price=10.0,
        change_pct=0.0,
        volume=0,
    )
    _assert(not suspended.ok and suspended.code == "suspended_or_no_open_price", "zero volume must block trading")

    st_buy = check_open_trade_constraints(
        side="BUY",
        symbol="000001",
        name="*ST测试",
        open_price=4.0,
        close_price=4.0,
        change_pct=0,
        volume=1000,
    )
    _assert(not st_buy.ok and st_buy.code == "st_buy_blocked", "ST new buy must be blocked by default")

    limit_up_buy = check_open_trade_constraints(
        side="BUY",
        symbol="000002",
        name="测试",
        open_price=11.0,
        close_price=11.0,
        change_pct=10.0,
        volume=1000,
    )
    _assert(not limit_up_buy.ok and limit_up_buy.code == "limit_up_buy_blocked", "limit-up buy must be blocked")

    limit_down_sell = check_open_trade_constraints(
        side="SELL",
        symbol="000003",
        name="测试",
        open_price=9.0,
        close_price=9.0,
        change_pct=-10.0,
        volume=1000,
    )
    _assert(not limit_down_sell.ok and limit_down_sell.code == "limit_down_sell_blocked", "limit-down sell must be blocked")

    _assert(apply_slippage_buy(10.0) > 10.0, "buy slippage must increase execution price")
    _assert(apply_slippage_sell(10.0) < 10.0, "sell slippage must reduce execution price")
    _assert(commission(1000) == 5.0, "commission minimum must be applied")
    _assert(stamp_tax(10000) == 5.0, "sell stamp tax must be 0.05%")
    _assert(transfer_fee(100000) == 1.0, "transfer fee must be 0.001%")
    amount, comm, transfer = cost_buy(10.0, 100)
    _assert((amount, comm, transfer) == (1000.0, 5.0, 0.01), "buy cost tuple must match fee policy")


def _install_quant_risk_stubs() -> dict[str, object]:
    records = [
        {"execute_date": "2026-01-03", "signal_date": "2026-01-02", "execution_id": 3},
        {"execute_date": "2026-01-02", "signal_date": "2026-01-01", "execution_id": 2},
    ]
    operations = {
        3: [{"side": "BUY", "symbol": "600000", "quantity": 100, "price": 10.0}],
        2: [{"side": "BUY", "symbol": "600001", "quantity": 100, "price": 20.0}],
    }

    store_mod = types.ModuleType("app.db.store")
    store_mod.list_quant_run_task_records = lambda task_id, limit=50: records[:limit]
    store_mod.list_operations = lambda execution_id=None, limit=5000: operations.get(int(execution_id or 0), [])
    sys.modules["app.db.store"] = store_mod

    ds_mod = types.ModuleType("app.services.backtest_data_source")

    class FakeBacktestDataSource:
        def get_price_on_date(self, symbol: str, execute_date: str, field: str = "close") -> float:
            prices = {
                ("600000", "2026-01-03"): 9.5,
                ("600001", "2026-01-02"): 19.0,
            }
            return prices.get((symbol, execute_date), 0.0)

    ds_mod.BacktestDataSource = FakeBacktestDataSource
    sys.modules["app.services.backtest_data_source"] = ds_mod

    health_mod = types.ModuleType("app.services.data_health")
    health_mod.build_data_health_report = lambda limit_days=5: {
        "ok": False,
        "trade_dates": {"count": 0},
        "snapshots": {"tushare_snapshot": {"count": 0}},
        "daily_basic": {"backtest_cache": {"count": 0}},
        "issues": [{"level": "error", "code": "all_in_one_missing", "message": "missing"}],
    }
    sys.modules["app.services.data_health"] = health_mod
    return {"health_mod": health_mod}


def check_risk_decisions() -> None:
    stubs = _install_quant_risk_stubs()
    import app.services.quant_risk as quant_risk

    data_decision = quant_risk.evaluate_data_health_gate(limit_days=5)
    _assert(data_decision.should_pause, "data health failure must pause")
    _assert(data_decision.code == "paused_data_health_failed", "data health pause code must be stable")
    _assert(data_decision.recovery_condition, "data health pause must include recovery condition")
    _assert(data_decision.exit_condition, "data health pause must include exit condition")
    _assert("all_in_one_missing" in data_decision.evidence.get("issue_codes", []), "data pause evidence must include issue codes")

    health_mod = stubs["health_mod"]
    health_mod.build_data_health_report = lambda limit_days=5: {"ok": True, "issues": []}
    quant_risk.build_data_health_report = health_mod.build_data_health_report
    ok_decision = quant_risk.evaluate_data_health_gate(limit_days=5)
    _assert(not ok_decision.should_pause and ok_decision.code == "ok", "healthy data must pass")

    loss_decision = quant_risk.evaluate_consecutive_loss_gate(task_id=1, max_consecutive_losses=2)
    _assert(loss_decision.should_pause, "consecutive losing execution nodes must pause")
    _assert(loss_decision.code == "paused_consecutive_losses", "consecutive loss pause code must be stable")
    _assert(len(loss_decision.evidence.get("loss_streak", [])) == 2, "loss streak evidence must include the failing nodes")
    _assert(loss_decision.recovery_condition and loss_decision.exit_condition, "loss pause must include recovery and exit conditions")

    disabled = quant_risk.evaluate_consecutive_loss_gate(task_id=1, max_consecutive_losses=0)
    _assert(not disabled.should_pause, "zero max_consecutive_losses must disable the gate")


def check_startup_init_split() -> None:
    main_path = ROOT / "app" / "main.py"
    init_routes_path = ROOT / "app" / "api" / "routes" / "init_routes.py"
    init_service_path = ROOT / "app" / "services" / "init_service.py"

    main_text = main_path.read_text(encoding="utf-8")
    tree = ast.parse(main_text)
    startup_fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "startup":
            startup_fn = node
            break
    _assert(startup_fn is not None, "main.py must expose a startup function")

    called = {
        n.func.id
        for n in ast.walk(startup_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    allowed = {"init_db", "resume_quant_tasks_on_startup"}
    heavy_calls = {
        "run_data_initialization",
        "roll_forward_daily_cache",
        "prefetch_daily_bars_for_init",
        "build_consolidated_cache_bundle",
    }
    _assert("init_db" in called, "startup must initialize database schema")
    _assert("resume_quant_tasks_on_startup" in called, "startup must reconcile interrupted quant tasks")
    _assert(called.isdisjoint(heavy_calls), "startup must not call heavy data initialization")
    _assert(allowed.issubset(called), "startup must stay limited to database and task reconciliation")

    init_routes_text = init_routes_path.read_text(encoding="utf-8")
    _assert("run_data_initialization" in init_routes_text, "/api/init route must call run_data_initialization")
    _assert("@router.post(\"/init\")" in init_routes_text, "POST /api/init route must exist")
    _assert("def run_data_initialization" in init_service_path.read_text(encoding="utf-8"), "init_service must own heavy data initialization")


def main() -> int:
    checks = [
        ("trading constraints and fees", check_trading_constraints),
        ("risk pause decisions", check_risk_decisions),
        ("startup/data initialization split", check_startup_init_split),
    ]
    for name, fn in checks:
        fn()
        print(f"[OK] {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
