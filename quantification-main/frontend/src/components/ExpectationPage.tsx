import { useState } from "react";
import { fetchExpectation, type ExpectationState } from "../api/research";

const LABELS: Record<string, string> = {
  revenue_yoy: "营收同比", profit_yoy: "利润同比", previous_revenue_yoy: "上期营收同比",
  previous_profit_yoy: "上期利润同比", growth_acceleration: "增长加速度",
  net_margin_change: "净利率变化", forecast_revision: "预告变化",
};

export default function ExpectationPage() {
  const [code, setCode] = useState("");
  const [data, setData] = useState<ExpectationState | null>(null);
  const [error, setError] = useState("");
  async function run() {
    if (!code.trim()) return setError("请输入股票代码");
    try { const result = await fetchExpectation(code.trim().toUpperCase()); setData(result); setError(result.ok ? "" : result.reason ?? "数据不足"); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }
  const forecast = data?.external_forecast;
  return <section className="expectation-page">
    <div className="product-module-head"><div><span>EXPECTATION CHANGE</span><h1>预期变化中心</h1><p>比较财务趋势、版本化基线和公司业绩预告；业绩预告不会被误标为分析师一致预期。</p></div></div>
    <div className="stock-research-search"><input value={code} onChange={event => setCode(event.target.value)} placeholder="输入股票代码"/><button onClick={() => void run()}>分析预期</button></div>
    {error && <div className="alert">{error}</div>}
    {data?.ok && <><div className="expectation-metrics">{Object.entries(data.observations ?? {}).map(([key, value]) => <article key={key}><span>{LABELS[key] ?? key}</span><strong>{value == null ? "—" : `${value.toFixed(2)}%`}</strong><small>{value == null ? "证据不足" : "真实财务/公告计算"}</small></article>)}</div>
      <div className="product-module-grid">
        <article className="product-module-card"><h2>预期维度</h2><p>得分 {data.dimension?.score.toFixed(1)} · 覆盖 {((data.dimension?.coverage ?? 0) * 100).toFixed(0)}%</p>{data.dimension?.missing.map(item => <div className="module-missing" key={item}>{item}</div>)}</article>
        <article className="product-module-card"><h2>系统预测基线</h2>{data.baseline ? <><p>{data.baseline.target_period} · 置信度 {(data.baseline.confidence * 100).toFixed(0)}%</p><div className="module-output"><i/>营收 {formatMoney(data.baseline.revenue_forecast)}</div><div className="module-output"><i/>利润 {formatMoney(data.baseline.profit_forecast)}</div><small>{data.baseline.model_version}</small></> : <p>可比财务期不足，未生成预测。</p>}</article>
        <article className="product-module-card"><h2>公司业绩预告</h2><p>{forecast?.status === "current" ? "可作为当前研究证据" : forecast?.status === "stale" ? "仅历史证据，不进入当前门控" : forecast?.status === "unscorable" ? "仅展示证据：缺财务报表，不能评分" : "尚未同步"}</p>{forecast?.latest ? <><div className="module-output"><i/>期间 {forecast.latest.period}</div><div className="module-output"><i/>预告同比 {forecast.latest.p_change_min ?? "—"}% ~ {forecast.latest.p_change_max ?? "—"}%</div><small>披露日 {forecast.latest.ann_date}；不是一致预期</small></> : <p>没有公司业绩预告记录。</p>}</article>
        <article className="product-module-card"><h2>限制</h2>{data.limitations?.map(item => <div className="module-missing" key={item}>{item}</div>)}</article>
      </div></>}
  </section>;
}

function formatMoney(value: number) { return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(value); }
