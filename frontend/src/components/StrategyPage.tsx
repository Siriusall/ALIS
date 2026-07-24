import { useEffect, useRef, useState } from "react";
import {
  autoTuneModels,
  fetchModelTrainRun,
  fetchStrategyWorkbench,
  startHsmmShadowTraining,
  startModelTraining,
  type ModelTrainRun,
  type StrategyWorkbenchResponse,
} from "../api/modelStrategy";
import { formatGmt8Time } from "../utils/time";
import LoadingState from "./LoadingState";

function pct(value: unknown, digits = 1) {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : "—";
}

function num(value: unknown, digits = 4) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : "—";
}

function statusText(value: string) {
  const map: Record<string, string> = {
    blocked: "阻塞",
    ready: "就绪",
    partial: "部分就绪",
    linked: "可下钻",
    passed: "通过",
    failed: "失败",
    transparent_rule_fallback: "透明规则降级",
    shadow_oos_validated_live_observation_pending: "影子观察期",
    shadow_live_observation: "影子观察中",
    shadow_live_observation_review_ready: "影子观察达标",
    shadow_promoted_ready: "评审通过待晋升",
    shadow_review_rejected: "评审未通过",
    shadow_ready_for_implementation_and_validation: "待影子验证",
    shadow_validation_failed: "影子验证失败",
    shadow_blocked_insufficient_history: "历史不足",
  };
  return map[value] || value || "—";
}

function evidenceText(value: unknown) {
  if (value == null) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return "—";
  }
}

export default function StrategyPage() {
  const [workbench, setWorkbench] = useState<StrategyWorkbenchResponse | null>(null);
  const [trainRun, setTrainRun] = useState<ModelTrainRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [training, setTraining] = useState(false);
  const [hsmmTraining, setHsmmTraining] = useState(false);
  const [tuning, setTuning] = useState(false);
  const [message, setMessage] = useState("");
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    refresh();
    return () => {
      if (pollRef.current != null) window.clearTimeout(pollRef.current);
    };
  }, []);

  async function refresh() {
    setLoading(true);
    setMessage("");
    try {
      const result = await fetchStrategyWorkbench({ limit: 10, include_details: true });
      setWorkbench(result);
      setTrainRun(result.model.latest_train_run ?? null);
      const latestRun = result.model.latest_train_run ?? null;
      const isRunning = latestRun?.status === "running" || latestRun?.status === "queued";
      setTraining(isRunning);
      if (isRunning && latestRun && pollRef.current == null) {
        pollRef.current = window.setTimeout(() => pollTrainRun(latestRun.id), 1200);
      }
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "策略工作台读取失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleTrain() {
    setTraining(true);
    setMessage("");
    try {
      const result = await startModelTraining({ exchange_group: "HS" });
      pollTrainRun(result.run_id);
    } catch (err) {
      setTraining(false);
      setMessage(err instanceof Error ? err.message : "启动训练失败");
    }
  }

  async function pollTrainRun(runId: number) {
    pollRef.current = null;
    try {
      const run = await fetchModelTrainRun(runId);
      setTrainRun(run);
      if (run.status === "running" || run.status === "queued") {
        pollRef.current = window.setTimeout(() => pollTrainRun(runId), 1200);
        return;
      }
      setTraining(false);
      await refresh();
    } catch (err) {
      setTraining(false);
      setMessage(err instanceof Error ? err.message : "训练进度读取失败");
    }
  }

  async function handleAutoTune() {
    setTuning(true);
    setMessage("");
    try {
      const result = await autoTuneModels();
      setMessage(`自动调优完成：${result.decision}，评估 ${result.evaluated} 个候选`);
      await refresh();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "自动调优失败");
    } finally {
      setTuning(false);
    }
  }

  async function handleHsmmShadowTraining() {
    setHsmmTraining(true);
    setMessage("");
    try {
      const result = await startHsmmShadowTraining();
      let resultMessage: string;
      if (!result.ok) {
        resultMessage = `HSMM 影子训练未完成：${result.status || "训练历史不足或 API 数据不可用"}`;
      } else {
        const validation = String(result.validation?.status || "完成");
        resultMessage = `HSMM 影子训练完成：${result.version || "新版本"}，OOS 验证=${validation}。已使用全市场历史聚合特征和三条宽基指数锚点。`;
      }
      await refresh();
      setMessage(resultMessage);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "HSMM 影子训练失败");
    } finally {
      setHsmmTraining(false);
    }
  }

  const active = workbench?.model.active ?? null;
  const challengers = workbench?.model.challengers ?? [];
  const experiments = workbench?.model.experiments ?? [];
  const failed = workbench?.model.failed_experiments ?? [];
  const decisions = workbench?.pending_user_decisions ?? [];
  const acceptance = workbench?.backend_acceptance;
  const acceptanceChecks = acceptance?.checks ?? [];

  if (loading && !workbench) {
    return (
      <section id="strategy" className="dashboard-section" data-section="strategy">
        <h2 className="dashboard-section-title">策略工作台</h2>
        <LoadingState variant="page" title="正在建立策略工作台" detail="正在汇总模型、验收矩阵和治理证据" rows={5} />
      </section>
    );
  }

  return (
    <section id="strategy" className="dashboard-section" data-section="strategy">
      <h2 className="dashboard-section-title">策略工作台</h2>
      <div className="dashboard-section-inner decision-page">
        <div className="product-module-head">
          <div>
            <h1>模型、规则、影子实验和调优决议</h1>
            <p>先看当前分析来源，再看候选、失败实验和待处理决议。</p>
          </div>
          <button type="button" className="button ghost" onClick={refresh} disabled={loading}>
            {loading ? "刷新中" : "刷新"}
          </button>
        </div>

        {message && <div className="alert">{message}</div>}

        <div className={`card model-training-status model-training-status-${trainRun?.status || "idle"}`}>
          <div className="product-module-head">
            <div>
              <h3>模型训练状态</h3>
              <p>
                {trainRun
                  ? `任务 #${trainRun.id} · ${trainRun.status === "running" ? "正在训练" : trainRun.status}`
                  : "当前没有训练记录"}
              </p>
            </div>
            <strong>{Math.round(Number(trainRun?.progress || 0) * 100)}%</strong>
          </div>
          {trainRun && (
            <>
              <div className="task-progress" aria-label="模型训练进度">
                <div className="bar"><i style={{ width: `${Math.round(Number(trainRun.progress || 0) * 100)}%` }} /></div>
              </div>
              <div className="decision-two-col" style={{ marginTop: 12 }}>
                <span>当前阶段</span><strong>{trainRun.phase || "准备中"}</strong>
                <span>实时消息</span><strong>{trainRun.error || trainRun.message || "等待进度更新"}</strong>
                <span>最近更新</span><strong>{trainRun.updated_at ? formatGmt8Time(trainRun.updated_at) : "—"}</strong>
              </div>
            </>
          )}
        </div>

        <div className="product-module-grid">
          <article className="product-module-card">
            <h2>总体状态</h2>
            <strong>{statusText(workbench?.status.overall || "")}</strong>
            <p>当前是否可运行</p>
          </article>
          <article className="product-module-card">
            <h2>评分来源</h2>
            <strong>{statusText(workbench?.current_analysis.scoring_source || "")}</strong>
            <p>当前打分口径</p>
          </article>
          <article className="product-module-card">
            <h2>市场状态模型</h2>
            <strong>{statusText(workbench?.status.market_regime || "")}</strong>
            <p>当前状态来源</p>
          </article>
          <article className="product-module-card">
            <h2>生成时间</h2>
            <strong>{workbench ? formatGmt8Time(workbench.generated_at) : "—"}</strong>
            <p>当前快照时间</p>
          </article>
        </div>

        <div className="card">
          <h3>当前分析口径</h3>
          <div className="decision-two-col">
            <span>模式</span><strong>{workbench?.current_analysis.mode || "—"}</strong>
            <span>成交规则</span><strong>{workbench?.current_analysis.execution_rule || "—"}</strong>
            <span>生产模型</span><strong>{active?.version || "无 active ML，使用透明规则"}</strong>
            <span>Artifact</span><strong>{String(workbench?.model.active_artifact?.status || "—")}</strong>
          </div>
        </div>

        {decisions.length > 0 && (
          <div className="card">
            <h3>待关注决议</h3>
            <div className="product-module-grid">
              {decisions.map((item) => (
                <article className="product-module-card" key={item.key}>
                  <h2>{item.key}</h2>
                  <div className="module-output"><i />{statusText(item.status)}</div>
                  <p>{item.message}</p>
                </article>
              ))}
            </div>
          </div>
        )}

        <div className="decision-two-column-grid">
          <div className="card">
            <h3>Champion / 当前版本</h3>
            <div className="decision-two-col">
              <span>版本</span><strong>{active?.version || "—"}</strong>
              <span>状态</span><strong>{active?.status || "transparent_rule_fallback"}</strong>
              <span>生产资格</span><strong>{active?.production_eligible ? "已满足" : "未满足"}</strong>
              <span>Lift</span><strong>{num(active?.metrics?.lift)}</strong>
              <span>命中率</span><strong>{pct(active?.metrics?.hit_rate)}</strong>
              <span>阻塞原因</span><strong>{active?.blocked_reasons?.join("、") || "—"}</strong>
            </div>
          </div>

          <div className="card">
            <h3>训练与调优</h3>
            <div className="decision-two-col">
              <span>最近任务</span><strong>{trainRun ? `#${trainRun.id} ${trainRun.status}` : "—"}</strong>
              <span>阶段</span><strong>{trainRun?.phase || "—"}</strong>
              <span>进度</span><strong>{Math.round(Number(trainRun?.progress || 0) * 100)}%</strong>
              <span>消息</span><strong>{trainRun?.error || trainRun?.message || "—"}</strong>
              <span>HSMM 数据范围</span><strong>全市场历史日线聚合特征 + 沪深300 / 中证500 / 中证1000指数锚点</strong>
            </div>
            <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
              <button type="button" className="quant-analyze-confirm-btn" onClick={handleTrain} disabled={training}>
                {training ? "训练中" : "训练 Challenger"}
              </button>
              <button type="button" className="quant-analyze-secondary-btn" onClick={handleAutoTune} disabled={tuning}>
                {tuning ? "调优中" : "运行自动调优"}
              </button>
              <button type="button" className="quant-analyze-secondary-btn" onClick={handleHsmmShadowTraining} disabled={hsmmTraining || training}>
                {hsmmTraining ? "HSMM 训练中" : "训练 HSMM 影子模型"}
              </button>
            </div>
          </div>
        </div>

        <div className="card">
          <h3>Challenger 与失败实验</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>版本/任务</th><th>状态</th><th>生产资格</th><th>Lift</th><th>Walk-forward</th><th>阻塞/错误</th></tr>
              </thead>
              <tbody>
                {challengers.map((item) => (
                  <tr key={`version-${item.id}`}>
                    <td>{item.version}</td>
                    <td>{item.status}</td>
                    <td>{item.production_eligible ? "可晋升" : "不可晋升"}</td>
                    <td>{num(item.metrics?.lift)}</td>
                    <td>{num(item.metrics?.walk_forward_lift)}</td>
                    <td>{item.blocked_reasons?.join("、") || "—"}</td>
                  </tr>
                ))}
                {failed.map((item) => (
                  <tr key={`run-${item.id}`}>
                    <td>训练任务 #{item.id}</td>
                    <td>{item.status}</td>
                    <td>不可晋升</td>
                    <td>{num(item.metrics?.lift)}</td>
                    <td>{num(item.metrics?.walk_forward_lift)}</td>
                    <td>{item.error || item.message || "—"}</td>
                  </tr>
                ))}
                {challengers.length === 0 && failed.length === 0 && (
                  <tr><td colSpan={6} className="empty">暂无 Challenger 或失败实验</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h3>固定模型构成</h3>
          <div className="table-scroll">
            <table>
              <thead><tr><th>组件</th><th>计划书规定模型</th><th>当前实现状态</th></tr></thead>
              <tbody>
                {Object.entries(workbench?.model.constitution ?? {}).map(([key, item]) => (
                  <tr key={key}><td>{key}</td><td>{item.model}</td><td>{item.status}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h3>计划书验收矩阵</h3>
          <div className="metrics-row" style={{ marginTop: 8 }}>
            <div className="card metric-card">
              <div className="sub">验收状态</div>
              <div className="metric">{statusText(acceptance?.status || "")}</div>
            </div>
            <div className="card metric-card">
              <div className="sub">通过</div>
              <div className="metric">{acceptance?.counts?.passed ?? 0}</div>
            </div>
            <div className="card metric-card">
              <div className="sub">部分</div>
              <div className="metric">{acceptance?.counts?.partial ?? 0}</div>
            </div>
            <div className="card metric-card">
              <div className="sub">失败</div>
              <div className="metric">{acceptance?.counts?.failed ?? 0}</div>
            </div>
          </div>
          <div className="table-scroll" style={{ marginTop: 12 }}>
            <table>
              <thead><tr><th>验收项</th><th>状态</th><th>计划书要求</th><th>当前证据</th></tr></thead>
              <tbody>
                {acceptanceChecks.map((raw) => (
                  <tr key={String(raw.key)}>
                    <td>{String(raw.name || raw.key || "—")}</td>
                    <td>{statusText(String(raw.status || ""))}</td>
                    <td>{String(raw.requirement || "—")}</td>
                    <td>{evidenceText(raw.evidence)}</td>
                  </tr>
                ))}
                {acceptanceChecks.length === 0 && (
                  <tr><td colSpan={4} className="empty">暂无验收明细</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h3>最近实验记录</h3>
          <div className="table-scroll">
            <table>
              <thead><tr><th>任务</th><th>状态</th><th>阶段</th><th>进度</th><th>更新时间</th><th>消息</th></tr></thead>
              <tbody>
                {experiments.map((item) => (
                  <tr key={item.id}>
                    <td>#{item.id}</td>
                    <td>{item.status}</td>
                    <td>{item.phase || "—"}</td>
                    <td>{Math.round(Number(item.progress || 0) * 100)}%</td>
                    <td>{item.updated_at ? formatGmt8Time(item.updated_at) : "—"}</td>
                    <td>{item.error || item.message || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {experiments.length === 0 && <div className="empty">暂无训练或调优记录</div>}
        </div>
      </div>
    </section>
  );
}
