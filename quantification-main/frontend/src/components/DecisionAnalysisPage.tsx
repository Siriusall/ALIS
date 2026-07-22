import { useEffect, useMemo, useState } from "react";
import { fetchDecisionSummary, type DecisionSummary } from "../api/decision";
import type { QuantAnalyzeResponse } from "../types";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";
import SecurityLabel from "./SecurityLabel";

const ANALYZE_RESULT_KEY = "quant_analyze_result_v1";
const STATE_LABEL: Record<string, string> = { S0: "风险扩张", S1: "震荡分化", S2: "风险收缩", S3: "恐慌修复" };
const TREND_LABEL: Record<string, string> = { uptrend: "趋势偏强", sideways: "震荡", downtrend: "趋势偏弱", high_risk: "高风险" };

function storedAnalysis(): QuantAnalyzeResponse | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(ANALYZE_RESULT_KEY) || "null") as QuantAnalyzeResponse | null;
    return value?.ok ? value : null;
  } catch { return null; }
}

function pct(value: unknown, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : "—";
}

function money(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : "—";
}

export default function DecisionAnalysisPage({ onNavigate }: { onNavigate?: (section: SectionId) => void }) {
  const [summary, setSummary] = useState<DecisionSummary | null>(null);
  const [analysis] = useState<QuantAnalyzeResponse | null>(() => storedAnalysis());
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    fetchDecisionSummary(30).then((data) => { if (active) setSummary(data); })
      .catch(() => { if (active) setError("完整分析读取失败，请返回今日建议后重试"); });
    return () => { active = false; };
  }, []);

  const actions = useMemo(() => [
    ...(analysis?.rebalance_actions?.sell ?? []).map((row) => ({ symbol: row.symbol, name: row.name, score: undefined, reason: row.reason, side: "卖出" })),
    ...(analysis?.rebalance_actions?.buy ?? []).map((row) => ({ symbol: row.symbol, name: row.name, score: row.score, reason: row.reason, side: "买入" })),
    ...(analysis?.rebalance_actions?.hold ?? []).map((symbol) => ({ symbol, side: "持有", name: undefined, score: undefined, reason: undefined })),
  ], [analysis]);

  if (!summary && !error) return <LoadingState variant="page" title="正在读取今日完整分析" detail="加载已生成的决策、市场证据、模型门控和调仓明细" rows={5} />;
  if (!summary) return <div className="alert">{error}</div>;

  const gate = summary.gate_decision;
  const market = summary.market_overview;
  const state = market.market_state;
  const sentiment = market.sentiment;
  const ledger = summary.ledger;
  const model = summary.model_status.active_version;
  const issues = summary.data_health.issues ?? [];
  const risks = ledger.account_risks ?? [];
  const contraction = gate.reason_code === "market_risk_contraction";
  const effectiveRiskBudget = market.position_gate?.combined_risk_budget_multiplier ?? state?.risk_budget_multiplier;
  const strategyTrace = analysis?.rebalance_decision_trace ?? analysis?.strategy_controller?.trace ?? [];
  const strategyContracts = analysis?.strategy_action_contracts ?? analysis?.strategy_controller?.contracts ?? [];

  return (
    <section className="decision-analysis-page">
      <header className="decision-analysis-head">
        <button type="button" onClick={() => onNavigate?.("market")}>← 返回今日建议</button>
        <div><span>DECISION REPORT</span><h1>今日建议完整分析</h1><p>{summary.data_health.trade_dates?.last || market.trade_date || "—"} · 已生成结果，只读展示</p></div>
        <em>不会重新生成计划</em>
      </header>

      <section className={`decision-analysis-verdict ${gate.tone}`}>
        <div><span>最终结论</span><h2>{gate.title}</h2><p>{gate.detail}</p></div>
        <dl><div><dt>决策状态</dt><dd>{gate.tone === "go" ? "允许进入候选" : gate.tone === "stop" ? "停止执行" : "暂缓动作"}</dd></div><div><dt>原因代码</dt><dd>{gate.reason_code}</dd></div></dl>
      </section>

      <div className="decision-analysis-grid">
        <section className="decision-analysis-card">
          <header><span>01</span><div><h2>市场判断</h2><p>今天为什么采取这个方向</p></div></header>
          <div className="decision-analysis-metrics">
            <div><span>市场状态</span><strong>{state?.label || STATE_LABEL[state?.state || ""] || state?.state || "—"}</strong><small>置信度 {pct(state?.confidence)}</small></div>
            <div><span>市场情绪</span><strong>{sentiment?.state || "—"}</strong><small>得分 {sentiment?.score ?? "—"}</small></div>
            <div><span>最终风险预算</span><strong>{pct(effectiveRiskBudget)}</strong><small>{contraction ? "仅允许收缩预算内调整" : market.position_gate?.allow_new_exposure ? "允许按预算新增风险" : "不新增风险敞口"}</small></div>
            <div><span>模型趋势</span><strong>{TREND_LABEL[analysis?.trend_analysis?.trend || ""] || analysis?.trend_analysis?.trend || "—"}</strong><small>{analysis?.summary || state?.reason || "依据市场横截面形成"}</small></div>
          </div>
          {market.position_gate?.reason && <p className="decision-analysis-explain">{market.position_gate.reason}</p>}
        </section>

        <section className="decision-analysis-card">
          <header><span>02</span><div><h2>生产与数据门控</h2><p>这份建议是否具备可信输入</p></div></header>
          <div className="decision-analysis-checks">
            <div className={summary.data_health.ok ? "pass" : "fail"}><i /> <span>市场数据</span><strong>{summary.data_health.ok ? "通过" : "存在阻断"}</strong></div>
            <div className={model ? "pass" : "fail"}><i /> <span>生产模型</span><strong>{model?.version || "未激活"}</strong></div>
            <div className={contraction || !market.position_gate?.allow_new_exposure ? "warn" : "pass"}><i /> <span>组合风险</span><strong>{contraction ? "收缩预算生效" : market.position_gate?.allow_new_exposure ? "允许按预算扩仓" : "限制扩仓"}</strong></div>
          </div>
          {(issues.length > 0 || risks.length > 0) ? <ul className="decision-analysis-issues">{issues.map((item) => <li key={item.code}>{item.message}</li>)}{risks.map((item) => <li key={item.code}>{item.message}</li>)}</ul> : <p className="decision-analysis-ok">未发现阻断今日判断的数据或账户风险。</p>}
        </section>

        <section className="decision-analysis-card wide">
          <header><span>03</span><div><h2>组合影响</h2><p>建议落到当前账户意味着什么</p></div></header>
          <div className="decision-analysis-account">
            <div><span>账户权益</span><strong>¥ {money(ledger.total_equity)}</strong></div><div><span>现金比例</span><strong>{pct(ledger.cash_pct)}</strong></div><div><span>当前回撤</span><strong>{pct(ledger.drawdown)}</strong></div><div><span>当前持仓</span><strong>{ledger.position_count} 只</strong></div><div><span>目标仓位</span><strong>{analysis ? pct(analysis.risk_budget?.target_position_pct) : "本次不调整"}</strong></div><div><span>单票上限</span><strong>{analysis ? pct(analysis.risk_budget?.max_single_position_pct) : "沿用现状"}</strong></div>
          </div>
        </section>

        <section className="decision-analysis-card wide">
          <header><span>04</span><div><h2>策略总控</h2><p>模型识别信号，策略决定动作，组合层只负责权重</p></div></header>
          {strategyTrace.length ? <div className="decision-strategy-trace">{strategyTrace.map((item, index) => <article key={`${item.step}-${index}`}><b>{index + 1}</b><span>{item.step}</span><strong>{item.detail}</strong></article>)}</div> : <p className="decision-analysis-ok">旧分析结果暂未包含策略总控追踪，重新生成今日分析后可查看。</p>}
          {strategyContracts.length ? <div className="decision-strategy-contracts">{strategyContracts.slice(0, 6).map((item) => <article key={`${item.side}-${item.symbol}-${item.priority}`}><div><strong><SecurityLabel code={item.symbol} name={item.name} /></strong><span>{item.action} · {item.expected_return_source}</span></div><em>{item.trigger_models?.slice(0, 3).join(" / ") || "策略控制器"}</em><small>退出：{item.exit_conditions?.[0] || "按策略合同复核"}</small></article>)}</div> : null}
        </section>

        <section className="decision-analysis-card wide">
          <header><span>05</span><div><h2>逐股调仓明细</h2><p>展示本次已经形成的建议，不在此页重新计算</p></div></header>
          {actions.length ? <div className="decision-analysis-table-wrap"><table><thead><tr><th>动作</th><th>标的</th><th>评分</th><th>理由</th></tr></thead><tbody>{actions.map((row, index) => <tr key={`${row.side}-${row.symbol}-${index}`}><td><b className={`action-${row.side}`}>{row.side}</b></td><td><SecurityLabel code={row.symbol} name={row.name} /></td><td>{row.score ?? "—"}</td><td>{row.reason || "遵循本次组合目标"}</td></tr>)}</tbody></table></div> : <div className="decision-analysis-empty"><strong>{gate.tone === "hold" || gate.tone === "stop" ? "本次结论无需生成交易动作" : "本次没有逐股调仓动作"}</strong><p>{gate.tone === "hold" || gate.tone === "stop" ? "系统已经完成分析，并决定保持当前组合；因此没有买卖计划需要展示或重新生成。" : "当前建议未产生买入或卖出标的。"}</p></div>}
        </section>
      </div>
    </section>
  );
}
