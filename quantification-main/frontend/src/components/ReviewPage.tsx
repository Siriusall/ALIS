import { useEffect, useMemo, useState } from "react";
import { createReview, fetchDecisionSnapshots, fetchReviewSuggestion, fetchReviews, type DecisionSnapshot, type ReviewRecord } from "../api/research";

const ERROR_TYPES = ["无", "数据质量", "模型失效", "估值判断", "风险执行", "交易执行", "研究遗漏", "其他"];

function asText(value: unknown): string {
  if (value == null) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export default function ReviewPage() {
  const [decisions, setDecisions] = useState<DecisionSnapshot[]>([]);
  const [reviews, setReviews] = useState<ReviewRecord[]>([]);
  const [selectedId, setSelectedId] = useState<number | "">("");
  const [returnPct, setReturnPct] = useState("");
  const [attribution, setAttribution] = useState("");
  const [errorType, setErrorType] = useState("无");
  const [lessons, setLessons] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [error, setError] = useState("");

  const reviewedIds = useMemo(() => new Set(reviews.map(item => item.decision_snapshot_id).filter(Boolean)), [reviews]);
  const selected = decisions.find(item => item.id === selectedId) ?? null;
  const awaiting = decisions.filter(item => !reviewedIds.has(item.id));

  async function load() {
    setLoading(true); setError("");
    try { const [nextDecisions, nextReviews] = await Promise.all([fetchDecisionSnapshots(), fetchReviews()]); setDecisions(nextDecisions); setReviews(nextReviews); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function suggestOutcome() {
    if (!selected) return;
    setSuggesting(true); setError("");
    try {
      const suggestion = await fetchReviewSuggestion(selected.id);
      if (!suggestion.ok || suggestion.outcome?.directional_return == null) {
        throw new Error(suggestion.reason || "No subsequent price data is available for this decision.");
      }
      setReturnPct((suggestion.outcome.directional_return * 100).toFixed(2));
      setAttribution(previous => previous || "Auto-calculated from the first available close after the decision and the latest available close; it is not realized PnL or full attribution.");
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSuggesting(false); }
  }

  async function submit() {
    if (!selected || !lessons.trim()) { setError("请选择一条冻结决策，并填写改进结论。"); return; }
    const parsedReturn = Number(returnPct);
    if (returnPct && !Number.isFinite(parsedReturn)) { setError("收益率必须为数字。"); return; }
    setSaving(true); setError("");
    try {
      await createReview({ decision_snapshot_id: selected.id, outcome: { return_pct: returnPct ? parsedReturn / 100 : null, reviewed_decision: selected.decision_type }, attribution: { summary: attribution.trim() || "未拆分归因" }, error_type: errorType === "无" ? undefined : errorType, lessons: lessons.trim() });
      setReturnPct(""); setAttribution(""); setErrorType("无"); setLessons(""); setSelectedId(""); await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSaving(false); }
  }

  return <section className="product-module-page review-page">
    <div className="product-module-head"><div><span>DECISION REVIEW</span><h1>复盘中心</h1><p>基于冻结的决策版本记录结果、收益归因、错误类型和下一次改进，不以当前数据改写当时判断。</p></div><button className="button ghost" onClick={() => void load()} disabled={loading}>{loading ? "加载中…" : "刷新"}</button></div>
    {error && <div className="alert">{error}</div>}
    <div className="decision-grid decision-metrics"><div className="card metric-card"><div className="sub">冻结决策</div><div className="metric">{decisions.length}</div></div><div className="card metric-card"><div className="sub">待复盘</div><div className="metric">{awaiting.length}</div></div><div className="card metric-card"><div className="sub">已记录复盘</div><div className="metric">{reviews.length}</div></div></div>
    <div className="decision-grid">
      <div className="card decision-panel"><h3>创建复盘</h3><div className="form-stack">
        <label>冻结决策<select value={selectedId} onChange={event => setSelectedId(event.target.value ? Number(event.target.value) : "")}><option value="">选择待复盘决策</option>{awaiting.map(item => <option key={item.id} value={item.id}>{item.decision_time} · {item.ts_code} · {item.decision_type}</option>)}</select></label>
        {selected && <div className="decision-issue warning"><strong>{selected.ts_code} · {selected.decision_type}</strong><br />数据截至：{selected.data_as_of_time} · 模型：{selected.model_version}<br />理由：{selected.reason}<br />失效条件：{(selected.invalidation_conditions ?? []).join("；") || "未记录"}</div>}
        <label>复盘收益率（%）<input value={returnPct} onChange={event => setReturnPct(event.target.value)} placeholder="例如 -4.2" /></label>
        <label>收益归因<textarea value={attribution} onChange={event => setAttribution(event.target.value)} placeholder="例如：行业回撤 2%，选股偏差 1%，执行滑点 0.3%" /></label>
        <label>错误分类<select value={errorType} onChange={event => setErrorType(event.target.value)}>{ERROR_TYPES.map(item => <option key={item}>{item}</option>)}</select></label>
        <label>改进结论<textarea value={lessons} onChange={event => setLessons(event.target.value)} placeholder="说明应保留、修正或废弃的判断规则" /></label>
        <button className="button primary" onClick={() => void submit()} disabled={saving}>{saving ? "保存中…" : "保存复盘"}</button>
        {selected && <button className="button ghost" onClick={() => void suggestOutcome()} disabled={suggesting}>{suggesting ? "Calculating…" : "Calculate subsequent price return"}</button>}
      </div></div>
      <div className="card decision-panel"><h3>决策回放</h3><div className="table-scroll"><table><thead><tr><th>时间</th><th>标的</th><th>动作</th><th>数据截至</th><th>模型</th></tr></thead><tbody>{decisions.map(item => <tr key={item.id}><td>{item.decision_time}</td><td>{item.ts_code}</td><td>{item.decision_type}</td><td>{item.data_as_of_time}</td><td>{item.model_version}</td></tr>)}</tbody></table></div>{!decisions.length && <div className="empty">暂无冻结决策；执行模拟调仓后会在此出现。</div>}</div>
    </div>
    <div className="card decision-panel"><h3>历史复盘与改进闭环</h3><div className="table-scroll"><table><thead><tr><th>复盘时间</th><th>决策</th><th>收益</th><th>归因</th><th>错误类型</th><th>改进结论</th></tr></thead><tbody>{reviews.map(item => <tr key={item.id}><td>{item.review_time}</td><td>{item.decision_snapshot_id ?? "独立复盘"}</td><td>{asText(item.outcome.return_pct)}</td><td>{asText(item.attribution.summary)}</td><td>{item.error_type || "—"}</td><td>{item.lessons || "—"}</td></tr>)}</tbody></table></div>{!reviews.length && <div className="empty">尚无复盘记录。</div>}</div>
  </section>;
}
