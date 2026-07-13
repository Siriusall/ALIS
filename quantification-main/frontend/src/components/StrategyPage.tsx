import React, { useEffect, useMemo, useRef, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fetchBacktest, fetchBacktestJob, runBacktestAsync, type BacktestRunResponse } from "../api/backtest";
import {
  fetchModelStrategyStatus,
  fetchModelConstitution,
  fetchModelTrainRun,
  startModelTraining,
  autoTuneModels,
  rollbackModelVersion,
  compareModelVersions,
  type ModelComparison,
  type ModelStrategyStatus,
  type ModelTrainRun,
  type ModelConstitution,
} from "../api/modelStrategy";
import { chartGridStroke, chartLinePrimary, chartTooltipStyle } from "../chartTheme";

export default function StrategyPage() {
  const [status, setStatus] = useState<ModelStrategyStatus | null>(null);
  const [constitution, setConstitution] = useState<ModelConstitution | null>(null);
  const [trainRun, setTrainRun] = useState<ModelTrainRun | null>(null);
  const [training, setTraining] = useState(false);
  const [validating, setValidating] = useState(false);
  const [tuning, setTuning] = useState(false);
  const [validation, setValidation] = useState<BacktestRunResponse | null>(null);
  const [error, setError] = useState("");
  const [rollingBackId, setRollingBackId] = useState<number | null>(null);
  const [comparison, setComparison] = useState<ModelComparison | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    refreshStatus();
    return () => {
      if (pollRef.current != null) window.clearTimeout(pollRef.current);
    };
  }, []);

  async function refreshStatus() {
    setError("");
    try {
      const data = await fetchModelStrategyStatus();
      setStatus(data);
      setConstitution(await fetchModelConstitution());
      setTrainRun(data.latest_train_run ?? null);
      setTraining(data.latest_train_run?.status === "running");
    } catch {
      setError("模型状态加载失败");
    }
  }

  async function handleTrain() {
    setTraining(true);
    setError("");
    try {
      const res = await startModelTraining({ exchange_group: "HS" });
      pollTrainRun(res.run_id);
    } catch (e: unknown) {
      setTraining(false);
      setError((e as Error).message || "启动训练失败");
    }
  }

  async function pollTrainRun(runId: number) {
    try {
      const run = await fetchModelTrainRun(runId);
      setTrainRun(run);
      if (run.status === "running") {
        pollRef.current = window.setTimeout(() => pollTrainRun(runId), 1000);
        return;
      }
      setTraining(false);
      await refreshStatus();
    } catch {
      setTraining(false);
      setError("训练进度查询失败");
    }
  }

  async function handleValidate() {
    const activeId = status?.active_version?.id;
    if (!activeId) {
      setError("还没有通过验证的模型版本，请先训练模型");
      return;
    }
    setValidating(true);
    setError("");
    try {
      const res = await runBacktestAsync({
        model_version_id: activeId,
        start_date: "2026-05-07",
        end_date: "2026-07-06",
        initial_cash: 200000,
        rebalance_freq: "daily",
        benchmark_symbol: "000300",
        exchange_group: "HS",
      });
      const loop = async () => {
        const job = await fetchBacktestJob(res.job_id);
        if (job.status === "completed" && job.backtest_id) {
          setValidation(await fetchBacktest(job.backtest_id));
          setValidating(false);
          return;
        }
        if (job.status === "failed") {
          throw new Error(job.error || "模型验证失败");
        }
        pollRef.current = window.setTimeout(loop, 800);
      };
      await loop();
    } catch (e: unknown) {
      setValidating(false);
      setError((e as Error).message || "模型验证失败");
    }
  }

  async function handleAutoTune() {
    setTuning(true);
    setError("");
    try {
      const result = await autoTuneModels();
      setError(result.decision === "promoted" ? "Challenger 已通过门槛并晋升" : `自动调优完成：${result.decision}（评估 ${result.evaluated} 个候选）`);
      await refreshStatus();
    } catch (e: unknown) {
      setError((e as Error).message || "自动调优失败");
    } finally {
      setTuning(false);
    }
  }

  async function handleRollback(modelVersionId: number) {
    setRollingBackId(modelVersionId);
    setError("");
    try {
      const result = await rollbackModelVersion(modelVersionId);
      setError(`已从当前版本回退到模型 #${result.model_version_id}`);
      await refreshStatus();
    } catch (e: unknown) {
      setError((e as Error).message || "模型回退失败");
    } finally {
      setRollingBackId(null);
    }
  }

  async function handleCompare(modelVersionId: number) {
    if (!active?.id || active.id === modelVersionId) return;
    setError("");
    try { setComparison(await compareModelVersions(active.id, modelVersionId)); }
    catch (e: unknown) { setError((e as Error).message || "模型比较失败"); }
  }

  const active = status?.active_version ?? null;
  const latestMetrics = active?.metrics ?? trainRun?.metrics ?? {};
  const versions = status?.versions ?? [];
  const chartData = useMemo(() => {
    if (!validation) return [];
    return [
      { name: "总收益", value: Number(validation.metrics.total_return || 0) * 100 },
      { name: "超额", value: Number(validation.metrics.excess_return || 0) * 100 },
      { name: "夏普", value: Number(validation.metrics.sharpe || 0) },
    ];
  }, [validation]);

  function pct(v: unknown, digits = 2) {
    const n = Number(v);
    return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : "—";
  }

  function num(v: unknown, digits = 4) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(digits) : "—";
  }

  return (
    <section id="strategy" className="dashboard-section" data-section="strategy">
      <h2 className="dashboard-section-title">模型策略</h2>
      <div className="dashboard-section-inner decision-page">
        <div className="card">
          <h3>主模型</h3>
          {error && <div className="alert">{error}</div>}
          <div className="decision-two-col">
            <span>当前版本</span>
            <strong>{active?.version || "尚未生成"}</strong>
            <span>状态</span>
            <strong>{active ? (active.is_active ? "已通过验证" : active.status) : "待训练"}</strong>
            <span>模型类型</span>
            <strong>{String(active?.config?.model_type || "机器学习 ranking")}</strong>
            <span>收益假设</span>
            <strong>用历史因子学习目标组合排序，优先选择风险调整后收益更高的股票</strong>
            <span>失效场景</span>
            <strong>市场风格突变、数据覆盖不足、交易约束导致无法成交</strong>
          </div>
          <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
            <button type="button" className="quant-analyze-confirm-btn" onClick={handleTrain} disabled={training}>
              {training ? "训练中…" : "开始自动训练"}
            </button>
            <button type="button" className="quant-analyze-secondary-btn" onClick={handleValidate} disabled={validating || !active}>
              {validating ? "验证中…" : "验证最新模型"}
            </button>
            <button type="button" className="quant-analyze-secondary-btn" onClick={refreshStatus}>
              刷新状态
            </button>
            <button type="button" className="quant-analyze-secondary-btn" onClick={handleAutoTune} disabled={tuning}>
              {tuning ? "调优中…" : "运行自动调优"}
            </button>
          </div>
        </div>

        {constitution && (
          <div className="card">
            <h3>固定模型构成与实现状态</h3>
            <div className="table-scroll">
              <table>
                <thead><tr><th>组件</th><th>规定模型</th><th>当前状态</th></tr></thead>
                <tbody>{Object.entries(constitution.components).map(([key, item]) => (
                  <tr key={key}><td>{key}</td><td>{item.model}</td><td>{item.status}</td></tr>
                ))}</tbody>
              </table>
            </div>
          </div>
        )}

        <div className="metrics-row">
          <div className="card metric-card">
            <div className="sub">Top 组合收益提升</div>
            <div className="metric">{num(latestMetrics.lift)}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">Top 命中率</div>
            <div className="metric">{pct(latestMetrics.hit_rate, 1)}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">训练样本</div>
            <div className="metric">{latestMetrics.train_rows ? String(latestMetrics.train_rows) : "—"}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">验证样本</div>
            <div className="metric">{latestMetrics.validation_rows ? String(latestMetrics.validation_rows) : "—"}</div>
          </div>
        </div>

        {trainRun && (
          <div className="card">
            <h3>训练进度</h3>
            <div className="decision-two-col">
              <span>任务</span>
              <strong>#{trainRun.id} {trainRun.status}</strong>
              <span>阶段</span>
              <strong>{trainRun.phase || "—"}</strong>
              <span>进度</span>
              <strong>{Math.round(Number(trainRun.progress || 0) * 100)}%</strong>
              <span>消息</span>
              <strong>{trainRun.error || trainRun.message || "—"}</strong>
            </div>
          </div>
        )}

        {validation && (
          <div className="card">
            <h3>最近验证</h3>
            <div className="metrics-row">
              <div className="card metric-card">
                <div className="sub">总收益</div>
                <div className="metric">{pct(validation.metrics.total_return)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">最大回撤</div>
                <div className="metric">{pct(validation.metrics.max_drawdown)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">夏普</div>
                <div className="metric">{num(validation.metrics.sharpe, 2)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">超额收益</div>
                <div className="metric">{pct(validation.metrics.excess_return)}</div>
              </div>
            </div>
            <div style={{ height: 220, marginTop: 12 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid stroke={chartGridStroke} strokeDasharray="3 3" />
                  <XAxis dataKey="name" />
                  <YAxis />
                  <Tooltip contentStyle={chartTooltipStyle} />
                  <Line type="monotone" dataKey="value" stroke={chartLinePrimary} dot />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {comparison && <div className="card"><h3>Champion / Challenger 比较</h3><p>{comparison.champion_version || comparison.champion_id} → {comparison.challenger_version || comparison.challenger_id}：{comparison.decision}</p><div className="decision-two-col">{Object.entries(comparison.deltas).map(([key, value]) => <React.Fragment key={key}><span>{key}</span><strong>{Number(value).toFixed(4)}</strong></React.Fragment>)}<span>门禁</span><strong>{Object.entries(comparison.gates).filter(([, passed]) => passed).length}/{Object.keys(comparison.gates).length} 通过</strong><span>配置差异</span><strong>{Object.keys(comparison.config_diff).length} 项</strong></div>{comparison.blocked_reasons.length > 0 && <div className="alert">阻断原因：{comparison.blocked_reasons.join("、")}</div>}</div>}
        <div className="card">
          <h3>模型版本</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>版本</th>
                  <th>状态</th>
                  <th>模型</th>
                  <th>Lift</th>
                  <th>命中率</th>
                  <th>时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {versions.length === 0 ? (
                  <tr><td colSpan={7} className="empty">暂无模型版本，点击开始自动训练。</td></tr>
                ) : (
                  versions.map((v) => (
                    <tr key={v.id}>
                      <td>{v.version}</td>
                      <td>{v.is_active ? "active" : v.status}</td>
                      <td>{String(v.config?.model_type || "—")}</td>
                      <td>{num(v.metrics?.lift)}</td>
                      <td>{pct(v.metrics?.hit_rate, 1)}</td>
                      <td>{v.created_at.slice(0, 19).replace("T", " ")}</td>
                      <td>{v.is_active ? "当前版本" : <><button type="button" className="button ghost" onClick={() => void handleCompare(v.id)} disabled={!active}>比较</button> <button type="button" className="button ghost" disabled={rollingBackId !== null || String(v.validation?.status || "") !== "passed"} onClick={() => void handleRollback(v.id)}>{rollingBackId === v.id ? "回退中…" : "回退至此"}</button></>}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  );
}
