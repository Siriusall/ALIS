import { useEffect, useMemo, useState } from "react";
import { analyzeQuantMarket, executeQuantAnalyzeBuy, previewQuantAnalyzeRebalance, runQuantRunOnce } from "../api/quantRuns";
import type { QuantAnalyzeResponse, QuantRebalancePreviewResponse } from "../types";
import SecurityLabel from "./SecurityLabel";

const ANALYZE_RESULT_KEY = "quant_analyze_result_v1";

type RebalanceMode = "full_rebalance" | "buy_only";

function loadStoredAnalyzeResult(): QuantAnalyzeResponse | null {
  try {
    const raw = sessionStorage.getItem(ANALYZE_RESULT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as QuantAnalyzeResponse;
    return parsed?.ok ? parsed : null;
  } catch {
    return null;
  }
}

function storeAnalyzeResult(data: QuantAnalyzeResponse | null) {
  try {
    if (data?.ok) sessionStorage.setItem(ANALYZE_RESULT_KEY, JSON.stringify(data));
    else sessionStorage.removeItem(ANALYZE_RESULT_KEY);
  } catch {
    // ignore quota / private mode errors
  }
}

const TREND_TEXT: Record<string, string> = {
  uptrend: "趋势偏强",
  sideways: "震荡",
  downtrend: "趋势偏弱",
  high_risk: "高风险",
};

const RISK_TEXT: Record<string, string> = {
  normal: "正常",
  elevated: "偏高",
  high: "高",
};

function fmtPct(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(0)}%`;
}

function fmtTime(value: string | undefined): string {
  if (!value) return "—";
  return String(value).slice(0, 19).replace("T", " ");
}

function fmtMoney(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function buildRebalancePayload(result: QuantAnalyzeResponse, mode: RebalanceMode) {
  const buys = result.rebalance_actions?.buy ?? [];
  const targetPct = Number(result.risk_budget?.target_position_pct);
  const maxSingle = Number(result.risk_budget?.max_single_position_pct);
  const selectionScores = Object.fromEntries(
    (result.selections ?? []).map((row) => [row.symbol, Number(row.score ?? 0)])
  );
  const symbols = buys.length > 0 ? buys.map((item) => item.symbol) : [];
  return {
    signal_date: result.signal_date,
    strategy_id: result.strategy_id,
    symbols,
    target_position_pct: Number.isFinite(targetPct) && targetPct > 0 ? targetPct : 1,
    max_single_position_pct: Number.isFinite(maxSingle) && maxSingle > 0 ? maxSingle : undefined,
    mode,
    target_symbols: result.target_symbols,
    selection_scores: selectionScores,
  };
}

export default function QuantRunPanel() {
  const [loading, setLoading] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [buying, setBuying] = useState(false);
  const [autoRunning, setAutoRunning] = useState(false);
  const [error, setError] = useState("");
  const [buyMessage, setBuyMessage] = useState("");
  const [rebalanceMode, setRebalanceMode] = useState<RebalanceMode>("full_rebalance");
  const [preview, setPreview] = useState<QuantRebalancePreviewResponse | null>(null);
  const [result, setResult] = useState<QuantAnalyzeResponse | null>(() => loadStoredAnalyzeResult());

  const buyList = result?.rebalance_actions?.buy ?? [];
  const sellList = result?.rebalance_actions?.sell ?? [];
  const holdList = result?.rebalance_actions?.hold ?? [];
  const canTrade = buyList.length > 0 || (rebalanceMode === "full_rebalance" && sellList.length > 0);

  useEffect(() => {
    setPreview(null);
    setBuyMessage("");
  }, [result?.signal_date, rebalanceMode]);

  async function handleAnalyze() {
    setLoading(true);
    setError("");
    setPreview(null);
    try {
      const data = await analyzeQuantMarket({ top_n: 10, exchange_group: "HS" });
      setResult(data);
      storeAnalyzeResult(data);
    } catch (e: unknown) {
      const resp = (e as { response?: { data?: { detail?: string | { message?: string } } } })?.response?.data?.detail;
      const msg = typeof resp === "string" ? resp : resp?.message;
      setError(msg || "分析失败，请稍后重试");
      setResult(null);
      storeAnalyzeResult(null);
    } finally {
      setLoading(false);
    }
  }

  async function handlePreview() {
    if (!result?.signal_date || !canTrade) return;
    setPreviewing(true);
    setError("");
    try {
      const data = await previewQuantAnalyzeRebalance(buildRebalancePayload(result, rebalanceMode));
      setPreview(data);
    } catch (e: unknown) {
      const resp = (e as { response?: { data?: { detail?: string | { message?: string } } } })?.response?.data?.detail;
      const msg = typeof resp === "string" ? resp : resp?.message;
      setError(msg || "预览失败，请稍后重试");
      setPreview(null);
    } finally {
      setPreviewing(false);
    }
  }

  async function handleAutoRunOnce() {
    setAutoRunning(true);
    setBuyMessage("");
    setError("");
    try {
      const exec = await runQuantRunOnce();
      if (!exec.ok) throw new Error(exec.message || "自动模拟执行未完成");
      setBuyMessage(
        `自动模拟盘已完成：signal=${exec.signal_date}，execute=${exec.execute_date}，卖出 ${exec.sold_count ?? 0} 只，买入 ${exec.bought_count ?? 0} 只，` +
          `花费 ${fmtMoney(exec.total_buy_cost)} 元。`
      );
      setPreview(null);
      const refreshed = await analyzeQuantMarket({ signal_date: exec.signal_date, top_n: 10, exchange_group: "HS" });
      setResult(refreshed);
      storeAnalyzeResult(refreshed);
    } catch (e: unknown) {
      const resp = (e as { response?: { data?: { detail?: string | { message?: string } } } })?.response?.data?.detail;
      const msg =
        (e as Error)?.message ||
        (typeof resp === "string" ? resp : resp?.message) ||
        "自动模拟执行失败，请稍后重试";
      setError(msg);
    } finally {
      setAutoRunning(false);
    }
  }

  async function handleExecuteSimulation() {
    if (!result?.signal_date || !canTrade) return;
    setBuying(true);
    setBuyMessage("");
    setError("");
    try {
      const exec = await executeQuantAnalyzeBuy(buildRebalancePayload(result, rebalanceMode));
      if (!exec.ok) {
        throw new Error(exec.message || "调仓未成交");
      }
      setBuyMessage(
        `${rebalanceMode === "full_rebalance" ? "完整调仓" : "仅买入"}完成：卖出 ${exec.sold_count ?? 0} 只，买入 ${exec.bought_count} 只，` +
          `花费 ${fmtMoney(exec.total_buy_cost)} 元，账户余额 ${fmtMoney(exec.account_balance)} 元`
      );
      setPreview(null);
      const refreshed = await analyzeQuantMarket({
        signal_date: result.signal_date,
        top_n: 10,
        exchange_group: (result.exchange_group as "HS" | "ALL" | "CYB" | "BJ") || "HS",
      });
      setResult(refreshed);
      storeAnalyzeResult(refreshed);
    } catch (e: unknown) {
      const resp = (e as { response?: { data?: { detail?: string | { message?: string } } } })?.response?.data?.detail;
      const msg =
        (e as Error)?.message ||
        (typeof resp === "string" ? resp : resp?.message) ||
        "模拟执行失败，请稍后重试";
      setError(msg);
    } finally {
      setBuying(false);
    }
  }

  const previewRows = useMemo(() => {
    if (!preview) return [];
    const sells = (preview.sell_orders ?? []).map((row) => ({ ...row, side: "SELL" as const }));
    const buys = (preview.buy_orders ?? []).map((row) => ({ ...row, side: "BUY" as const }));
    const blocked = (preview.blocked_events ?? []).map((row) => ({
      ...row,
      side: (row as { side?: string }).side === "SELL" ? ("SELL" as const) : ("BUY" as const),
    }));
    return [...sells, ...buys, ...blocked];
  }, [preview]);

  return (
    <section id="quantRun" className="dashboard-section" data-section="quantRun">
      <h2 className="dashboard-section-title">量化分析</h2>
      <div className="dashboard-section-inner quant-analyze-page">
        <div className="card quant-analyze-hero">
          <p className="text-muted quant-analyze-desc">
            系统会先判断当日趋势，再按固定成交模型自动模拟执行与记账。预览只用于诊断，不代表用户逐笔审批。
          </p>
          <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="quant-analyze-btn"
              onClick={handleAnalyze}
              disabled={loading || autoRunning}
            >
              {loading ? "分析中…" : "生成今日计划"}
            </button>
            <button
              type="button"
              className="quant-analyze-confirm-btn"
              onClick={handleAutoRunOnce}
              disabled={autoRunning || loading}
            >
              {autoRunning ? "自动执行中…" : "自动执行今日模拟盘"}
            </button>
          </div>
          {error && <div className="alert" style={{ marginTop: 12 }}>{error}</div>}
          {buyMessage && <div className="quant-analyze-buy-success" style={{ marginTop: 12 }}>{buyMessage}</div>}
        </div>

        {result && (
          <div className="quant-analyze-results">
            <div className="card">
              <h3>分析结论</h3>
              <div className="decision-two-col">
                <span>分析日</span>
                <strong>{result.signal_date || "—"}</strong>
                <span>执行日</span>
                <strong>{result.execute_date || "—"}</strong>
                <span>趋势</span>
                <strong>
                  {TREND_TEXT[String(result.trend_analysis?.trend || "")] || result.trend_analysis?.trend || "—"}
                </strong>
                <span>风险</span>
                <strong>
                  {RISK_TEXT[String(result.trend_analysis?.risk_level || "")] || result.trend_analysis?.risk_level || "—"}
                </strong>
                <span>策略模板</span>
                <strong>{result.strategy_template?.name || result.strategy_template?.id || "—"}</strong>
                <span>目标总仓位</span>
                <strong>{fmtPct(result.risk_budget?.target_position_pct)}</strong>
                <span>单票上限</span>
                <strong>{fmtPct(result.risk_budget?.max_single_position_pct)}</strong>
                <span>使用因子</span>
                <strong>{(result.selected_factors ?? result.strategy_template?.factors ?? []).join(" / ") || "—"}</strong>
                <span>分析时间</span>
                <strong>{fmtTime(result.analyzed_at)}</strong>
              </div>
              <div className="decision-message">{result.summary || result.strategy_template?.reason || "—"}</div>
              {result.execution_note && <div className="quant-analyze-note">{result.execution_note}</div>}
            </div>

            <div className="card">
              <div className="quant-analyze-section-head">
                <h3>调仓建议</h3>
                <div className="quant-analyze-toolbar">
                  <label className="quant-analyze-mode">
                    <input
                      type="radio"
                      name="rebalance-mode"
                      checked={rebalanceMode === "full_rebalance"}
                      onChange={() => setRebalanceMode("full_rebalance")}
                    />
                    完整调仓
                  </label>
                  <label className="quant-analyze-mode">
                    <input
                      type="radio"
                      name="rebalance-mode"
                      checked={rebalanceMode === "buy_only"}
                      onChange={() => setRebalanceMode("buy_only")}
                    />
                    仅买入
                  </label>
                  {canTrade && (
                    <>
                      <button
                        type="button"
                        className="quant-analyze-secondary-btn"
                        onClick={handlePreview}
                        disabled={previewing || loading || buying}
                      >
                        {previewing ? "预览中…" : "预览调仓"}
                      </button>
                      <button
                        type="button"
                        className="quant-analyze-confirm-btn"
                        onClick={handleExecuteSimulation}
                        disabled={buying || loading || previewing}
                      >
                        {buying ? "执行中…" : "执行模拟调仓"}
                      </button>
                    </>
                  )}
                </div>
              </div>
              <div className="quant-analyze-actions">
                <div>
                  <div className="sub">建议买入 ({buyList.length})</div>
                  {buyList.length === 0 ? (
                    <div className="empty">暂无买入建议</div>
                  ) : (
                    <ul className="decision-explain-list">
                      {buyList.map((item) => (
                        <li key={`buy-${item.symbol}`}>
                          <SecurityLabel code={item.symbol} name={item.name} />
                          {item.name ? ` ${item.name}` : ""}
                          {item.price != null ? ` @ ${Number(item.price).toFixed(2)}` : ""}
                          {item.score != null ? `（得分 ${Number(item.score).toFixed(3)}）` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div>
                  <div className="sub">建议卖出 ({sellList.length})</div>
                  {sellList.length === 0 ? (
                    <div className="empty">暂无卖出建议</div>
                  ) : (
                    <ul className="decision-explain-list">
                      {sellList.map((item) => (
                        <li key={`sell-${item.symbol}`} className="decision-cell-stop">
                          <SecurityLabel code={item.symbol} name={item.name} />
                          {item.name ? ` ${item.name}` : ""}
                          {item.price != null ? ` @ ${Number(item.price).toFixed(2)}` : ""}
                          {item.quantity != null ? ` × ${item.quantity}` : ""}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div>
                  <div className="sub">继续持有 ({holdList.length})</div>
                  {holdList.length === 0 ? (
                    <div className="empty">暂无持仓重合</div>
                  ) : (
                    <div className="quant-analyze-tags">
                      {holdList.map((sym) => (
                        <span key={`hold-${sym}`} className="quant-analyze-tag">{sym}</span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>

            {preview && (
              <div className="card">
                <h3>调仓预览</h3>
                <div className="decision-two-col">
                  <span>总权益</span>
                  <strong>{fmtMoney(preview.total_equity)}</strong>
                  <span>可用买入预算</span>
                  <strong>{fmtMoney(preview.buy_budget)}</strong>
                  <span>预计买入成本</span>
                  <strong>{fmtMoney(preview.total_buy_cost)}</strong>
                  <span>预计剩余现金</span>
                  <strong>{fmtMoney(preview.estimated_cash_after)}</strong>
                  <span>预计卖出</span>
                  <strong>{preview.sold_count} 只</strong>
                  <span>预计买入</span>
                  <strong>{preview.bought_count} 只</strong>
                </div>
                {(preview.pruned_symbols?.length ?? 0) > 0 && (
                  <div className="quant-analyze-note">
                    以下标的在当前预算与单票上限下买不起 1 手，已自动裁剪（不影响其余可买标的）：
                    {preview.pruned_symbols?.join("、")}
                  </div>
                )}
                <div className="table-scroll" style={{ marginTop: 12 }}>
                  <table>
                    <thead>
                      <tr>
                        <th>方向</th>
                        <th>股票</th>
                        <th>数量</th>
                        <th>参考价</th>
                        <th>成本</th>
                        <th>仓位%</th>
                        <th>状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {previewRows.length === 0 ? (
                        <tr>
                          <td colSpan={7} className="empty">暂无预计成交</td>
                        </tr>
                      ) : (
                        previewRows.map((row, idx) => (
                          <tr key={`${row.symbol}-${row.side}-${idx}`}>
                            <td>{row.side === "SELL" ? "卖出" : "买入"}</td>
                            <td><SecurityLabel code={row.symbol} name={row.name} /></td>
                            <td>{row.quantity ?? "—"}</td>
                            <td>{row.exec_price ?? row.price ?? row.request_price ?? "—"}</td>
                            <td>{row.total_cost != null ? fmtMoney(row.total_cost) : "—"}</td>
                            <td>{row.position_pct != null ? `${row.position_pct}%` : "—"}</td>
                            <td>{row.status === "blocked" ? row.message || "拦截" : row.message || "可成交"}</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {(result.selections?.length ?? 0) > 0 && (
              <div className="card">
                <h3>目标股票</h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>股票</th>
                        <th>得分</th>
                        <th>主要因子贡献</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(result.selections ?? []).map((row) => (
                        <tr key={row.symbol}>
                          <td><SecurityLabel code={row.symbol} name={row.name} /></td>
                          <td>{row.score == null ? "—" : Number(row.score).toFixed(4)}</td>
                          <td>
                            {(row.factor_contributions ?? [])
                              .slice(0, 3)
                              .map((f) => `${f.factor}:${Number(f.contribution ?? 0).toFixed(3)}`)
                              .join("；") || "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {(result.current_positions?.length ?? 0) > 0 && (
              <div className="card">
                <h3>当前持仓</h3>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>股票</th>
                        <th>数量</th>
                        <th>成本价</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(result.current_positions ?? []).map((pos) => (
                        <tr key={pos.symbol}>
                          <td><SecurityLabel code={pos.symbol} name={pos.name} /></td>
                          <td>{pos.quantity ?? 0}</td>
                          <td>{pos.cost_price == null ? "—" : Number(pos.cost_price).toFixed(3)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
