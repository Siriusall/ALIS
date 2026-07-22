import { useEffect, useMemo, useState } from "react";
import { fetchManualReviewQueue, fetchProfitLoop, fetchReviewLearningOverview, generateLlmReviewDraft, materializeReviewOutcomes, submitManualReviewAction, type LlmReviewDraft, type ManualReviewQueue, type ManualReviewQueueItem, type ProfitLoopOverview, type ReviewLearningOverview } from "../api/research";
import { formatGmt8Time } from "../utils/time";
import { getQuantPerformance, type QuantPerformanceOverview } from "../api/quantRuns";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";

interface Props { onNavigate?: (section: SectionId) => void }
type Fact = { label: string; value: string; note?: string };

const ERROR_TYPES = ["数据质量", "模型失效", "估值判断", "风险执行", "交易执行", "研究遗漏", "其他"];
const KIND_LABEL: Record<string, string> = {
  decision_review: "决策复盘", event_evidence: "公告证据", model_release: "模型上线",
  system_exception: "异常任务", data_quality: "数据质量",
};
const KIND_HINT: Record<string, string> = {
  decision_review: "判断当时的投资决策是否合理", event_evidence: "确认公告证据和影响判断是否可信",
  model_release: "决定模型是否可以接管生产", system_exception: "确认异常原因及下一步处置",
  data_quality: "确认数据缺口是否影响研究和交易",
};

function defaultErrorType(kind: string) {
  if (kind === "data_quality") return "数据质量";
  if (kind === "decision_review") return "模型失效";
  if (kind === "system_exception") return "风险执行";
  return "研究遗漏";
}

function humanizeStatus(status: string) {
  return ({ failed: "执行失败", paused: "已暂停", running: "执行中", completed: "已完成", pending: "等待执行" } as Record<string, string>)[status] || status;
}

function humanizeReason(reason: string) {
  if (/duplicate key value.*positions_symbol_key/i.test(reason)) {
    const symbol = reason.match(/\(symbol\)=\(([^)]+)\)/i)?.[1];
    return `${symbol ? `股票 ${symbol} 的` : "该股票的"}持仓记录已存在，任务重复写入时失败。建议检查持仓导入的去重逻辑后重试。`;
  }
  if (/timeout|timed out/i.test(reason)) return "后台任务执行超时，未能在限定时间内完成。建议检查任务进度或重新执行。";
  if (/connection refused|could not connect/i.test(reason)) return "后台服务连接失败，任务未能继续。建议确认依赖服务已经启动。";
  return reason;
}

function evidenceRows(item: ManualReviewQueueItem): Fact[] {
  const evidence = item.evidence ?? {};
  if (item.kind === "data_quality") {
    const detail = (evidence.detail && typeof evidence.detail === "object" ? evidence.detail : {}) as Record<string, unknown>;
    const factors = (detail.factor_counts && typeof detail.factor_counts === "object" ? detail.factor_counts : {}) as Record<string, unknown>;
    const totalDays = Number(detail.total_days || detail.affected_days || 0);
    const coverage = (value: unknown) => `${Number(value || 0)} / ${totalDays} 天`;
    return [
      { label: "问题级别", value: evidence.level === "warning" ? "警告" : String(evidence.level || "异常") },
      { label: "检查范围", value: `最近 ${totalDays} 个交易日` },
      { label: "成交量覆盖", value: coverage(factors.volume) },
      { label: "换手率覆盖", value: coverage(factors.turnover) },
      { label: "市盈率覆盖", value: coverage(factors.pe_ttm) },
    ];
  }
  if (item.kind === "decision_review") return [
    { label: "研究标的", value: item.subject || "组合" },
    { label: "决策动作", value: String(evidence.decision_type || "未记录") },
    { label: "模型版本", value: String(evidence.model_version || "规则系统") },
    { label: "当时理由", value: String(evidence.reason || "未记录") },
  ];
  if (item.kind === "system_exception") return [
    { label: "任务编号", value: item.subject }, { label: "任务阶段", value: String(evidence.phase || "未知") },
    { label: "任务类型", value: String(evidence.task_type || "后台任务") }, { label: "当前状态", value: humanizeStatus(item.status) },
  ];
  if (item.kind === "model_release") return [
    { label: "模型版本", value: item.subject },
    { label: "历史验证", value: String(((evidence.validation as Record<string, unknown> | undefined)?.status) || "待确认") },
    { label: "影子观察", value: `${Number(evidence.live_observation_weeks || 0).toFixed(2)} 周` },
  ];
  return Object.entries(evidence).slice(0, 4).map(([key, value]) => ({
    label: key, value: typeof value === "object" ? "已附结构化证据" : String(value ?? "—"),
  }));
}

function reviewQuestion(item: ManualReviewQueueItem) {
  if (item.kind === "data_quality") return "这些数据覆盖不足是否可以接受，并允许系统继续使用当前数据？";
  if (item.kind === "decision_review") return "事后看，这次买入、卖出或持有判断是否合理？";
  if (item.kind === "model_release") return "该模型是否满足上线条件，可以接管生产决策？";
  if (item.kind === "event_evidence") return "公告证据真实，且系统对影响方向的判断正确吗？";
  return "系统给出的异常判断和建议处置是否正确？";
}

function systemConclusion(item: ManualReviewQueueItem) {
  const triage = (item.evidence?.llm_triage || {}) as Record<string, unknown>;
  const triageDraft = (triage.draft && typeof triage.draft === "object" ? triage.draft : {}) as Record<string, unknown>;
  const conclusion = String(triage.summary || triageDraft.summary || humanizeReason(item.reason) || "系统尚未形成明确结论");
  const action = String(triageDraft.recommended_action || (
    item.kind === "system_exception" ? "先检查任务详情，再决定继续执行、重试或终止。" :
    item.kind === "data_quality" ? "确认数据影响范围，决定补数后放行或保持阻断。" :
    item.kind === "model_release" ? "核对验证与影子观察结果后决定是否上线。" :
    "核对证据后确认、修改或驳回系统结论。"
  ));
  const confidence = Number(triage.confidence ?? triageDraft.confidence);
  return { conclusion, action, confidence: Number.isFinite(confidence) ? confidence : null };
}

function pct(value: unknown, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${(number * 100).toFixed(digits)}%` : "—";
}

function rawPct(value: unknown, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%` : "—";
}

export default function ReviewWorkbenchPage({ onNavigate }: Props) {
  const [queue, setQueue] = useState<ManualReviewQueue | null>(null);
  const [learning, setLearning] = useState<ReviewLearningOverview | null>(null);
  const [profitLoop, setProfitLoop] = useState<ProfitLoopOverview | null>(null);
  const [performance, setPerformance] = useState<QuantPerformanceOverview | null>(null);
  const [selectedKey, setSelectedKey] = useState("");
  const [draft, setDraft] = useState<LlmReviewDraft | null>(null);
  const [errorType, setErrorType] = useState("模型失效");
  const [comment, setComment] = useState("");
  const [mode, setMode] = useState<"reject" | "modify" | null>(null);
  const [loading, setLoading] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true); setError("");
    try {
      const [result, learningResult, performanceResult, profitResult] = await Promise.all([fetchManualReviewQueue(), fetchReviewLearningOverview(), getQuantPerformance(), fetchProfitLoop()]);
      setQueue(result);
      setLearning(learningResult);
      setPerformance(performanceResult);
      setProfitLoop(profitResult);
      setSelectedKey((current) => result.items.some((item) => item.key === current) ? current : result.items[0]?.key ?? "");
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setLoading(false); }
  }

  async function refreshOutcomes() {
    setLoading(true); setError("");
    try { await materializeReviewOutcomes(); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); setLoading(false); }
  }

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (!queue?.ai_triage?.running && !((queue?.ai_triage?.pending ?? 0) > 0)) return;
    const timer = window.setTimeout(() => void load(), 5000);
    return () => window.clearTimeout(timer);
  }, [queue?.ai_triage?.running, queue?.ai_triage?.pending]);
  const selected = useMemo(() => queue?.items.find((item) => item.key === selectedKey) ?? queue?.items[0] ?? null, [queue, selectedKey]);
  const conclusion = useMemo(() => selected ? systemConclusion(selected) : null, [selected]);
  useEffect(() => {
    setDraft(null); setComment(""); setMode(null);
    if (selected) {
      setErrorType(defaultErrorType(selected.kind));
      if (selected.kind === "decision_review") {
        const triage = selected.evidence?.llm_triage as Record<string, unknown> | undefined;
        const frozenDraft = triage?.draft as LlmReviewDraft["draft"] | undefined;
        if (frozenDraft?.outcome_assessment) {
          setDraft({ ok:true, model:String(triage?.model || "deepseek-v4-flash"),
            decision_snapshot_id:Number(selected.key.split(":")[1]), draft:frozenDraft,
            disclaimer:"这是LLM预审时冻结的判断；人工操作会与该证据一并保存。" });
        } else void createDraft(selected);
      }
    }
  }, [selected?.key]);

  async function createDraft(item: ManualReviewQueueItem) {
    const id = Number(item.key.split(":")[1]);
    if (!Number.isFinite(id)) return;
    setDrafting(true); setError("");
    try { setDraft(await generateLlmReviewDraft(id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setDrafting(false); }
  }

  async function decide(action: "approved" | "rejected" | "modified_approved") {
    if (!selected) return;
    if (action === "rejected" && errorType === "其他" && !comment.trim()) {
      setError("选择“其他”时，请简单说明驳回原因。"); return;
    }
    setSaving(true); setError("");
    try {
      await submitManualReviewAction({ review_key: selected.key, item_kind: selected.kind, action,
        error_type: action === "approved" ? "" : errorType, comment,
        llm_draft: draft?.draft as unknown as Record<string, unknown> | undefined });
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSaving(false); }
  }

  async function adoptAiSuggestion() {
    if (!selected || !draft) return;
    const adoptedComment = [draft.draft.summary, draft.draft.outcome_assessment, draft.draft.attribution,
      draft.draft.limitations.length ? `限制：${draft.draft.limitations.join("；")}` : ""].filter(Boolean).join("\n");
    setSaving(true); setError("");
    try {
      await submitManualReviewAction({ review_key: selected.key, item_kind: selected.kind,
        action: "ai_adopted", error_type: draft.draft.error_type || "", comment: adoptedComment,
        llm_draft: draft.draft as unknown as Record<string, unknown> } as unknown as Parameters<typeof submitManualReviewAction>[0]);
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSaving(false); }
  }

  if (loading && !queue) return <LoadingState variant="page" title="正在汇总人工复核队列" detail="正在检查决策、公告、模型与系统异常" rows={5} />;

  const latestWeek = performance?.weekly[performance.weekly.length - 1];
  const latestMonth = performance?.monthly[performance.monthly.length - 1];
  const latestYear = performance?.yearly[performance.yearly.length - 1];

  return <section className="product-module-page manual-review-page">
    <div className="product-module-head"><div><span>REVIEW CENTER</span><h1>复盘中心</h1><p>先看组合结果，再处理真正需要你确认的决策、模型和系统问题。</p></div><button className="button ghost" onClick={() => void load()} disabled={loading}>刷新复盘</button></div>
    {error && <div className="alert">{error}</div>}
    <section className="review-scoreboard">
      <div className="review-scoreboard-lead">
        <span>本月组合结果</span>
        <strong className={Number(latestMonth?.return_pct || 0) >= 0 ? "positive" : "negative"}>{latestMonth ? `${latestMonth.return_pct >= 0 ? "+" : ""}${latestMonth.return_pct.toFixed(2)}%` : "—"}</strong>
        <p>相对基准 {latestMonth?.benchmark_return_pct == null ? "—" : `${(latestMonth.return_pct - latestMonth.benchmark_return_pct) >= 0 ? "+" : ""}${(latestMonth.return_pct - latestMonth.benchmark_return_pct).toFixed(2)}%`} · {latestMonth?.attribution_status === "complete" ? "收益归因完整" : "收益归因待补充"}</p>
      </div>
      <div className="review-scoreboard-metrics">
        <article><span>本周收益</span><strong>{latestWeek ? `${latestWeek.return_pct.toFixed(2)}%` : "—"}</strong><small>成本 {latestWeek?.transaction_cost.toFixed(2) ?? "—"}</small></article>
        <article><span>年度回撤</span><strong>{latestYear ? `${latestYear.max_drawdown_pct.toFixed(2)}%` : "—"}</strong><small>年度收益 {latestYear ? `${latestYear.return_pct.toFixed(2)}%` : "—"}</small></article>
        <article className={(queue?.counts.high ?? 0) > 0 ? "urgent" : ""}><span>待我确认</span><strong>{queue?.counts.total ?? 0}</strong><small>{queue?.counts.high ?? 0} 条高优先级</small></article>
      </div>
    </section>
    <section className="profit-loop-panel">
      <header>
        <div><span>收益闭环</span><strong>系统每天回答：谁赚钱、谁失效、要不要调仓、为什么卖、学到了什么</strong></div>
        <button className="button ghost" disabled={loading} onClick={() => void refreshOutcomes()}>更新闭环</button>
      </header>
      <div className="profit-loop-grid">
        <article>
          <span>真实收益归因</span>
          <strong>{rawPct(profitLoop?.attribution.latest_daily_return_pct)}</strong>
          <small>模型 {pct(profitLoop?.attribution.components.model_selection)} · 行业 {pct(profitLoop?.attribution.components.industry_exposure)} · 成本 {profitLoop?.attribution.components.trading_cost?.toFixed?.(2) ?? "—"}</small>
        </article>
        <article className={(profitLoop?.model_monitor.degraded_count ?? 0) > 0 ? "warning" : ""}>
          <span>模型上线后监控</span>
          <strong>{profitLoop?.model_monitor.degraded_count ? `${profitLoop.model_monitor.degraded_count} 个降权` : "健康"}</strong>
          <small>{profitLoop?.model_monitor.items?.[0] ? `${profitLoop.model_monitor.items[0].name} · ${profitLoop.model_monitor.items[0].reason}` : "live IC / hit rate / decay 已监控"}</small>
        </article>
        <article>
          <span>组合层再优化</span>
          <strong>{profitLoop?.portfolio_optimization.optimizer.cost_gate?.triggered ? "保持当前持仓" : profitLoop?.portfolio_optimization.optimizer.ok ? "可执行" : "需处理"}</strong>
          <small>{profitLoop?.portfolio_optimization.optimizer.cost_gate?.reason || `状态 ${profitLoop?.portfolio_optimization.optimizer.status || "—"} · 优化标的 ${profitLoop?.portfolio_optimization.candidate_universe.optimization_count ?? "—"} 只 · 风险 ${profitLoop?.portfolio_optimization.risk_budget.status || "—"}`}</small>
        </article>
        <article className={(profitLoop?.sell_rebalance.items ?? []).some((item) => item.action !== "hold") ? "warning" : ""}>
          <span>卖出/调仓诊断</span>
          <strong>{(profitLoop?.sell_rebalance.items ?? []).filter((item) => item.action !== "hold").length} 个需减仓</strong>
          <small>{profitLoop?.sell_rebalance.items?.[0]?.reasons?.[0]?.detail || "未触发退出条件"}</small>
        </article>
        <article>
          <span>复盘自动学习</span>
          <strong>{profitLoop?.learning.overview.counts.feedback_samples ?? learning?.counts.feedback_samples ?? 0}</strong>
          <small>反馈样本 · 卖出标签 {profitLoop?.sell_rebalance.learning_labels_written ?? 0} 条</small>
        </article>
      </div>
    </section>
    <div className="manual-review-summary"><div><strong>{queue?.counts.total ?? 0}</strong><span>需人工处理</span></div><div className="danger"><strong>{queue?.counts.high ?? 0}</strong><span>高优先级</span></div><div><strong>{queue?.ai_triage?.auto_resolved ?? 0}</strong><span>AI已自动处理</span></div><div><strong>{queue?.ai_triage?.awaiting_outcome ?? 0}</strong><span>等待结果观察</span></div><p>{queue?.ai_triage?.running || (queue?.ai_triage?.pending ?? 0) > 0 ? `AI正在预审 ${queue?.ai_triage?.pending ?? 0} 条…` : "未成熟决策不会进入人工队列"}</p></div>
    <section className="manual-review-learning"><header><div><span>复盘学习闭环</span><strong>人工结论正在反馈给模型治理</strong></div><button className="button ghost" disabled={loading} onClick={() => void refreshOutcomes()}>更新收益归因</button></header><div><article><span>已归因决策</span><strong>{learning?.counts.decisions ?? 0}</strong></article><article><span>固定周期结果</span><strong>{learning?.counts.outcomes ?? 0}</strong></article><article><span>方向正确率</span><strong>{learning?.accuracy == null ? "待观察" : `${(learning.accuracy * 100).toFixed(0)}%`}</strong></article><article><span>训练反馈样本</span><strong>{learning?.counts.feedback_samples ?? 0}</strong></article></div><p>结果按 5、20、60 个交易日自动冻结；市场、行业、选股贡献进入归因，人工错误分类和成熟结果共同约束模型晋升。</p></section>
    <details className="module-detail-drawer">
      <summary><span>展开人工复核队列和 AI 预算</span><em>{queue?.items.length ?? 0} 条待确认，系统已先做预审</em></summary>
    <section className={`llm-token-budget ${queue?.llm_budget?.status ?? "available"}`}><div><span>LLM Token 预算</span><strong>{(queue?.llm_budget?.used_tokens ?? 0).toLocaleString()} / {(queue?.llm_budget?.daily_budget ?? 0).toLocaleString()}</strong></div><div className="bar"><i style={{width:`${Math.min(100,((queue?.llm_budget?.used_tokens ?? 0)/Math.max(1,queue?.llm_budget?.daily_budget ?? 1))*100)}%`}} /></div><p>今日剩余 {(queue?.llm_budget?.remaining_tokens ?? 0).toLocaleString()} Token · 单次最多 {(queue?.llm_budget?.per_request_limit ?? 0).toLocaleString()} · 已调用 {queue?.llm_budget?.requests ?? 0} 次</p></section>
    <div className="manual-review-layout">
      <aside className="manual-review-list"><header><h2>复核队列</h2><span>{queue?.items.length ?? 0} 条</span></header>{queue?.items.map((item) => <button key={item.key} className={selected?.key === item.key ? "active" : ""} onClick={() => setSelectedKey(item.key)}><i className={item.priority} /><div><strong>{item.title}</strong><span>{KIND_LABEL[item.kind]} · {item.created_at ? formatGmt8Time(item.created_at) : "待处理"}</span></div></button>)}{!queue?.items.length && <div className="empty">队列已经处理完成。</div>}</aside>
      <main className="manual-review-detail">{selected ? <>
        <div className="manual-review-title"><div><span>{KIND_LABEL[selected.kind]}</span><h2>{selected.title}</h2><p>{KIND_HINT[selected.kind]}</p></div>{selected.priority === "high" && <em>高优先级</em>}</div>
        {Boolean(selected.evidence?.llm_triage) && <section className="manual-review-escalation"><span>为什么交给你</span><strong>{String((selected.evidence.llm_triage as Record<string,unknown>).escalation_reason || "LLM无法安全自动处理")}</strong><p>{String((selected.evidence.llm_triage as Record<string,unknown>).summary || "")}</p></section>}
        {conclusion && <section className="manual-review-conclusion"><header><div><span>系统结论</span><strong>{conclusion.conclusion}</strong></div>{conclusion.confidence != null && <em>AI 结论可信度 {(conclusion.confidence * 100).toFixed(0)}%</em>}</header><div><span>建议处置</span><p>{conclusion.action}</p></div><small>请核对下方证据后，再决定通过、修改或驳回。</small></section>}
        <section className="manual-review-question"><span>请判断</span><strong>{reviewQuestion(selected)}</strong></section>
        <section><div className="manual-review-section-title"><h3>系统证据</h3><span>已转换为业务含义</span></div><div className="manual-review-facts">{evidenceRows(selected).map((row) => <article key={`${row.label}-${row.value}`}><span>{row.label}</span><strong>{row.value}</strong>{row.note && <small>{row.note}</small>}</article>)}</div><p className="manual-review-reason">{selected.kind === "data_quality" ? "系统发现部分核心因子在近期交易日覆盖不足，可能影响选股、回测和模型判断。" : humanizeReason(selected.reason)}</p></section>
        {selected.kind === "decision_review" && <section className="manual-review-ai"><div className="manual-review-section-title"><h3>DeepSeek 建议</h3><span>{draft ? `AI 结论可信度 ${(draft.draft.confidence * 100).toFixed(0)}%` : "仅供参考"}</span></div>{drafting ? <p>正在阅读冻结证据并生成判断…</p> : draft ? <><strong>{draft.draft.outcome_assessment || draft.draft.summary}</strong><p>{draft.draft.attribution}</p>{draft.draft.limitations.length > 0 && <small>限制：{draft.draft.limitations.join("；")}</small>}<button className="manual-review-adopt-ai" disabled={saving} onClick={() => void adoptAiSuggestion()}>采纳 AI 建议</button></> : <button className="button ghost" onClick={() => void createDraft(selected)}>重新生成建议</button>}</section>}
        {mode && <section className="manual-review-reason-form"><h3>{mode === "reject" ? "为什么驳回？" : "需要修改什么？"}</h3>{mode === "reject" && <div className="manual-review-reason-chips">{ERROR_TYPES.map((item) => <button key={item} className={errorType === item ? "active" : ""} onClick={() => setErrorType(item)}>{item}</button>)}</div>}<textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder={mode === "reject" ? "通常可不填；选择“其他”或驳回高风险事项时请说明" : "简单说明修改内容，留空则采用 DeepSeek 草稿"} /><div><button className={mode === "reject" ? "button danger" : "button primary"} disabled={saving} onClick={() => void decide(mode === "reject" ? "rejected" : "modified_approved")}>{mode === "reject" ? "确认驳回" : "确认修改后通过"}</button><button className="button ghost" onClick={() => setMode(null)}>取消</button></div></section>}
        {!mode && <div className="manual-review-actions"><button className="approve" disabled={saving || drafting} onClick={() => void decide("approved")}><b>✓</b><span>通过</span><small>认可系统结论并进入下一条</small></button><button className="reject" disabled={saving || drafting} onClick={() => setMode("reject")}><b>×</b><span>驳回</span><small>结论不正确或风险不可接受</small></button><button className="modify" disabled={saving || drafting} onClick={() => setMode("modify")}>修改后通过</button></div>}
        <button className="manual-review-context-link" onClick={() => onNavigate?.(selected.target_section as SectionId)}>查看完整上下文 →</button>
      </> : <div className="empty">当前没有需要人工复核的事项。</div>}</main>
    </div>
    </details>
  </section>;
}
