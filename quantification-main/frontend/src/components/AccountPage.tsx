import { useEffect, useState } from "react";
import { fetchAccount, setBalance, topUp, withdraw } from "../api/account";

function formatMoney(value: number) {
  return Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(value);
}

export default function AccountPage() {
  const [account, setAccount] = useState<{ balance: number; position_value: number; assets: number } | null>(null);
  const [topUpAmount, setTopUpAmount] = useState("");
  const [withdrawAmount, setWithdrawAmount] = useState("");
  const [customAmount, setCustomAmount] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    fetchAccount()
      .then((a) => setAccount({ balance: a.balance, position_value: a.position_value ?? 0, assets: a.assets ?? a.balance + (a.position_value ?? 0) }))
      .catch(() => setError("获取账户失败"));
  };

  useEffect(() => {
    load();
  }, []);

  async function handleTopUp() {
    const num = Number(topUpAmount.replace(/,/g, ""));
    if (!(num > 0)) {
      setError("请输入有效充值金额");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const a = await topUp(num);
      setAccount({ balance: a.balance, position_value: a.position_value ?? 0, assets: a.assets ?? a.balance });
      setTopUpAmount("");
    } catch (e: unknown) {
      setError((e as Error).message || "充值失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleWithdraw() {
    const num = Number(withdrawAmount.replace(/,/g, ""));
    if (!(num > 0)) {
      setError("请输入有效提现金额");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const a = await withdraw(num);
      setAccount({ balance: a.balance, position_value: a.position_value ?? 0, assets: a.assets ?? a.balance });
      setWithdrawAmount("");
    } catch (e: unknown) {
      setError((e as Error).message || "提现失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleSetBalance() {
    const num = Number(customAmount.replace(/,/g, ""));
    if (num < 0) {
      setError("余额不能为负");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const a = await setBalance(num);
      setAccount({ balance: a.balance, position_value: a.position_value ?? 0, assets: a.assets ?? a.balance });
      setCustomAmount("");
    } catch (e: unknown) {
      setError((e as Error).message || "设置失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section id="account" className="dashboard-section" data-section="account">
      <h2 className="dashboard-section-title">账户</h2>
      <p className="dashboard-section-desc text-muted" style={{ margin: "0 0 1rem", fontSize: "13px" }}>
        模拟交易账户，用于检测策略可行性。充值、提现与设置余额仅影响模拟资金；执行买入时按剩余资金下单。
      </p>
      <div className="dashboard-section-inner">
        {error && <div className="alert">{error}</div>}
        <div className="card" style={{ marginBottom: "1rem" }}>
          <h3>资产概览</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <div>
              <span className="text-muted" style={{ fontSize: "13px" }}>余额（元）</span>
              <div className="metric" style={{ fontSize: "20px" }}>
                {account != null ? formatMoney(account.balance) : "—"}
              </div>
            </div>
            <div>
              <span className="text-muted" style={{ fontSize: "13px" }}>持仓总金额（元，按成本价）</span>
              <div className="metric" style={{ fontSize: "20px" }}>
                {account != null ? formatMoney(account.position_value) : "—"}
              </div>
            </div>
            <div>
              <span className="text-muted" style={{ fontSize: "13px" }}>资产（元）= 余额 + 持仓总金额</span>
              <div className="metric text-accent" style={{ fontSize: "24px" }}>
                {account != null ? formatMoney(account.assets) : "—"}
              </div>
            </div>
          </div>
        </div>
        <div className="card" style={{ marginBottom: "1rem" }}>
          <h3>充值</h3>
          <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
            <input
              type="text"
              inputMode="numeric"
              placeholder="金额"
              value={topUpAmount}
              onChange={(e) => setTopUpAmount(e.target.value)}
              style={{ padding: "8px 12px", width: "140px" }}
            />
            <button
              type="button"
              onClick={handleTopUp}
              disabled={loading || !topUpAmount.trim()}
              style={{ padding: "8px 16px", background: "#22c55e", color: "#fff", border: "none", borderRadius: "6px", cursor: "pointer" }}
            >
              充值
            </button>
          </div>
        </div>
        <div className="card" style={{ marginBottom: "1rem" }}>
          <h3>提现</h3>
          <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
            <input
              type="text"
              inputMode="numeric"
              placeholder="金额"
              value={withdrawAmount}
              onChange={(e) => setWithdrawAmount(e.target.value)}
              style={{ padding: "8px 12px", width: "140px" }}
            />
            <button
              type="button"
              onClick={handleWithdraw}
              disabled={loading || !withdrawAmount.trim()}
              style={{ padding: "8px 16px", background: "#f59e0b", color: "#fff", border: "none", borderRadius: "6px", cursor: "pointer" }}
            >
              提现
            </button>
          </div>
        </div>
        <div className="card">
          <h3>设置余额</h3>
          <p className="text-muted" style={{ margin: "0 0 8px", fontSize: "13px" }}>
            直接设置账户余额（自定义初始资金）
          </p>
          <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
            <input
              type="text"
              inputMode="numeric"
              placeholder="金额"
              value={customAmount}
              onChange={(e) => setCustomAmount(e.target.value)}
              style={{ padding: "8px 12px", width: "140px" }}
            />
            <button
              type="button"
              onClick={handleSetBalance}
              disabled={loading || customAmount.trim() === ""}
              style={{ padding: "8px 16px", background: "#2563eb", color: "#ffffff", border: "none", borderRadius: "6px", cursor: "pointer" }}
            >
              设置
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
