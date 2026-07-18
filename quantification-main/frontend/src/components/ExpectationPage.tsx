import { useState } from "react";
import { fetchExpectation, type ExpectationState } from "../api/research";
import { useStockContext } from "../context/StockContext";

const LABELS: Record<string, string> = {
  revenue_yoy: "营收同比",
  profit_yoy: "利润同比",
  previous_revenue_yoy: "上期营收同比",
  previous_profit_yoy: "上期利润同比",
  growth_acceleration: "增长加速度",
  net_margin_change: "净利率变化",
  forecast_revision: "预告变化",
};

function formatMoney(value: number) {
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(value);
}

function pct(value: number | undefined | null, digits = 0) {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function observationText(value: number | null | undefined) {
  return value == null ? "—" : `${value.toFixed(2)}%`;
}

function forecastStatus(value: string | undefined) {
  if (value === "current") return "当前证据";
  if (value === "stale") return "历史证据";
  if (value === "unscorable") return "仅展示";
  return "尚未同步";
}

export default function ExpectationPage() {
  const { activeStock, setActiveStock } = useStockContext();
  const [code, setCode] = useState(activeStock);
  const [data, setData] = useState<ExpectationState | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function run(input = code) {
    const normalized = input.trim().toUpperCase();
    if (!normalized) {
      setError("请输入股票代码");
      return;
    }
    setLoading(true);
    try {
      setCode(normalized);
      setActiveStock(normalized);
      const result = await fetchExpectation(normalized);
      setData(result);
      setError(result.ok ? "" : result.reason ?? "数据不足");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  const forecast = data?.external_forecast;
  const latest = forecast?.latest;

  return (
    <section className="expectation-page">
      <div className="product-module-head">
        <div>
          <span>EXPECTATION CHANGE</span>
          <h1>预期变化与业绩线索</h1>
          <p>先看真实经营，再看预期有没有偏。</p>
        </div>
        <button className="button ghost" onClick={() => void run()} disabled={loading}>
          {loading ? "分析中…" : "刷新预期"}
        </button>
      </div>

      <div className="stock-research-search">
        <input value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} placeholder="输入股票代码" />
        <button onClick={() => void run()} disabled={loading}>{loading ? "分析中…" : "分析预期"}</button>
      </div>

      {error && <div className="alert">{error}</div>}

      {data?.ok && (
        <>
          <div className="decision-command-bar" aria-label="预期判断顺序">
            <div className="decision-command-intro">
              <span>EXPECTATION RULE</span>
              <strong>{data.ts_code} 的预期证据</strong>
            </div>
            <button type="button">
              <span>01 · 财务期</span>
              <strong>{data.latest_period || "—"}</strong>
            </button>
            <button type="button">
              <span>02 · 维度覆盖</span>
              <strong>{pct(data.dimension?.coverage)}</strong>
              <em>{data.dimension?.status || "—"}</em>
            </button>
            <button type="button">
              <span>03 · 系统基线</span>
              <strong>{data.baseline ? data.baseline.target_period : "未生成"}</strong>
              <em>{data.baseline ? `置信度 ${pct(data.baseline.confidence)}` : "可比期不足"}</em>
            </button>
            <button type="button">
              <span>04 · 公司预告</span>
              <strong>{forecastStatus(forecast?.status)}</strong>
              <em>公司披露</em>
            </button>
          </div>

          <div className="expectation-metrics">
            {Object.entries(data.observations ?? {}).map(([key, value]) => (
              <article key={key}>
                <span>{LABELS[key] ?? key}</span>
                <strong>{observationText(value)}</strong>
                <em>{value == null ? "证据不足" : "财务/公告"}</em>
              </article>
            ))}
          </div>

          <div className="product-module-grid">
            <article className="product-module-card">
              <h2>预期维度</h2>
              <p>得分 {data.dimension?.score.toFixed(1) ?? "—"} · 覆盖 {pct(data.dimension?.coverage)}</p>
              {data.dimension?.missing.map((item) => <div className="module-missing" key={item}>{item}</div>)}
            </article>
            <article className="product-module-card">
              <h2>系统预测基线</h2>
              {data.baseline ? (
                <>
                  <p>{data.baseline.target_period} · 置信度 {pct(data.baseline.confidence)}</p>
                  <div className="module-output"><i />营收 {formatMoney(data.baseline.revenue_forecast)}</div>
                  <div className="module-output"><i />利润 {formatMoney(data.baseline.profit_forecast)}</div>
                  <em>{data.baseline.model_version}</em>
                </>
              ) : (
                <p>可比财务期不足，未生成预测。</p>
              )}
            </article>
            <article className="product-module-card">
              <h2>公司业绩预告</h2>
              <p>{forecastStatus(forecast?.status)}</p>
              {latest ? (
                <>
                  <div className="module-output"><i />期间 {latest.period}</div>
                  <div className="module-output"><i />预告同比 {latest.p_change_min ?? "—"}% ~ {latest.p_change_max ?? "—"}%</div>
                  <em>披露日 {latest.ann_date}</em>
                </>
              ) : (
                <p>没有公司业绩预告记录。</p>
              )}
            </article>
            <article className="product-module-card">
              <h2>限制</h2>
              {data.limitations?.length ? data.limitations.map((item) => <div className="module-missing" key={item}>{item}</div>) : <p>暂无预期限制。</p>}
            </article>
          </div>
        </>
      )}
    </section>
  );
}
