import { useEffect, useMemo, useState } from "react";
import { fetchResearchEvents, reviewLinkedEventTheses, type ResearchEvent } from "../api/research";
import { useStockContext } from "../context/StockContext";

const TYPES: Record<string, string> = {
  regulatory_risk: "监管风险",
  shareholder_reduction: "股东减持",
  capital_operation: "资本运作",
  earnings: "业绩",
  pledge: "质押",
  repurchase: "回购",
  major_contract: "重大合同",
};

function directionText(value: string) {
  const map: Record<string, string> = { positive: "正面", negative: "负面", neutral: "中性" };
  return map[value] || value || "—";
}

export default function EventCenterPage() {
  const { activeStock, setActiveStock } = useStockContext();
  const [items, setItems] = useState<ResearchEvent[]>([]);
  const [code, setCode] = useState(activeStock);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const [loading, setLoading] = useState(false);

  async function load(value = code.trim().toUpperCase()) {
    setLoading(true);
    try {
      if (value) {
        setActiveStock(value);
        setCode(value);
      }
      setItems(await fetchResearchEvents(value));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function reviewLinked() {
    setReviewing(true);
    try {
      const result = await reviewLinkedEventTheses();
      setNotice(`已复核 ${result.reviewed.length}/${result.candidates} 个关联投资逻辑`);
      await load(code.trim().toUpperCase());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setReviewing(false);
    }
  }

  useEffect(() => {
    void load("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const urgentCount = useMemo(() => items.filter((item) => item.follow_up.status === "urgent").length, [items]);
  const linkedCount = useMemo(() => items.filter((item) => item.thesis_link).length, [items]);
  const negativeCount = useMemo(() => items.filter((item) => item.impact_direction === "negative").length, [items]);

  return (
    <section className="event-center">
      <div className="product-module-head">
        <div>
          <span>EVENT ENGINE</span>
          <h1>系统事件判断</h1>
          <p>系统根据公告事件更新投资逻辑和风险判断，最终结论进入复核。</p>
        </div>
        <button className="button ghost" disabled={reviewing} onClick={() => void reviewLinked()}>
          {reviewing ? "计算中…" : "重新计算事件影响"}
        </button>
      </div>

      <div className="stock-research-search">
        <input value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} placeholder="按股票代码过滤，例如 000001.SZ" />
        <button onClick={() => void load(code.trim().toUpperCase())} disabled={loading}>
          {loading ? "查询中…" : "查询事件"}
        </button>
      </div>

      {notice && <div className="decision-issue warning">{notice}</div>}
      {error && <div className="alert">{error}</div>}

      <div className="decision-command-bar" aria-label="事件处理顺序">
        <div className="decision-command-intro">
          <span>EVENT REVIEW</span>
          <strong>系统把事件影响同步到投资逻辑</strong>
        </div>
        <button type="button" className={urgentCount > 0 ? "urgent" : ""}>
          <span>01 · 紧急跟进</span>
          <strong>{urgentCount}</strong>
        </button>
        <button type="button" className={negativeCount > 0 ? "active" : ""}>
          <span>02 · 负面事件</span>
          <strong>{negativeCount}</strong>
        </button>
        <button type="button">
          <span>03 · 关联逻辑</span>
          <strong>{linkedCount}</strong>
        </button>
        <button type="button">
          <span>04 · 事件总数</span>
          <strong>{items.length}</strong>
          <em>{code ? code : "全市场"}</em>
        </button>
      </div>

      <div className="event-list">
        {items.map((item) => (
          <article className={`event-card event-${item.impact_direction}`} key={item.id}>
            <div className="event-score">
              <strong>{item.impact.decayed_score.toFixed(1)}</strong>
              <span>当前影响</span>
              <em>初始 {item.impact.score.toFixed(1)}</em>
            </div>
            <div className="event-copy">
              <div>
                <span>{item.ts_code} · {TYPES[item.event_type] ?? item.event_type}</span>
                <i>{item.severity} · {directionText(item.impact_direction)}</i>
              </div>
              <h2>{item.title}</h2>
              <p>{item.summary || item.evidence.excerpt || "暂无摘要"}</p>
              <div className={`decision-issue ${item.follow_up.status === "urgent" ? "error" : "warning"}`}>
                {item.follow_up.next_action}
                {item.thesis_link ? ` · 关联逻辑：${item.thesis_link.title}（${item.thesis_link.status}）` : " · 尚未关联投资逻辑"}
              </div>
              <footer>
                <span>{item.event_date || item.evidence.available_time?.slice(0, 10)}</span>
                <span>半衰期 {item.impact.half_life_days} 天 · 已过 {item.impact.age_days} 天</span>
                {item.evidence.url && <a href={item.evidence.url} target="_blank" rel="noreferrer">查看原文证据</a>}
              </footer>
            </div>
          </article>
        ))}
        {!items.length && !error && (
          <div className="research-empty">
            <strong>{loading ? "正在查询事件" : "没有匹配事件"}</strong>
            <span>可清空股票代码查看全局事件，或在单股研究页进入当前标的事件。</span>
          </div>
        )}
      </div>
    </section>
  );
}
