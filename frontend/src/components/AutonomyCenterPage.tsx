import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchEvolutionTasks,
  fetchOwnerDailySummary,
  fetchResearchDataAssets,
  fetchTaskCenter,
  type EvolutionTask,
  type OwnerDailySummaryResponse,
  type ResearchDataAssets,
  type TaskCenterResponse,
} from "../api/modelStrategy";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";
import { formatGmt8Time } from "../utils/time";
import { fetchDailyUpdateStatus, fetchSimpleDataHealth, type DailyUpdateStatus, type SimpleDataHealth } from "../api/dataCenter";

type Props = { onNavigate: (section: SectionId) => void };
type IssueScope = "all" | "trade" | "promotion" | "data" | "owner";
type PipelineStatus = "done" | "running" | "waiting" | "blocked" | "failed" | "degraded";

type PipelineStation = {
  key: string;
  label: string;
  status: PipelineStatus;
  detail: string;
  next: string;
  target: SectionId;
};

function formatDuration(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return "正在计算完成时间";
  const minutes = Math.max(1, Math.ceil(value / 60));
  return minutes < 60 ? `预计约 ${minutes} 分钟完成` : `预计约 ${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟完成`;
}

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

const incidentModuleLabel: Record<string, string> = {
  sealed_holdout_data_gate: "密封验证集",
  decision_data_gate: "每日决策数据",
  periodic_performance_review: "收益复盘",
  official_policy_documents: "官方政策来源",
  research_data_assets: "研究数据资产",
  llm_data_operations: "LLM 数据运维",
  autonomous_self_healing: "确定性自愈",
};

const incidentReasonLabel: Record<string, string> = {
  blocked: "生产门未满足，当前流程已被安全阻断",
  module_returned_not_ok: "模块复核未通过，未向下游放行",
  attention: "复盘发现需要继续验证的表现信号",
  partial: "证据只有部分覆盖，尚未达到生产门",
  degraded: "本轮能力降级，系统已进入受限处理",
};

const incidentActionLabel: Record<string, string> = {
  data_backfill: "有界补数",
  daily_refresh: "刷新后复核",
  research_iteration: "生成受约束候选",
  source_observation: "继续观察官方源",
  research_observation: "研究态积累证据",
  bounded_recheck: "隔离并有界复核",
  next_cycle_retry: "下周期安全重试",
  diagnose_backfill_contract: "诊断补数无进展",
  build_research_capability: "补齐内部研究能力",
  diagnose_data_operations: "重新诊断数据运维",
};

const incidentNextCheck: Record<string, string> = {
  data_backfill: "下一自治周期检查密封集完整率；未完成前继续禁止消费。",
  daily_refresh: "收盘数据刷新后重新检查决策数据门，再决定是否恢复正式计划。",
  research_iteration: "候选产生后继续经过成本后 OOS、统计门和影子观察。",
  source_observation: "后续周期复查官方来源覆盖；当前不影响模拟交易安全主链。",
  research_observation: "新证据到达后重算生产门，达标前保持研究态。",
  bounded_recheck: "下一周期依据冻结证据重新诊断，继续禁止危险修改。",
  next_cycle_retry: "原任务退出后由下一周期复核，期间禁止同名任务并发重入。",
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
    ready: "就绪", passed: "一致", completed: "已完成", healthy: "健康", partial: "部分覆盖", degraded: "降级运行", running: "运行中",
    queued: "等待执行", retry_wait: "重试等待", failed: "失败", blocked: "阻断", unchanged: "无新增提案",
    cancelled: "已取消", audited_no_actionable_hard_gap: "已审计", insufficient_observations: "样本积累中", attention: "需关注",
    unmeasured_accuracy: "待验收", proxy_ready: "代理可用", configured: "已配置待取数",
  };
  return labels[normalized] || normalized || "待检查";
}

function pipelineStatus(value: unknown): PipelineStatus {
  const status = String(value || "").toLowerCase();
  if (["failed", "error"].includes(status)) return "failed";
  if (["blocked", "quarantined"].includes(status)) return "blocked";
  if (["degraded", "partial"].includes(status)) return "degraded";
  if (["running", "active"].includes(status)) return "running";
  if (["waiting", "queued", "observing", "retry_wait"].includes(status)) return "waiting";
  return "done";
}

const pipelineStatusLabel: Record<PipelineStatus, string> = {
  done: "已完成", running: "进行中", waiting: "等待条件", blocked: "安全阻断", failed: "失败", degraded: "降级运行",
};

function PipelineLine({ title, subtitle, stations, defaultOpen = false, onNavigate }: { title: string; subtitle: string; stations: PipelineStation[]; defaultOpen?: boolean; onNavigate: (section: SectionId) => void }) {
  const current = stations.find((item) => ["running", "blocked", "failed", "degraded"].includes(item.status)) || stations.find((item) => item.status === "waiting");
  return <details className={`autonomy-pipeline ${defaultOpen ? "primary" : ""}`} open={defaultOpen || undefined}>
    <summary><div><span>{title}</span><h3>{subtitle}</h3></div><b className={`pipeline-summary-${current?.status || "done"}`}>{current ? `${current.label} · ${pipelineStatusLabel[current.status]}` : "本轮已完成"}</b></summary>
    <div className="pipeline-track">{stations.map((station, index) => <div className="pipeline-station-wrap" key={station.key}>
      <button type="button" className={`pipeline-station status-${station.status}`} onClick={() => onNavigate(station.target)} title="点击进入对应模块查看任务、日志和证据">
        <i>{station.status === "done" ? "✓" : station.status === "running" ? "●" : station.status === "blocked" ? "!" : station.status === "failed" ? "×" : "…"}</i>
        <strong>{station.label}</strong>
        <b>{pipelineStatusLabel[station.status]}</b>
        <small>{station.detail}</small>
        <em>下一步：{station.next}</em>
      </button>
      {index < stations.length - 1 && <span className={`pipeline-rail rail-${station.status}`} />}
    </div>)}</div>
  </details>;
}

function toCount(value: number | undefined) { return Number.isFinite(value) ? Number(value) : 0; }

function incidentHealthText(value: string) {
  if (value === "failed" || value === "blocked") return "安全阻断";
  if (value === "degraded" || value === "partial") return "降级处理";
  return statusText(value);
}

function resolutionText(value?: string) {
  if (value === "waiting_condition") return "等待条件";
  if (value === "internal_fault") return "内部故障";
  if (value === "owner_required") return "需要所有者";
  return "系统自管";
}

function legacyResolutionClass(module: string) {
  if (["decision_data_gate", "periodic_performance_review", "official_policy_documents", "llm_data_operations"].includes(module)) return "waiting_condition";
  return "internal_fault";
}

function effectiveIncidentAction(module: string, resolutionClass: string | undefined, action: string) {
  if (resolutionClass !== "internal_fault") return action;
  if (module === "sealed_holdout_data_gate") return "diagnose_backfill_contract";
  if (module === "research_data_assets") return "build_research_capability";
  if (module === "llm_data_operations") return "diagnose_data_operations";
  return action;
}

function effectiveIncidentPolicy(module: string, resolutionClass: string | undefined, policy: string) {
  if (resolutionClass !== "internal_fault") return policy;
  if (module === "sealed_holdout_data_gate") return "补数已经执行但完整率没有增长，系统转入点时合同与缺失字段诊断";
  if (module === "research_data_assets") return "系统将缺失的数据接入或标注基准作为内部能力缺口推进，不能仅等待";
  return policy;
}

function progressText(progress?: { before?: number | null; after?: number | null; delta?: number | null; required?: number | null; unit?: string }) {
  if (!progress || progress.before == null || progress.after == null) return "";
  const total = progress.required == null ? "" : ` / ${progress.required}`;
  const delta = progress.delta == null ? "" : `（本轮 ${progress.delta > 0 ? "+" : ""}${progress.delta}）`;
  return `${progress.before} → ${progress.after}${total} ${progress.unit || ""}${delta}`.trim();
}

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
  const [liveHealth, setLiveHealth] = useState<SimpleDataHealth | null>(null);
  const [dailyUpdate, setDailyUpdate] = useState<DailyUpdateStatus | null>(null);
  const [taskControl, setTaskControl] = useState<TaskCenterResponse | null>(null);
  const [issueScope, setIssueScope] = useState<IssueScope>("all");

  const load = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const results = await Promise.allSettled([fetchOwnerDailySummary(), fetchEvolutionTasks(20), fetchResearchDataAssets(), fetchSimpleDataHealth(), fetchDailyUpdateStatus(), fetchTaskCenter(80, 300)]);
      const [owner, evolution, researchAssets, health, update, taskCenter] = results;
      if (owner.status === "fulfilled") setSummary(owner.value);
      if (evolution.status === "fulfilled") { setTasks(evolution.value.items ?? []); setSchedule(evolution.value.schedule); }
      if (researchAssets.status === "fulfilled") setAssets(researchAssets.value);
      if (health.status === "fulfilled") setLiveHealth(health.value);
      if (update.status === "fulfilled") setDailyUpdate(update.value);
      if (taskCenter.status === "fulfilled") setTaskControl(taskCenter.value);
      setError(results.every((item) => item.status === "rejected") ? "自治状态暂时无法读取，请稍后刷新" : "");
    } catch {
      setError("自治状态暂时无法读取，请稍后刷新");
    } finally { setRefreshing(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const questions = summary?.summary?.questions;
  const changes = questions?.system_changes;
  const supervision = questions?.incident_supervision;
  const incidentCases = (supervision?.cases ?? [])
    .filter((item) => item.module !== "autonomous_self_healing")
    .map((item) => ({ ...item, resolution_class: item.resolution_class || legacyResolutionClass(item.module) }));
  const internalFaultCases = incidentCases.filter((item) => item.resolution_class === "internal_fault");
  const waitingCases = incidentCases.filter((item) => item.resolution_class === "waiting_condition");
  const unclassifiedCases = incidentCases.filter((item) => !item.owner_action_required && item.owner !== "owner" && !["internal_fault", "waiting_condition", "owner_required"].includes(item.resolution_class || ""));
  const ownerCases = incidentCases.filter((item) => item.owner_action_required || item.owner === "owner");
  const incidentGroups = [
    { key: "internal_fault", title: "内部故障", detail: "系统必须采取实质修复，不能只等待", items: internalFaultCases },
    { key: "waiting_condition", title: "等待条件", detail: "等明确条件到达后自动复核；超时或无进展会转内部故障", items: waitingCases },
    { key: "owner_required", title: "需要所有者", detail: "仅限系统无法自行改变的外部权限或条件", items: ownerCases },
    { key: "system_managed", title: "系统自管", detail: "兼容旧任务的未分类记录", items: unclassifiedCases },
  ].filter((group) => group.items.length > 0);
  const latest = tasks.find((task) => task.task_type === "system_autonomy_cycle") ?? tasks[0];
  const incidentModules = new Set(incidentCases.map((item) => item.module));
  const taskAlerts = (taskControl?.tasks ?? []).filter((task) =>
    ["failed", "blocked", "degraded"].includes(task.status)
    && task.activity_scope !== "history"
    && !["autonomous_self_healing", "llm_incident_supervisor"].includes(task.task_type)
    && !incidentModules.has(task.task_type)
  );
  const issueItems = [
    ...incidentCases.map((item) => ({
      id: `incident:${item.module}`,
      scope: (item.owner_action_required || item.owner === "owner" ? "owner" : ["strategy", "model", "factor"].some((key) => item.module.includes(key)) ? "promotion" : ["decision", "execution", "portfolio"].some((key) => item.module.includes(key)) ? "trade" : "data") as IssueScope,
      title: incidentModuleLabel[item.module] || item.module,
      status: incidentHealthText(item.health),
      detail: item.summary || item.policy || item.reason,
      owner: item.owner_action_required || item.owner === "owner" ? "需要你处理" : "系统处理",
    })),
    ...taskAlerts.map((task) => ({
      id: task.id,
      scope: (task.module === "strategy" || task.module === "model" || task.module === "factor" ? "promotion" : task.module === "data" || task.module === "research" ? "data" : task.module === "execution" || task.module === "portfolio" ? "trade" : "all") as IssueScope,
      title: task.name,
      status: statusText(task.status),
      detail: task.error || task.message || task.current_step,
      owner: "系统处理",
    })),
  ];
  const visibleIssues = issueItems.filter((item) => issueScope === "all" || item.scope === issueScope);
  const noTrade = (() => {
    const plan = questions?.today_rebalance;
    const proposalCount = plan?.proposal_count ?? 0;
    const gate = changes?.champion_execution_gate;
    if (liveHealth && liveHealth.level !== "healthy") return { title: "没有正式交易", reason: liveHealth.summary || "实时决策数据门未通过", abnormal: false, permission: "禁止生成新订单，等待数据门恢复" };
    if (ownerCases.length) return { title: "没有正式交易", reason: ownerCases[0]?.summary || "存在需要所有者处理的外部条件", abnormal: true, permission: "正式执行暂缓" };
    if (plan?.status === "planned" && proposalCount > 0) return { title: "已有调仓建议", reason: `已生成 ${proposalCount} 项可执行动作${gate?.status === "quarantined" ? "；Champion 隔离期间仅包含减仓或卖出" : ""}。`, abnormal: false, permission: gate?.status === "quarantined" ? "仅允许减仓 / 卖出" : "允许正式模拟执行" };
    if (plan?.status === "executing") return { title: "调仓正在原子落账", reason: plan.blocked_reason || "正式计划已被唯一执行器领取，成交、任务记录和计划状态正在提交。", abnormal: false, permission: "禁止其他任务重复领取" };
    if (plan?.status?.startsWith("blocked_by_")) return { title: "计划被安全阻断", reason: plan.blocked_reason || "执行前置检查未通过", abnormal: true, permission: "不会生成订单" };
    if (gate?.status === "quarantined") return { title: "没有新增交易", reason: "当前 Champion 正式 OOS 未通过，系统处于研究隔离，仅允许降低已有风险。", abnormal: false, permission: "仅允许减仓 / 卖出" };
    return { title: "本轮没有交易", reason: "系统完成了数据、信号和组合检查，但没有产生满足交易约束且值得覆盖成本的调仓动作。", abnormal: false, permission: "正常等待下一轮决策" };
  })();
  const moduleStatus = (key: string) => taskControl?.modules.find((item) => item.key === key);
  const stationFromModule = (key: string, label: string, target: SectionId, fallback: PipelineStatus = "waiting"): PipelineStation => {
    const module = moduleStatus(key);
    const status = module ? pipelineStatus(module.status) : fallback;
    return { key, label, status, detail: module?.current_step || "等待任务中心返回状态", next: module?.next_step || "等待下一自治周期复核", target };
  };
  const dailyStations: PipelineStation[] = [
    { key: "close-data", label: "收盘数据", status: liveHealth?.level === "healthy" ? "done" : liveHealth ? "blocked" : "waiting", detail: liveHealth?.summary || "等待实时数据门", next: liveHealth?.level === "healthy" ? "进入质量检查" : "刷新数据后重试", target: "dataCenter" },
    { key: "decision-gate", label: "决策数据门", status: liveHealth?.level === "healthy" ? "done" : "blocked", detail: liveHealth?.summary || "检查 daily、估值、涨跌停和快照覆盖", next: liveHealth?.level === "healthy" ? "冻结今日决策输入" : "补齐缺失数据", target: "dataCenter" },
    stationFromModule("factor", "因子与信号", "factorLab"),
    stationFromModule("strategy", "策略控制器", "strategyLab"),
    stationFromModule("portfolio", "组合优化", "portfolioWorkbench"),
    { key: "plan", label: "调仓计划", status: noTrade.title === "已有调仓建议" ? "done" : noTrade.abnormal ? "blocked" : "waiting", detail: noTrade.reason, next: noTrade.title === "已有调仓建议" ? `计划执行：${questions?.today_rebalance.planned_execute_at ? formatGmt8Time(questions.today_rebalance.planned_execute_at) : questions?.today_rebalance.planned_execute_date || "下一交易日"}` : "下一轮收盘后重新评估", target: "portfolioWorkbench" },
    { key: "execution", label: "模拟执行", status: questions?.today_rebalance.status === "executed" ? "done" : questions?.today_rebalance.status === "executing" ? "running" : questions?.today_rebalance.status === "planned" ? "waiting" : questions?.today_rebalance.status?.startsWith("blocked_by_") ? "blocked" : "waiting", detail: questions?.today_rebalance.status === "executing" ? "唯一执行租约已领取，正在原子写入成交、任务记录和计划状态" : questions?.today_rebalance.status === "planned" ? `已接收 ${questions.today_rebalance.proposal_count} 项订单，尚未到执行时间` : questions?.today_rebalance.status === "executed" ? "本计划已写入模拟成交与账本" : "当前没有可执行计划", next: questions?.today_rebalance.status === "executing" ? "提交完成后自动转为已执行；中断则隔离复核，禁止重放" : questions?.today_rebalance.status === "planned" ? `到 ${questions.today_rebalance.planned_execute_at ? formatGmt8Time(questions.today_rebalance.planned_execute_at) : questions.today_rebalance.planned_execute_date || "计划时间"} 自动执行` : "等待正式计划", target: "taskCenter" },
    { key: "ledger", label: "账本与收益", status: changes?.ledger_status === "failed" ? "failed" : changes?.ledger_status === "passed" ? "done" : "waiting", detail: `账本状态：${statusText(changes?.ledger_status)}`, next: "完成成交、费用和收益归因", target: "portfolioWorkbench" },
  ];
  const strategyStations: PipelineStation[] = [
    stationFromModule("review", "复盘归因", "reviewWorkbench"),
    stationFromModule("strategy", "候选生成", "strategyLab"),
    { key: "oos", label: "正式 OOS", status: taskControl?.tasks.some((task) => task.task_type.includes("evolution") && task.status === "running") ? "running" : changes?.strategy_candidates_ready_for_review ? "waiting" : "done", detail: `${toCount(changes?.strategy_candidates_re_evaluated)} 个候选已重评`, next: "通过统计门后进入 Shadow", target: "strategyLab" },
    stationFromModule("strategy", "未来 Shadow", "strategyLab"),
    { key: "champion", label: "Champion / 回退", status: changes?.champion_execution_gate?.status === "quarantined" ? "blocked" : "done", detail: changes?.champion_execution_gate?.reason || "当前 Champion 继续服务生产", next: "持续 Cooling 观察", target: "strategyLab" },
  ];
  const dataModelStations: PipelineStation[] = [
    (() => {
      const module = moduleStatus("data");
      const checkpoint = module?.checkpoint;
      if (checkpoint?.schema_version !== "alis_sealed_holdout_adj_factor_backfill_v1") return stationFromModule("data", "数据补全", "dataCenter");
      const eligible = Number(checkpoint.eligible_sessions || 0);
      const required = Number(checkpoint.required_sessions || 126);
      const batch = Number(checkpoint.current_batch_size || checkpoint.batch_size || 0);
      const speed = Number(checkpoint.sessions_per_minute || 0);
      const checkpointEta = checkpoint.estimated_remaining_seconds == null ? null : Number(checkpoint.estimated_remaining_seconds);
      return {
        key: "data", label: "数据补全", status: pipelineStatus(module?.status), target: "taskCenter",
        detail: `密封验证集 adj_factor ${eligible}/${required}；当前批次 ${batch} 日；速度 ${speed > 0 ? `${speed.toFixed(1)} 日/分钟` : "计算中"}`,
        next: `继续分批补全并自动复核；${formatDuration(checkpointEta ?? module?.estimated_remaining_seconds)}`,
      };
    })(),
    stationFromModule("factor", "因子评价", "factorLab"),
    stationFromModule("model", "模型训练", "strategyLab"),
    stationFromModule("model", "模型治理", "strategyLab"),
    stationFromModule("research", "研究资产", "dataCenter"),
  ];
  const completedSteps = useMemo(() => {
    const raw = latest?.result?.steps;
    return Array.isArray(raw) ? raw.filter((item) => item && typeof item === "object") as Array<{ name?: string; ok?: boolean; result?: { status?: string } }> : [];
  }, [latest]);
  const healthy = Boolean(summary?.available) && ownerCases.length === 0 && taskAlerts.length === 0 && latest?.status === "completed";
  const performanceNeedsReview = changes?.performance_review_status === "attention";
  const changeCount = toCount(changes?.model_auto_promoted) + toCount(changes?.model_auto_rejected) + toCount(changes?.model_training_started) + toCount(changes?.strategy_candidates_created) + toCount(changes?.corporate_actions_applied);
  const industryReady = assets ? Object.values(assets.industry_components).filter((item) => item.status === "ready").length : 0;

  if (!summary && !error) return <LoadingState variant="page" title="正在读取自治系统" detail="汇总任务、研究资产与候选状态" rows={4} />;

  return <main className="autonomy-page">
    <header className="autonomy-head">
      <div><span>ALIS AUTONOMY</span><h2>自治中心</h2><p>每天只需在这里确认系统是否正常运行、资产是否可靠，以及研究正在向哪里演化。</p></div>
      <div className="autonomy-head-actions"><time>{summary?.finished_at ? `最近完整周期 ${formatGmt8Time(summary.finished_at)}` : "等待首个自治周期"}</time><button type="button" onClick={() => void load(true)} disabled={refreshing}>{refreshing ? "刷新中" : "刷新"}</button></div>
    </header>

    {error && <div className="autonomy-error">{error}</div>}
    <section className="autonomy-pipeline-board">
      <header><div><span>ALIS AUTONOMY MAP</span><h2>系统自治流水线</h2><p>沿线路查看系统当前走到哪一步、卡在哪里，以及下一步会做什么。点击站点可进入对应任务和证据。</p></div><button type="button" onClick={() => onNavigate("taskCenter")}>打开任务中心</button></header>
      <PipelineLine title="每日生产线" subtitle="从收盘数据到模拟执行与收益归因" stations={dailyStations} defaultOpen onNavigate={onNavigate} />
      <PipelineLine title="策略进化线" subtitle="从复盘归因到候选晋升与自动回退" stations={strategyStations} onNavigate={onNavigate} />
      <PipelineLine title="数据模型线" subtitle="数据、因子、训练和治理的后台维护" stations={dataModelStations} onNavigate={onNavigate} />
    </section>
    <section className={`autonomy-command ${healthy ? "healthy" : "attention"}`}>
      <div><span>{healthy ? "自治安全运行中" : "需要外部条件"}</span><strong>{healthy ? (supervision?.status === "self_managed" ? "内部问题正在由系统自行消化" : performanceNeedsReview ? "系统运行正常，策略表现由后台复盘" : "系统正在按计划自行运行") : ownerCases.length ? `${ownerCases.length} 项外部条件需要你提供` : "等待当前周期完成"}</strong><small>{healthy ? (supervision?.status === "self_managed" ? `${incidentCases.length} 项原始问题已分类：内部故障 ${internalFaultCases.length} 项、等待条件 ${waitingCases.length} 项，不需要你进入模块处理。` : performanceNeedsReview ? "自动复盘会据此生成并验证后续候选，不要求人工审批。" : "数据、模型、策略、组合和研究模块均由后台任务协调执行。") : (ownerCases[0]?.summary || ownerCases[0]?.policy || latest?.message || "系统会明确说明缺少的凭证、权限或外部服务。")}{liveHealth && liveHealth.level !== "healthy" ? ` 实时决策门：${liveHealth.summary}；日更状态：${dailyUpdate?.phase === "cooldown" ? "冷却后自动复核" : dailyUpdate?.message || "系统处理中"}。` : ""}</small></div>
      <dl><div><dt>今日调仓</dt><dd>{questions?.today_rebalance.proposal_count ?? 0} 项</dd></div><div><dt>系统变化</dt><dd>{changeCount} 项</dd></div><div><dt>后台告警</dt><dd>{taskAlerts.length} 项</dd></div><div><dt>账本</dt><dd>{statusText(changes?.ledger_status)}</dd></div></dl>
    </section>

    {taskAlerts.length > 0 && <section className="autonomy-card autonomy-alert-panel">
      <header><div><span>需要关注</span><h3>后台任务失败与安全阻断</h3></div><button type="button" onClick={() => onNavigate("taskCenter")}>打开任务中心</button></header>
      <div className="disposal-list">{taskAlerts.slice(0, 8).map((task) => <article className="system-owned" key={task.id}>
        <div className="disposal-identity"><b>{task.name}</b><span>{statusText(task.status)}</span><small>{task.module} · {task.current_step}</small></div>
        <dl><div><dt>错误/状态</dt><dd>{task.error || task.message || "任务未提供错误说明"}</dd></div><div><dt>下一步</dt><dd>{task.next_step || "等待任务中心复核"}</dd></div><div><dt>时间</dt><dd>{task.updated_at ? formatGmt8Time(task.updated_at) : "—"} · 尝试 {task.attempt}/{task.max_attempts}</dd></div></dl>
      </article>)}</div>
      {taskAlerts.length > 8 && <footer><span>还有 {taskAlerts.length - 8} 项告警，请进入任务中心查看全部错误证据。</span></footer>}
    </section>}

    <section className="autonomy-card autonomy-issues-panel">
      <header><div><span>问题筛选</span><h3>按影响范围查看阻塞</h3></div><div className="issue-scope-tabs">{([['all', '全部问题'], ['trade', '影响今日交易'], ['promotion', '影响策略晋升'], ['data', '影响数据完整性'], ['owner', '需要我处理']] as Array<[IssueScope, string]>).map(([key, label]) => <button key={key} type="button" className={issueScope === key ? "active" : ""} onClick={() => setIssueScope(key)}>{label}<b>{issueItems.filter((item) => key === "all" || item.scope === key).length}</b></button>)}</div></header>
      {visibleIssues.length ? <div className="issue-scope-list">{visibleIssues.map((item) => <article key={item.id}><span className={`issue-scope-dot scope-${item.scope}`} /><div><strong>{item.title}</strong><small>{item.status} · {item.owner}</small></div><p>{item.detail || "暂无详细说明"}</p></article>)}</div> : <p className="disposal-empty">当前范围没有需要处理的阻塞或异常。</p>}
    </section>

    <section className={`autonomy-card no-trade-panel ${noTrade.abnormal ? "abnormal" : "normal"}`}>
      <header><div><span>今日生产结论</span><h3>系统现在为什么没有交易</h3></div><b>{noTrade.abnormal ? "需要关注" : "正常状态"}</b></header>
      <div className="no-trade-grid"><div><span>结论</span><strong>{noTrade.title}</strong></div><div><span>原因</span><strong>{noTrade.reason}</strong></div><div><span>当前权限</span><strong>{noTrade.permission}</strong></div><div><span>是否异常</span><strong>{noTrade.abnormal ? "是，需要运维处理" : "否，属于安全门或正常决策结果"}</strong></div></div>
    </section>

    <section className="autonomy-card autonomy-disposal-log">
      <header>
        <div><span>自治处置日志</span><h3>系统发现了什么，如何自行消化</h3></div>
        <div className="disposal-run-meta"><b className={supervision?.llm?.status === "completed" ? "good" : "neutral"}>{supervision?.llm?.status === "completed" ? "LLM 已解释" : "规则已接管"}</b><small>任务 #{latest?.id ?? "—"} · {latest?.finished_at ? formatGmt8Time(latest.finished_at) : "等待完成"}</small></div>
      </header>
      {incidentCases.length ? <div className="disposal-list">{incidentGroups.map((group) => <section className={`disposal-group ${group.key}`} key={group.key}>
        <header><div><strong>{group.title}</strong><small>{group.detail}</small></div><b>{group.items.length} 项</b></header>
        {group.items.map((item, index) => {
          const ownerRequired = item.owner_action_required || item.owner === "owner";
          const progress = progressText(item.progress);
          const displayAction = effectiveIncidentAction(item.module, item.resolution_class, item.action);
          const displayPolicy = effectiveIncidentPolicy(item.module, item.resolution_class, item.system_action || item.policy);
          return <article className={`${ownerRequired ? "owner-required" : "system-owned"} resolution-${item.resolution_class || "legacy"}`} key={`${item.module}-${index}`}>
            <div className="disposal-identity"><b>{incidentModuleLabel[item.module] || item.module.replace(/_/g, " ")}</b><span>{resolutionText(item.resolution_class)}</span><small>{ownerRequired ? "等待外部条件" : "处理主体：系统"}</small></div>
            <dl>
              <div><dt>触发依据</dt><dd>{incidentReasonLabel[item.reason] || item.reason.replace(/_/g, " ")}</dd></div>
              <div><dt>系统动作</dt><dd><strong>{incidentActionLabel[displayAction] || displayAction.replace(/_/g, " ")}</strong>{displayPolicy}{progress && <em>实质进展：{progress}</em>}</dd></div>
              <div><dt>{item.resolution_class === "waiting_condition" ? "等待与复核" : "修复与验收"}</dt><dd>{item.next_check || item.retry_policy || item.waiting_for || incidentNextCheck[item.action] || "下一自治周期按冻结证据重新检查。"}</dd></div>
            </dl>
            <div className="disposal-state"><span className={item.resolution_class === "internal_fault" ? "warn" : statusTone(item.health)}>{item.resolution_class === "internal_fault" ? "修复中" : incidentHealthText(item.health)}</span><small>{item.summary || (ownerRequired ? "等待外部条件，不会绕过安全门。" : item.resolution_class === "waiting_condition" ? item.waiting_for || "等待明确条件到达。" : "系统继续诊断和修复，无需人工进入模块。")}</small></div>
          </article>;
        })}
      </section>)}</div> : <p className="disposal-empty">当前周期未发现需要处置的问题。</p>}
      <footer><span>处理主体：{supervision?.llm?.status === "completed" ? `${supervision.llm.model || "LLM"} 解释 + 确定性治理执行` : "确定性治理接管"}</span><span>内部故障 {supervision?.internal_fault_count ?? internalFaultCases.length} · 等待条件 {supervision?.waiting_condition_count ?? waitingCases.length} · 需要所有者 {supervision?.owner_action_required_count ?? 0}</span></footer>
    </section>

    <section className="autonomy-kpis">
      <article><span>正式数据</span><strong className={statusTone(changes?.hard_data_status)}>{statusText(changes?.hard_data_status)}</strong><small>策略 OOS 永久硬数据</small></article>
      <article><span>策略候选</span><strong>{toCount(changes?.strategy_candidates_created)}</strong><small>本周期生成 · 已重评 {toCount(changes?.strategy_candidates_re_evaluated)}</small></article>
      <article><span>成本样本</span><strong>{toCount(changes?.execution_cost_observations)}</strong><small>{statusText(changes?.execution_cost_status)}</small></article>
      <article><span>LLM 研究</span><strong>{toCount(changes?.llm_research_routed_count)}</strong><small>已路由实验 · 提案 {toCount(changes?.llm_research_proposal_count)}</small></article>
    </section>

    <section className="autonomy-grid">
      <details className="autonomy-card autonomy-cycle-card"><summary><div><span>技术轨迹</span><h3>本轮 27 步执行明细</h3></div><b className={statusTone(latest?.status)}>{statusText(latest?.status)}</b></summary>
        <div className="cycle-meta"><span>{taskLabel[latest?.task_type || ""] || latest?.task_type || "暂无任务"}</span><small>{latest?.finished_at ? formatGmt8Time(latest.finished_at) : latest?.message || "等待任务"}</small></div>
        <div className="cycle-steps">{completedSteps.length ? completedSteps.map((step) => <div key={step.name}><i className={step.ok ? "done" : "failed"} /><span>{stepLabel[step.name || ""] || step.name}</span><small>{statusText(step.result?.status || (step.ok ? "completed" : "failed"))}{step.result && typeof step.result === "object" && "error" in step.result ? ` · ${String((step.result as { error?: unknown }).error || "")}` : ""}</small></div>) : <p>本次任务尚未返回步骤明细。</p>}</div>
        <div><button type="button" onClick={() => onNavigate("strategyLab")}>查看策略验证</button> <button type="button" onClick={() => onNavigate("taskCenter")}>查看完整任务与错误证据</button></div>
      </details>

      <article className="autonomy-card autonomy-schedule-card"><header><div><span>后台节奏</span><h3>下一轮自动执行</h3></div><b className={schedule?.auto_enabled ? "good" : "warn"}>{schedule?.auto_enabled ? "已启用" : "未启用"}</b></header>
        <dl><div><dt>因子与影子</dt><dd>{schedule?.next_factor_cycle_at ? formatGmt8Time(schedule.next_factor_cycle_at) : "—"}</dd></div><div><dt>每日自治</dt><dd>{schedule?.next_system_cycle_at ? formatGmt8Time(schedule.next_system_cycle_at) : "—"}</dd></div><div><dt>正式演化</dt><dd>{schedule?.next_formal_cycle_at ? formatGmt8Time(schedule.next_formal_cycle_at) : "—"}</dd></div></dl>
        <p>单工作器 · 每周期最多 {schedule?.budgets.max_candidates_per_cycle ?? "—"} 个候选 · 单任务时限 {schedule?.budgets.task_timeout_minutes ?? "—"} 分钟</p>
      </article>
    </section>

    <details className="autonomy-card autonomy-research-card"><summary><div><span>研究技术明细</span><h3>证据覆盖与当前限制</h3></div><b className={statusTone(assets?.status)}>{statusText(assets?.status)}</b></summary>
      <div className="research-detail-body"><div className="research-depth-grid"><article><span>行业经营字段</span><strong>{industryReady}/7</strong><small>{assets?.industry_components.product_price?.status === "proxy_ready" ? "产品价格：期货代理可用" : "已具备直接点时序列"}</small></article><article><span>一致预期</span><strong>{assets?.analyst_consensus_pit.symbols?.toLocaleString("zh-CN") ?? "—"}</strong><small>{statusText(assets?.analyst_consensus_pit.status)} · 覆盖股票</small></article><article><span>主题收入暴露</span><strong>{assets?.theme_exposure.revenue_companies?.toLocaleString("zh-CN") ?? "—"}</strong><small>{assets?.theme_exposure.revenue_exposures?.toLocaleString("zh-CN") ?? "—"} 条点时分部记录</small></article><article><span>公告抽取</span><strong>{assets?.announcement_extraction.events?.toLocaleString("zh-CN") ?? "—"}</strong><small>{statusText(assets?.announcement_extraction.status)} · 仍待标注验收</small></article></div>
        <div className="research-gaps"><strong>当前限制</strong><span>{assets?.gaps?.length ? assets.gaps.map(researchGapText).join(" · ") : "研究资产合同完整"}</span></div>
        <button type="button" onClick={() => onNavigate("dataCenter")}>查看数据资产</button></div>
    </details>

    <section className="autonomy-card autonomy-exceptions"><header><div><span>任务索引</span><h3>最近后台任务</h3></div><button type="button" onClick={() => onNavigate("backendLogs")}>查看原始日志</button></header><div className="task-table">{tasks.map((task) => <article key={task.id}><div><b className={statusTone(task.status)}>{statusText(task.status)}</b><strong>{taskLabel[task.task_type] || task.task_type}</strong><small>{task.message}</small></div><span>{task.attempt}/{task.max_attempts} 次</span><time>{task.finished_at ? formatGmt8Time(task.finished_at) : formatGmt8Time(task.updated_at)}</time></article>)}</div></section>
  </main>;
}
