from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_stubs() -> dict[str, object]:
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
        "issues": [{"level": "error", "code": "all_in_one_missing", "message": "missing"}],
    }
    sys.modules["app.services.data_health"] = health_mod
    return {"health_mod": health_mod}


def main() -> int:
    from app.core.trading_constraints import check_open_trade_constraints
    from app.services import quant_risk
    from app.services.quant_task_runner import _preflight_check_step

    print("=== 风控端到端模拟 ===")

    session = _preflight_check_step(
        signal_date="2026-07-01",
        execute_date="2026-07-02",
        min_account_balance=1000.0,
        enforce_session_gate=True,
    )
    print(f"[{'OK' if not session.should_pause else 'PAUSE'}] 非交易时段: {session.code} | {session.message}")

    funds = _preflight_check_step(
        signal_date="2026-07-01",
        execute_date="2026-07-02",
        min_account_balance=1_000_000.0,
        enforce_session_gate=False,
    )
    print(f"[{'OK' if not funds.should_pause else 'PAUSE'}] 资金/快照预检: {funds.code} | {funds.message}")

    stubs = _install_stubs()
    data_bad = quant_risk.evaluate_data_health_gate(limit_days=5)
    print(f"[{'OK' if not data_bad.should_pause else 'PAUSE'}] 数据异常: {data_bad.code}")
    print(f"      恢复: {data_bad.recovery_condition}")
    print(f"      退出: {data_bad.exit_condition}")

    health_mod = stubs["health_mod"]
    health_mod.build_data_health_report = lambda limit_days=5: {"ok": True, "issues": []}
    quant_risk.build_data_health_report = health_mod.build_data_health_report
    data_ok = quant_risk.evaluate_data_health_gate(limit_days=5)
    print(f"[{'OK' if not data_ok.should_pause else 'PAUSE'}] 数据恢复后: {data_ok.code}")

    loss = quant_risk.evaluate_consecutive_loss_gate(task_id=1, max_consecutive_losses=2)
    print(f"[{'OK' if not loss.should_pause else 'PAUSE'}] 连续亏损: {loss.code}")
    print(f"      证据: {json.dumps(loss.evidence, ensure_ascii=False)}")
    print(f"      恢复: {loss.recovery_condition}")
    print(f"      退出: {loss.exit_condition}")

    st = check_open_trade_constraints(
        side="BUY", symbol="000001", name="*ST测试", open_price=4.0, close_price=4.0, change_pct=0, volume=1000
    )
    print(f"[{'BLOCK' if not st.ok else 'PASS'}] ST买入拦截: {st.code} | {st.message}")

    limit_up = check_open_trade_constraints(
        side="BUY", symbol="000002", name="测试", open_price=11.0, close_price=11.0, change_pct=10.0, volume=1000
    )
    print(f"[{'BLOCK' if not limit_up.ok else 'PASS'}] 涨停买入拦截: {limit_up.code}")

    print("=== 模拟完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
