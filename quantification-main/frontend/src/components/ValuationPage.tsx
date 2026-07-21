import { useState } from "react";
import { fetchValuation, type ValuationState } from "../api/research";
import { useStockContext } from "../context/StockContext";
import { formatGmt8Time } from "../utils/time";

function margin(value: number, price?: number) {
  return price ? `安全边际 ${((value / price - 1) * 100).toFixed(1)}%` : "—";
}

function pct(value: number | undefined | null) {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export default function ValuationPage() {
  const { activeStock, setActiveStock } = useStockContext();
  const [code, setCode] = useState(activeStock);
  const [data, setData] = useState<ValuationState | null>(null);
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
      const result = await fetchValuation(normalized);
      setData(result);
      setError(result.ok ? "" : result.reason ?? "估值数据不足");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  const baseMargin = data?.scenarios?.base != null ? margin(data.scenarios.base, data.current_price) : "—";

  return (
    <section className="valuation-page">
      <div className="product-module-head">
        <div>
          <h1>系统估值判断</h1>
          <p>系统用当前价格、历史分位和同行证据判断安全边际。</p>
        </div>
        <button className="button ghost" onClick={() => void run()} disabled={loading}>
          {loading ? "计算中…" : "刷新估值"}
        </button>
      </div>

      <div className="stock-research-search">
        <input value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} placeholder="输入股票代码" />
        <button onClick={() => void run()} disabled={loading}>{loading ? "计算中…" : "查看系统估值"}</button>
      </div>

      {error && <div className="alert">{error}</div>}

      {data?.ok && data.scenarios && (
        <>
          <div className="decision-command-bar" aria-label="估值使用规则">
            <div className="decision-command-intro">
              <span>估值规则</span>
              <strong>{data.ts_code} 的价格纪律</strong>
            </div>
            <button type="button">
              <span>01 · 当前价</span>
              <strong>{data.current_price?.toFixed(2) ?? "—"}</strong>
              <em>{data.trade_date || "无交易日"}</em>
            </button>
            <button type="button">
              <span>02 · 基准情景</span>
              <strong>{data.scenarios.base.toFixed(2)}</strong>
              <em>{baseMargin}</em>
            </button>
            <button type="button">
              <span>03 · 历史分位</span>
              <strong>{pct(data.history_percentile)}</strong>
              <em>自身位置</em>
            </button>
            <button type="button">
              <span>04 · 行业分位</span>
              <strong>{pct(data.industry_percentile)}</strong>
              <em>同行位置</em>
            </button>
          </div>

          <div className="valuation-hero">
            <div><span>当前价格</span><strong>{data.current_price?.toFixed(2)}</strong><em>{data.trade_date}</em></div>
            <div className="bear"><span>悲观价值</span><strong>{data.scenarios.bear.toFixed(2)}</strong><em>{margin(data.scenarios.bear, data.current_price)}</em></div>
            <div className="base"><span>基准价值</span><strong>{data.scenarios.base.toFixed(2)}</strong><em>{margin(data.scenarios.base, data.current_price)}</em></div>
            <div className="bull"><span>乐观价值</span><strong>{data.scenarios.bull.toFixed(2)}</strong><em>{margin(data.scenarios.bull, data.current_price)}</em></div>
          </div>

          <div className="product-module-grid">
            <article className="product-module-card">
              <h2>相对位置</h2>
              <div className="module-output"><i />自身历史分位 {pct(data.history_percentile)}</div>
              <div className="module-output"><i />行业分位 {pct(data.industry_percentile)}</div>
              <p>{data.method}</p>
            </article>
            <article className="product-module-card">
              <h2>证据范围</h2>
              <div className="module-output"><i />历史 {data.evidence?.history_points ?? 0} 个交易日</div>
              <div className="module-output"><i />同行 {data.evidence?.peer_count ?? 0} 家</div>
              <p>{formatGmt8Time(data.evidence?.available_time)}</p>
            </article>
            <article className="product-module-card">
              <h2>限制</h2>
              {data.limitations?.length ? data.limitations.map((item) => <div className="module-missing" key={item}>{item}</div>) : <p>暂无估值限制。</p>}
            </article>
          </div>
        </>
      )}

      {data?.ok && !data.scenarios && (
        <div className="research-empty">
          <strong>暂不输出目标价值</strong>
          <span>{data.reason || "历史或同行证据不足"}</span>
        </div>
      )}
    </section>
  );
}
