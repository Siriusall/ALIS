import { useEffect, useMemo, useState } from "react";
import { fetchIndustries, fetchIndustryAllocation, type IndustryAllocation, type IndustryResearchItem } from "../api/research";

export default function IndustryResearchPage() {
  const [items, setItems] = useState<IndustryResearchItem[]>([]);
  const [allocation, setAllocation] = useState<IndustryAllocation | null>(null);
  const [error, setError] = useState("");
  const [asOfTime, setAsOfTime] = useState("");
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<"ready" | "all" | "insufficient">("ready");

  async function load(value = asOfTime) {
    setLoading(true);
    try {
      const pointInTime = value ? `${value}T23:59:59` : "";
      const [research, budget] = await Promise.all([fetchIndustries(pointInTime), fetchIndustryAllocation(pointInTime)]);
      setItems(research);
      setAllocation(budget);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load("");
    // Initial latest-state load only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const visibleItems = useMemo(() => items.filter((item) => {
    const matchesQuery = !query.trim() || `${item.industry_code} ${item.industry_name}`.toLowerCase().includes(query.trim().toLowerCase());
    const matchesState = stateFilter === "all" || (stateFilter === "ready" ? item.state.status === "ready" : item.state.status !== "ready");
    return matchesQuery && matchesState;
  }), [items, query, stateFilter]);

  return (
    <section className="industry-page">
      <div className="product-module-head"><div><span>INDUSTRY RESEARCH</span><h1>行业研究中心</h1><p>只看证据够不够、结论稳不稳。</p></div></div>
      <div className="stock-research-search workspace-search">
        <input aria-label="行业状态历史日期" type="date" value={asOfTime} onInput={(event) => setAsOfTime(event.currentTarget.value)} onChange={(event) => setAsOfTime(event.target.value)} />
        <button type="button" onClick={() => void load()} disabled={loading}>{loading ? "正在回放…" : asOfTime ? "读取历史时点" : "读取最新状态"}</button>
        {asOfTime && <button type="button" className="button ghost" onClick={() => { setAsOfTime(""); void load(""); }}>回到最新</button>}
      </div>
      {allocation && <section className="industry-allocation-panel">
        <div className="product-module-head"><div><span>MARKET → INDUSTRY → STOCK</span><h2>今日行业预算</h2><p>市场状态 {allocation.market_state} · 股票预算 {(allocation.requested_equity_budget * 100).toFixed(1)}% · 实际分配 {(allocation.allocated_equity_budget * 100).toFixed(1)}%，未分配部分进入防御资产。</p></div></div>
        <div className="industry-budget-grid">
          {allocation.items.filter((item) => item.target_weight > 0).map((item) => <article key={item.industry}>
            <header><strong>{item.industry}</strong><em>{(item.target_weight * 100).toFixed(1)}%</em></header>
            <p>叙事 {item.narrative_score.toFixed(1)} · {item.narrative_lifecycle} · 基本面景气 {item.raw_score.toFixed(1)}</p>
            {!!item.candidates.length && <small>候选：{item.candidates.slice(0, 4).map((x) => `${x.name}(${x.symbol})`).join("、")}</small>}
          </article>)}
          {!allocation.items.some((item) => item.target_weight > 0) && <div className="research-empty"><strong>当前没有行业通过预算门</strong><span>股票预算将全部转入防御资产，不会留成现金。</span></div>}
        </div>
      </section>}
      <div className="decision-command-bar" aria-label="行业研究筛选">
        <div className="decision-command-intro"><span>INDUSTRY FOCUS</span><strong>默认先看可评价行业</strong></div>
        <input aria-label="搜索行业" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索行业名称或代码" />
        <select aria-label="行业状态筛选" value={stateFilter} onChange={(event) => setStateFilter(event.target.value as typeof stateFilter)}><option value="ready">仅可评价</option><option value="insufficient">仅证据不足</option><option value="all">全部行业</option></select>
        <strong>{visibleItems.length} / {items.length}</strong>
      </div>
      {error && <div className="alert">{error}</div>}
      <div className="industry-list">
        {visibleItems.map((item) => (
          <article className="industry-card" key={item.industry_code}>
            <header><div><span>{item.industry_code}</span><h2>{item.industry_name}</h2></div><div className={`module-state module-state-${item.state.status === "ready" ? "ready" : "partial"}`}>{item.state.lifecycle}</div></header>
            <div className="industry-facts">
              <div><strong>{item.total_evidence_points}</strong><span>历史证据点</span></div>
              <div><strong>{item.company_count}</strong><span>映射公司</span></div>
              <div><strong>{(item.state.coverage * 100).toFixed(0)}%</strong><span>景气分量覆盖</span></div>
              <div><strong>{item.state.status === "ready" ? item.state.score.toFixed(1) : "—"}</strong><span>景气评分</span></div>
            </div>
            {item.state_as_of_date && <p className="text-muted">状态时点：{item.state_as_of_date} · {item.state_model_version || "历史代理模型"}</p>}
            {item.companies.length > 0 && <p className="text-muted">公司样本：{item.companies.slice(0, 4).map((x) => `${x.name}(${x.ts_code})`).join("、")}</p>}
            <div>{item.metrics.map((metric) => <div className="industry-metric" key={metric.metric_code}><strong>{metric.metric_name}</strong><span>{metric.frequency} · {metric.source_code || "来源未登记"}</span><em>{metric.value_count} 条 · {metric.first_date || "—"} 至 {metric.last_date || "—"}</em></div>)}</div>
            {item.limitations.map((value) => <p className="module-missing" key={value}>{value}</p>)}
          </article>
        ))}
        {!items.length && !error && <div className="research-empty"><strong>正在读取行业指标和公司映射</strong></div>}
        {!!items.length && !visibleItems.length && <div className="research-empty"><strong>当前筛选没有匹配行业</strong><span>可切换到“全部行业”继续查看。</span></div>}
      </div>
    </section>
  );
}
