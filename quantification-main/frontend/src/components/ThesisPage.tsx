import { useEffect, useMemo, useState } from "react";
import {
  fetchTheses,
  generateThesisForStock,
  generateWatchPoolTheses,
  reviewDueTheses,
  reviewThesis,
  type InvestmentThesis,
  type ThesisGenerationResponse,
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

function statusTone(value: string) {
  if (["valid", "verified"].includes(value)) return "ready";
  if (["warning", "failed"].includes(value)) return "partial";
  if (["invalidated"].includes(value)) return "blocked";
  return "building";
}

function renderResult(result: ThesisGenerationResponse) {
  if (!result.ok) return `${result.ts_code}：${result.reason || result.status}`;
  if (result.created) return `${result.ts_code}：已生成并复核`;
  if (result.status === "reviewed_existing") return `${result.ts_code}：已复核既有逻辑`;
  return `${result.ts_code}：${result.status}`;
}

export default function ThesisPage() {
  const { activeStock, setActiveStock } = useStockContext();
  const [items, setItems] = useState<InvestmentThesis[]>([]);
  const [code, setCode] = useState(activeStock);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyId, setBusyId] = useState<number | null>(null);
  const [generatingStock, setGeneratingStock] = useState(false);
  const [generatingWatch, setGeneratingWatch] = useState(false);
  const [reviewingDue, setReviewingDue] = useState(false);

  async function refresh() {
    try {
      const data = await fetchTheses();
      setItems(data);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function generateOne() {
    const normalized = code.trim().toUpperCase();
    if (!normalized) {
      setError("请输入股票代码");
      return;
    }
    setGeneratingStock(true);
    setError("");
    try {
      const result = await generateThesisForStock(normalized);
      setActiveStock(normalized);
      setCode(normalized);
      setNotice(renderResult(result));
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setGeneratingStock(false);
    }
  }

  async function generateWatchPool() {
    setGeneratingWatch(true);
    setError("");
    try {
      const result = await generateWatchPoolTheses(100);
      const count = result.generated_or_reviewed?.length ?? 0;
      setNotice(`观察池已处理 ${count}/${result.candidates ?? 0} 个候选`);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setGeneratingWatch(false);
    }
  }

  async function autoReview(id: number) {
    setBusyId(id);
    setError("");
    try {
      const updated = await reviewThesis(id);
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyId(null);
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
          <span>SYSTEM THESIS CONTROL</span>
          <h1>投资逻辑中控台</h1>
          <p>系统自动生成、自动复核和自动展示；你只在最终确认时介入。</p>
        </div>
        <div className="thesis-toolbar">
          <button className="button ghost" onClick={() => void generateWatchPool()} disabled={generatingWatch}>
            {generatingWatch ? "生成观察池中…" : "批量生成观察池"}
          </button>
          <button className="button ghost" onClick={() => void reviewDue()} disabled={reviewingDue}>
            {reviewingDue ? "到期复核中…" : "复核到期逻辑"}
          </button>
          <span className="module-state module-state-building">系统自动化已启用</span>
        </div>
      </div>

      {error && <div className="alert">{error}</div>}
      {notice && <div className="decision-issue warning">{notice}</div>}

      <div className="decision-command-bar" aria-label="投资逻辑中控">
        <div className="decision-command-intro">
          <span>THESIS LOOP</span>
          <strong>系统先生成证据，再做复核</strong>
        </div>
        <button type="button" onClick={() => void generateOne()} className={generatingStock ? "urgent" : ""}>
          <span>01 · 单股生成</span>
          <strong>{generatingStock ? "运行中" : code || "输入代码"}</strong>
          <em>自动生成可证伪逻辑</em>
        </button>
        <button type="button" className={warningCount > 0 ? "urgent" : ""}>
          <span>02 · 预警/证伪</span>
          <strong>{warningCount}</strong>
        </button>
        <button type="button">
          <span>03 · 有效逻辑</span>
          <strong>{activeCount}</strong>
        </button>
        <button type="button" onClick={() => void refresh()}>
          <span>04 · 刷新状态</span>
          <strong>{items.length}</strong>
          <em>重新读取当前中控</em>
        </button>
      </div>

      <div className="thesis-shell">
        <section className="thesis-summary">
          <h2>系统生成与复核入口</h2>
          <div className="thesis-actions">
            <input
              placeholder="股票代码 000001.SZ"
              value={code}
              onChange={(event) => setCode(event.target.value.toUpperCase())}
              onKeyDown={(event) => event.key === "Enter" && void generateOne()}
            />
            <button onClick={() => void generateOne()} disabled={generatingStock}>
              {generatingStock ? "生成中…" : "系统生成当前逻辑"}
            </button>
          </div>
          <div className="thesis-metrics">
            <article><span>逻辑总数</span><strong>{items.length}</strong></article>
            <article><span>有效逻辑</span><strong>{activeCount}</strong></article>
            <article><span>预警/证伪</span><strong>{warningCount}</strong></article>
            <article><span>当前关注</span><strong>{code || "未选择"}</strong></article>
          </div>
          <p className="text-muted">逻辑卡只展示系统结论、证据、反证和下次复核时间，手工编辑已移出此页。</p>
        </section>

        <section className="thesis-summary">
          <h2>字段合同</h2>
          <div className="thesis-contract-grid">
            {[
              ["功能仓", "系统把逻辑绑定到对应收益角色和风险角色"],
              ["收益来源", "说明主要靠什么兑现"],
              ["持有周期", "说明判断兑现需要多久"],
              ["叙事生命周期", "说明主线处在萌芽、扩散还是退潮"],
              ["资金确认", "说明资金和价格是否支持"],
              ["退出条件", "说明什么证据会触发退出"],
              ["数据状态", "说明证据是完整、部分还是缺失"],
            ].map(([name, detail]) => (
              <article key={name}>
                <span>{name}</span>
                <strong>{detail}</strong>
              </article>
            ))}
          </div>
        </section>
      </div>

      <div className="thesis-list">
        {items.length === 0 && (
          <div className="research-empty">
            <strong>还没有系统逻辑卡</strong>
            <span>先用上方入口生成一只股票或批量生成观察池。</span>
          </div>
        )}
        {items.map((item) => (
          <article className="thesis-card" key={item.id}>
            <header>
              <div>
                <span>{item.ts_code} · {item.thesis_type}</span>
                <h2>{item.title}</h2>
                <p>{item.core_thesis || "未生成核心逻辑"}</p>
              </div>
              <div>
                <strong>{STATE_NAME[item.derived_status] ?? item.derived_status}</strong>
                <em>置信度 {item.derived_confidence.toFixed(1)}</em>
                <button className="button ghost" onClick={() => void autoReview(item.id)} disabled={busyId === item.id}>
                  {busyId === item.id ? "复核中…" : "自动复核"}
                </button>
              </div>
            </header>
            <div className="thesis-meta">
              <span>下次复核：{item.next_review_at?.slice(0, 10) || "未设置"}</span>
              <span className={`module-state module-state-${statusTone(item.derived_status)}`}>系统判定：{STATE_NAME[item.derived_status] ?? item.derived_status}</span>
            </div>
            <div className="thesis-hypotheses">
              {item.hypotheses.map((hypothesisItem) => (
                <div key={hypothesisItem.id}>
                  <span className={hypothesisItem.necessary ? "necessary" : ""}>{hypothesisItem.necessary ? "必要" : "普通"}</span>
                  <strong>{hypothesisItem.statement}</strong>
                  <em>{hypothesisItem.metric || "未绑定指标"} · {STATE_NAME[hypothesisItem.status] ?? hypothesisItem.status}</em>
                  <div>{hypothesisItem.evidence?.length ? `${hypothesisItem.evidence.length} 条证据` : "无历史证据"}</div>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
