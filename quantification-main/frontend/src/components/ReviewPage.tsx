import { useEffect, useMemo, useState } from "react";
import {
  createReview,
  fetchDecisionSnapshots,
  fetchReviewSuggestion,
  generateLlmReviewDraft,
  fetchReviews,
  type DecisionSnapshot,
  type ReviewRecord,
} from "../api/research";
import { fetchPerformanceReports, type PerformanceReport } from "../api/modelStrategy";
import { formatGmt8Time } from "../utils/time";

const ERROR_TYPES = ["无", "数据质量", "模型失效", "估值判断", "风险执行", "交易执行", "研究遗漏", "其他"];

function asText(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "number") return Number.isFinite(value) ? value.toFixed(4) : "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function pctText(value: unknown): string {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : "—";
}

function percentagePointsText(value: unknown): string {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : "—";
}

export default function ReviewPage() {
  const [decisions, setDecisions] = useState<DecisionSnapshot[]>([]);
  const [reviews, setReviews] = useState<ReviewRecord[]>([]);
  const [dailyReports, setDailyReports] = useState<PerformanceReport[]>([]);
  const [selectedId, setSelectedId] = useState<number | "">("");
  const [returnPct, setReturnPct] = useState("");
  const [attribution, setAttribution] = useState("");
  const [errorType, setErrorType] = useState("无");
  const [lessons, setLessons] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [llmDrafting, setLlmDrafting] = useState(false);
  const [llmMeta, setLlmMeta] = useState("");
  const [error, setError] = useState("");

  const reviewedIds = useMemo(
    () => new Set(reviews.map((item) => item.decision_snapshot_id).filter(Boolean)),
    [reviews],
  );
  const selected = decisions.find((item) => item.id === selectedId) ?? null;
  const awaiting = decisions.filter((item) => !reviewedIds.has(item.id));
  const latestReview = reviews[0] ?? null;
  const selectedOrFirstAwaiting = selected ?? awaiting[0] ?? null;

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [nextDecisions, nextReviews, performance] = await Promise.all([fetchDecisionSnapshots(), fetchReviews(), fetchPerformanceReports()]);
      setDecisions(nextDecisions);
      setReviews(nextReviews);
      setDailyReports(performance.items.filter((item) => item.period_type === "daily").slice(0, 20));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function suggestOutcome() {
    if (!selected) return;
    setSuggesting(true);
    setError("");
    try {
      const suggestion = await fetchReviewSuggestion(selected.id);
      if (!suggestion.ok || suggestion.outcome?.directional_return == null) {
        throw new Error(suggestion.reason || "后续价格数据不足，暂不能自动估算收益。");
      }
      setReturnPct((suggestion.outcome.directional_return * 100).toFixed(2));
      setAttribution((previous) => previous || "系统按决策后首个可用收盘价与最新可用收盘价估算方向收益；这不是完整已实现盈亏，需要人工补充分解。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSuggesting(false);
    }
  }

  async function generateDraft() {
    const target = selected ?? selectedOrFirstAwaiting;
    if (!target) { setError("请先选择一条待复核决策。"); return; }
    setSelectedId(target.id);
    setLlmDrafting(true);
    setError("");
    try {
      const result = await generateLlmReviewDraft(target.id);
      setErrorType(result.draft.error_type || "其他");
      setAttribution([
        result.draft.summary,
        result.draft.outcome_assessment,
        result.draft.attribution,
        ...result.draft.evidence_points.map((item) => `证据：${item}`),
      ].filter(Boolean).join("\n"));
      setLessons(result.draft.lessons || "");
      setLlmMeta(`${result.model} · 置信度 ${(result.draft.confidence * 100).toFixed(0)}%${result.draft.limitations.length ? ` · 限制：${result.draft.limitations.join("；")}` : ""}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLlmDrafting(false);
    }
  }

  async function submit() {
    if (!selected || !lessons.trim()) {
      setError("请选择一条冻结决策，并填写改进结论。");
      return;
    }
    const parsedReturn = Number(returnPct);
    if (returnPct && !Number.isFinite(parsedReturn)) {
      setError("收益率必须为数字。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await createReview({
        decision_snapshot_id: selected.id,
        outcome: {
          return_pct: returnPct ? parsedReturn / 100 : null,
          reviewed_decision: selected.decision_type,
        },
        attribution: { summary: attribution.trim() || "未拆分归因" },
        error_type: errorType === "无" ? undefined : errorType,
        lessons: lessons.trim(),
      });
      setReturnPct("");
      setAttribution("");
      setErrorType("无");
      setLessons("");
      setSelectedId("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="product-module-page review-page">
      <div className="product-module-head">
        <div>
          <h1>复盘归因与规则改进</h1>
          <p>先回放当时证据，再写归因和改进。</p>
        </div>
        <button className="button ghost" onClick={() => void load()} disabled={loading}>
          {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {error && <div className="alert">{error}</div>}

      <div className="card decision-panel">
        <div className="quant-analyze-section-head">
          <h3>你在复核什么？</h3>
          <em>这里仅处理历史决策复盘</em>
        </div>
        <div className="decision-issue warning">
          <strong>核心问题：系统当时的这次买入、卖出或持有判断，事后看是否正确？</strong>
          <br />
          先从待复核队列选择一条决策；系统可以估算后续收益，你再确认错误类型、补充归因，并写下以后应如何改进。
          <br />
          公告证据确认、模型上线审批、异常任务和数据质量问题不在本页处理，请返回“人工复核队列”进入对应模块。
        </div>
      </div>

      <div className="decision-grid decision-metrics">
        <div className="card metric-card">
          <div className="sub">冻结决策</div>
          <div className="metric">{decisions.length}</div>
        </div>
        <div className="card metric-card">
          <div className="sub">待复盘</div>
          <div className="metric">{awaiting.length}</div>
        </div>
        <div className="card metric-card">
          <div className="sub">已记录复盘</div>
          <div className="metric">{reviews.length}</div>
        </div>
        <div className="card metric-card">
          <div className="sub">最近错误类型</div>
          <div className="metric">{latestReview?.error_type || "—"}</div>
        </div>
        <div className="card metric-card">
          <div className="sub">日度绩效快照</div>
          <div className="metric">{dailyReports.length}</div>
        </div>
      </div>

      <div className="decision-command-bar" aria-label="复盘流程">
        <div className="decision-command-intro">
          <span>复盘顺序</span>
          <strong>先回放证据，再写改进</strong>
        </div>
        <button type="button" className={awaiting.length > 0 ? "urgent" : ""}>
          <span>01 · 待复盘</span>
          <strong>{awaiting.length}</strong>
        </button>
        <button type="button">
          <span>02 · 收益归因</span>
          <strong>{selected ? selected.ts_code : "选择决策"}</strong>
        </button>
        <button type="button">
          <span>03 · 错误分类</span>
          <strong>{errorType}</strong>
        </button>
        <button type="button">
          <span>04 · 规则改进</span>
          <strong>{lessons.trim() ? "已填写" : "待填写"}</strong>
        </button>
      </div>

      <div className="decision-grid">
        <div className="card decision-panel">
          <div className="quant-analyze-section-head">
            <h3>待复核队列</h3>
            <em>{awaiting.length ? "从上往下处理" : "当前为空"}</em>
          </div>
          <div className="review-queue">
            {awaiting.slice(0, 8).map((item) => (
              <button
                key={item.id}
                type="button"
                className={`review-queue-item ${selectedOrFirstAwaiting?.id === item.id ? "active" : ""}`}
                onClick={() => setSelectedId(item.id)}
              >
                <div>
                  <strong>{item.ts_code}</strong>
                  <span>{item.decision_type}</span>
                </div>
                <em>{formatGmt8Time(item.decision_time)}</em>
              </button>
            ))}
            {!awaiting.length && <div className="empty">没有待复盘决策，新的执行快照出现后会自动进入这里。</div>}
          </div>
        </div>

        <div className="card decision-panel">
          <h3>当前复核对象</h3>
          {selectedOrFirstAwaiting ? (
            <div className="decision-issue warning review-focus-card">
              <strong>{selectedOrFirstAwaiting.ts_code} · {selectedOrFirstAwaiting.decision_type}</strong>
              <br />
              决策时间：{formatGmt8Time(selectedOrFirstAwaiting.decision_time)} · 数据截至：{formatGmt8Time(selectedOrFirstAwaiting.data_as_of_time)}
              <br />
              模型：{selectedOrFirstAwaiting.model_version || "—"} · 价格：{selectedOrFirstAwaiting.price ?? "—"}
              <br />
              理由：{selectedOrFirstAwaiting.reason || "—"}
              <br />
              失效条件：{(selectedOrFirstAwaiting.invalidation_conditions ?? []).join("；") || "未记录"}
            </div>
          ) : (
            <div className="empty">请选择一条待复盘决策。</div>
          )}
          <div className="review-focus-actions">
            <button
              type="button"
              className="button primary"
              disabled={!selectedOrFirstAwaiting || llmDrafting}
              onClick={() => void generateDraft()}
            >
              {llmDrafting ? "DeepSeek 分析中…" : "DeepSeek 生成复核草稿"}
            </button>
            <button
              type="button"
              className="button ghost"
              disabled={!selectedOrFirstAwaiting}
              onClick={() => {
                if (selectedOrFirstAwaiting) setSelectedId(selectedOrFirstAwaiting.id);
              }}
            >
              设为当前填写对象
            </button>
            <button
              type="button"
              className="button ghost"
              disabled={!selectedOrFirstAwaiting}
              onClick={() => void suggestOutcome()}
            >
              {suggesting ? "计算中…" : "自动估算后续价格收益"}
            </button>
          </div>
          {llmMeta && <div className="module-output"><i />{llmMeta} · 草稿必须人工确认后保存</div>}
        </div>
      </div>

      <div className="decision-grid">
        <div className="card decision-panel">
          <h3>填写复盘</h3>
          <div className="form-stack">
            <label>
              冻结决策
              <select value={selectedId} onChange={(event) => setSelectedId(event.target.value ? Number(event.target.value) : "")}>
                <option value="">选择待复盘决策</option>
                {awaiting.map((item) => (
                  <option key={item.id} value={item.id}>
                    {formatGmt8Time(item.decision_time)} · {item.ts_code} · {item.decision_type}
                  </option>
                ))}
              </select>
            </label>

            <label>
              复盘收益率（%）
              <input value={returnPct} onChange={(event) => setReturnPct(event.target.value)} placeholder="例如 -4.2" />
            </label>
            <label>
              收益归因
              <textarea value={attribution} onChange={(event) => setAttribution(event.target.value)} placeholder="例如：行业回撤 2%，选股偏差 1%，执行滑点 0.3%" />
            </label>
            <label>
              错误分类
              <select value={errorType} onChange={(event) => setErrorType(event.target.value)}>
                {ERROR_TYPES.map((item) => <option key={item}>{item}</option>)}
              </select>
            </label>
            <label>
              改进结论
              <textarea value={lessons} onChange={(event) => setLessons(event.target.value)} placeholder="说明应保留、修正或废弃的判断规则" />
            </label>
            <button className="button primary" onClick={() => void submit()} disabled={saving}>
              {saving ? "保存中…" : "保存复盘"}
            </button>
          </div>
        </div>

        <div className="card decision-panel">
          <h3>决策回放</h3>
          <div className="table-scroll">
            <table>
              <thead><tr><th>时间</th><th>标的</th><th>动作</th><th>数据截至</th><th>模型</th></tr></thead>
              <tbody>
                {decisions.map((item) => (
                  <tr key={item.id} className={reviewedIds.has(item.id) ? "" : "active"} onClick={() => setSelectedId(item.id)}>
                    <td>{formatGmt8Time(item.decision_time)}</td>
                    <td>{item.ts_code}</td>
                    <td>{item.decision_type}</td>
                    <td>{formatGmt8Time(item.data_as_of_time)}</td>
                    <td>{item.model_version}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!decisions.length && <div className="empty">暂无冻结决策；执行模拟调仓后会在此出现。</div>}
        </div>
      </div>

      <div className="card decision-panel">
        <h3>日度绩效与交易成本</h3>
        <div className="table-scroll">
          <table>
            <thead><tr><th>交易日</th><th>收益</th><th>基准</th><th>最大回撤</th><th>成交额</th><th>成本</th><th>拦截信号</th></tr></thead>
            <tbody>
              {dailyReports.map((item) => (
                <tr key={`${item.task_id ?? "all"}-${item.period}`}>
                  <td>{item.period}</td>
                  <td>{percentagePointsText(item.payload.return_pct)}</td>
                  <td>{percentagePointsText(item.payload.benchmark_return_pct)}</td>
                  <td>{percentagePointsText(item.payload.max_drawdown_pct)}</td>
                  <td>{asText(item.payload.traded_amount)}</td>
                  <td>{asText(item.payload.transaction_cost)}</td>
                  <td>{asText(item.payload.rejected_signal_count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!dailyReports.length && <div className="empty">尚无日度绩效快照；下一次完成模拟调仓后会自动生成。</div>}
      </div>

      <div className="card decision-panel">
        <h3>历史复盘与改进闭环</h3>
        <div className="table-scroll">
          <table>
            <thead><tr><th>复盘时间</th><th>决策</th><th>收益</th><th>归因</th><th>错误类型</th><th>改进结论</th></tr></thead>
            <tbody>
              {reviews.map((item) => (
                <tr key={item.id}>
                  <td>{formatGmt8Time(item.review_time)}</td>
                  <td>{item.decision_snapshot_id ?? "独立复盘"}</td>
                  <td>{pctText(item.outcome.return_pct)}</td>
                  <td>{asText(item.attribution.summary)}</td>
                  <td>{item.error_type || "—"}</td>
                  <td>{item.lessons || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!reviews.length && <div className="empty">尚无复盘记录。</div>}
      </div>
    </section>
  );
}
