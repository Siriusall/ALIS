import { useEffect, useMemo, useRef, useState } from "react";
import type { SectionId } from "./Layout";
import { fetchDecisionSummary, fetchDailyReport, type DecisionSummary, type DailyRebalanceReport } from "../api/decision";
import { fetchPlanModels, type PlanModelControl } from "../api/modelStrategy";
import type { QuantAnalyzeResponse } from "../types";
import { useStockContext } from "../context/StockContext";
import LoadingState from "./LoadingState";
import SecurityLabel from "./SecurityLabel";
import { GOVERNANCE_SECTION_EVENT, GOVERNANCE_SECTION_STORAGE_KEY } from "./governanceSections";

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
  const [planModels, setPlanModels] = useState<PlanModelControl | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const showDetails = useMemo(() => false, []);
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
      void fetchPlanModels().then(setPlanModels).catch(() => undefined);
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
    }, 60000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  const health = summary?.data_health ?? null;
  const ledger = summary?.ledger ?? null;
  const gate = summary?.gate_decision;
  const riskExplanation = summary?.risk_gate_explanation;
  const modelStatus = summary?.model_status ?? null;
  const activeModel = modelStatus?.active_version ?? null;
  const market = summary?.market_overview ?? null;
  const enhancement = summary?.decision_enhancement ?? null;
  const revenueDashboard = enhancement?.revenue_dashboard ?? null;
  const protection = enhancement?.extreme_market_protection ?? null;
  const positionPolicy = enhancement?.market_position_policy ?? revenueDashboard?.position_ceiling ?? null;
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
  const recentBuyOps = (recentExec?.operations ?? []).filter((item) => item.side.toUpperCase() === "BUY");
  const recentSellOps = (recentExec?.operations ?? []).filter((item) => item.side.toUpperCase() === "SELL");
  const hasRecentExecution = Boolean(recentExec && recentExec.trade_date && recentExec.trade_date.replace(/-/g, "") === String(health?.trade_dates?.last || market?.trade_date || "").replace(/-/g, "").slice(0, 8));
  const displayedBuyCount = buySuggestions || (hasRecentExecution ? recentBuyOps.length : 0);
  const displayedSellCount = sellSuggestions || (hasRecentExecution ? recentSellOps.length : 0);
  const pendingAnalyze = Boolean(analyze?.ok && (buySuggestions > 0 || sellSuggestions > 0));
  const topSelections = (analyze?.selections ?? []).slice(0, 5);
  const stopIssues = (health?.issues ?? []).filter((item) => item.level === "error").slice(0, 4);
  const accountRisks = (ledger?.account_risks ?? []).slice(0, 4);
  const actionCount = displayedBuyCount + displayedSellCount;
  const latestDataDate = health?.trade_dates?.last || market?.trade_date || "—";
  const requiresRiskRebalance = Boolean(riskExplanation?.requires_rebalance);
  const noTradeReason = dataErrorCount > 0
    ? "系统有阻断项，今天先不进入执行。"
    : requiresRiskRebalance
      ? "风险预算收缩，系统应先卖出超配或切到防御资产。"
    : Number(ledger?.drawdown || 0) <= -0.08
      ? "组合风险较高，优先降风险。"
      : "当前没有新的买卖动作。";
  const primaryActionText = dataErrorCount > 0
    ? `先修复 ${dataErrorCount} 个系统阻断`
    : requiresRiskRebalance
      ? "系统执行降风险调仓，不新增股票仓位"
    : Number(ledger?.drawdown || 0) <= -0.08
      ? "先查看系统风险处置方案"
      : pendingAnalyze
        ? `系统生成 ${actionCount} 条待确认调仓建议`
        : hasRecentExecution
          ? `今日已执行 ${displayedBuyCount} 笔买入、${displayedSellCount} 笔卖出`
        : "今天先观察，不急于调仓";
  const reviewStatus = recentExec ? "最近执行已入账，可按需复盘" : "暂无新执行记录";
  const riskGatePassed = Boolean(riskExplanation?.allow_new_exposure ?? positionGate?.allow_new_exposure ?? true);
  const riskGateConstrained = !riskGatePassed || Number(riskExplanation?.risk_budget_multiplier ?? positionGate?.combined_risk_budget_multiplier ?? 1) < 0.8;
  const riskGateLabel = !riskGatePassed ? (requiresRiskRebalance ? "禁买/降险" : "禁买") : riskGateConstrained ? "收缩" : "通过";
  const riskGateIcon = !riskGatePassed ? (requiresRiskRebalance ? "↓" : "!") : riskGateConstrained ? "↓" : "✓";
  const riskGateClass = !riskGatePassed ? (requiresRiskRebalance ? "warning" : "blocked") : riskGateConstrained ? "warning" : "passed";
  const riskGateDetail = !riskGatePassed && (riskExplanation?.allow_reduce || riskExplanation?.allow_sell)
    ? "不允许新增仓位；允许并优先执行减仓/防御调仓"
    : riskExplanation?.reasons?.[0]?.detail || positionGate?.reason || "等待风险状态";
  const modelPipelineReady = Boolean(activeModel);
  const marketPipelineReady = Boolean(market?.ok);
  const riskPipelineReady = Boolean(riskExplanation ? (riskExplanation.allow_new_exposure || riskExplanation.allow_reduce || riskExplanation.allow_defensive) : positionGate);
  const actionPipelineReady = pendingAnalyze || hasRecentExecution || requiresRiskRebalance || gate?.tone === "go";
  const pipelineItems = [
    {
      title: "数据已更新",
      detail: health?.ok ? `最新交易日 ${latestDataDate}` : dataErrorCount ? `${dataErrorCount} 个数据阻断` : "等待数据中台确认",
      status: health?.ok ? "done" : dataErrorCount ? "blocked" : "working",
      target: "dataCenter" as SectionId,
    },
    {
      title: "模型已接管",
      detail: activeModel ? activeModel.version : displayedModel ? `${displayedModel.version} 待上线` : "等待训练",
      status: modelPipelineReady ? "done" : "working",
      target: "governance" as SectionId,
    },
    {
      title: "行情已识别",
      detail: marketState?.label || market?.reason || "等待市场观测",
      status: marketPipelineReady ? "done" : "working",
      target: "sentiment" as SectionId,
    },
    {
      title: "风险已定界",
      detail: riskGateLabel === "通过" ? "允许按目标组合调仓" : riskGateDetail,
      status: riskGatePassed ? "done" : riskPipelineReady ? "warning" : "blocked",
      target: "positionRisk" as SectionId,
    },
    {
      title: "动作已生成",
      detail: primaryActionText,
      status: actionPipelineReady ? (requiresRiskRebalance ? "warning" : "done") : "working",
      target: "validation" as SectionId,
    },
  ];

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

  function openModelConsole() {
    window.localStorage.setItem(GOVERNANCE_SECTION_STORAGE_KEY, "models");
    window.dispatchEvent(new CustomEvent(GOVERNANCE_SECTION_EVENT, { detail: "models" }));
    onNavigate?.("governance");
  }

  async function loadDailyReport() {
    if (reportLoading) return;
    setReportLoading(true);
    try {
      setDailyReport(await fetchDailyReport());
    } finally {
      setReportLoading(false);
    }
  }

  if (loading && !summary) {
    return (
      <section id="decision" className="dashboard-section" data-section="decision">
        <div className="dashboard-section-inner decision-page decision-dashboard">
          <LoadingState
            variant="page"
            title="正在生成今日调仓建议"
            detail="正在读取数据门控、市场状态、生产模型、账户持仓和组合风险"
            rows={5}
          />
        </div>
      </section>
    );
  }

  return (
    <section id="decision" className="dashboard-section" data-section="decision">
      <div className="dashboard-section-inner decision-page decision-dashboard">
        <header className="decision-dashboard-head">
          <div>
            <span className="decision-dashboard-eyebrow">DAILY DECISION</span>
            <h1>今日决策</h1>
            <p>{latestDataDate === "—" ? "等待交易数据" : `${latestDataDate} · 数据、模型与账户联合判断`}</p>
          </div>
          <div className="decision-dashboard-head-actions">
            <span className={`decision-live-status ${loading ? "loading" : ""}`}><i />{loading ? "正在更新" : "实时状态"}</span>
            <button type="button" className="button ghost" onClick={() => void refresh()} disabled={loading}>刷新数据</button>
          </div>
        </header>

        {loading && summary && (
          <LoadingState
            variant="inline"
            className="decision-refresh-loading"
            title="正在更新今日调仓建议"
            detail="保留当前结果，待最新数据返回后自动替换"
          />
        )}

        <div className={`decision-verdict ${gate?.tone || "hold"}`}>
          <div className="decision-verdict-copy">
            <div className="decision-verdict-label"><i /> 今日建议</div>
            <h2>{gate?.title || "正在形成今日结论"}</h2>
            <p>{gate?.detail || "系统正在读取数据质量、市场环境、组合风险与生产模型。"}</p>
            <div className="decision-verdict-footer">
              <button type="button" onClick={() => onNavigate?.("validation")}>查看完整分析 <b>→</b></button>
            </div>
          </div>
          <div className="decision-verdict-actions">
            <div className="decision-priority">
              <span>现在应该做什么</span>
              <strong>{primaryActionText}</strong>
            </div>
          </div>
        </div>
        {error && <div className="alert">{error}</div>}
        <div className="decision-quick-glance" aria-label="今日关键数字">
          <button type="button" onClick={() => onNavigate?.("positionRisk")}>
            <span>股票仓位</span>
            <strong>{fmtPct(positionPolicy?.current_equity)} <i>→</i> {fmtPct(positionPolicy?.max_equity)}</strong>
            <em>{requiresRiskRebalance ? "需要降到上限内" : "处于预算内"}</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("portfolio")}>
            <span>账户现金</span>
            <strong>{fmtPct(ledger?.cash_pct)}</strong>
            <em>{ledger?.position_count ?? 0} 只持仓</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("sentiment")}>
            <span>市场状态</span>
            <strong>{marketState?.label || "观测中"}</strong>
            <em>风险预算 {positionGate ? `${Math.round(positionGate.combined_risk_budget_multiplier * 100)}%` : "—"}</em>
          </button>
        </div>
        {showDetails && <>
        {revenueDashboard && (
          <section className="decision-revenue-dashboard" aria-label="收益目标仪表盘">
            <header>
              <div>
                <span>收益目标仪表盘</span>
                <strong>今天只看这四件事</strong>
              </div>
              <em>{positionPolicy?.label || marketState?.label || "市场状态待确认"}</em>
            </header>
            {protection?.active && (
              <div className={`decision-protection-strip ${protection.severity}`}>
                <strong>极端行情保护已启用</strong>
                <span>{protection.triggers.map((item) => item.label).join(" / ")}</span>
              </div>
            )}
            <div>
              <article>
                <span>今日是否调仓</span>
                <strong>{revenueDashboard.today_action}</strong>
                <small>{revenueDashboard.next_step}</small>
              </article>
              <article>
                <span>预期收益来源</span>
                <strong>{revenueDashboard.expected_return_source}</strong>
                <small>排序模型选股，资金/叙事/事件只做确认和退出。</small>
              </article>
              <article>
                <span>最大风险</span>
                <strong>{revenueDashboard.max_risk}</strong>
                <small>{revenueDashboard.why_not_move}</small>
              </article>
              <article>
                <span>仓位上限</span>
                <strong>{fmtPct(positionPolicy?.max_equity)} / 单票 {fmtPct(positionPolicy?.max_single_position)}</strong>
                <small>当前股票仓位 {fmtPct(positionPolicy?.current_equity)} · {positionPolicy?.holding_period_bias || "正常轮动"}</small>
              </article>
            </div>
          </section>
        )}
        {!activeModel && latestCandidate && (
          <div className="decision-issue warning">
            训练已完成：{latestCandidate.version} 是候选版本，{displayedEligibility.validationStatus === "passed" ? "验证通过" : "验证尚未通过"}，但尚未允许接管交易。
            {displayedEligibility.historySessions > 0 && displayedEligibility.minimumSessions > 0
              ? ` 当前历史 ${displayedEligibility.historySessions}/${displayedEligibility.minimumSessions} 个交易日。`
              : " 请在模型策略页查看上线门槛。"}
          </div>
        )}

        <section className="decision-system-flow" aria-label="系统自动决策流程">
          <header>
            <span>系统自动闭环</span>
            <strong>数据 → 模型 → 行情 → 风险 → 调仓</strong>
            <p>你不需要逐个模块填空；这里显示系统自己已经判断到哪一步。</p>
          </header>
          <div>
            {pipelineItems.map((item, index) => (
              <button key={item.title} type="button" className={item.status} onClick={() => onNavigate?.(item.target)}>
                <i>{index + 1}</i>
                <span>{item.title}</span>
                <strong>{item.detail}</strong>
              </button>
            ))}
          </div>
        </section>

        <div className="decision-glance-grid" aria-label="核心状态">
          <button type="button" onClick={() => onNavigate?.("governance")} className={dataErrorCount ? "danger" : "ready"}>
            <span>数据门控</span><strong>{health ? (health.ok ? "通过" : "阻断") : "检查中"}</strong><em>{dataErrorCount ? `${dataErrorCount} 个错误` : `${warningCount} 项提示`}</em>
          </button>
          <button type="button" onClick={openModelConsole} className={activeModel ? "ready" : "warning"}>
            <span>生产模型</span><strong>{activeModel ? "已就绪" : "未就绪"}</strong><em>{displayedModel?.version || modelReadyText}</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("positionRisk")} className={Number(ledger?.drawdown || 0) <= -0.08 ? "danger" : "ready"}>
            <span>组合回撤</span><strong>{fmtPct(ledger?.drawdown)}</strong><em>最终风险预算 {positionGate ? `${Math.round(positionGate.combined_risk_budget_multiplier * 100)}%` : marketState ? `${Math.round(marketState.risk_budget_multiplier * 100)}%` : "—"}</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("portfolio")}>
            <span>账户权益</span><strong>{fmtMoney(ledger?.total_equity)}</strong><em>现金 {fmtPct(ledger?.cash_pct)} · {ledger?.position_count ?? 0} 只持仓</em>
          </button>
        </div>

        <div className="decision-dashboard-main">
          <section className="decision-focus-card">
            <div className="decision-card-head">
              <div><span>{hasRecentExecution && !pendingAnalyze ? "今日成交" : "系统建议"}</span><h3>{pendingAnalyze ? `${actionCount} 条调仓建议等待确认` : hasRecentExecution ? `已完成 ${displayedBuyCount} 笔买入、${displayedSellCount} 笔卖出` : "今天没有必须执行的交易"}</h3></div>
              <button type="button" onClick={() => onNavigate?.("validation")}>量化分析 →</button>
            </div>
            {!analyze && hasRecentExecution ? (
              <div className="decision-pick-list">
                {[...recentBuyOps, ...recentSellOps].map((row, index) => (
                  <article key={`${row.side}-${row.symbol}`}>
                    <b>{String(index + 1).padStart(2, "0")}</b>
                    <div><strong><SecurityLabel code={row.symbol} name={row.name} /></strong><span>{row.side.toUpperCase() === "BUY" ? "买入" : "卖出"} {row.quantity.toLocaleString("zh-CN")} 份</span></div>
                    <em>¥{fmtMoney(row.amount ?? row.price * row.quantity)}</em>
                    <button type="button" onClick={() => openResearch(row.symbol)}>研究</button>
                    <button type="button" className="primary" onClick={() => onNavigate?.("executionLedger")}>成交 ¥{fmtMoney(row.price)}</button>
                  </article>
                ))}
              </div>
            ) : !analyze ? (
              <div className="decision-zero-state decision-plan-state"><i>—</i><strong>{riskExplanation?.headline || "尚未生成今日分析"}</strong><p>{riskExplanation?.constraints?.[0] || "当前保持原仓位，不根据不完整信号交易。"}</p>{riskExplanation?.today_plan?.length ? <div className="decision-plan-list">{riskExplanation.today_plan.slice(0, 4).map((item, index) => <div key={`${item.step}-${index}`}><b>{index + 1}</b><span>{item.step}</span><strong>{item.detail}</strong></div>)}</div> : null}</div>
            ) : topSelections.length ? (
              <div className="decision-pick-list">
                {topSelections.map((row, index) => (
                  <article key={row.symbol}>
                    <b>{String(index + 1).padStart(2, "0")}</b>
                    <div><strong><SecurityLabel code={row.symbol} name={row.name} /></strong></div>
                    <em>{row.score == null ? "待评分" : Number(row.score).toFixed(3)}</em>
                    <button type="button" onClick={() => openResearch(row.symbol)}>看证据</button>
                    <button type="button" className="primary" onClick={() => openReview(row.symbol)}>确认</button>
                  </article>
                ))}
              </div>
            ) : (
              <div className="decision-zero-state"><i>✓</i><strong>无需新增交易</strong><p>{analyze.summary || noTradeReason}</p></div>
            )}
          </section>

          <aside className="decision-gate-card">
            <div className="decision-card-head">
              <div><span>执行许可</span><h3>四道门控</h3></div>
              <button type="button" onClick={() => onNavigate?.("governance")}>查看治理 →</button>
            </div>
            <div className="decision-gate-list">
              <button type="button" onClick={() => onNavigate?.("dataCenter")} className={health?.ok ? "passed" : "blocked"}><i>{health?.ok ? "✓" : "!"}</i><div><strong>数据可信</strong><span>{health?.ok ? "核心数据满足决策要求" : "存在数据阻断"}</span></div><em>{health?.ok ? "通过" : "阻断"}</em></button>
              <button type="button" onClick={openModelConsole} className={activeModel ? "passed" : "blocked"}><i>{activeModel ? "✓" : "!"}</i><div><strong>模型可用</strong><span>{activeModel ? activeModel.version : modelReadyText}</span></div><em>{activeModel ? "通过" : "阻断"}</em></button>
              <button type="button" onClick={() => onNavigate?.("positionRisk")} className={riskGateClass}><i>{riskGateIcon}</i><div><strong>风险预算</strong><span>{riskGateDetail}</span></div><em>{riskGateLabel}</em></button>
            <button type="button" onClick={() => onNavigate?.("reviewWorkbench")} className="manual"><i>人</i><div><strong>执行复盘</strong><span>{reviewStatus}</span></div><em>{recentExec ? "可选" : "无待办"}</em></button>
            </div>
            {(riskExplanation?.release_conditions?.length || stopIssues.length > 0 || accountRisks.length > 0) && <div className="decision-blocker-note">{riskExplanation?.release_conditions?.[0] || [...stopIssues.map(issueText), ...accountRisks.map((risk) => risk.message)][0]}</div>}
          </aside>
        </div>

        <div className="decision-context-row">
          <button type="button" onClick={() => onNavigate?.("sentiment")}>
            <span>市场环境</span><strong>{marketState?.label || "观测不足"}</strong><em>{sentiment ? `${SENTIMENT_TEXT[sentiment.state] || sentiment.state} · ${sentiment.score.toFixed(1)}` : market?.reason || "等待市场观测"}</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("themes")}>
            <span>当前主线</span><strong>{market?.themes.items?.[0]?.theme || "暂无确认主题"}</strong><em>{market?.themes.reason || "尚未通过主题确认门"}</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("portfolio")}>
            <span>组合状态</span><strong>{ledger?.position_count ?? 0} 只持仓</strong><em>最大单票 {ledger?.max_position_symbol ? <><SecurityLabel code={ledger.max_position_symbol} /> {fmtPct(ledger.max_position_pct)}</> : "—"}</em>
          </button>
          <button type="button" onClick={() => onNavigate?.("review")}>
            <span>最近执行</span><strong>{recentExec ? `${recentExec.buy_count} 买 / ${recentExec.sell_count} 卖` : "暂无执行"}</strong><em>{recentExec ? fmtDate(recentExec.executed_at) : "等待有效交易信号"}</em>
          </button>
        </div>

        <details className="decision-detail-drawer">
          <summary><span>展开完整决策证据</span><em>市场观测、模型验证、数据问题与最近成交</em></summary>
          <div className="decision-detail-grid">
            {enhancement?.model_role_contract && (
              <section className="wide">
                <div className="decision-card-head"><div><span>模型分工</span><h3>谁负责什么，谁不能越界</h3></div></div>
                <p className="decision-detail-copy">{enhancement.model_role_contract.summary}</p>
                <div className="decision-role-contract-grid">
                  {enhancement.model_role_contract.items.map((item) => (
                    <article key={item.model_key}>
                      <strong>{item.name}</strong>
                      <span>{item.decision_role}</span>
                      <small>负责：{item.owns.slice(0, 3).join("、")}</small>
                      <em>输出：{item.output}</em>
                    </article>
                  ))}
                </div>
              </section>
            )}
            <section>
              <div className="decision-card-head"><div><span>市场证据</span><h3>{marketState?.label || "暂无结论"}</h3></div></div>
              <div className="market-observation-grid">
                {Object.entries(market?.observations ?? {}).filter(([key]) => key in OBSERVATION_TEXT).map(([key, value]) => (
                  <div key={key}><span>{OBSERVATION_TEXT[key]}</span><strong>{key.includes("ratio") || key === "breadth" || key === "average_return" || key === "crowding" ? fmtPct(value) : Number(value).toFixed(2)}</strong></div>
                ))}
              </div>
              {!market?.ok && <div className="decision-issue warning">{market?.reason || "市场数据不足"}</div>}
            </section>
            <section>
              <div className="decision-card-head"><div><span>模型验证</span><h3>{displayedModel?.version || "未生成模型"}</h3></div></div>
              <div className="decision-two-col">
                <span>状态</span><strong>{modelReadyText}</strong>
                <span>总收益</span><strong>{validation ? fmtPct(validation.metrics.total_return) : "—"}</strong>
                <span>最大回撤</span><strong>{validation ? fmtPct(validation.metrics.max_drawdown) : "—"}</strong>
                <span>夏普</span><strong>{validation ? Number(validation.metrics.sharpe || 0).toFixed(2) : "—"}</strong>
              </div>
            </section>
            <section>
              <div className="decision-card-head"><div><span>数据与账户问题</span><h3>{dataErrorCount + accountRisks.length ? "需要处理" : "无硬阻断"}</h3></div></div>
              <div className="decision-issue-list">
                {[...(health?.issues ?? []).slice(0, 5).map(issueText), ...accountRisks.map((risk) => risk.message)].map((item) => <div className="decision-issue" key={item}>{item}</div>)}
                {!health?.issues?.length && !accountRisks.length && <div className="empty">当前没有系统阻断或账户级硬风险。</div>}
              </div>
            </section>
            <section>
              <div className="decision-card-head"><div><span>每日报告</span><h3>{dailyReport?.ok ? dailyReport.trade_date : "尚未生成"}</h3></div><button type="button" onClick={() => void loadDailyReport()}>{reportLoading ? "加载中" : "读取报告"}</button></div>
              <p className="decision-detail-copy">{dailyReport?.summary.decision || "读取后查看当日组合、持仓与调仓结论。"}</p>
            </section>
          </div>
        </details>
        </>}
      </div>
    </section>
  );
}
