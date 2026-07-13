import { useEffect, useState } from "react";
import { createThesis, fetchTheses, reviewDueTheses, reviewThesis, updateHypothesis, type InvestmentThesis } from "../api/research";

const STATE_NAME: Record<string, string> = { draft: "草稿", watch: "观察", valid: "成立", warning: "预警", invalidated: "证伪", unverified: "未验证", verified: "已验证", failed: "失败" };

export default function ThesisPage() {
  const [items, setItems] = useState<InvestmentThesis[]>([]);
  const [code, setCode] = useState(""); const [title, setTitle] = useState(""); const [core, setCore] = useState(""); const [hypothesis, setHypothesis] = useState("");
  const [metric,setMetric]=useState("revenue_growth");const[minValue,setMinValue]=useState("");const[maxValue,setMaxValue]=useState("");
  const [error, setError] = useState(""); const [notice,setNotice]=useState(""); const [creating, setCreating] = useState(false);const[reviewingDue,setReviewingDue]=useState(false);
  async function refresh() { try { setItems(await fetchTheses()); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } }
  useEffect(() => { refresh(); }, []);
  async function submit() {
    if (!code.trim() || !title.trim() || !hypothesis.trim()) return setError("股票代码、逻辑标题和至少一个必要条件不能为空");
    setCreating(true); setError("");
    try {
      const expected:Record<string,number>={};if(minValue!=="")expected.min=Number(minValue);if(maxValue!=="")expected.max=Number(maxValue);
      await createThesis({ ts_code: code.trim().toUpperCase(), title: title.trim(), core_thesis: core.trim(), hypotheses: [{ statement: hypothesis.trim(),metric,expected, necessary: true }] });
      setCode(""); setTitle(""); setCore(""); setHypothesis(""); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setCreating(false); }
  }
  async function change(hypothesisId: number, status: string) { try { const updated = await updateHypothesis(hypothesisId, status); setItems(current => current.map(item => item.id === updated.id ? updated : item)); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } }
  async function autoReview(id:number){try{const updated=await reviewThesis(id);setItems(current=>current.map(item=>item.id===updated.id?updated:item))}catch(reason){setError(reason instanceof Error?reason.message:String(reason))}}
  async function reviewDue(){setReviewingDue(true);setError("");try{const result=await reviewDueTheses();setNotice(`已复核 ${result.reviewed.length}/${result.due} 条到期投资逻辑`);await refresh()}catch(reason){setError(reason instanceof Error?reason.message:String(reason))}finally{setReviewingDue(false)}}
  return <section className="thesis-page">
    <div className="product-module-head"><div><span>INVESTMENT THESIS</span><h1>投资逻辑中心</h1><p>逻辑不是一段说明文字，而是一组能验证、能失败、能触发减仓的必要条件。</p></div><div><button className="button ghost" onClick={reviewDue} disabled={reviewingDue}>{reviewingDue?"到期复核中…":"复核到期逻辑"}</button><span className="module-state module-state-building">事实层已启用</span></div></div>
    {error && <div className="alert">{error}</div>}
    {notice && <div className="decision-issue warning">{notice}</div>}
    <div className="thesis-create"><h2>建立逻辑卡片</h2><div className="thesis-form"><input placeholder="股票代码 000001.SZ" value={code} onChange={e=>setCode(e.target.value)}/><input placeholder="逻辑标题" value={title} onChange={e=>setTitle(e.target.value)}/><textarea placeholder="核心逻辑" value={core} onChange={e=>setCore(e.target.value)}/><textarea placeholder="必要条件：失败时置信度最高40分" value={hypothesis} onChange={e=>setHypothesis(e.target.value)}/><select value={metric} onChange={e=>setMetric(e.target.value)}><option value="revenue_growth">收入同比</option><option value="core_profit_growth">净利润同比</option><option value="cash_conversion">经营现金/利润</option><option value="debt_to_assets">资产负债率</option></select><input type="number" placeholder="失效下限（可选）" value={minValue} onChange={e=>setMinValue(e.target.value)}/><input type="number" placeholder="失效上限（可选）" value={maxValue} onChange={e=>setMaxValue(e.target.value)}/><button onClick={submit} disabled={creating}>{creating ? "创建中…" : "创建逻辑"}</button></div></div>
    <div className="thesis-list">{items.length === 0 && <div className="research-empty"><strong>还没有投资逻辑卡片</strong><span>建立后系统会持续跟踪必要条件。</span></div>}{items.map(item => <article className="thesis-card" key={item.id}><header><div><span>{item.ts_code} · {item.thesis_type}</span><h2>{item.title}</h2><p>{item.core_thesis || "未填写核心逻辑"}</p></div><div><strong>{STATE_NAME[item.derived_status] ?? item.derived_status}</strong><small>置信度 {item.derived_confidence.toFixed(1)}</small><button className="button ghost" onClick={()=>autoReview(item.id)}>自动复核</button></div></header><div className="thesis-hypotheses">{item.hypotheses.map(h => <div key={h.id}><span className={h.necessary ? "necessary" : ""}>{h.necessary ? "必要" : "普通"}</span><strong>{h.statement}</strong><small>{h.metric||"未绑定指标"} · {STATE_NAME[h.status] ?? h.status}</small><select value={h.status} onChange={e=>change(h.id,e.target.value)}><option value="unverified">未验证</option><option value="verified">已验证</option><option value="warning">预警</option><option value="failed">失败</option><option value="invalidated">证伪</option></select></div>)}</div></article>)}</div>
  </section>;
}
