import { useEffect, useMemo, useRef, useState } from "react";
import { fetchGovernanceOverview, fetchMarketContextGovernance, type GovernanceOverview, type MarketContextGovernance } from "../api/research";
import {
  autoTuneModels,
  fetchPlanModelAutoOptimization,
  fetchPlanModels,
  fetchModelStrategyStatus,
  materializeFormalPerformanceReports,
  promoteModelVersion,
  rollbackModelVersion,
  startAllTrainablePlanModels,
  startPlanModelTraining,
  startModelTraining,
  type ModelStrategyStatus,
  type ModelTrainRun,
  type PlanModelAutoOptimizationStatus,
  type PlanModelControl,
  type ModelVersion,
} from "../api/modelStrategy";
import { resumeQuantTask } from "../api/quantRuns";
import { listQuantTasks } from "../api/quantRuns";
import {
  fetchBackfillStatus,
  fetchDailyUpdateStatus,
  fetchDataObservability,
  fetchReadinessRefreshStatus,
  startBackfill,
  startDailyUpdate,
  startReadinessRefresh,
  type BackfillStatus,
  type DailyUpdateStatus,
  type DataObservability,
  type ReadinessRefreshStatus,
} from "../api/dataCenter";
import type { QuantTaskItem } from "../types";
import { formatGmt8Time } from "../utils/time";
import LoadingState from "./LoadingState";
import {
  GOVERNANCE_SECTION_EVENT,
  GOVERNANCE_SECTION_STORAGE_KEY,
  normalizeGovernanceSection,
  type GovernanceSectionKey,
} from "./governanceSections";

function statusText(value: unknown) {
  const key = String(value || "unknown");
  const map: Record<string, string> = {
    ready: "正常", blocked: "阻塞", degraded: "降级", unknown: "待检查",
    running: "运行中", queued: "排队中", completed: "已完成", failed: "失败",
    paused: "已暂停", candidate: "候选", watch: "观察", active: "生产中",
    production: "生产中", trainable: "可训练", proxy: "待数据",
    artifact_missing: "模型文件缺失", passed: "验证通过",
  };
  return map[key] || key;
}

function percent(value: unknown, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : "—";
}

function number(value: unknown, digits = 4) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
}

function walkForwardText(metrics: Record<string, unknown> | undefined) {
  if (!metrics) return "—";
  const lift = Number(metrics.lift);
  const walkForward = Number(metrics.walk_forward_lift);
  const windows = Number(metrics.walk_forward_windows || metrics.walk_forward_folds || 0);
  if (Number.isFinite(lift) && Number.isFinite(walkForward) && lift === walkForward && windows <= 0) {
    return "旧版未分窗";
  }
  return number(metrics.walk_forward_lift);
}

function costAfterLift(metrics: Record<string, unknown> | undefined) {
  return metricValue(metrics, "net_lift") ?? metricValue(metrics, "lift");
}

function worstWindowLift(metrics: Record<string, unknown> | undefined) {
  return metricValue(metrics, "worst_window_net_lift") ?? metricValue(metrics, "walk_forward_lift") ?? costAfterLift(metrics);
}

function metricValue(metrics: Record<string, unknown> | undefined, key: string) {
  const value = Number(metrics?.[key]);
  return Number.isFinite(value) ? value : null;
}

function signedNumber(value: unknown, digits = 4) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return `${parsed > 0 ? "+" : ""}${parsed.toFixed(digits)}`;
}

function approvalAdvice(candidate: Record<string, unknown> | undefined, production: Record<string, unknown> | undefined) {
  const candidateLift = costAfterLift(candidate);
  const productionLift = costAfterLift(production);
  const candidateWf = metricValue(candidate, "walk_forward_lift");
  const productionWf = metricValue(production, "walk_forward_lift");
  const candidateWorst = worstWindowLift(candidate);
  const productionWorst = worstWindowLift(production);
  if (candidateLift == null || candidateWf == null || candidateWorst == null) return "缺少费用后验证指标，留在影子观察";
  if (candidateLift <= 0 || candidateWf <= 0 || candidateWorst <= 0) return "候选费用后指标为负，系统应自动驳回";
  if (productionWf != null && candidateWf != null && candidateWf < productionWf) return "弱于生产模型，系统自动驳回";
  if (productionLift != null && candidateLift < productionLift) return "费用后收益弱于生产模型，系统自动驳回";
  if (productionWorst != null && candidateWorst < productionWorst) return "最差窗口弱于生产模型，系统自动驳回";
  return "候选通过比较，等待确定性硬门";
}

function autoOptimizationVerdict(item: {
  trigger?: string;
  requires_human?: boolean;
  running?: boolean;
  shadow_ab?: { has_challenger?: boolean; status?: string; delta?: number; observation_sessions?: number; minimum_sessions?: number };
}) {
  const shadow = item.shadow_ab;
  if (item.requires_human) return { tone: "action", title: "需要诊断", detail: "数据或模型合约出现异常，系统暂停演化并转入诊断队列。" };
  if (shadow?.status === "auto_rejected" || item.trigger === "auto_rejected") return { tone: "done", title: "系统已驳回", detail: "候选弱于现有生产模型，不进入晋升流程。" };
  if (item.running) return { tone: "watch", title: "正在训练", detail: "系统正在生成新候选，完成后自动比较。" };
  if (item.trigger === "retrain_challenger") return { tone: "watch", title: "系统待重训", detail: "生产指标偏弱，下一轮生成候选对照。" };
  if (shadow?.has_challenger) {
    const delta = Number(shadow.delta);
    const sessions = `${Number(shadow.observation_sessions || 0)}/${Number(shadow.minimum_sessions || 20)}`;
    return { tone: delta >= 0 ? "watch" : "done", title: delta >= 0 ? "继续观察" : "系统会驳回", detail: `候选对比差值 ${signedNumber(delta)}，观察期 ${sessions}。` };
  }
  return { tone: "idle", title: "无需处理", detail: "当前生产模型可用，暂无新候选。" };
}

function compact(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length ? value.join("、") : "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function trainingTimeline(run: ModelTrainRun | null | undefined) {
  const timeline = Array.isArray(run?.metrics?.timeline) ? run.metrics.timeline : [];
  if (timeline.length) return timeline;
  if (!run) return [];
  return [{
    at: run.updated_at || run.created_at,
    phase: run.phase,
    progress: run.progress,
    message: run.message || statusText(run.status),
  }];
}

function visibleVersionHistory(versions: ModelVersion[] | undefined, limitPrevious = 2) {
  const list = [...(versions ?? [])].sort((left, right) => {
    const leftTime = Date.parse(left.updated_at || left.created_at || "");
    const rightTime = Date.parse(right.updated_at || right.created_at || "");
    return (Number.isFinite(rightTime) ? rightTime : right.id) - (Number.isFinite(leftTime) ? leftTime : left.id);
  });
  const active = list.find((item) => item.is_active);
  const previous = list.filter((item) => !item.is_active).slice(0, limitPrevious);
  return active ? [active, ...previous] : previous;
}

const PARAM_LABELS: Record<string, string> = {
  learning_rate: "学习率",
  num_leaves: "叶子数",
  max_depth: "最大深度",
  n_estimators: "树数量",
  min_child_samples: "叶子最小样本",
  feature_fraction: "特征采样比例",
  bagging_fraction: "样本采样比例",
  bagging_freq: "采样频率",
  reg_alpha: "L1 正则",
  reg_lambda: "L2 正则",
  max_bin: "最大分箱数",
  random_state: "随机种子",
  early_stopping_rounds: "早停轮数",
};

function parameterPairs(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>).map(([key, raw]) => ({
    key,
    label: PARAM_LABELS[key] || key,
    value: typeof raw === "number" ? (Number.isInteger(raw) ? String(raw) : String(raw)) : compact(raw),
  }));
}

function statusClass(value: unknown) {
  const key = String(value || "unknown");
  if (["production", "active", "ready", "passed", "completed"].includes(key)) return "ready";
  if (["failed", "blocked", "rejected"].includes(key)) return "blocked";
  if (["candidate", "watch", "trainable", "running", "queued"].includes(key)) return "watch";
  return "degraded";
}

function artifactValue(item: Record<string, unknown> | undefined, key: string) {
  return item && Object.prototype.hasOwnProperty.call(item, key) ? item[key] : undefined;
}

function firstContractValue(contract: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = contract[key];
    if (value !== null && value !== undefined && value !== "") return value;
  }
  return "未配置";
}

const PLAN_MODEL_KEYS = [
  "medium_horizon_lambdarank_v1",
  "capital_persistence_multitask_v1",
  "leader_lambdarank_v1",
  "event_surprise_lgbm_v1",
  "narrative_transition_multitask_v1",
];

const MODEL_SECTION_KEYS: GovernanceSectionKey[] = [
  "modelStatus",
  "modelTraining",
  "modelParameters",
  "modelAutoOptimization",
  "modelPromotion",
  "modelVersions",
];

const MODEL_SECTION_TITLES: Partial<Record<GovernanceSectionKey, string>> = {
  modelStatus: "模型状态总览",
  modelTraining: "模型训练过程",
  modelParameters: "模型参数与证据合同",
  modelAutoOptimization: "自优化与确定性硬门",
  modelPromotion: "候选模型自动晋升",
  modelVersions: "模型版本与回退",
};

function eligibility(version: ModelVersion) {
  const validation = version.validation || {};
  const detail = (validation.production_eligibility && typeof validation.production_eligibility === "object"
    ? validation.production_eligibility : {}) as Record<string, unknown>;
  return {
    eligible: Boolean(validation.production_eligible),
    history: Number(detail.history_sessions || 0),
    minimum: Number(detail.minimum_sessions_required || 0),
    reasons: Array.isArray(detail.blocked_reasons) ? detail.blocked_reasons.map(String) : [],
  };
}

function taskInfo(item: Record<string, unknown>) {
  const snapshot = (item.strategy_snapshot && typeof item.strategy_snapshot === "object"
    ? item.strategy_snapshot : {}) as Record<string, unknown>;
  return {
    id: Number(item.id || 0),
    name: String(snapshot.name || (item.strategy_id ? `策略 ${item.strategy_id}` : "量化任务")),
    status: String(item.status || "unknown"),
    message: String(item.error || item.last_message || "没有错误详情").replace(/\s+/g, " ").trim(),
    updatedAt: String(item.updated_at || ""),
  };
}

export default function GovernancePage() {
  const [overview, setOverview] = useState<GovernanceOverview | null>(null);
  const [models, setModels] = useState<ModelStrategyStatus | null>(null);
  const [planModels, setPlanModels] = useState<PlanModelControl | null>(null);
  const [autoOptimization, setAutoOptimization] = useState<PlanModelAutoOptimizationStatus | null>(null);
  const [marketGovernance, setMarketGovernance] = useState<MarketContextGovernance | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [section, setSection] = useState<GovernanceSectionKey>(() =>
    normalizeGovernanceSection(window.localStorage.getItem(GOVERNANCE_SECTION_STORAGE_KEY))
  );
  const [dataTasks, setDataTasks] = useState<DataObservability | null>(null);
  const [strategyTasks, setStrategyTasks] = useState<QuantTaskItem[]>([]);
  const [dailyUpdate, setDailyUpdate] = useState<DailyUpdateStatus | null>(null);
  const [backfill, setBackfill] = useState<BackfillStatus | null>(null);
  const [readinessRefresh, setReadinessRefresh] = useState<ReadinessRefreshStatus | null>(null);
  const [taskLoading, setTaskLoading] = useState(false);
  const planModelsLoadingRef = useRef(false);

  async function load(silent = false) {
    if (!silent) setLoading(true);
    const results = await Promise.allSettled([
      fetchGovernanceOverview(),
      fetchModelStrategyStatus(),
      fetchMarketContextGovernance(),
      fetchReadinessRefreshStatus(),
    ]);
    const errors: string[] = [];
    if (results[0].status === "fulfilled") setOverview(results[0].value);
    else errors.push(results[0].reason instanceof Error ? results[0].reason.message : String(results[0].reason));
    if (results[1].status === "fulfilled") setModels(results[1].value);
    else errors.push(results[1].reason instanceof Error ? results[1].reason.message : String(results[1].reason));
    if (results[2].status === "fulfilled") setMarketGovernance(results[2].value);
    else errors.push(results[2].reason instanceof Error ? results[2].reason.message : String(results[2].reason));
    if (results[3].status === "fulfilled") setReadinessRefresh(results[3].value);
    else errors.push(results[3].reason instanceof Error ? results[3].reason.message : String(results[3].reason));
    setError(errors.join("；"));
    if (!silent) setLoading(false);
  }

  async function loadPlanModels(silent = false) {
    if (planModelsLoadingRef.current) return;
    planModelsLoadingRef.current = true;
    if (!silent) setTaskLoading(true);
    try {
      const plan = await fetchPlanModels();
      setPlanModels(plan);
      try {
        setAutoOptimization(await fetchPlanModelAutoOptimization());
      } catch {
        setAutoOptimization(null);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      planModelsLoadingRef.current = false;
      if (!silent) setTaskLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 10000);
    return () => window.clearInterval(timer);
  }, []);

  async function loadTaskCenter(silent = false) {
    if (!silent) setTaskLoading(true);
    try {
      const [observability, quantTasks, daily, fill] = await Promise.all([
        fetchDataObservability(30),
        listQuantTasks(30),
        fetchDailyUpdateStatus(),
        fetchBackfillStatus(),
      ]);
      setDataTasks(observability);
      setStrategyTasks(quantTasks);
      setDailyUpdate(daily);
      setBackfill(fill);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (!silent) setTaskLoading(false);
    }
  }

  useEffect(() => {
    if (section !== "tasks") return;
    void loadTaskCenter();
    const timer = window.setInterval(() => void loadTaskCenter(true), 5000);
    return () => window.clearInterval(timer);
  }, [section]);

  useEffect(() => {
    if (!MODEL_SECTION_KEYS.includes(section)) return;
    void loadPlanModels();
    const timer = window.setInterval(() => void loadPlanModels(true), 15000);
    return () => window.clearInterval(timer);
  }, [section]);

  useEffect(() => {
    const onSection = (event: Event) => {
      const next = normalizeGovernanceSection((event as CustomEvent<string>).detail);
      if (next) setSection(next);
    };
    window.addEventListener(GOVERNANCE_SECTION_EVENT, onSection);
    return () => window.removeEventListener(GOVERNANCE_SECTION_EVENT, onSection);
  }, []);

  async function action(key: string, run: () => Promise<unknown>, success: string) {
    setBusy(key);
    setNotice("");
    setError("");
    try {
      await run();
      setNotice(success);
      await load(true);
      if (MODEL_SECTION_KEYS.includes(section)) await loadPlanModels(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  }

  const active = models?.active_version ?? null;
  const trainRun = models?.latest_train_run ?? null;
  const versions = models?.versions ?? [];
  const failedTasks = overview?.tasks.failed_or_paused ?? [];
  const blockers = overview?.gates.filter((item) => item.status === "blocked") ?? [];
  const healthGate = overview?.gates.find((item) => item.key === "data_health");
  const dataReadyGate = overview?.gates.find((item) => item.key === "data_readiness");
  const planBookModels = useMemo(() => {
    const items = planModels?.items ?? [];
    return PLAN_MODEL_KEYS.map((key) => items.find((item) => item.model_key === key)).filter(Boolean) as NonNullable<PlanModelControl["items"]>;
  }, [planModels]);
  const candidatePlanBookModels = planBookModels.filter((item) => {
    const latest = item.latest_version;
    const activeId = Number(item.active_version?.id || 0);
    const latestId = Number(latest?.id || 0);
    const reviewStatus = String((latest?.validation?.review as Record<string, unknown> | undefined)?.status || "");
    return latestId > 0
      && latestId !== activeId
      && String(latest?.status || "") === "candidate"
      && reviewStatus !== "rejected"
      && approvalAdvice(latest?.metrics || {}, item.active_version?.metrics || {}) === "候选通过比较，等待确定性硬门";
  });
  const runningPlanBookModels = planBookModels.filter((item) => item.latest_train_run?.status === "running");
  const autoReviewQueue = autoOptimization?.review_queue ?? [];
  const autoTrainableItems = (autoOptimization?.items ?? []).filter((item) => ["train_challenger", "retrain_challenger"].includes(item.trigger));
  const autoRejectedItemCount = (autoOptimization?.items ?? []).filter((item) => item.trigger === "auto_rejected" || item.shadow_ab?.status === "auto_rejected").length;
  const modelSection = MODEL_SECTION_KEYS.includes(section);

  if (loading && !overview && !models) {
    return <LoadingState variant="page" title="正在打开模型工厂" detail="正在读取生产模型、训练任务和系统健康状态" rows={5} />;
  }

  return (
    <section className="product-module-page governance-console">
      <div className="product-module-head">
        <div>
          <span>SYSTEM CENTER</span>
          <h1>模型工厂</h1>
          <p>这里展示模型训练、影子观察和确定性硬门；符合全部条件的候选会自动进入模拟决策链路。</p>
        </div>
        <button className="button ghost" onClick={() => void load()} disabled={loading}>刷新状态</button>
      </div>

      {error && <div className="alert">{error}</div>}
      {notice && <div className="inline-message">{notice}</div>}

      {modelSection && <>
      <section className="model-training-console">
        <header>
          <div>
            <span>模型中控台</span>
            <h2>{MODEL_SECTION_TITLES[section] || "模型状态总览"}</h2>
            <p>{section === "modelStatus"
              ? "这里只回答：哪些模型在生产、哪些在候选、哪些还能训练。其他操作收进对应页。"
              : "五个计划书模型按“状态、训练、参数、自优化、晋升、版本”拆开管理；训练、淘汰、观察和晋升均由系统完成，只有数据或合约异常需要查看。"}</p>
          </div>
          {section === "modelTraining" && <div className="model-training-actions">
            <button className="button primary" disabled={busy !== ""} onClick={() => void action("plan-train-all", startAllTrainablePlanModels, "已启动全部可训练计划书模型")}>{busy === "plan-train-all" ? "启动中" : "训练待训练模型"}</button>
          </div>}
        </header>
        {section === "modelStatus" && <div className="model-training-status">
          <article><span>主决策模型</span><strong>{active?.version || "未上线"}</strong><p>{active ? "正在服务模拟决策" : "未上线前使用规则降级"}</p></article>
          <article><span>当前训练</span><strong>{trainRun ? `#${trainRun.id} ${statusText(trainRun.status)}` : "无任务"}</strong><p>{trainRun?.message || "可以启动新训练"}</p></article>
          <article><span>行情识别</span><strong>{marketGovernance?.hsmm?.production_eligible ? "可接管" : "观察中"}</strong><p>{marketGovernance?.hsmm ? `观察 ${marketGovernance.hsmm.live_observation_weeks.toFixed(2)} / ${marketGovernance.hsmm.minimum_live_observation_weeks} 周` : "等待状态"}</p></article>
        </div>}

        {section === "modelStatus" && <div className="model-status-list">
          {!planModels && <LoadingState variant="section" title="正在读取模型状态" detail="正在汇总五个计划书模型的生产、候选和训练状态" rows={4} />}
          {planBookModels.map((item) => {
            const running = item.latest_train_run?.status === "running";
            const trainable = item.status === "trainable" || item.status === "production" || item.status === "candidate";
            return <article key={item.model_key}>
              <div>
                <strong>{item.name}</strong>
                <span>{item.role}</span>
              </div>
              <em className={`status-pill status-${statusClass(item.status)}`}>{statusText(item.status)}</em>
              <p>{running ? "训练中" : item.latest_train_run ? `最近训练 #${item.latest_train_run.id} · ${statusText(item.latest_train_run.status)}` : "还没有训练记录"}</p>
              <small>{item.status === "production" ? "已接入模拟决策链路" : item.status === "candidate" ? "正在进行自动硬门观察" : trainable ? "由系统在数据满足时训练" : "等待数据补齐"}</small>
            </article>;
          })}
          {planModels && !planBookModels.length && <div className="empty">暂无模型状态。</div>}
        </div>}

        {section === "modelTraining" && <div className="model-training-list">
          <div className="model-training-list-head"><span>模型</span><span>训练阶段</span><span>状态</span><span>进度</span><span>操作</span></div>
          {planBookModels.map((item) => {
            const run = item.latest_train_run;
            const running = run?.status === "running";
            const trainable = item.status === "trainable" || item.status === "production" || item.status === "candidate";
            const timeline = trainingTimeline(run);
            const datasetSummary = run?.metrics?.dataset && typeof run.metrics.dataset === "object" ? run.metrics.dataset as Record<string, unknown> : null;
            return <details key={item.model_key} className="model-training-run" open={running}>
              <summary>
                <strong>{item.name}</strong>
                <p>{run?.message || "等待训练触发"}</p>
                <em className={`status-pill status-${statusClass(run?.status || item.status)}`}>{statusText(run?.status || item.status)}</em>
                <span>{run ? `${run.phase || "—"} · ${Math.round(Number(run.progress || 0) * 100)}%` : "未训练"}</span>
                <div><button className="button ghost" disabled={busy !== "" || running || !trainable} onClick={(event) => { event.preventDefault(); void action(`training-${item.model_key}`, () => startPlanModelTraining(item.model_key), `${item.name} 训练已启动`); }}>{running ? "训练中" : "启动训练"}</button></div>
              </summary>
              <div className="model-training-log">
                <div>
                  <strong>训练日志</strong>
                  <span>{run ? `任务 #${run.id} · ${run.updated_at ? formatGmt8Time(run.updated_at) : "等待更新"}` : "尚未启动训练"}</span>
                </div>
                {datasetSummary && (
                  <div className="model-training-dataset">
                    <span>样本 {compact(datasetSummary.sample_rows)}</span>
                    <span>训练 {compact(datasetSummary.train_rows)}</span>
                    <span>验证 {compact(datasetSummary.validation_rows)}</span>
                    <span>特征 {compact(datasetSummary.feature_count)}</span>
                    <span>周期 {compact(datasetSummary.horizon)} 天</span>
                  </div>
                )}
                {timeline.length ? timeline.slice(-8).map((entry, index) => (
                  <article key={`${entry.phase || "phase"}-${entry.at || index}`}>
                    <i>{Math.round(Number(entry.progress || 0) * 100)}%</i>
                    <div>
                      <strong>{statusText(entry.phase || "running")}</strong>
                      <span>{entry.message || "阶段更新"}</span>
                    </div>
                    <time>{entry.at ? formatGmt8Time(entry.at) : "—"}</time>
                  </article>
                )) : <p>暂无日志；启动训练后系统会自动写入覆盖检查、样本构造、训练、验证、产物生成等阶段。</p>}
                {run?.error && <p className="model-training-error">错误：{run.error}</p>}
              </div>
            </details>;
          })}
        </div>}

        {section === "modelAutoOptimization" && <details open className="model-auto-optimizer">
          <summary>
            <div>
              <strong>模型自优化</strong>
              <span>{autoOptimization?.policy.summary || "系统自动训练 challenger、自动驳回弱候选；全部确定性硬门通过后自动晋升。"}</span>
            </div>
            <em>{autoReviewQueue.length ? `${autoReviewQueue.length} 个可批准` : autoOptimization?.auto_rejected?.length ? `${autoOptimization.auto_rejected.length} 个已自动驳回` : autoTrainableItems.length ? `${autoTrainableItems.length} 个可自训练` : "无需介入"}</em>
          </summary>
          <div className="model-auto-optimizer-body">
            <div className="model-auto-optimizer-command">
              <div>
                <strong>候选模型 A/B 影子运行</strong>
                <p>新模型不上线也每天虚拟打分；弱于生产模型或核心指标为负时系统自动驳回，满足全部硬门后才会自动替换生产版本。</p>
              </div>
              <span className="status-pill status-ready">按自动排程运行</span>
            </div>
            {!!candidatePlanBookModels.length && (
              <div className="model-review-now">
                <div>
                  <strong>{candidatePlanBookModels.length} 个候选模型等待确定性硬门</strong>
                  <span>系统已自动过滤弱候选；候选将在正式 OOS 和影子观察通过后自动晋升。</span>
                </div>
                <div>
                  {candidatePlanBookModels.slice(0, 5).map((item) => {
                    const candidateMetrics = item.latest_version?.metrics || {};
                    const productionMetrics = item.active_version?.metrics || {};
                    const deltaWf = metricValue(candidateMetrics, "walk_forward_lift") != null && metricValue(productionMetrics, "walk_forward_lift") != null
                      ? Number(candidateMetrics.walk_forward_lift) - Number(productionMetrics.walk_forward_lift)
                      : null;
                    return <article key={item.model_key}>
                      <div>
                        <strong>{item.name}</strong>
                        <span>{approvalAdvice(candidateMetrics, productionMetrics)}</span>
                        <small>候选 {item.latest_version?.version || "—"} · 生产 {item.active_version?.version || "—"}</small>
                      </div>
                      <div className="model-approval-metrics">
                        <em><b>候选费用后</b>{number(costAfterLift(candidateMetrics))}</em>
                        <em><b>候选滚动验证</b>{walkForwardText(candidateMetrics)}</em>
                        <em><b>候选最差窗口</b>{number(worstWindowLift(candidateMetrics))}</em>
                        <em><b>生产滚动验证</b>{walkForwardText(productionMetrics)}</em>
                        <em><b>滚动差值</b>{deltaWf == null ? "—" : signedNumber(deltaWf)}</em>
                        <em><b>胜率</b>{percent(candidateMetrics.hit_rate)}</em>
                        <em><b>IC/相关</b>{number(candidateMetrics.rank_return_corr)}</em>
                        <em><b>样本</b>{compact(candidateMetrics.validation_rows || candidateMetrics.sample_rows)}</em>
                        <em><b>窗口</b>{compact(candidateMetrics.walk_forward_windows || "旧版")}</em>
                      </div>
                      <div className="model-approval-actions">
                        <span>系统正在执行 OOS 与影子观察</span>
                      </div>
                    </article>;
                  })}
                </div>
              </div>
            )}
            {!!autoOptimization?.auto_rejected?.length && (
              <div className="inline-message">
                自优化已自动驳回：{autoOptimization.auto_rejected.map((item) => `${item.version || item.model_key}（${item.reason || "弱于生产模型"}）`).join("；")}
              </div>
            )}
            <div className="model-auto-summary">
              <article className={autoReviewQueue.length ? "needs-action" : ""}>
                <span>诊断队列</span>
                <strong>{autoReviewQueue.length ? `${autoReviewQueue.length} 项需诊断` : "暂无异常诊断"}</strong>
                <p>{autoReviewQueue.length ? "仅在数据或模型合约异常时出现。" : "弱候选、重训、观察和晋升均由系统处理。"}</p>
              </article>
              <article>
                <span>系统自动处理</span>
                <strong>{autoRejectedItemCount} 个已驳回</strong>
                <p>收益指标为负或弱于生产模型时自动剔除。</p>
              </article>
              <article>
                <span>可自训练</span>
                <strong>{autoTrainableItems.length} 个模型</strong>
                <p>生产走弱时系统会训练 challenger 对照。</p>
              </article>
            </div>
            <div className="model-auto-list">
              {(autoOptimization?.items ?? []).map((item) => {
                const shadow = item.shadow_ab;
                const candidateMetrics = item.candidate_metrics || {};
                const productionMetrics = item.production_metrics || {};
                const hasChallenger = Boolean(shadow?.has_challenger);
                const verdict = autoOptimizationVerdict(item);
                return <details key={item.model_key} className={`model-auto-row model-auto-row-${verdict.tone}`}>
                  <summary>
                    <i>{verdict.title}</i>
                    <div>
                      <strong>{item.name}</strong>
                      <span>{verdict.detail}</span>
                    </div>
                    <b>{hasChallenger ? `候选对比 ${shadow ? signedNumber(shadow.delta) : "—"}` : item.trigger === "retrain_challenger" ? "等待新候选" : "无候选"}</b>
                    <em className={`status-pill status-${statusClass(shadow?.status || item.status)}`}>{shadow?.status === "auto_rejected" ? "自动驳回" : shadow?.status === "review_ready" ? "可评审" : hasChallenger && shadow?.status === "shadow_running" ? "观察中" : item.trigger === "retrain_challenger" ? "待重训" : "稳定"}</em>
                  </summary>
                  <div className="model-auto-row-detail">
                    <section>
                      <h4>系统判断</h4>
                      <p>{item.reason}</p>
                      <small>{hasChallenger ? `生产 ${shadow?.champion_version || item.active_version || "—"} vs 候选 ${shadow?.challenger_version || item.latest_version || "—"}` : `当前生产 ${shadow?.champion_version || item.active_version || item.latest_version || "—"} · 暂无 challenger`}</small>
                    </section>
                    <div className="model-shadow-metrics">
                      <em><b>观察期</b>{hasChallenger && shadow ? `${shadow.observation_sessions}/${shadow.minimum_sessions}` : "无候选"}</em>
                      <em><b>{hasChallenger ? "候选费用后" : "生产费用后"}</b>{number(costAfterLift(hasChallenger ? candidateMetrics : productionMetrics))}</em>
                      <em><b>{hasChallenger ? "候选滚动验证" : "生产滚动验证"}</b>{walkForwardText(hasChallenger ? candidateMetrics : productionMetrics)}</em>
                      <em><b>{hasChallenger ? "候选最差窗口" : "生产最差窗口"}</b>{number(worstWindowLift(hasChallenger ? candidateMetrics : productionMetrics))}</em>
                      {hasChallenger && <em><b>生产滚动验证</b>{walkForwardText(productionMetrics)}</em>}
                      {hasChallenger && <em><b>候选差值</b>{shadow ? signedNumber(shadow.delta) : "—"}</em>}
                      <em><b>{hasChallenger ? "候选胜率" : "生产胜率"}</b>{percent((hasChallenger ? candidateMetrics : productionMetrics).hit_rate)}</em>
                      <em><b>排序相关</b>{number((hasChallenger ? candidateMetrics : productionMetrics).rank_return_corr)}</em>
                      <em><b>建议</b>{hasChallenger ? approvalAdvice(candidateMetrics, productionMetrics) : item.trigger === "retrain_challenger" ? "生产指标偏弱，系统应训练新 challenger" : "暂无新候选需要处理"}</em>
                    </div>
                    <div className="model-auto-row-actions">
                      {item.requires_human
                        ? <span>数据或模型合约异常，等待系统诊断完成</span>
                        : <span>{item.trigger === "auto_rejected" ? "系统已处理，不需要你操作" : item.running ? "训练完成后自动比较" : item.trigger === "retrain_challenger" ? "等待系统重训" : item.trigger === "healthy" ? "系统自动观察" : "系统处理"}</span>}
                    </div>
                  </div>
                </details>;
              })}
              {!autoOptimization?.items?.length && <div className="empty">自优化状态读取中。</div>}
            </div>
            <details className="model-auto-raw">
              <summary>查看系统自动处理记录</summary>
              <div className="model-auto-grid">
                {(autoOptimization?.items ?? []).map((item) => {
                  const shadow = item.shadow_ab;
                  const candidateMetrics = item.candidate_metrics || {};
                  const productionMetrics = item.production_metrics || {};
                  const hasChallenger = Boolean(shadow?.has_challenger);
                  return <article key={item.model_key}>
                    <div>
                    <strong>{item.name}</strong>
                    <span>{item.reason}</span>
                    <small>{hasChallenger ? `生产 ${shadow?.champion_version || item.active_version || "—"} vs 候选 ${shadow?.challenger_version || item.latest_version || "—"}` : `当前生产 ${shadow?.champion_version || item.active_version || item.latest_version || "—"} · 暂无 challenger`}</small>
                    </div>
                    <div className="model-shadow-metrics">
                    <em><b>观察期</b>{hasChallenger && shadow ? `${shadow.observation_sessions}/${shadow.minimum_sessions}` : "无候选"}</em>
                    <em><b>{hasChallenger ? "候选费用后" : "生产费用后"}</b>{number(costAfterLift(hasChallenger ? candidateMetrics : productionMetrics))}</em>
                    <em><b>{hasChallenger ? "候选滚动验证" : "生产滚动验证"}</b>{walkForwardText(hasChallenger ? candidateMetrics : productionMetrics)}</em>
                    <em><b>{hasChallenger ? "候选最差窗口" : "生产最差窗口"}</b>{number(worstWindowLift(hasChallenger ? candidateMetrics : productionMetrics))}</em>
                    {hasChallenger && <em><b>生产滚动验证</b>{walkForwardText(productionMetrics)}</em>}
                    {hasChallenger && <em><b>滚动差值</b>{shadow ? signedNumber(shadow.delta) : "—"}</em>}
                    <em><b>{hasChallenger ? "候选胜率" : "生产胜率"}</b>{percent((hasChallenger ? candidateMetrics : productionMetrics).hit_rate)}</em>
                    <em><b>IC/相关</b>{number((hasChallenger ? candidateMetrics : productionMetrics).rank_return_corr)}</em>
                    <em><b>系统建议</b>{hasChallenger ? approvalAdvice(candidateMetrics, productionMetrics) : item.trigger === "retrain_challenger" ? "生产指标偏弱，系统应训练新 challenger" : "暂无新候选需要处理"}</em>
                    </div>
                    <em className={`status-pill status-${statusClass(shadow?.status || item.status)}`}>{shadow?.status === "auto_rejected" ? "自动驳回" : shadow?.status === "review_ready" ? "可评审" : hasChallenger && shadow?.status === "shadow_running" ? "影子运行" : item.trigger === "retrain_challenger" ? "待重训" : "无候选"}</em>
                    <div>
                    {item.requires_human
                      ? <span>数据或模型合约异常，等待系统诊断完成</span>
                      : <span>{item.trigger === "auto_rejected" ? "系统已处理" : item.running ? "训练中" : item.trigger === "retrain_challenger" ? "等待重训" : item.trigger === "healthy" ? "自动观察" : "系统处理"}</span>}
                    </div>
                  </article>;
                })}
                {!autoOptimization?.items?.length && <div className="empty">自优化状态读取中。</div>}
              </div>
            </details>
          </div>
        </details>}

      </section>

      {section === "modelParameters" && <section className="model-parameter-panel">
        <div className="governance-section-head">
          <div><h3>生产模型参数</h3><p>这五个模型具体按什么目标、数据、窗口、指标和固定参数训练。</p></div>
          <span>{planModels ? `${planModels.counts.production ?? 0} 个已上线` : "读取中"}</span>
        </div>
        <div className="governance-task-list">
          {planBookModels.map((item) => {
            const coverage = item.coverage || {};
            const effective = Number(coverage.effective_sessions || 0);
            const minimum = Number(coverage.minimum_sessions || 0);
            const progress = minimum > 0 ? Math.min(100, Math.round((effective / minimum) * 100)) : 0;
            const running = item.latest_train_run?.status === "running";
            const trainable = item.status === "trainable" || item.status === "production" || item.status === "candidate";
            const review = (item.latest_version?.validation?.review || {}) as Record<string, unknown>;
            const latestArtifact = (item.latest_artifact || {}) as Record<string, unknown>;
            const contract = (item.training_contract || {}) as Record<string, unknown>;
            const fixedParams = firstContractValue(contract, ["params", "hyperparams", "hyperparameters", "model_params"]);
            const fixedParamPairs = parameterPairs(fixedParams);
            const metrics = item.latest_version?.metrics || {};
            return <article key={item.model_key} className="governance-model-card compact">
              <details className="model-parameter-detail">
                <summary>
                  <div>
                    <strong>{item.name}</strong>
                    <span>{item.role}</span>
                    <small>{item.latest_version?.version || item.model_key}</small>
                  </div>
                  <em className={`status-pill status-${statusClass(item.status)}`}>{statusText(item.status)}</em>
                  <i>{minimum ? `${effective}/${minimum}` : "覆盖待配置"}</i>
                </summary>
                <div className="bar"><i style={{ width: `${progress}%` }} /></div>
                <div className="decision-two-col">
                  <span>算法族</span><strong>{item.family}</strong>
                  <span>训练目标</span><strong>{compact(firstContractValue(contract, ["objective", "target", "training_target", "tasks"]))}</strong>
                  <span>标签/任务</span><strong>{compact(firstContractValue(contract, ["labels", "label", "tasks", "task"]))}</strong>
                  <span>特征输入</span><strong>{compact(firstContractValue(contract, ["features", "feature_sets", "inputs", "required_features"]))}</strong>
                  <span>评估指标</span><strong>{compact(firstContractValue(contract, ["metric", "metrics", "eval_metric", "eval_at"]))}</strong>
                  <span>数据覆盖</span><strong>{minimum ? `${effective}/${minimum} sessions` : "—"}</strong>
                  <span>训练数据</span><strong>{item.required_data?.join("、") || "—"}</strong>
                  <span>产物类型</span><strong>{compact(artifactValue(latestArtifact, "model_type"))}</strong>
                  <span>模型文件</span><strong>{compact(artifactValue(latestArtifact, "model_path_exists"))}</strong>
                  <span>训练窗口</span><strong>{compact(firstContractValue(contract, ["train_window", "lookback_days", "history_window", "sample_window"]))}</strong>
                  <span>OOS/回放</span><strong>{compact(firstContractValue(contract, ["oos", "rolling_oos", "walk_forward", "backtest"]))}</strong>
                  <span>固定参数</span><strong>{fixedParamPairs.length ? <div className="model-param-tags">{fixedParamPairs.map((param) => <em key={param.key}><b>{param.label}</b>{param.value}</em>)}</div> : compact(fixedParams)}</strong>
                  <span>最新版本</span><strong>{item.latest_version?.version || "—"}</strong>
                  <span>最新训练</span><strong>{item.latest_train_run ? `#${item.latest_train_run.id} ${statusText(item.latest_train_run.status)} ${Math.round(Number(item.latest_train_run.progress || 0) * 100)}%` : "未训练"}</strong>
                  <span>费用后 / 滚动 / 最差</span><strong>{number(costAfterLift(metrics))} / {walkForwardText(metrics)} / {number(worstWindowLift(metrics))}</strong>
                  <span>上线评审</span><strong>{review.status ? `${review.status} · ${review.reviewer || "operator"}` : item.status === "candidate" ? "待评审" : "—"}</strong>
                  <span>阻塞原因</span><strong>{(item.blocked_reasons ?? []).join("、") || "—"}</strong>
                </div>
                {!!item.data_actions?.length && <div className="inline-message">补数建议：{item.data_actions.map((action) => `${action.label}（${action.reason}）`).join("；")}</div>}
              </details>
              <div className="governance-actions">
                <button className="button ghost" disabled={busy !== "" || running || !trainable} onClick={() => void action(`plan-model-${item.model_key}`, () => startPlanModelTraining(item.model_key), `${item.name} 训练已启动`)}>{running ? "训练中" : item.status === "production" ? "重训" : "训练"}</button>
              </div>
            </article>;
          })}
          {!planBookModels.length && <div className="empty">模型参数读取中。</div>}
        </div>
      </section>}

      {section === "modelPromotion" && <section className="model-release-panel">
        <div className="governance-section-head">
          <div><h3>自动晋升队列</h3><p>只有通过候选比较的版本会出现在这里；正式 OOS、影子观察和费用后硬门全部通过后，系统自动替换生产模型。</p></div>
          <span>{candidatePlanBookModels.length ? `${candidatePlanBookModels.length} 个观察中` : "无观察中候选"}</span>
        </div>
        <div className="model-release-list">
          {candidatePlanBookModels.map((item) => {
            const candidateMetrics = item.latest_version?.metrics || {};
            const productionMetrics = item.active_version?.metrics || {};
            const deltaWf = metricValue(candidateMetrics, "walk_forward_lift") != null && metricValue(productionMetrics, "walk_forward_lift") != null
              ? Number(candidateMetrics.walk_forward_lift) - Number(productionMetrics.walk_forward_lift)
              : null;
            return <article key={item.model_key}>
              <div>
                <strong>{item.name}</strong>
                <span>{approvalAdvice(candidateMetrics, productionMetrics)}</span>
                <small>候选 {item.latest_version?.version || "—"} · 生产 {item.active_version?.version || "—"}</small>
              </div>
              <div className="model-approval-metrics">
                <em><b>候选费用后</b>{number(costAfterLift(candidateMetrics))}</em>
                <em><b>候选滚动验证</b>{walkForwardText(candidateMetrics)}</em>
                <em><b>候选最差窗口</b>{number(worstWindowLift(candidateMetrics))}</em>
                <em><b>生产滚动验证</b>{walkForwardText(productionMetrics)}</em>
                <em><b>滚动差值</b>{deltaWf == null ? "—" : signedNumber(deltaWf)}</em>
                <em><b>胜率</b>{percent(candidateMetrics.hit_rate)}</em>
                <em><b>IC/相关</b>{number(candidateMetrics.rank_return_corr)}</em>
              </div>
              <em className="status-pill status-watch">硬门观察中</em>
              <div className="model-approval-actions">
                <span>由系统完成后续验证与自动晋升</span>
              </div>
            </article>;
          })}
          {!candidatePlanBookModels.length && <div className="empty">暂无处于自动晋升观察的候选模型。</div>}
        </div>
      </section>}

      {section === "modelTraining" && <div className="card governance-training-card model-auxiliary-card">
        <div className="governance-section-head"><div><h3>辅助：主线代理模型</h3><p>只用于降级和对照，不是五个正式生产模型的主入口。</p></div><strong>{trainRun ? `${Math.round(Number(trainRun.progress || 0) * 100)}%` : "—"}</strong></div>
        <div className="bar"><i style={{ width: `${Math.round(Number(trainRun?.progress || 0) * 100)}%` }} /></div>
        <div className="decision-two-col">
          <span>任务</span><strong>{trainRun ? `#${trainRun.id}` : "—"}</strong>
          <span>状态</span><strong>{statusText(trainRun?.status)}</strong>
          <span>阶段</span><strong>{trainRun?.phase || "—"}</strong>
          <span>实时消息</span><strong>{trainRun?.error || trainRun?.message || "—"}</strong>
          <span>更新时间</span><strong>{trainRun?.updated_at ? formatGmt8Time(trainRun.updated_at) : "—"}</strong>
        </div>
        <div className="governance-actions">
          <button className="button ghost" disabled={busy !== "" || trainRun?.status === "running"} onClick={() => void action("train", () => startModelTraining({ exchange_group: "HS" }), "主线代理训练任务已启动")}>训练主线代理</button>
          <button className="button ghost" disabled={busy !== "" || trainRun?.status === "running"} onClick={() => void action("tune", autoTuneModels, "主线代理自动调优和晋升检查已完成")}>自动调优</button>
        </div>
      </div>}

      {section === "modelVersions" && <>
      <div className="card model-version-card">
        <div className="governance-section-head"><div><h3>计划书五模型版本管理</h3><p>每个模型只展示当前生产版和最近两个历史版本；完整历史保留在数据库，页面不堆料。</p></div><span>{planBookModels.reduce((sum, item) => sum + visibleVersionHistory(item.versions).length, 0)} 个展示版本</span></div>
        <div className="table-scroll">
          <table>
            <thead><tr><th>模型</th><th>版本</th><th>角色</th><th>验证/评审</th><th>费用后</th><th>滚动/最差窗口</th><th>更新时间</th><th>操作</th></tr></thead>
            <tbody>
              {planBookModels.flatMap((model) => visibleVersionHistory(model.versions).map((version) => {
                const review = (version.validation?.review || {}) as Record<string, unknown>;
                const eligible = Boolean(version.validation?.production_eligible);
                const reviewStatus = String(review.status || "");
                const rejected = reviewStatus === "rejected";
                const candidate = !version.is_active && !rejected && String(version.status || "") === "candidate";
                const canRollback = !version.is_active && eligible && String(version.validation?.status || "") === "passed" && Number(costAfterLift(version.metrics) || 0) > 0 && Number(version.metrics?.walk_forward_lift || 0) > 0 && Number(worstWindowLift(version.metrics) || 0) > 0;
                return <tr key={`${model.model_key}-${version.id}`}>
                  <td><strong>{model.name}</strong><small>{model.role}</small></td>
                  <td>{version.version}</td>
                  <td>{version.is_active ? "生产模型" : statusText(version.status)}</td>
                  <td>{reviewStatus ? `${reviewStatus === "rejected" ? "已驳回" : "已批准"} · ${review.reviewer || "operator"}` : eligible ? "生产资格通过" : statusText(version.validation?.status)}</td>
                  <td>{number(costAfterLift(version.metrics))}</td>
                  <td>{walkForwardText(version.metrics)} / {number(worstWindowLift(version.metrics))}</td>
                  <td>{formatGmt8Time(version.updated_at)}</td>
                  <td>{version.is_active
                    ? <span className="status-pill status-ready">当前生产</span>
                      : rejected
                        ? <span className="status-pill status-blocked">已驳回</span>
                        : candidate
                      ? <span className="status-pill status-watch">自动观察中</span>
                      : canRollback
                        ? <button className="button ghost" disabled={busy !== ""} onClick={() => void action(`plan-version-rollback-${version.id}`, () => rollbackModelVersion(version.id), `${model.name} 已回退至 ${version.version}`)}>回退至此</button>
                      : <span className="muted">无操作</span>}</td>
                </tr>;
              }))}
              {!planBookModels.some((item) => item.versions?.length) && <tr><td colSpan={8} className="empty">暂无计划书模型版本，先到“模型训练过程”启动训练。</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      <div className="card model-version-card model-auxiliary-card">
        <div className="governance-section-head"><div><h3>辅助：主线代理版本记录</h3><p>只展示当前和前两个版本；用于降级、对照和排障。</p></div><span>{visibleVersionHistory(versions).length} 个展示版本</span></div>
        <div className="table-scroll">
          <table>
            <thead><tr><th>版本</th><th>角色</th><th>验证</th><th>历史覆盖</th><th>费用后</th><th>滚动/最差窗口</th><th>更新时间</th><th>操作</th></tr></thead>
            <tbody>
              {visibleVersionHistory(versions).map((item) => {
                const gate = eligibility(item);
                const canControl = gate.eligible && String(item.validation?.status || "") === "passed";
                return <tr key={item.id}>
                  <td><strong>{item.version}</strong></td>
                  <td>{item.is_active ? "生产模型" : statusText(item.status)}</td>
                  <td>{gate.eligible ? "生产资格通过" : gate.reasons.join("、") || statusText(item.validation?.status)}</td>
                  <td>{gate.minimum ? `${gate.history}/${gate.minimum}` : "—"}</td>
                  <td>{number(costAfterLift(item.metrics))}</td>
                  <td>{number(item.metrics?.walk_forward_lift)} / {number(worstWindowLift(item.metrics))}</td>
                  <td>{formatGmt8Time(item.updated_at)}</td>
                  <td>{item.is_active
                    ? <span className="status-pill status-ready">当前生产</span>
                    : <button className="button ghost" disabled={!canControl || busy !== ""} onClick={() => void action(`model-${item.id}`, () => active ? rollbackModelVersion(item.id) : promoteModelVersion(item.id), active ? `已回退至 ${item.version}` : `${item.version} 已晋升为生产模型`)}>{active ? "回退至此" : "晋升生产"}</button>}</td>
                </tr>;
              })}
              {!versions.length && <tr><td colSpan={8} className="empty">暂无模型版本，先启动训练。</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      </>}
      </>}

      {section === "tasks" && <div className="governance-task-center">
        {taskLoading && !dataTasks && <LoadingState variant="section" title="正在读取任务中心" detail="正在汇总数据、策略执行和系统维护任务" rows={4} />}

        <div className="card">
          <div className="governance-section-head"><div><h3>数据任务</h3><p>数据采集、接口同步和质量物化任务。</p></div><span>{dataTasks?.logs.length ?? 0} 条近期记录</span></div>
          <div className="table-scroll">
            <table>
              <thead><tr><th>任务</th><th>来源</th><th>状态</th><th>读取</th><th>写入</th><th>开始时间</th><th>错误</th></tr></thead>
              <tbody>
                {(dataTasks?.logs ?? []).map((item) => <tr key={item.id}>
                  <td>#{item.id} · {item.job_name || "数据任务"}</td><td>{item.source_code || "—"}</td>
                  <td><span className={`status-pill status-${item.status === "completed" ? "ready" : item.status === "failed" ? "blocked" : "degraded"}`}>{statusText(item.status)}</span></td>
                  <td>{item.rows_read ?? 0}</td><td>{item.rows_written ?? 0}</td><td>{item.started_at ? formatGmt8Time(item.started_at) : "—"}</td><td>{item.error || "—"}</td>
                </tr>)}
                {!dataTasks?.logs.length && <tr><td colSpan={7} className="empty">暂无数据任务记录。</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <div className="governance-section-head"><div><h3>策略执行任务</h3><p>信号生成、历史回放和模拟交易任务；与模型训练无关。</p></div><span>{strategyTasks.length} 项</span></div>
          <div className="governance-task-list">
            {strategyTasks.map((raw) => {
              const item = taskInfo(raw as unknown as Record<string, unknown>);
              return <article key={item.id}>
                <div><strong>策略执行 #{item.id} · {item.name}</strong><span>{item.message}</span><small>进度 {raw.processed_steps}/{raw.total_steps || "?"} · {item.updatedAt ? formatGmt8Time(item.updatedAt) : "—"}</small></div>
                <em className={`status-pill status-${item.status === "completed" ? "ready" : item.status === "failed" ? "blocked" : "degraded"}`}>{statusText(item.status)}</em>
                {item.status === "paused" && <button className="button ghost" disabled={busy !== ""} onClick={() => void action(`task-${item.id}`, () => resumeQuantTask(item.id), `策略执行任务 #${item.id} 已恢复`)}>继续任务</button>}
              </article>;
            })}
            {!strategyTasks.length && <div className="empty">暂无策略执行任务。</div>}
          </div>
        </div>

        <div className="card">
          <div className="governance-section-head"><div><h3>系统维护任务</h3><p>日常数据更新、全部数据补全和后台维护事件。</p></div></div>
          <div className="governance-maintenance-grid">
            <article><div><span>日常数据更新</span><strong>{dailyUpdate?.running ? "运行中" : statusText(dailyUpdate?.phase)}</strong><small>{dailyUpdate?.message || "等待调度"}</small></div><button className="button ghost" disabled={Boolean(dailyUpdate?.running) || busy !== ""} onClick={() => void action("daily-update", startDailyUpdate, "日常更新已启动")}>执行日更</button></article>
            <article><div><span>全部数据补全</span><strong>{backfill?.running ? "运行中" : statusText(backfill?.phase)}</strong><small>{backfill?.message || "等待补全计划"}</small></div><button className="button ghost" disabled={Boolean(backfill?.running) || busy !== ""} onClick={() => void action("backfill", () => startBackfill(0), "全部补全已启动")}>一键全部补全</button></article>
          </div>
          <div className="governance-runtime-events">
            {(dataTasks?.runtime_logs ?? []).filter((item) => item.logger !== "model-governance").slice(0, 12).map((item) => <div key={item.id}><time>{formatGmt8Time(item.created_at)}</time><strong>{item.logger}</strong><span>{item.message}</span></div>)}
          </div>
        </div>
      </div>}

      {section === "services" &&
      <div className="card governance-health-card">
        <div className="governance-section-head"><div><h3>系统健康</h3><p>只展示影响模型和交易运行的关键状态。</p></div></div>
        <div className="decision-two-col">
          <span>数据质量</span><strong>{statusText(healthGate?.status)} · {healthGate?.detail || "—"}</strong>
          <span>数据就绪</span><strong>{statusText(dataReadyGate?.status)}</strong>
          <span>生产模型文件</span><strong>{models?.active_artifact?.status ? statusText(String(models.active_artifact.status)) : "无生产模型"}</strong>
          <span>异常任务</span><strong>{failedTasks.length} 项</strong>
        </div>
      </div>
      }
    </section>
  );
}
