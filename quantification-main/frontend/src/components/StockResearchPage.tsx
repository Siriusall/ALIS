import { useState } from "react";
import { fetchStockResearch, type StockResearchResult } from "../api/research";

const DIMENSION_NAMES: Record<string, string> = { quality: "企业质量", growth: "成长", cashflow: "现金流", governance: "治理", industry: "行业景气", expectation: "预期变化", valuation: "估值" };

export default function StockResearchPage() {
  const [code, setCode] = useState("");
  const [data, setData] = useState<StockResearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  async function run() {
    const normalized = code.trim().toUpperCase();
    if (!normalized) return setError("请输入股票代码，例如 000001.SZ");
    setLoading(true); setError("");
    try {
      const result = await fetchStockResearch(normalized);
      if (!result.ok) setError(result.reason === "stock_not_found" ? "股票主数据中不存在该代码" : result.reason ?? "研究失败");
      setData(result.ok ? result : null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setLoading(false); }
  }
  return <section className="stock-research-page">
    <div className="product-module-head"><div><span>COMPANY RESEARCH</span><h1>股票研究中心</h1><p>所有结论拆解到权重、覆盖率和可用时间；缺数据时明确降级，不生成虚假的完整评分。</p></div></div>
    <div className="stock-research-search"><input value={code} onChange={event => setCode(event.target.value)} onKeyDown={event => event.key === "Enter" && run()} placeholder="输入 Tushare 代码，如 000001.SZ"/><button onClick={run} disabled={loading}>{loading ? "分析中…" : "开始研究"}</button></div>
    {error && <div className="alert">{error}</div>}
    {!data && !error && <div className="research-empty"><strong>选择一只沪深股票</strong><span>系统将读取真实财务、行情和来源证据后计算，不使用示例数据。</span></div>}
    {data && <>
      <div className="research-profile"><div><span>{data.ts_code}</span><h2>{data.profile?.name}</h2><p>{data.classification?.industry} · {data.classification?.business_model} · {data.classification?.lifecycle}</p></div><div><span>研究状态</span><strong>{data.research_state?.classification}</strong><small>置信度 {((data.research_state?.confidence ?? 0) * 100).toFixed(1)}%</small></div><div><span>风险门控</span><strong>{data.risk?.eligible ? data.risk.risk_level : "禁止准入"}</strong><small>单股上限 {((data.risk?.max_position ?? 0) * 100).toFixed(0)}%</small></div></div>
      <div className="research-dimensions">{Object.entries(data.dimensions ?? {}).map(([key, item]) => <article key={key} className={item.status === "ready" ? "ready" : "insufficient"}><span>{DIMENSION_NAMES[key] ?? key}</span><strong>{item.score.toFixed(1)}</strong><div><i style={{width: `${item.coverage * 100}%`}}/></div><small>证据覆盖 {(item.coverage * 100).toFixed(0)}% · {item.status === "ready" ? "可评价" : "数据不足"}</small></article>)}</div>
      <div className="product-module-grid"><article className="product-module-card"><h2>风险与预警</h2>{[...(data.risk?.hard_reasons ?? []), ...(data.risk?.warnings ?? [])].map(item => <div className="module-missing" key={item}>{item}</div>)}{!(data.risk?.hard_reasons.length || data.risk?.warnings.length) && <p>当前已映射风险项未触发。</p>}</article><article className="product-module-card"><h2>证据时间</h2>{data.evidence?.map(item => <div className="module-output" key={item.api}><i />{item.api} · {item.period} · {item.available_time?.slice(0, 16)}</div>)}</article><article className="product-module-card"><h2>尚未纳入</h2>{data.limitations?.map(item => <div className="module-missing" key={item}>{item}</div>)}</article></div>
    </>}
  </section>;
}
