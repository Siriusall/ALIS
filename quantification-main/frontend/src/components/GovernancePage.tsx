import { useEffect, useMemo, useState } from "react";
import { fetchGovernanceOverview, fetchMarketContextGovernance, type GovernanceOverview, type MarketContextGovernance } from "../api/research";
import {
  autoTuneModels,
  fetchModelStrategyStatus,
  promoteModelVersion,
  rollbackModelVersion,
  startModelTraining,
  type ModelStrategyStatus,
  type ModelVersion,
} from "../api/modelStrategy";
import { resumeQuantTask } from "../api/quantRuns";
import { listQuantTasks } from "../api/quantRuns";
import {
  fetchBackfillStatus,
  fetchDailyUpdateStatus,
  fetchDataObservability,
  startBackfill,
  startDailyUpdate,
  type BackfillStatus,
  type DailyUpdateStatus,
  type DataObservability,
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
  const [taskLoading, setTaskLoading] = useState(false);

  async function load(silent = false) {
    if (!silent) setLoading(true);
    try {
      const [governance, modelStatus, marketStatus] = await Promise.all([
        fetchGovernanceOverview(),
        fetchModelStrategyStatus(),
        fetchMarketContextGovernance(),
      ]);
      setOverview(governance);
      setModels(modelStatus);
      setMarketGovernance(marketStatus);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (!silent) setLoading(false);
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
    return <LoadingState variant="page" title="正在打开系统治理" detail="正在读取生产模型、训练任务和系统健康状态" rows={5} />;
  }

  return (
    <section className="product-module-page governance-console">
      <div className="product-module-head">
        <div>
          <h1>系统治理</h1>
          <p>查看和控制生产模型、持续训练、版本晋升与异常任务。</p>
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
        </div>
      </div>

      <div className="product-module-grid governance-summary-grid">
        <article className="product-module-card"><h2>生产模型</h2><strong>{active?.version || "未设置"}</strong><p>{active ? "当前接管生产评分" : "透明规则降级运行"}</p></article>
        <article className="product-module-card"><h2>当前训练</h2><strong>{trainRun ? `#${trainRun.id} ${statusText(trainRun.status)}` : "无任务"}</strong><p>{trainRun?.message || "等待下一次训练触发"}</p></article>
        <article className="product-module-card"><h2>可晋升候选</h2><strong>{eligibleCandidates.length}</strong><p>共 {candidates.length} 个非生产版本</p></article>
        <article className="product-module-card"><h2>系统健康</h2><strong>{statusText(healthGate?.status)}</strong><p>数据就绪：{statusText(dataReadyGate?.status)}</p></article>
      </div>
      </>}

      {section === "models" && <>
      <div className="card governance-training-card">
        <div className="governance-section-head"><div><h3>训练任务</h3><p>训练启动后会持续执行，完成后自动验证并检查晋升资格。</p></div><strong>{trainRun ? `${Math.round(Number(trainRun.progress || 0) * 100)}%` : "—"}</strong></div>
        <div className="bar"><i style={{ width: `${Math.round(Number(trainRun?.progress || 0) * 100)}%` }} /></div>
        <div className="decision-two-col">
          <span>任务</span><strong>{trainRun ? `#${trainRun.id}` : "—"}</strong>
          <span>状态</span><strong>{statusText(trainRun?.status)}</strong>
          <span>阶段</span><strong>{trainRun?.phase || "—"}</strong>
          <span>实时消息</span><strong>{trainRun?.error || trainRun?.message || "—"}</strong>
          <span>更新时间</span><strong>{trainRun?.updated_at ? formatGmt8Time(trainRun.updated_at) : "—"}</strong>
        </div>
      </div>

      {marketGovernance?.hsmm && <div className="card governance-training-card">
        <div className="governance-section-head"><div><h3>市场状态模型治理：HSMM</h3><p>HSMM（隐半马尔可夫模型）用于识别市场状态及其持续时间；它不是单独的交易策略。</p></div><span>{marketGovernance.hsmm.production_eligible ? "可接管" : "影子观察"}</span></div>
        <div className="decision-two-col">
          <span>当前生产状态</span><strong>透明四状态机</strong>
          <span>HSMM 角色</span><strong>影子模型，不参与仓位门控</strong>
          <span>历史 OOS</span><strong>{marketGovernance.hsmm.validation?.status === "passed" ? "已通过" : "未通过"}</strong>
          <span>真实影子观察</span><strong>{marketGovernance.hsmm.live_observation_weeks.toFixed(2)} / {marketGovernance.hsmm.minimum_live_observation_weeks} 周</strong>
          <span>上线评审</span><strong>{marketGovernance.hsmm.review?.status ?? "未完成"}</strong>
          <span>阻塞原因</span><strong>{marketGovernance.hsmm.blocking_reason}</strong>
        </div>
      </div>}

      <div className="card">
        <div className="governance-section-head"><div><h3>辅助模型与决策工具</h3><p>统一展示每个模型的作用和生产权限；只有明确标记“可接管”的模型才能改变生产决策。</p></div><span>治理清单</span></div>
        <div className="product-module-grid">
          {[
            ["主 Ranking 模型", "股票排序与候选信号", "生产候选 / 需版本晋升", "版本化治理"],
            ["BOCPD", "市场突变和风险切换报警", "辅助报警，不直接接管", "已启用"],
            ["行业动态因子 + Kalman", "行业景气与状态估计", "辅助研究，不直接接管", "建设中"],
            ["主题聚类", "主题发现与生命周期", "辅助研究，不直接接管", "初步实现"],
            ["Black-Litterman", "组合观点融合与目标权重", "调仓工具，需人工/策略确认", "部分接入"],
            ["贝叶斯逻辑复核", "投资假设证据更新", "复核工具，不自动失效", "部分接入"],
          ].map(([name, purpose, role, status]) => <article className="product-module-card" key={name}>
            <h2>{name}</h2><p>{purpose}</p><strong>{role}</strong><div className="module-output"><i />{status}</div>
          </article>)}
        </div>
      </div>

      <div className="card">
        <div className="governance-section-head"><div><h3>模型版本与生产控制</h3><p>只有通过验证且具备生产资格的版本才能晋升或回退。</p></div><span>{versions.length} 个版本</span></div>
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
