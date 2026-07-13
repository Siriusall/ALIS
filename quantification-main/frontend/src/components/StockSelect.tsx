import { useEffect, useRef, useState } from "react";
import type { Candidate, Strategy } from "../types";
import { fetchAccount } from "../api/account";
import { fetchCandidates, fetchQuantRunStatus } from "../api/market";
import { fetchStrategies } from "../api/strategies";
import { apiErrorMessage } from "../api/client";

type ExchangeFilter = "ALL" | "HS" | "CYB" | "BJ";

function formatNumber(value: number) {
  return Intl.NumberFormat("zh-CN").format(value);
}

/** 同花顺个股页，代码规范为 6 位 */
function thsStockUrl(symbol: string): string {
  const code = symbol.replace(/\.(SZ|SH)$/i, "").slice(0, 6);
  return `https://stockpage.10jqka.com.cn/${code}/`;
}

export default function StockSelect() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [balance, setBalance] = useState<number | null>(null);
  /** 账户已请求过（成功或失败），避免在 balance 未返回时就请求候选导致 budget 未传 */
  const [accountLoaded, setAccountLoaded] = useState(false);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [candidateWarnings, setCandidateWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [quantRunning, setQuantRunning] = useState(false);
  const [exchange, setExchange] = useState<ExchangeFilter>("HS");
  const [error, setError] = useState("");
  const statusPollRunning = useRef(false);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const list = await fetchStrategies();
        if (!active) return;
        setStrategies(list);
        if (list.length > 0 && strategyId === null) {
          setStrategyId(list[0].id);
        }
      } catch {
        if (!active) return;
        setError("策略列表加载失败");
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    fetchAccount()
      .then((a) => {
        setBalance(a.balance);
        setAccountLoaded(true);
      })
      .catch(() => {
        setBalance(null);
        setAccountLoaded(true);
      });
  }, []);

  useEffect(() => {
    let active = true;
    const pull = async () => {
      if (statusPollRunning.current) return;
      statusPollRunning.current = true;
      try {
        const st = await fetchQuantRunStatus();
        if (!active) return;
        setQuantRunning(Boolean(st.running));
      } catch {
        if (!active) return;
        setQuantRunning(false);
      } finally {
        statusPollRunning.current = false;
      }
    };
    pull();
    const t = setInterval(pull, 5000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  useEffect(() => {
    if (strategyId == null || !accountLoaded) return;
    let active = true;
    setLoading(true);
    setError("");
    setCandidateWarnings([]);
    (async () => {
      try {
        const bal = balance != null && balance > 0 ? balance : undefined;
        const res = await fetchCandidates(0, strategyId, bal, exchange);
        if (!active) return;
        setCandidates(res.candidates);
        setCandidateWarnings(res.warnings ?? []);
      } catch (error) {
        if (!active) return;
        setError(apiErrorMessage(error, "选股数据加载失败"));
        setCandidates([]);
        setCandidateWarnings([]);
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [strategyId, balance, accountLoaded, exchange]);

  return (
    <section id="stockSelect" className="dashboard-section" data-section="stockSelect">
      <h2 className="dashboard-section-title">选股</h2>
      <p className="dashboard-section-desc text-muted" style={{ margin: "0 0 1rem", fontSize: "13px" }}>
        当前页面仅展示各策略的选股结果（候选、打分、信心值），不执行买入。量化买卖请在「量化任务」中操作。
      </p>
      <div className="dashboard-section-inner">
        <div className="strategy-selector" style={{ display: "flex", flexWrap: "wrap", gap: "1rem", marginBottom: "1rem", alignItems: "center" }}>
          {balance != null && (
            <span className="text-muted" style={{ fontSize: "13px" }}>
              账户余额：<strong style={{ color: "#111827" }}>{formatNumber(balance)} 元</strong>
            </span>
          )}
          <label>
            策略：
            <select
              value={strategyId ?? ""}
              onChange={(e) => setStrategyId(e.target.value ? Number(e.target.value) : null)}
              disabled={loading}
            >
              {strategies.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            交易所：
            <select
              value={exchange}
              onChange={(e) => setExchange((e.target.value as ExchangeFilter) || "HS")}
              disabled={loading}
            >
              <option value="ALL">全市场</option>
              <option value="HS">沪深（不含创业板）</option>
              <option value="CYB">创业板</option>
              <option value="BJ">北交所</option>
            </select>
          </label>
          {quantRunning && (
            <span style={{ color: "#f59e0b", fontSize: "12px" }}>量化任务运行中：禁止手动开仓，仅允许平仓</span>
          )}
        </div>
        {error && <div className="alert">{error}</div>}
        {candidateWarnings.length > 0 && (
          <div style={{ marginBottom: "1rem", padding: "0.75rem 1rem", background: "rgba(251,191,36,0.15)", border: "1px solid rgba(251,191,36,0.5)", borderRadius: "6px", color: "#fcd34d", fontSize: "13px" }}>
            {candidateWarnings.map((msg, i) => (
              <div key={i}>{msg}</div>
            ))}
          </div>
        )}
        {loading && candidates.length === 0 ? (
          <div className="empty">加载中…（带市值/日线因子的策略可能需 1～3 分钟，请稍候）</div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>价格</th>
                  <th>涨跌幅</th>
                  <th>成交量</th>
                  <th>信心值</th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((item, idx) => (
                  <tr key={`${item.symbol}-${idx}`}>
                    <td>
                      <a
                        href={thsStockUrl(item.symbol)}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{ color: "inherit", textDecoration: "none" }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        {item.symbol}
                      </a>
                    </td>
                    <td>
                      <a
                        href={thsStockUrl(item.symbol)}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{ color: "inherit", textDecoration: "none" }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        {item.name}
                      </a>
                    </td>
                    <td>{item.price.toFixed(2)}</td>
                    <td>{item.change_pct.toFixed(2)}%</td>
                    <td>{formatNumber(item.volume)}</td>
                    <td>{item.confidence.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {candidates.length === 0 && !loading && (
              <div className="empty">
                {balance != null && balance > 0
                  ? "暂无符合余额与策略的股票，可在账户中充值或更换策略"
                  : "请先在账户中设置或充值余额，再选择策略"}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
