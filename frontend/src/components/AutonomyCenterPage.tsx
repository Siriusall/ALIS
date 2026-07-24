import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchEvolutionTasks,
  fetchOwnerDailySummary,
  fetchResearchDataAssets,
  type EvolutionTask,
  type OwnerDailySummaryResponse,
  type ResearchDataAssets,
} from "../api/modelStrategy";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";
import { formatGmt8Time } from "../utils/time";

type Props = { onNavigate: (section: SectionId) => void };

const taskLabel: Record<string, string> = {
  system_autonomy_cycle: "每日自治循环",
  factor_daily_cycle: "因子日评价",
  strategy_shadow_cycle: "策略影子观察",
  strategy_evolution_cycle: "正式策略演化",
};

const stepLabel: Record<string, string> = {
  hard_data_gate: "硬数据门", plan_model_autonomy: "模型自治", periodic_performance_review: "周期复盘",
  review_data_quality_governance: "数据治理", strategy_policy_maturity: "策略候选成熟度",
  strategy_policy_candidates: "策略候选", execution_cost_calibration: "执行成本", simulation_corporate_actions: "公司行动",
  simulation_ledger_audit: "模拟账本", profit_and_portfolio: "收益与组合", research_maintenance: "投研维护",
  free_industry_financials: "行业财务序列", futures_price_proxies: "期货价格代理",
  theme_revenue_exposure: "主题收入暴露", research_data_assets: "研究资产审计", llm_research_agent: "LLM 研究",
  llm_research_governance: "提案质量门", llm_proposal_router: "实验路由",
};

function statusTone(value?: string | null) {
  if (["ready", "passed", "completed", "healthy", "active", "production"].includes(String(value))) return "good";
  if (["failed", "blocked", "degraded", "partial"].includes(String(value))) return "warn";
  return "neutral";
}

function statusText(value?: unknown) {
  const nestedStatus = value && typeof value === "object" && "status" in value ? (value as { status?: unknown }).status : value;
  const normalized = typeof nestedStatus === "string" ? nestedStatus : "completed";
  const labels: Record<string, string> = {
    ready: "就绪", passed: "一致", completed: "已完成", healthy: "健康", partial: "部分覆盖", running: "运行中",
    queued: "等待执行", retry_wait: "重试等待", failed: "失败", blocked: "阻断", unchanged: "无新增提案",
    audited_no_actionable_hard_gap: "已审计", insufficient_observations: "样本积累中", attention: "需关注",
    unmeasured_accuracy: "待验收", proxy_ready: "代理可用", configured: "已配置待取数",
  };
  return labels[normalized] || normalized || "待检查";
}

function toCount(value: number | undefined) { return Number.isFinite(value) ? Number(value) : 0; }

function researchGapText(value: string) {
  const labels: Record<string, string> = {
    "industry:product_price": "行业产品价格尚未接入",
    "industry:product_price:direct_source_not_integrated": "行业产品价格已接入期货代理，直接现货源尚未接入",
    "industry:inventory_cycle": "行业库存周期尚未接入",
    "industry:capacity_utilization": "行业产能利用率尚未接入",
    "industry:profit_diffusion": "行业盈利扩散尚未接入",
    "industry:policy_supply": "行业政策与供给尚未接入",
    "industry:industry_valuation": "行业估值序列尚未接入",
    "announcement_extraction_accuracy:labelled_benchmark_not_integrated": "公告抽取准确率的标注基准尚未接入",
  };
  return labels[value] || value.replace(/[:_]/g, " ");
}

export default function AutonomyCenterPage({ onNavigate }: Props) {
  const [summary, setSummary] = useState<OwnerDailySummaryResponse | null>(null);
  const [tasks, setTasks] = useState<EvolutionTask[]>([]);
  const [schedule, setSchedule] = useState<Awaited<ReturnType<typeof fetchEvolutionTasks>>["schedule"] | null>(null);
  const [assets, setAssets] = useState<ResearchDataAssets | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const [owner, evolution, researchAssets] = await Promise.all([fetchOwnerDailySummary(), fetchEvolutionTasks(8), fetchResearchDataAssets()]);
      setSummary(owner); setTasks(evolution.items ?? []); setSchedule(evolution.schedule); setAssets(researchAssets); setError("");
    } catch {
      setError("自治状态暂时无法读取，请稍后刷新");
    } finally { setRefreshing(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const questions = summary?.summary?.questions;
  const changes = questions?.system_changes;
  const anomalies = questions?.anomalies ?? [];
  const latest = tasks.find((task) => task.task_type === "system_autonomy_cycle") ?? tasks[0];
  const completedSteps = useMemo(() => {
    const raw = latest?.result?.steps;
    return Array.isArray(raw) ? raw.filter((item) => item && typeof item === "object") as Array<{ name?: string; ok?: boolean; result?: { status?: string } }> : [];
  }, [latest]);
  const healthy = Boolean(summary?.available) && anomalies.length === 0 && latest?.status === "completed";
  const performanceNeedsReview = changes?.performance_review_status === "attention";
  const changeCount = toCount(changes?.model_auto_promoted) + toCount(changes?.model_auto_rejected) + toCount(changes?.model_training_started) + toCount(changes?.strategy_candidates_created) + toCount(changes?.corporate_actions_applied);
  const industryReady = assets ? Object.values(assets.industry_components).filter((item) => item.status === "ready").length : 0;

  if (!summary && !error) return <LoadingState variant="page" title="正在读取自治系统" detail="汇总任务、研究资产与候选状态" rows={4} />;

  return <main className="autonomy-page">
    <header className="autonomy-head">
      <div><span>ALIS AUTONOMY</span><h2>自治中心</h2><p>每天只需在这里确认系统是否正常运行、资产是否可靠，以及研究正在向哪里演化。</p></div>
      <div className="autonomy-head-actions"><time>{summary?.finished_at ? `最近完成 ${formatGmt8Time(summary.finished_at)}` : "等待首个自治周期"}</time><button type="button" onClick={() => void load(true)} disabled={refreshing}>{refreshing ? "刷新中" : "刷新"}</button></div>
    </header>

    {error && <div className="autonomy-error">{error}</div>}
    <section className={`autonomy-command ${healthy ? "healthy" : "attention"}`}>
      <div><span>{healthy ? "自治运行正常" : "需要关注"}</span><strong>{healthy ? (performanceNeedsReview ? "系统运行正常，策略表现需复盘" : "系统正在按计划自行运行") : anomalies.length ? `${anomalies.length} 个模块需要检查` : "等待当前周期完成"}</strong><small>{healthy ? (performanceNeedsReview ? "自动复盘已识别到收益或交易质量信号，系统会据此生成并验证后续候选。" : "数据、模型、策略、组合和研究模块均由后台任务协调执行。") : (anomalies[0]?.message || latest?.message || "请查看下方任务和异常详情。")}</small></div>
      <dl><div><dt>今日调仓</dt><dd>{questions?.today_rebalance.proposal_count ?? 0} 项</dd></div><div><dt>系统变化</dt><dd>{changeCount} 项</dd></div><div><dt>账本</dt><dd>{statusText(changes?.ledger_status)}</dd></div></dl>
    </section>

    <section className="autonomy-kpis">
      <article><span>正式数据</span><strong className={statusTone(changes?.hard_data_status)}>{statusText(changes?.hard_data_status)}</strong><small>策略 OOS 永久硬数据</small></article>
      <article><span>策略候选</span><strong>{toCount(changes?.strategy_candidates_created)}</strong><small>本周期生成 · 已重评 {toCount(changes?.strategy_candidates_re_evaluated)}</small></article>
      <article><span>成本样本</span><strong>{toCount(changes?.execution_cost_observations)}</strong><small>{statusText(changes?.execution_cost_status)}</small></article>
      <article><span>LLM 研究</span><strong>{toCount(changes?.llm_research_routed_count)}</strong><small>已路由实验 · 提案 {toCount(changes?.llm_research_proposal_count)}</small></article>
    </section>

    <section className="autonomy-grid">
      <article className="autonomy-card autonomy-cycle-card"><header><div><span>当前闭环</span><h3>自治周期</h3></div><b className={statusTone(latest?.status)}>{statusText(latest?.status)}</b></header>
        <div className="cycle-meta"><span>{taskLabel[latest?.task_type || ""] || latest?.task_type || "暂无任务"}</span><small>{latest?.finished_at ? formatGmt8Time(latest.finished_at) : latest?.message || "等待任务"}</small></div>
        <div className="cycle-steps">{completedSteps.length ? completedSteps.map((step) => <div key={step.name}><i className={step.ok ? "done" : "failed"} /><span>{stepLabel[step.name || ""] || step.name}</span><small>{statusText(step.result?.status || (step.ok ? "completed" : "failed"))}</small></div>) : <p>本次任务尚未返回步骤明细。</p>}</div>
        <button type="button" onClick={() => onNavigate("strategyLab")}>查看策略验证</button>
      </article>

      <article className="autonomy-card autonomy-schedule-card"><header><div><span>后台节奏</span><h3>下一轮自动执行</h3></div><b className={schedule?.auto_enabled ? "good" : "warn"}>{schedule?.auto_enabled ? "已启用" : "未启用"}</b></header>
        <dl><div><dt>因子与影子</dt><dd>{schedule?.next_factor_cycle_at ? formatGmt8Time(schedule.next_factor_cycle_at) : "—"}</dd></div><div><dt>每日自治</dt><dd>{schedule?.next_system_cycle_at ? formatGmt8Time(schedule.next_system_cycle_at) : "—"}</dd></div><div><dt>正式演化</dt><dd>{schedule?.next_formal_cycle_at ? formatGmt8Time(schedule.next_formal_cycle_at) : "—"}</dd></div></dl>
        <p>单工作器 · 每周期最多 {schedule?.budgets.max_candidates_per_cycle ?? "—"} 个候选 · 单任务时限 {schedule?.budgets.task_timeout_minutes ?? "—"} 分钟</p>
      </article>
    </section>

    <section className="autonomy-card autonomy-research-card"><header><div><span>研究数据深度</span><h3>哪些证据已经能进入系统</h3></div><b className={statusTone(assets?.status)}>{statusText(assets?.status)}</b></header>
      <div className="research-depth-grid"><article><span>行业经营字段</span><strong>{industryReady}/7</strong><small>{assets?.industry_components.product_price?.status === "proxy_ready" ? "产品价格：期货代理可用" : "已具备直接点时序列"}</small></article><article><span>一致预期</span><strong>{assets?.analyst_consensus_pit.symbols?.toLocaleString("zh-CN") ?? "—"}</strong><small>{statusText(assets?.analyst_consensus_pit.status)} · 覆盖股票</small></article><article><span>主题收入暴露</span><strong>{assets?.theme_exposure.revenue_companies?.toLocaleString("zh-CN") ?? "—"}</strong><small>{assets?.theme_exposure.revenue_exposures?.toLocaleString("zh-CN") ?? "—"} 条点时分部记录</small></article><article><span>公告抽取</span><strong>{assets?.announcement_extraction.events?.toLocaleString("zh-CN") ?? "—"}</strong><small>{statusText(assets?.announcement_extraction.status)} · 仍待标注验收</small></article></div>
      <div className="research-gaps"><strong>当前限制</strong><span>{assets?.gaps?.length ? assets.gaps.map(researchGapText).join(" · ") : "研究资产合同完整"}</span></div>
      <button type="button" onClick={() => onNavigate("dataCenter")}>查看数据资产</button>
    </section>

    <section className="autonomy-card autonomy-exceptions"><header><div><span>运行记录</span><h3>最近后台任务</h3></div><button type="button" onClick={() => onNavigate("backendLogs")}>查看日志</button></header><div className="task-table">{tasks.map((task) => <article key={task.id}><div><b className={statusTone(task.status)}>{statusText(task.status)}</b><strong>{taskLabel[task.task_type] || task.task_type}</strong><small>{task.message}</small></div><span>{task.attempt}/{task.max_attempts} 次</span><time>{task.finished_at ? formatGmt8Time(task.finished_at) : formatGmt8Time(task.updated_at)}</time></article>)}</div>
      {anomalies.length > 0 && <div className="autonomy-anomalies">{anomalies.map((item) => <p key={`${item.module}-${item.message}`}><strong>{item.module}</strong>{item.message}</p>)}</div>}
    </section>
  </main>;
}
