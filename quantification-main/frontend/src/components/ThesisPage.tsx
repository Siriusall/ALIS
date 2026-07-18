import { useEffect, useMemo, useState } from "react";
import {
  createThesis,
  fetchTheses,
  reviewDueTheses,
  reviewThesis,
  updateHypothesis,
  type InvestmentThesis,
} from "../api/research";
import { useStockContext } from "../context/StockContext";

const STATE_NAME: Record<string, string> = {
  draft: "草稿",
  watch: "观察",
  valid: "成立",
  warning: "预警",
  invalidated: "证伪",
  unverified: "未验证",
  verified: "已验证",
  failed: "失败",
};

const METRICS = [
  { value: "revenue_growth", label: "收入同比" },
  { value: "core_profit_growth", label: "净利润同比" },
  { value: "cash_conversion", label: "经营现金/利润" },
  { value: "debt_to_assets", label: "资产负债率" },
];

export default function ThesisPage() {
  const { activeStock, setActiveStock } = useStockContext();
  const [items, setItems] = useState<InvestmentThesis[]>([]);
  const [code, setCode] = useState(activeStock);
  const [title, setTitle] = useState("");
  const [core, setCore] = useState("");
  const [hypothesis, setHypothesis] = useState("");
  const [metric, setMetric] = useState("revenue_growth");
  const [minValue, setMinValue] = useState("");
  const [maxValue, setMaxValue] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [reviewingDue, setReviewingDue] = useState(false);

  async function refresh() {
    try {
      setItems(await fetchTheses());
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function submit() {
    const normalized = code.trim().toUpperCase();
    if (!normalized || !title.trim() || !hypothesis.trim()) {
      setError("股票代码、逻辑标题和至少一个必要条件不能为空");
      return;
    }
    setCreating(true);
    setError("");
    try {
      const expected: Record<string, number> = {};
      if (minValue !== "") expected.min = Number(minValue);
      if (maxValue !== "") expected.max = Number(maxValue);
      await createThesis({
        ts_code: normalized,
        title: title.trim(),
        core_thesis: core.trim(),
        hypotheses: [{ statement: hypothesis.trim(), metric, expected, necessary: true }],
      });
      setActiveStock(normalized);
      setCode(normalized);
      setTitle("");
      setCore("");
      setHypothesis("");
      setMinValue("");
      setMaxValue("");
      setNotice("已建立投资逻辑卡片，后续事件和财务事实会触发复核。");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setCreating(false);
    }
  }

  async function change(hypothesisId: number, status: string) {
    try {
      const updated = await updateHypothesis(hypothesisId, status);
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function autoReview(id: number) {
    try {
      const updated = await reviewThesis(id);
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function reviewDue() {
    setReviewingDue(true);
    setError("");
    try {
      const result = await reviewDueTheses();
      setNotice(`已复核 ${result.reviewed.length}/${result.due} 条到期投资逻辑`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setReviewingDue(false);
    }
  }

  const warningCount = useMemo(() => items.filter((item) => ["warning", "invalidated"].includes(item.derived_status)).length, [items]);
  const activeCount = useMemo(() => items.filter((item) => !["invalidated"].includes(item.derived_status)).length, [items]);

  return (
    <section className="thesis-page">
      <div className="product-module-head">
        <div>
          <span>INVESTMENT THESIS</span>
          <h1>投资逻辑与可证伪条件</h1>
          <p>逻辑必须能被验证，也必须能被推翻。</p>
        </div>
        <div>
          <button className="button ghost" onClick={() => void reviewDue()} disabled={reviewingDue}>
            {reviewingDue ? "到期复核中…" : "复核到期逻辑"}
          </button>
          <span className="module-state module-state-building">事实层已启用</span>
        </div>
      </div>

      {error && <div className="alert">{error}</div>}
      {notice && <div className="decision-issue warning">{notice}</div>}

      <div className="decision-command-bar" aria-label="投资逻辑处理顺序">
        <div className="decision-command-intro">
          <span>THESIS LOOP</span>
          <strong>每个核心候选都要能被证伪</strong>
        </div>
        <button type="button">
          <span>01 · 逻辑卡片</span>
          <strong>{items.length}</strong>
          <em>{activeCount} 条跟踪中</em>
        </button>
        <button type="button" className={warningCount > 0 ? "urgent" : ""}>
          <span>02 · 预警/证伪</span>
          <strong>{warningCount}</strong>
        </button>
        <button type="button">
          <span>03 · 当前标的</span>
          <strong>{code || "未选择"}</strong>
        </button>
        <button type="button">
          <span>04 · 到期复核</span>
          <strong>{reviewingDue ? "运行中" : "手动触发"}</strong>
        </button>
      </div>

      <div className="thesis-create">
        <h2>建立逻辑卡片</h2>
        <div className="thesis-form">
          <input placeholder="股票代码 000001.SZ" value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} />
          <input placeholder="逻辑标题" value={title} onChange={(event) => setTitle(event.target.value)} />
          <textarea placeholder="核心逻辑" value={core} onChange={(event) => setCore(event.target.value)} />
          <textarea placeholder="必要条件：失败时置信度最高40分" value={hypothesis} onChange={(event) => setHypothesis(event.target.value)} />
          <select value={metric} onChange={(event) => setMetric(event.target.value)}>
            {METRICS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <input type="number" placeholder="失效下限（可选）" value={minValue} onChange={(event) => setMinValue(event.target.value)} />
          <input type="number" placeholder="失效上限（可选）" value={maxValue} onChange={(event) => setMaxValue(event.target.value)} />
          <button onClick={() => void submit()} disabled={creating}>{creating ? "创建中…" : "创建逻辑"}</button>
        </div>
      </div>

      <div className="thesis-list">
        {items.length === 0 && (
          <div className="research-empty">
            <strong>还没有投资逻辑卡片</strong>
            <span>建立后系统会持续跟踪必要条件。</span>
          </div>
        )}
        {items.map((item) => (
          <article className="thesis-card" key={item.id}>
            <header>
              <div>
                <span>{item.ts_code} · {item.thesis_type}</span>
                <h2>{item.title}</h2>
                <p>{item.core_thesis || "未填写核心逻辑"}</p>
              </div>
              <div>
                <strong>{STATE_NAME[item.derived_status] ?? item.derived_status}</strong>
                <em>置信度 {item.derived_confidence.toFixed(1)}</em>
                <button className="button ghost" onClick={() => void autoReview(item.id)}>自动复核</button>
              </div>
            </header>
            <div className="thesis-hypotheses">
              {item.hypotheses.map((hypothesisItem) => (
                <div key={hypothesisItem.id}>
                  <span className={hypothesisItem.necessary ? "necessary" : ""}>{hypothesisItem.necessary ? "必要" : "普通"}</span>
                  <strong>{hypothesisItem.statement}</strong>
                  <em>{hypothesisItem.metric || "未绑定指标"} · {STATE_NAME[hypothesisItem.status] ?? hypothesisItem.status}</em>
                  <select value={hypothesisItem.status} onChange={(event) => void change(hypothesisItem.id, event.target.value)}>
                    <option value="unverified">未验证</option>
                    <option value="verified">已验证</option>
                    <option value="warning">预警</option>
                    <option value="failed">失败</option>
                    <option value="invalidated">证伪</option>
                  </select>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
