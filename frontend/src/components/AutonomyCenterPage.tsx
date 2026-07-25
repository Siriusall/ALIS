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
import { fetchDailyUpdateStatus, fetchSimpleDataHealth, type DailyUpdateStatus, type SimpleDataHealth } from "../api/dataCenter";

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

  const load = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const [owner, evolution, researchAssets, health, update] = await Promise.all([fetchOwnerDailySummary(), fetchEvolutionTasks(8), fetchResearchDataAssets(), fetchSimpleDataHealth(), fetchDailyUpdateStatus()]);
      setSummary(owner); setTasks(evolution.items ?? []); setSchedule(evolution.schedule); setAssets(researchAssets); setError("");
      setLiveHealth(health); setDailyUpdate(update);
    } catch {
      setError("自治状态暂时无法读取，请稍后刷新");
    } finally { setRefreshing(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);

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
  const completedSteps = useMemo(() => {
    const raw = latest?.result?.steps;
    return Array.isArray(raw) ? raw.filter((item) => item && typeof item === "object") as Array<{ name?: string; ok?: boolean; result?: { status?: string } }> : [];
  }, [latest]);
  const healthy = Boolean(summary?.available) && ownerCases.length === 0 && latest?.status === "completed";
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
    <section className={`autonomy-command ${healthy ? "healthy" : "attention"}`}>
      <div><span>{healthy ? "自治安全运行中" : "需要外部条件"}</span><strong>{healthy ? (supervision?.status === "self_managed" ? "内部问题正在由系统自行消化" : performanceNeedsReview ? "系统运行正常，策略表现由后台复盘" : "系统正在按计划自行运行") : ownerCases.length ? `${ownerCases.length} 项外部条件需要你提供` : "等待当前周期完成"}</strong><small>{healthy ? (supervision?.status === "self_managed" ? `${incidentCases.length} 项原始问题已分类：内部故障 ${internalFaultCases.length} 项、等待条件 ${waitingCases.length} 项，不需要你进入模块处理。` : performanceNeedsReview ? "自动复盘会据此生成并验证后续候选，不要求人工审批。" : "数据、模型、策略、组合和研究模块均由后台任务协调执行。") : (ownerCases[0]?.summary || ownerCases[0]?.policy || latest?.message || "系统会明确说明缺少的凭证、权限或外部服务。")}{liveHealth && liveHealth.level !== "healthy" ? ` 实时决策门：${liveHealth.summary}；日更状态：${dailyUpdate?.phase === "cooldown" ? "冷却后自动复核" : dailyUpdate?.message || "系统处理中"}。` : ""}</small></div>
      <dl><div><dt>今日调仓</dt><dd>{questions?.today_rebalance.proposal_count ?? 0} 项</dd></div><div><dt>系统变化</dt><dd>{changeCount} 项</dd></div><div><dt>账本</dt><dd>{statusText(changes?.ledger_status)}</dd></div></dl>
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
        <div className="cycle-steps">{completedSteps.length ? completedSteps.map((step) => <div key={step.name}><i className={step.ok ? "done" : "failed"} /><span>{stepLabel[step.name || ""] || step.name}</span><small>{statusText(step.result?.status || (step.ok ? "completed" : "failed"))}</small></div>) : <p>本次任务尚未返回步骤明细。</p>}</div>
        <button type="button" onClick={() => onNavigate("strategyLab")}>查看策略验证</button>
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
