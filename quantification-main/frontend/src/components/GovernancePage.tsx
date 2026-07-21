import { useEffect, useMemo, useRef, useState } from "react";
import { fetchGovernanceOverview, fetchMarketContextGovernance, type GovernanceOverview, type MarketContextGovernance } from "../api/research";
import {
  autoTuneModels,
  fetchPlanModels,
  fetchModelStrategyStatus,
  materializeFormalPerformanceReports,
  promoteModelVersion,
  reviewCandidatePlanModels,
  reviewPlanModelVersion,
  rollbackModelVersion,
  startAllTrainablePlanModels,
  startPlanModelTraining,
  startModelTraining,
  type ModelStrategyStatus,
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
  type GovernanceSectionKey,
} from "./governanceSections";

function statusText(value: unknown) {
  const key = String(value || "unknown");
  const map: Record<string, string> = {
    ready: "正常", blocked: "阻塞", degraded: "降级", unknown: "待检查",
    running: "运行中", queued: "排队中", completed: "已完成", failed: "失败",
    paused: "已暂停", candidate: "候选", watch: "观察", active: "生产中",
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

function compact(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) return value.length ? value.join("、") : "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
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
  const [marketGovernance, setMarketGovernance] = useState<MarketContextGovernance | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [section, setSection] = useState<GovernanceSectionKey>(() =>
    (window.localStorage.getItem(GOVERNANCE_SECTION_STORAGE_KEY) as GovernanceSectionKey | null) || "overview"
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
      setPlanModels(await fetchPlanModels());
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
    if (section !== "models") return;
    void loadPlanModels();
    const timer = window.setInterval(() => void loadPlanModels(true), 15000);
    return () => window.clearInterval(timer);
  }, [section]);

  useEffect(() => {
    const onSection = (event: Event) => {
      const next = (event as CustomEvent<GovernanceSectionKey>).detail;
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
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  }

  const active = models?.active_version ?? null;
  const trainRun = models?.latest_train_run ?? null;
  const versions = models?.versions ?? [];
  const candidates = versions.filter((item) => !item.is_active);
  const failedTasks = overview?.tasks.failed_or_paused ?? [];
  const blockers = overview?.gates.filter((item) => item.status === "blocked") ?? [];
  const healthGate = overview?.gates.find((item) => item.key === "data_health");
  const dataReadyGate = overview?.gates.find((item) => item.key === "data_readiness");
  const systemReady = Boolean(active) && blockers.length === 0;
  const eligibleCandidates = useMemo(() => candidates.filter((item) => eligibility(item).eligible), [candidates]);

  if (loading && !overview && !models) {
    return <LoadingState variant="page" title="正在打开模型工厂" detail="正在读取生产模型、训练任务和系统健康状态" rows={5} />;
  }

  return (
    <section className="product-module-page governance-console">
      <div className="product-module-head">
        <div>
          <span>SYSTEM CENTER</span>
          <h1>模型工厂</h1>
          <p>这里负责训练计划书模型、查看数据参数、审核候选模型并决定是否上线到模拟决策链路。</p>
        </div>
        <button className="button ghost" onClick={() => void load()} disabled={loading}>刷新状态</button>
      </div>

      {error && <div className="alert">{error}</div>}
      {notice && <div className="inline-message">{notice}</div>}

      {section === "overview" && <>
      <div className={`governance-command ${systemReady ? "ready" : "blocked"}`}>
        <div>
          <span>当前运行结论</span>
          <h2>{systemReady ? "生产链路全部就绪" : active ? "每日决策已就绪，部分后台能力受限" : trainRun?.status === "running" ? "正在建立生产模型" : "生产运行仍被阻塞"}</h2>
          <p>{active
            ? `当前由 ${active.version} 提供生产评分。`
            : trainRun?.status === "running"
              ? `训练任务 #${trainRun.id} 正在执行，完成验证并满足门槛后可自动晋升。`
              : "当前没有已晋升的生产模型，系统只能使用透明规则降级运行。"}</p>
        </div>
        <div className="governance-actions">
          <button className="button primary" disabled={busy !== "" || trainRun?.status === "running"} onClick={() => void action("train", () => startModelTraining({ exchange_group: "HS" }), "训练任务已启动")}>{busy === "train" ? "启动中" : "启动新训练"}</button>
          <button className="button ghost" disabled={busy !== "" || trainRun?.status === "running"} onClick={() => void action("tune", autoTuneModels, "自动调优和晋升检查已完成")}>{busy === "tune" ? "执行中" : "运行自动调优"}</button>
          <button className="button ghost" disabled={busy !== "" || readinessRefresh?.running} onClick={() => void action("readiness-refresh", startReadinessRefresh, "深度数据 readiness 已转入后台刷新")}>{readinessRefresh?.running ? "自检中" : "刷新深度自检"}</button>
        </div>
      </div>

      <div className="product-module-grid governance-summary-grid">
        <article className="product-module-card"><h2>生产模型</h2><strong>{active?.version || "未设置"}</strong><p>{active ? "当前接管生产评分" : "透明规则降级运行"}</p></article>
        <article className="product-module-card"><h2>当前训练</h2><strong>{trainRun ? `#${trainRun.id} ${statusText(trainRun.status)}` : "无任务"}</strong><p>{trainRun?.message || "等待下一次训练触发"}</p></article>
        <article className="product-module-card"><h2>可晋升候选</h2><strong>{eligibleCandidates.length}</strong><p>共 {candidates.length} 个非生产版本</p></article>
        <article className="product-module-card"><h2>系统健康</h2><strong>{statusText(healthGate?.status)}</strong><p>数据就绪：{readinessRefresh?.running ? "后台自检中" : statusText(dataReadyGate?.status)} · 缓存 {readinessRefresh?.cached_consumers ?? 0} 项</p></article>
      </div>
      </>}

      {section === "models" && <>
      <section className="model-training-console">
        <header>
          <div>
            <span>模型训练</span>
            <h2>{trainRun?.status === "running" ? "系统正在训练模型" : (planModels?.counts.candidate ?? 0) > 0 ? "有模型等待你审核上线" : "模型工厂正常待命"}</h2>
            <p>已上线 {planModels?.counts.production ?? 0} 个，待审核 {planModels?.counts.candidate ?? 0} 个，待训练 {planModels?.counts.trainable ?? 0} 个。上线后才会参与行情识别、选股和配置。</p>
          </div>
          <div className="model-training-actions">
            <button className="button primary" disabled={busy !== ""} onClick={() => void action("plan-train-all", startAllTrainablePlanModels, "已启动全部可训练计划书模型")}>{busy === "plan-train-all" ? "启动中" : "训练待训练模型"}</button>
            <button className="button ghost" disabled={busy !== "" || (planModels?.counts.candidate ?? 0) <= 0} onClick={() => void action("plan-review-all", reviewCandidatePlanModels, "候选模型已批量评审")}>{busy === "plan-review-all" ? "评审中" : "审核候选上线"}</button>
            <button className="button ghost" disabled={busy !== ""} onClick={() => void action("formal-reports", materializeFormalPerformanceReports, "周期绩效报告已正式物化")}>{busy === "formal-reports" ? "生成中" : "生成绩效报表"}</button>
          </div>
        </header>

        <div className="model-training-status">
          <article><span>主决策模型</span><strong>{active?.version || "未上线"}</strong><p>{active ? "正在服务模拟决策" : "未上线前使用规则降级"}</p></article>
          <article><span>当前训练</span><strong>{trainRun ? `#${trainRun.id} ${statusText(trainRun.status)}` : "无任务"}</strong><p>{trainRun?.message || "可以启动新训练"}</p></article>
          <article><span>行情识别</span><strong>{marketGovernance?.hsmm?.production_eligible ? "可接管" : "观察中"}</strong><p>{marketGovernance?.hsmm ? `观察 ${marketGovernance.hsmm.live_observation_weeks.toFixed(2)} / ${marketGovernance.hsmm.minimum_live_observation_weeks} 周` : "等待状态"}</p></article>
        </div>

        <div className="model-training-list">
          <div className="model-training-list-head">
            <span>模型</span><span>负责什么</span><span>状态</span><span>最新训练</span><span>下一步</span>
          </div>
          {(planModels?.items ?? []).filter((item) => ["production", "candidate", "trainable", "proxy"].includes(String(item.status))).slice(0, 6).map((item) => {
            const running = item.latest_train_run?.status === "running";
            const trainable = item.status === "trainable" || item.status === "production" || item.status === "candidate";
            const latestId = Number(item.latest_version?.id || 0);
            return <article key={item.model_key}>
              <strong>{item.name}</strong>
              <p>{item.role}</p>
              <em className={`status-pill status-${statusClass(item.status)}`}>{statusText(item.status)}</em>
              <span>{running ? "训练中" : item.latest_train_run ? `#${item.latest_train_run.id} ${statusText(item.latest_train_run.status)}` : "暂无记录"}</span>
              <div>
                {item.status === "candidate"
                  ? <button className="button ghost" disabled={busy !== "" || latestId <= 0} onClick={() => void action(`operator-review-${latestId}`, () => reviewPlanModelVersion(latestId, { decision: "approved", comment: "人工批准上线；模拟系统豁免真实20日观察", waive_live_observation: true }), `${item.name} 已批准上线`)}>批准上线</button>
                  : <button className="button ghost" disabled={busy !== "" || running || !trainable} onClick={() => void action(`operator-train-${item.model_key}`, () => startPlanModelTraining(item.model_key), `${item.name} 训练已启动`)}>{running ? "训练中" : "训练"}</button>}
              </div>
            </article>;
          })}
          {!planModels?.items.length && <div className="empty">模型状态读取中。</div>}
        </div>
      </section>

      <details className="module-detail-drawer">
        <summary><span>查看训练参数和历史版本</span><em>高级信息，平时不用打开</em></summary>

      <div className="card">
        <div className="governance-section-head"><div><h3>模型族地图</h3><p>这里把“其他模型”也展开：LightGBM 只负责部分监督学习组件，风险/状态模型保持各自专门算法。</p></div></div>
        <div className="governance-maintenance-grid">
          {Object.entries(models?.constitution || {}).map(([key, item]) => <article key={key}>
            <div><span>{key}</span><strong>{item.model}</strong><small>{item.status}</small></div>
          </article>)}
          <article><div><span>plan_book_supervised_models</span><strong>LightGBM / LambdaRank / Multitask</strong><small>计划书命名的五个功能模型，由下方逐个训练和上线</small></div></article>
          {!Object.keys(models?.constitution || {}).length && <div className="empty">模型族地图读取中。</div>}
        </div>
      </div>

      {marketGovernance?.hsmm && <div className="card governance-training-card">
        <div className="governance-section-head"><div><h3>市场状态模型：HSMM + BOCPD</h3><p>这是市场状态/持有周期/门控证据，不归入 LightGBM 排序器。</p></div><span className={`status-pill status-${marketGovernance.hsmm.production_eligible ? "ready" : "watch"}`}>{marketGovernance.hsmm.production_eligible ? "可接管" : "影子观察"}</span></div>
        <div className="decision-two-col">
          <span>当前生产状态</span><strong>透明四状态机</strong>
          <span>HSMM 角色</span><strong>影子状态模型，观察期满后再接管门控</strong>
          <span>历史 OOS</span><strong>{marketGovernance.hsmm.validation?.status === "passed" ? "已通过" : "未通过"}</strong>
          <span>真实影子观察</span><strong>{marketGovernance.hsmm.live_observation_weeks.toFixed(2)} / {marketGovernance.hsmm.minimum_live_observation_weeks} 周</strong>
          <span>上线评审</span><strong>{marketGovernance.hsmm.review?.status ?? "未完成"}</strong>
          <span>阻塞原因</span><strong>{marketGovernance.hsmm.blocking_reason}</strong>
        </div>
      </div>}

      <div className="card">
        <div className="governance-section-head">
          <div><h3>计划书监督模型控制台</h3><p>逐个模型展示算法族、训练数据、固定参数、产物状态、训练进度、人工上线/驳回入口。</p></div>
          <span>{planModels ? `${planModels.counts.production ?? 0} production / ${planModels.counts.proxy ?? 0} proxy` : "读取中"}</span>
        </div>
        <div className="governance-task-list">
          {(planModels?.items ?? []).map((item) => {
            const coverage = item.coverage || {};
            const effective = Number(coverage.effective_sessions || 0);
            const minimum = Number(coverage.minimum_sessions || 0);
            const progress = minimum > 0 ? Math.min(100, Math.round((effective / minimum) * 100)) : 0;
            const running = item.latest_train_run?.status === "running";
            const trainable = item.status === "trainable" || item.status === "production" || item.status === "candidate";
            const latestId = Number(item.latest_version?.id || 0);
            const review = (item.latest_version?.validation?.review || {}) as Record<string, unknown>;
            const latestArtifact = (item.latest_artifact || {}) as Record<string, unknown>;
            const contract = (item.training_contract || {}) as Record<string, unknown>;
            const metrics = item.latest_version?.metrics || {};
            return <article key={item.model_key} className="governance-model-card">
              <div>
                <div className="governance-section-head compact-head">
                  <div><strong>{item.name}</strong><span>{item.model_key}</span><small>{item.role}</small></div>
                  <em className={`status-pill status-${statusClass(item.status)}`}>{item.status}</em>
                </div>
                <div className="bar"><i style={{ width: `${progress}%` }} /></div>
                <div className="decision-two-col">
                  <span>算法族</span><strong>{item.family}</strong>
                  <span>训练目标</span><strong>{compact(contract.objective || contract.tasks || "—")}</strong>
                  <span>评估指标</span><strong>{compact(contract.metric || contract.eval_at || "—")}</strong>
                  <span>数据覆盖</span><strong>{minimum ? `${effective}/${minimum} sessions` : "—"}</strong>
                  <span>训练数据</span><strong>{item.required_data?.join("、") || "—"}</strong>
                  <span>产物类型</span><strong>{compact(artifactValue(latestArtifact, "model_type"))}</strong>
                  <span>模型文件</span><strong>{compact(artifactValue(latestArtifact, "model_path_exists"))}</strong>
                  <span>训练窗口</span><strong>{compact(artifactValue(latestArtifact, "training_dataset"))}</strong>
                  <span>LightGBM 参数</span><strong>{compact(contract.params)}</strong>
                  <span>最新版本</span><strong>{item.latest_version?.version || "—"}</strong>
                  <span>最新训练</span><strong>{item.latest_train_run ? `#${item.latest_train_run.id} ${statusText(item.latest_train_run.status)} ${Math.round(Number(item.latest_train_run.progress || 0) * 100)}%` : "未训练"}</strong>
                  <span>费用后 Lift</span><strong>{number(metrics.lift)} / WF {number(metrics.walk_forward_lift)}</strong>
                  <span>上线评审</span><strong>{review.status ? `${review.status} · ${review.reviewer || "operator"}` : item.status === "candidate" ? "待评审" : "—"}</strong>
                  <span>阻塞原因</span><strong>{(item.blocked_reasons ?? []).join("、") || "—"}</strong>
                </div>
                {!!item.data_actions?.length && <div className="inline-message">补数建议：{item.data_actions.map((action) => `${action.label}（${action.reason}）`).join("；")}</div>}
              </div>
              <div className="governance-actions">
                <button className="button ghost" disabled={busy !== "" || running || !trainable} onClick={() => void action(`plan-model-${item.model_key}`, () => startPlanModelTraining(item.model_key), `${item.name} 训练已启动`)}>{running ? "训练中" : item.status === "production" ? "重训" : "训练"}</button>
                <button className="button ghost" disabled={busy !== "" || latestId <= 0 || item.status !== "candidate"} onClick={() => void action(`plan-review-${latestId}`, () => reviewPlanModelVersion(latestId, { decision: "approved", comment: "人工批准上线；模拟系统豁免真实20日观察", waive_live_observation: true }), `${item.name} 已批准上线`)}>批准上线</button>
                <button className="button ghost" disabled={busy !== "" || latestId <= 0 || item.status !== "candidate"} onClick={() => void action(`plan-reject-${latestId}`, () => reviewPlanModelVersion(latestId, { decision: "rejected", comment: "人工驳回，继续观察", waive_live_observation: false }), `${item.name} 已驳回`)}>驳回</button>
              </div>
            </article>;
          })}
          {!planModels?.items.length && <div className="empty">暂无模型族状态。</div>}
        </div>
      </div>

      <div className="card governance-training-card">
        <div className="governance-section-head"><div><h3>主线代理模型训练</h3><p>这是 legacy/main 综合代理模型，不等同于计划书五个专门模型；保留用于降级和对照。</p></div><strong>{trainRun ? `${Math.round(Number(trainRun.progress || 0) * 100)}%` : "—"}</strong></div>
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
      </div>

      <div className="card">
        <div className="governance-section-head"><div><h3>主线代理版本控制</h3><p>只有通过验证且具备生产资格的版本才能晋升或回退。</p></div><span>{versions.length} 个版本</span></div>
        <div className="table-scroll">
          <table>
            <thead><tr><th>版本</th><th>角色</th><th>验证</th><th>历史覆盖</th><th>Lift</th><th>Walk-forward</th><th>更新时间</th><th>操作</th></tr></thead>
            <tbody>
              {versions.map((item) => {
                const gate = eligibility(item);
                const canControl = gate.eligible && String(item.validation?.status || "") === "passed";
                return <tr key={item.id}>
                  <td><strong>{item.version}</strong></td>
                  <td>{item.is_active ? "生产模型" : statusText(item.status)}</td>
                  <td>{gate.eligible ? "生产资格通过" : gate.reasons.join("、") || statusText(item.validation?.status)}</td>
                  <td>{gate.minimum ? `${gate.history}/${gate.minimum}` : "—"}</td>
                  <td>{number(item.metrics?.lift)}</td>
                  <td>{number(item.metrics?.walk_forward_lift)}</td>
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
      </details>
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
          <div className="governance-section-head"><div><h3>系统维护任务</h3><p>日常数据更新、历史补全和后台维护事件。</p></div></div>
          <div className="governance-maintenance-grid">
            <article><div><span>日常数据更新</span><strong>{dailyUpdate?.running ? "运行中" : statusText(dailyUpdate?.phase)}</strong><small>{dailyUpdate?.message || "等待调度"}</small></div><button className="button ghost" disabled={Boolean(dailyUpdate?.running) || busy !== ""} onClick={() => void action("daily-update", startDailyUpdate, "日常更新已启动")}>执行日更</button></article>
            <article><div><span>历史数据补全</span><strong>{backfill?.running ? "运行中" : statusText(backfill?.phase)}</strong><small>{backfill?.message || "等待补全计划"}</small></div><button className="button ghost" disabled={Boolean(backfill?.running) || busy !== ""} onClick={() => void action("backfill", () => startBackfill(0), "历史补全已启动")}>执行补全</button></article>
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
