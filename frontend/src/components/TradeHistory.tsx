import { useEffect, useMemo, useState } from "react";
import { getSimulationLedger } from "../api/quantRuns";
import type { SimulationLedgerItem, SimulationLedgerResponse } from "../types";
import { formatGmt8Time } from "../utils/time";
import type { SectionId } from "./Layout";
import SecurityLabel from "./SecurityLabel";

function money(value: number | null | undefined, digits = 2) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
}

function sideText(side: string) {
  return side === "BUY" ? "买入" : side === "SELL" ? "卖出" : side || "—";
}

interface TradeHistoryProps {
  onNavigate?: (section: SectionId) => void;
}

export default function TradeHistory({ onNavigate }: TradeHistoryProps) {
  const [data, setData] = useState<SimulationLedgerResponse | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(executionId?: number | null) {
    setLoading(true);
    setError("");
    try {
      const result = await getSimulationLedger({ execution_id: executionId || undefined, limit: 20 });
      setData(result);
      const first = result.items[0]?.execution_id;
      setSelectedId(executionId || first || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "模拟执行账本读取失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const items = data?.items ?? [];
  const selected = useMemo<SimulationLedgerItem | null>(
    () => items.find((item) => item.execution_id === selectedId) ?? items[0] ?? null,
    [items, selectedId],
  );
  const blockedCount = selected?.blocked_signals.length ?? 0;
  const operationCount = selected?.summary.operation_count ?? 0;

  return (
    <section id="execution-ledger" className="dashboard-section" data-section="execution-ledger">
      <h2 className="dashboard-section-title">模拟执行账本</h2>
      <div className="dashboard-section-inner decision-page">
        <div className="product-module-head">
          <div>
            <span>SIMULATION LEDGER</span>
            <h1>逐笔模拟成交、费用和证据</h1>
            <p>固定展示信号日、执行日、成交假设、滑点、费用、阻塞信号和策略版本；过程数据不覆盖，旧批次保留原始结果。</p>
          </div>
          <button type="button" className="button ghost" onClick={() => load(selectedId)} disabled={loading}>
            {loading ? "刷新中" : "刷新"}
          </button>
        </div>

      {error && <div className="alert">{error}</div>}

      <div className="product-module-grid">
        <article className="product-module-card">
          <h2>执行批次</h2>
          <strong>{items.length}</strong>
          <p>当前账本中可回放的批次数量</p>
        </article>
        <article className="product-module-card">
          <h2>当前来源</h2>
          <strong>{selected?.source === "quant_task" ? "量化任务" : "手工/旧版"}</strong>
          <p>区分自动流程与历史/手工批次</p>
        </article>
        <article className="product-module-card">
          <h2>信号 / 执行日</h2>
          <strong>{selected?.signal_date || "—"}</strong>
          <p>执行日 {selected?.execute_date || "—"}</p>
        </article>
        <article className="product-module-card">
          <h2>阻塞信号</h2>
          <strong>{blockedCount}</strong>
          <p>{operationCount} 笔成交进入当前批次</p>
        </article>
      </div>

        <div className="decision-command-bar" aria-label="模拟执行后续动作">
          <div className="decision-command-intro">
            <span>POST EXECUTION</span>
            <strong>执行后必须能复盘</strong>
          </div>
          <button type="button" className={blockedCount > 0 ? "urgent" : ""} onClick={() => onNavigate?.("positionRisk")}>
            <span>01 · 阻塞/风险</span>
            <strong>{blockedCount}</strong>
          </button>
          <button type="button" onClick={() => onNavigate?.("portfolio")}>
            <span>02 · 组合状态</span>
            <strong>{operationCount}</strong>
          </button>
          <button type="button" onClick={() => onNavigate?.("review")}>
            <span>03 · 复盘归因</span>
            <strong>记录</strong>
          </button>
          <button type="button" onClick={() => onNavigate?.("validation")}>
            <span>04 · 重新分析</span>
            <strong>再平衡</strong>
          </button>
        </div>

        <div className="decision-grid">
          <div className="card decision-panel">
            <h3>执行批次</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr><th>批次</th><th>执行时间</th><th>来源</th><th>成交</th><th>费用</th></tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr
                      key={item.execution_id}
                      className={item.execution_id === selected?.execution_id ? "active" : ""}
                      onClick={() => setSelectedId(item.execution_id)}
                    >
                      <td>#{item.execution_id}</td>
                      <td>{formatGmt8Time(item.executed_at)}</td>
                      <td>{item.source === "quant_task" ? "量化任务" : "手工/旧版"}</td>
                      <td>{item.summary.operation_count}</td>
                      <td>{money(item.summary.total_fees, 4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {items.length === 0 && <div className="empty">暂无模拟执行批次</div>}
          </div>

          <div className="card decision-panel">
            <h3>当前批次上下文</h3>
            <div className="decision-two-col">
              <span>策略版本</span><strong>{selected?.strategy_version || "—"}</strong>
              <span>模型版本</span><strong>{selected?.model_version || "透明规则/未记录"}</strong>
              <span>总成交额</span><strong>{money(selected?.summary.gross_turnover)}</strong>
              <span>总费用</span><strong>{money(selected?.summary.total_fees, 4)}</strong>
              <span>未成交/阻塞</span><strong>{selected?.summary.unfilled_count ?? 0} / {selected?.blocked_signals.length ?? 0}</strong>
              <span>说明</span><strong>{selected?.message || "—"}</strong>
            </div>
          </div>
        </div>

        <div className="card decision-panel">
          <h3>逐笔成交</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>标的</th><th>方向</th><th>数量</th><th>模拟价</th><th>收盘价</th><th>滑点</th><th>费用</th><th>估算盈亏</th><th>费用版本</th>
                </tr>
              </thead>
              <tbody>
                {(selected?.operations ?? []).map((op) => (
                  <tr key={op.operation_uid || op.operation_id}>
                    <td><SecurityLabel code={op.symbol} name={op.name} /><div className="sub">{op.trade_time}</div></td>
                    <td>{sideText(op.side)}</td>
                    <td>{op.quantity.toLocaleString("zh-CN")}</td>
                    <td>{money(op.simulated_price, 3)}</td>
                    <td>{money(op.close_price, 3)}</td>
                    <td>{money(op.assumption.slippage_bps, 2)}bp</td>
                    <td>{money(op.fees.total, 4)}</td>
                    <td>{money(op.mark_to_market_pnl)}</td>
                    <td>{op.assumption.fee_schedule_version || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(selected?.operations ?? []).length === 0 && <div className="empty">该批次没有关联逐笔成交</div>}
        </div>

        {(selected?.blocked_signals.length ?? 0) > 0 && (
          <div className="card decision-panel">
            <h3>被拒绝信号</h3>
            <div className="table-scroll">
              <table>
                <thead><tr><th>标的</th><th>方向</th><th>原因</th><th>说明</th></tr></thead>
                <tbody>
                  {selected?.blocked_signals.map((item, index) => (
                    <tr key={index}>
                      <td><SecurityLabel code={String(item.symbol || item.ts_code || "—")} name={String(item.name || "")} /></td>
                      <td>{String(item.side || "—")}</td>
                      <td>{String(item.code || "—")}</td>
                      <td>{String(item.message || item.reason || "—")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {data?.limitations.map((item) => <div className="quant-analyze-note" key={item}>{item}</div>)}
      </div>
    </section>
  );
}
