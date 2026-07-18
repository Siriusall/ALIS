import { useEffect, useMemo, useRef, useState } from "react";
import type { SectionId } from "./Layout";
import { fetchDecisionSummary, fetchDailyReport, type DecisionSummary, type DailyRebalanceReport } from "../api/decision";
import type { QuantAnalyzeResponse } from "../types";
import { useStockContext } from "../context/StockContext";

const ANALYZE_RESULT_KEY = "quant_analyze_result_v1";

const TREND_TEXT: Record<string, string> = {
  uptrend: "趋势偏强",
  sideways: "震荡",
  downtrend: "趋势偏弱",
  high_risk: "高风险",
};

const RISK_TEXT: Record<string, string> = {
  normal: "正常",
  elevated: "偏高",
  high: "高",
};

const SENTIMENT_TEXT: Record<string, string> = {
  risk_on: "偏进攻",
  neutral: "中性",
  risk_off: "偏防守",
};

const OBSERVATION_TEXT: Record<string, string> = {
  breadth: "上涨宽度",
  average_return: "全市场平均收益",
  volume_ratio: "成交额强度",
  limit_up_ratio: "涨停占比",
  limit_down_ratio: "跌停占比",
  crowding: "成交拥挤度",
};

const TONE_SUMMARY: Record<string, string> = {
  go: "可以进入候选与执行确认",
  hold: "先看门控，再决定是否动作",
  stop: "先处理阻断，不进入执行",
};

function loadStoredAnalyzeResult(): QuantAnalyzeResponse | null {
  try {
    const raw = sessionStorage.getItem(ANALYZE_RESULT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as QuantAnalyzeResponse;
    return parsed?.ok ? parsed : null;
  } catch {
    return null;
  }
}

function fmtMoney(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  return String(value).slice(0, 19).replace("T", " ");
}

function fmtPct(value: unknown, digits = 1): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function issueText(issue: { level?: string; code?: string; message?: string }): string {
  const prefix = issue.level === "error" ? "错误" : issue.level === "warning" ? "警告" : issue.level || "提示";
  return `${prefix} / ${issue.code}：${issue.message}`;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function candidateEligibility(model: NonNullable<DecisionSummary["model_status"]>["versions"][number] | null | undefined) {
  const validation = record(model?.validation);
  const eligibility = record(validation.production_eligibility);
  const gates = record(eligibility.gates);
  const reasons = Array.isArray(eligibility.blocked_reasons) ? eligibility.blocked_reasons.map(String) : [];
  return {
    eligible: eligibility.eligible === true || model?.config?.production_eligible === true,
    historySessions: Number(eligibility.history_sessions || 0),
    minimumSessions: Number(eligibility.minimum_sessions_required || 0),
    reasons,
    gates,
    validationStatus: String(validation.status || ""),
  };
}

interface DecisionPageProps {
  onNavigate?: (section: SectionId) => void;
}

export default function DecisionPage({ onNavigate }: DecisionPageProps) {
  const { setActiveStock } = useStockContext();
  const [summary, setSummary] = useState<DecisionSummary | null>(null);
  const [analyze, setAnalyze] = useState<QuantAnalyzeResponse | null>(() => loadStoredAnalyzeResult());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [dailyReport, setDailyReport] = useState<DailyRebalanceReport | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const refreshRunning = useRef(false);

  async function refresh(showLoading = true) {
    if (refreshRunning.current) return;
    refreshRunning.current = true;
    if (showLoading) {
      setLoading(true);
      setError("");
    }
    try {
      const data = await fetchDecisionSummary(30);
      setSummary(data);
      setAnalyze(loadStoredAnalyzeResult());
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      if (showLoading) {
        setError(typeof msg === "string" ? msg : "决策展板加载失败");
      }
    } finally {
      refreshRunning.current = false;
      if (showLoading) setLoading(false);
    }
  }

  useEffect(() => {
    let active = true;
    (async () => {
      await refresh();
      if (!active) return;
    })();
    const timer = setInterval(() => {
      void refresh(false);
    }, 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  const health = summary?.data_health ?? null;
  const ledger = summary?.ledger ?? null;
  const gate = summary?.gate_decision;
  const modelStatus = summary?.model_status ?? null;
  const activeModel = modelStatus?.active_version ?? null;
  const market = summary?.market_overview ?? null;
  const marketState = market?.market_state ?? null;
  const sentiment = market?.sentiment ?? null;
  const positionGate = market?.position_gate ?? null;
  const validation = summary?.latest_model_validation ?? null;
  const recentExec = summary?.recent_execution ?? null;
  const latestCandidate = useMemo(() => {
    const versions = modelStatus?.versions ?? [];
    const trainedId = modelStatus?.latest_train_run?.model_version_id;
    return versions.find((item) => item.id === trainedId)
      ?? versions.find((item) => item.status === "candidate")
      ?? null;
  }, [modelStatus]);
  const displayedModel = activeModel ?? latestCandidate;
  const displayedEligibility = candidateEligibility(displayedModel);

  const dataErrorCount = health?.issues?.filter((x) => x.level === "error").length ?? 0;
  const warningCount = health?.issues?.filter((x) => x.level === "warning").length ?? 0;
  const buySuggestions = analyze?.rebalance_actions?.buy?.length ?? 0;
  const sellSuggestions = analyze?.rebalance_actions?.sell?.length ?? 0;
  const holdSuggestions = analyze?.rebalance_actions?.hold?.length ?? 0;
  const pendingAnalyze = Boolean(analyze?.ok && (buySuggestions > 0 || sellSuggestions > 0));
  const topSelections = (analyze?.selections ?? []).slice(0, 5);
  const stopIssues = (health?.issues ?? []).filter((item) => item.level === "error").slice(0, 4);
  const accountRisks = (ledger?.account_risks ?? []).slice(0, 4);
  const actionCount = buySuggestions + sellSuggestions;
  const noTradeReason = dataErrorCount > 0
    ? "系统有阻断项，今天先不进入执行。"
    : Number(ledger?.drawdown || 0) <= -0.08
      ? "组合风险较高，优先降风险。"
      : "当前没有新的买卖动作。";
  const primaryActionText = dataErrorCount > 0
    ? `先修复 ${dataErrorCount} 个系统阻断`
    : Number(ledger?.drawdown || 0) <= -0.08
      ? "先处理组合风险"
      : pendingAnalyze
        ? `今天有 ${actionCount} 条待确认调仓建议`
        : "今天先观察，不急于调仓";
  const reviewStatus = recentExec ? "有最近执行，待人工复核" : "暂无新执行记录";

  const modelReadyText = useMemo(() => {
    if (!displayedModel) return "待训练";
    if (activeModel?.is_active) return "已通过验证";
    return displayedEligibility.validationStatus === "passed" ? "候选已验证，未上线" : String(displayedModel.status || "候选");
  }, [activeModel, displayedEligibility.validationStatus, displayedModel]);

  function openResearch(symbol: string) {
    setActiveStock(symbol);
    onNavigate?.("stockResearch");
  }

  function openReview(symbol?: string) {
    if (symbol) setActiveStock(symbol);
    onNavigate?.("review");
  }

  return (
    <section id="decision" className="dashboard-section" data-section="decision">
      <div className="dashboard-section-inner decision-page">
        <div className={`decision-hero ${gate?.tone || "hold"}`}>
          <div className="decision-hero-main">
            <div className="decision-hero-kicker">今日调仓建议</div>
            <div className="decision-hero-title">{gate?.title || "加载中"}</div>
            <div className="decision-hero-detail">{gate?.detail || "正在读取数据、模型与账户状态。"}</div>
            <div className="decision-hero-summary">{TONE_SUMMARY[gate?.tone || "hold"]}</div>
          </div>
          <div className="decision-hero-aside">
            <div className="decision-hero-action">
              <span>当前优先动作</span>
              <strong>{primaryActionText}</strong>
            </div>
            <button type="button" onClick={() => void refresh()} disabled={loading}>
              {loading ? "检查中" : "刷新"}
            </button>
          </div>
        </div>
        {error && <div className="alert">{error}</div>}
        {!activeModel && latestCandidate && (
          <div className="decision-issue warning">
            训练已完成：{latestCandidate.version} 是候选版本，{displayedEligibility.validationStatus === "passed" ? "验证通过" : "验证尚未通过"}，但尚未允许接管交易。
            {displayedEligibility.historySessions > 0 && displayedEligibility.minimumSessions > 0
              ? ` 当前历史 ${displayedEligibility.historySessions}/${displayedEligibility.minimumSessions} 个交易日。`
              : " 请在模型策略页查看上线门槛。"}
          </div>
        )}

        <div className="decision-command-bar" aria-label="今日行动入口">
          <div className="decision-command-intro">
            <span>行动顺序</span>
            <strong>先门控，再执行，最后复核</strong>
          </div>
          <button type="button" className={dataErrorCount ? "urgent" : ""} onClick={() => onNavigate?.("governance")}>
            <span>01 · 系统阻断</span><strong>{dataErrorCount}</strong><em>{dataErrorCount ? "先修数据" : `${warningCount} 项提示`}</em>
          </button>
          <button type="button" className={Number(ledger?.drawdown || 0) <= -0.08 ? "urgent" : ""} onClick={() => onNavigate?.("positionRisk")}>
            <span>02 · 组合风险</span><strong>{fmtPct(ledger?.drawdown)}</strong>
          </button>
          <button type="button" className={pendingAnalyze ? "active" : ""} onClick={() => onNavigate?.("validation")}>
            <span>03 · 待执行建议</span><strong>{buySuggestions + sellSuggestions}</strong><em>{buySuggestions} 买 / {sellSuggestions} 卖</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("review")}>
            <span>04 · 决策复盘</span><strong>{recentExec ? "待检查" : "—"}</strong>
          </button>
        </div>

        <div className="decision-grid decision-primary-grid">
          <div className="card decision-panel decision-primary-panel">
            <div className="quant-analyze-section-head">
              <h3>今日动作建议</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("validation")}>
                查看完整量化分析
              </button>
            </div>
            {!analyze ? (
              <div className="empty">暂无今日调仓结果，请先运行量化分析。</div>
            ) : (
              <>
                <div className="decision-result-grid">
                  <article className={`decision-result-card ${buySuggestions > 0 ? "buy" : "muted"}`}>
                    <span>买入</span>
                    <strong>{buySuggestions}</strong>
                    <em>{buySuggestions > 0 ? "进入候选复核" : "暂无新增买入"}</em>
                  </article>
                  <article className={`decision-result-card ${sellSuggestions > 0 ? "sell" : "muted"}`}>
                    <span>卖出</span>
                    <strong>{sellSuggestions}</strong>
                    <em>{sellSuggestions > 0 ? "需要处理减仓/卖出" : "暂无卖出动作"}</em>
                  </article>
                  <article className={`decision-result-card ${holdSuggestions > 0 ? "hold" : "muted"}`}>
                    <span>持有</span>
                    <strong>{holdSuggestions}</strong>
                    <em>{holdSuggestions > 0 ? "继续观察与跟踪" : "暂无持有建议"}</em>
                  </article>
                  <article className={`decision-result-card ${actionCount === 0 ? "pause" : "muted"}`}>
                    <span>暂停</span>
                    <strong>{actionCount === 0 ? "是" : "否"}</strong>
                    <em>{actionCount === 0 ? noTradeReason : "今天存在可复核动作"}</em>
                  </article>
                </div>
                <div className="decision-two-col">
                  <span>分析日</span>
                  <strong>{analyze.signal_date || "—"}</strong>
                  <span>执行日</span>
                  <strong>{analyze.execute_date || "—"}</strong>
                  <span>建议动作</span>
                  <strong>买 {buySuggestions} / 卖 {sellSuggestions} / 持有 {holdSuggestions}</strong>
                  <span>执行口径</span>
                  <strong>{analyze.execution_note || (analyze.same_day_fallback ? "同日口径" : "T+1 口径")}</strong>
                </div>
                <div className="decision-message">{analyze.summary || "暂无摘要"}</div>
                {topSelections.length > 0 && (
                  <div className="decision-suggestion-grid">
                    {topSelections.map((row) => (
                      <article key={row.symbol} className="decision-suggestion-card">
                        <div className="decision-suggestion-head">
                          <div>
                            <strong>{row.symbol}</strong>
                            <span>{row.name || "候选标的"}</span>
                          </div>
                          <em>{row.score == null ? "—" : `分数 ${Number(row.score).toFixed(4)}`}</em>
                        </div>
                        <div className="decision-suggestion-meta">
                          <span>方向</span>
                          <strong>{buySuggestions > 0 ? "优先看买入候选" : "进入人工复核"}</strong>
                          <span>下一步</span>
                          <strong>先研究，再决定是否执行</strong>
                        </div>
                        <div className="decision-suggestion-actions">
                          <button type="button" className="button ghost" onClick={() => openResearch(row.symbol)}>
                            打开研究
                          </button>
                          <button type="button" className="button primary" onClick={() => openReview(row.symbol)}>
                            人工复核
                          </button>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          <div className="card decision-panel">
            <div className="quant-analyze-section-head">
              <h3>今日门控结论</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("governance")}>
                查看系统治理
              </button>
            </div>
            <div className="decision-two-col">
              <span>数据状态</span>
              <strong>{health?.ok ? "可信" : "需修复"}</strong>
              <span>模型状态</span>
              <strong>{displayedModel?.version || "未生成"} · {modelReadyText}</strong>
              <span>当前回撤</span>
              <strong>{fmtPct(ledger?.drawdown)}</strong>
              <span>人工复核</span>
              <strong>{reviewStatus}</strong>
            </div>
            <div className="decision-issue-list">
              {stopIssues.map((issue) => (
                <div key={`${issue.code}-${issue.message}`} className={`decision-issue ${issue.level}`}>
                  {issueText(issue)}
                </div>
              ))}
              {accountRisks.map((risk) => (
                <div key={risk.code} className={`decision-issue ${risk.level}`}>
                  {risk.message}
                </div>
              ))}
              {!stopIssues.length && !accountRisks.length && (
                <div className="empty">当前没有系统阻断或账户级硬风险。</div>
              )}
            </div>
          </div>
        </div>

        <div className="market-overview-strip">
          <article className={`market-overview-state ${market?.ok ? "ready" : "degraded"}`}>
            <span>市场状态</span>
            <strong>{marketState?.label || "观测不足"}</strong>
            <em>
              {marketState
                ? `置信度 ${(marketState.confidence * 100).toFixed(0)}% · ${marketState.model}`
                : market?.reason || "等待市场数据"}
            </em>
          </article>
          <article>
            <span>市场情绪</span>
            <strong>{sentiment ? sentiment.score.toFixed(1) : "—"}</strong>
            <em>{sentiment ? SENTIMENT_TEXT[sentiment.state] || sentiment.state : "无合格观测"}</em>
          </article>
          <article>
            <span>组合风险预算</span>
            <strong>{marketState ? `${Math.round(marketState.risk_budget_multiplier * 100)}%` : "收缩"}</strong>
            <em>{positionGate?.reason || "环境门控只降低预算，不改变企业质量"}</em>
          </article>
          <article>
            <span>当前回撤</span>
            <strong className={Number(ledger?.drawdown || 0) <= -0.08 ? "decision-cell-stop" : ""}>
              {fmtPct(ledger?.drawdown)}
            </strong>
            <em>高水位 {fmtMoney(ledger?.high_water_mark)}</em>
          </article>
          <article>
            <span>主题主线</span>
            <strong>{market?.themes.items?.[0]?.theme || "未确认"}</strong>
            <em>{market?.themes.reason || "暂无通过确认门的主题"}</em>
          </article>
        </div>

        <div className="decision-grid market-overview-context">
          <div className="card decision-panel">
            <div className="quant-analyze-section-head">
              <h3>市场环境证据</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("sentiment")}>
                查看情绪详情
              </button>
            </div>
            {market?.ok ? (
              <>
                <div className="market-observation-grid">
                  {Object.entries(market.observations ?? {})
                    .filter(([key]) => key in OBSERVATION_TEXT)
                    .map(([key, value]) => (
                      <div key={key}>
                        <span>{OBSERVATION_TEXT[key]}</span>
                        <strong>{key.includes("ratio") || key === "breadth" || key === "average_return" || key === "crowding" ? fmtPct(value) : Number(value).toFixed(2)}</strong>
                      </div>
                    ))}
                </div>
                <p className="decision-message">{marketState?.reason || "当前环境判断已生成。"}</p>
                {(market.limitations ?? []).map((item) => <div className="decision-issue warning" key={item}>{item}</div>)}
              </>
            ) : (
              <div className="decision-issue warning">{market?.reason || "市场数据不足，当前不生成环境判断。"}</div>
            )}
          </div>
          <div className="card decision-panel">
            <div className="quant-analyze-section-head">
              <h3>主题与组合解释</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("themes")}>
                查看主题中心
              </button>
            </div>
            {market?.themes.items?.length ? market.themes.items.slice(0, 5).map((theme) => (
              <div className="market-theme-row" key={theme.theme}><strong>{theme.theme}</strong><span>{theme.score.toFixed(1)} · {theme.state}</span></div>
            )) : <div className="market-empty-state"><strong>没有已确认主题</strong><p>{market?.themes.reason || "主题数据尚未接入"}</p></div>}
            <div className="decision-two-col market-account-compact">
              <span>总权益</span><strong>{fmtMoney(ledger?.total_equity)}</strong>
              <span>现金占比</span><strong>{fmtPct(ledger?.cash_pct)}</strong>
              <span>持仓数量</span><strong>{ledger?.position_count ?? 0}</strong>
              <span>最大单票</span><strong>{ledger?.max_position_symbol ? `${ledger.max_position_symbol} ${fmtPct(ledger.max_position_pct)}` : "—"}</strong>
            </div>
          </div>
        </div>

        <div className="card decision-panel" style={{marginBottom: 16}}>
            <div className="quant-analyze-section-head">
              <h3>📋 每日调仓建议</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => reportLoading || fetchDailyReport().then(setDailyReport).catch(() => {})}>
                {reportLoading ? "加载中…" : "刷新"}
              </button>
            </div>
            {dailyReport?.ok ? (
              <>
                <div className="decision-message" style={{whiteSpace: "pre-wrap", marginBottom: 8}}>
                  {dailyReport.summary.decision}
                </div>
                <div className="decision-two-col">
                  <span>交易日期</span><strong>{dailyReport.trade_date}</strong>
                  <span>市场状态</span><strong>{(dailyReport.sections[0] as any)?.market_state || "—"}</strong>
                  <span>组合净值</span><strong>{(dailyReport.sections[1] as any)?.total_equity ? "¥" + Number((dailyReport.sections[1] as any).total_equity).toLocaleString() : "—"}</strong>
                  <span>总收益</span><strong>{(dailyReport.sections[1] as any)?.total_return_pct + "%" || "—"}</strong>
                  <span>持仓数量</span><strong>{(dailyReport.sections[1] as any)?.position_count ?? 0}</strong>
                  <span>现金余额</span><strong>{(dailyReport.sections[1] as any)?.cash_balance ? "¥" + Number((dailyReport.sections[1] as any).cash_balance).toLocaleString() : "—"}</strong>
                </div>
                {(dailyReport.sections[2] as any)?.positions?.length > 0 && (
                  <details style={{marginTop: 8}}>
                    <summary style={{cursor: "pointer", fontWeight: 600}}>当前持仓（{(dailyReport.sections[2] as any).positions.length} 只）</summary>
                    <div className="decision-two-col" style={{marginTop: 4}}>
                      {(dailyReport.sections[2] as any).positions.map((p: any) => (
                        <span key={p.symbol} style={{gridColumn: "1 / -1", display: "flex", justifyContent: "space-between"}}>
                          <strong>{p.symbol}</strong>
                          <span>{p.quantity}股 @ {p.avg_price} / {p.profit_loss_pct != null ? p.profit_loss_pct.toFixed(1) + "%" : "—"}</span>
                        </span>
                      ))}
                    </div>
                  </details>
                )}
                <div style={{fontSize: "0.85em", color: "var(--text-secondary)", marginTop: 4}}>
                  生成时间：{dailyReport.generated_at?.slice(0, 19)?.replace("T", " ")}
                </div>
              </>
            ) : (
              <div className="market-empty-state">
                <strong>每日调仓报告未生成</strong>
                <p>系统尚在初始化，或数据尚未就绪</p>
              </div>
            )}
          </div>

        <div className="decision-grid">
          <div className="card decision-panel">
            <div className="quant-analyze-section-head">
              <h3>执行前检查</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("dataCenter")}>
                查看数据中台
              </button>
            </div>
            <div className="decision-two-col">
              <span>交易日</span>
              <strong>{health?.trade_dates?.last || "—"} / 共 {health?.trade_dates?.count ?? 0} 天</strong>
              <span>行情快照</span>
              <strong>{health?.market_snapshots?.last || "—"} / 共 {health?.market_snapshots?.count ?? 0} 天</strong>
              <span>账户总权益</span>
              <strong>{fmtMoney(ledger?.total_equity)}</strong>
              <span>持仓市值</span>
              <strong>{fmtMoney(ledger?.position_market_value)}</strong>
              <span>最近估值日</span>
              <strong>{ledger?.valuation_date || "—"}</strong>
              <span>最大单票</span>
              <strong>{ledger?.max_position_symbol ? `${ledger.max_position_symbol} ${fmtPct(ledger.max_position_pct)}` : "—"}</strong>
            </div>
            <div className="decision-issue-list">
              {(health?.issues ?? []).slice(0, 5).map((issue) => (
                <div key={`${issue.code}-${issue.message}`} className={`decision-issue ${issue.level}`}>
                  {issueText(issue)}
                </div>
              ))}
              {health && health.issues.length === 0 && <div className="empty">暂无数据健康问题</div>}
            </div>
          </div>

          <div className="card decision-panel">
            <div className="quant-analyze-section-head">
              <h3>模型与执行状态</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("strategy")}>
                前往模型策略
              </button>
            </div>
            <div className="decision-two-col">
              <span>{activeModel ? "生产版本" : "最近候选"}</span>
              <strong>{displayedModel?.version || "尚未生成"}</strong>
              <span>模型状态</span>
              <strong>{modelReadyText}</strong>
              <span>模型类型</span>
              <strong>{String(displayedModel?.config?.model_type || "机器学习 ranking")}</strong>
              <span>最近训练</span>
              <strong>
                {modelStatus?.latest_train_run
                  ? `${modelStatus.latest_train_run.status} / ${fmtDate(modelStatus.latest_train_run.updated_at)}`
                  : "—"}
              </strong>
              <span>最近模拟执行</span>
              <strong>{recentExec ? fmtDate(recentExec.executed_at) : "暂无"}</strong>
            </div>
          {validation && (
            <div className="metrics-row" style={{ marginTop: 12 }}>
              <div className="card metric-card">
                <div className="sub">验证总收益</div>
                <div className="metric">{fmtPct(validation.metrics.total_return)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">最大回撤</div>
                <div className="metric">{fmtPct(validation.metrics.max_drawdown)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">夏普</div>
                <div className="metric">{Number(validation.metrics.sharpe || 0).toFixed(2)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">验证区间</div>
                <div className="metric" style={{ fontSize: 14 }}>
                  {validation.start_date} ~ {validation.end_date}
                </div>
              </div>
            </div>
          )}
          {!activeModel && latestCandidate && (
            <div className="decision-message">
              候选版本已完成训练，系统仍禁止自动交易。{displayedEligibility.reasons.length ? `拦截原因：${displayedEligibility.reasons.join("、")}。` : "请在模型策略页完成上线审查。"}
              {displayedEligibility.historySessions > 0 && displayedEligibility.minimumSessions > 0 ? ` 历史门槛：${displayedEligibility.historySessions}/${displayedEligibility.minimumSessions} 个交易日。` : ""}
            </div>
          )}
          {!displayedModel && (
            <div className="decision-message">还没有可用模型，请先在模型策略页完成自动训练与验证。</div>
          )}
          {pendingAnalyze && (
            <div className="decision-message" style={{ marginTop: 8 }}>
              有待执行调仓建议，请在回测验证页查看自动模拟执行结果或诊断预览。
            </div>
          )}
        </div>

        <div className="card decision-panel">
          <h3>最近模拟执行</h3>
          {recentExec ? (
            <>
              <div className="decision-two-col">
                <span>执行时间</span>
                <strong>{fmtDate(recentExec.executed_at)}</strong>
                <span>买入/卖出</span>
                <strong>
                  买 {recentExec.buy_count} / 卖 {recentExec.sell_count}
                </strong>
                <span>买入成本</span>
                <strong>{fmtMoney(recentExec.total_buy_cost)}</strong>
                <span>预算 / TopN</span>
                <strong>
                  {fmtMoney(recentExec.budget)} / {recentExec.top_n}
                </strong>
              </div>
              <div className="table-scroll decision-trade-table" style={{ marginTop: 12 }}>
                <table>
                  <thead>
                    <tr>
                      <th>股票</th>
                      <th>方向</th>
                      <th>数量</th>
                      <th>成交价</th>
                      <th>时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentExec.operations.map((op, index) => (
                      <tr key={`${op.symbol}-${op.side}-${index}`}>
                        <td>
                          {op.symbol}
                          {op.name ? ` ${op.name}` : ""}
                        </td>
                        <td>{String(op.side).toUpperCase() === "SELL" ? "卖出" : "买入"}</td>
                        <td>{op.quantity}</td>
                        <td>{Number(op.price || 0).toFixed(3)}</td>
                        <td>{fmtDate(op.at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <div className="empty">暂无模拟执行记录。自动模拟盘执行后，这里会显示最近流水。</div>
          )}
        </div>
      </div>
      </div>
    </section>
  );
}
