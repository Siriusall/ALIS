import { useEffect, useMemo, useState } from "react";
import { fetchTaskCenter, type TaskCenterResponse, type TaskCenterTask } from "../api/modelStrategy";
import LoadingState from "./LoadingState";

const statusLabel: Record<string, string> = {
  running: "运行中", waiting: "等待条件", paused: "已暂停", failed: "内部错误",
  cancelled: "已取消", completed: "已完成", blocked: "安全阻断", idle: "等待计划",
  observing: "观察中", degraded: "降级运行", partial: "部分完成",
};

const moduleLoggerHints: Record<string, string[]> = {
  data: ["data", "tushare", "http"], factor: ["factor"], model: ["model", "training", "governance"],
  strategy: ["strategy", "evolution", "oos", "shadow"], portfolio: ["portfolio", "profit"],
  execution: ["quant", "execution", "ledger", "corporate"], review: ["review", "performance"],
  research: ["research", "policy", "industry", "theme"], llm: ["llm"], autonomy: ["autonomy", "healing", "incident"],
};

function exactTime(value?: string | null, fallback = "—") {
  if (!value) return fallback;
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}+08:00`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date).replace(/\//g, "-");
}

function countdown(value?: string | null) {
  if (!value) return "未安排精确时间";
  const date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}+08:00`);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  if (!Number.isFinite(seconds)) return "时间不可解析";
  if (seconds <= 0) return "已到复核时间，等待调度器领取";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return `${days ? `${days} 天 ` : ""}${hours ? `${hours} 小时 ` : ""}${minutes} 分 ${remainder} 秒后`;
}

function progress(value: number) { return `${Math.round(Math.max(0, Math.min(1, value || 0)) * 100)}%`; }

export default function TaskCenterPage() {
  const [data, setData] = useState<TaskCenterResponse | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [moduleFilter, setModuleFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("active");
  const [logQuery, setLogQuery] = useState("");
  const [logLevel, setLogLevel] = useState("all");
  const [showAllLogs, setShowAllLogs] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await fetchTaskCenter();
        if (!active) return;
        setData(result); setError("");
        setSelectedId((current) => current && result.tasks.some((item) => item.id === current)
          ? current
          : result.tasks.find((item) => item.status === "running")?.id || result.tasks[0]?.id || "");
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "任务中心读取失败");
      } finally { pending = false; }
    };
    void load();
    const timer = window.setInterval(load, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const visibleTasks = useMemo(() => (data?.tasks ?? []).filter((item) => {
    const modulePass = moduleFilter === "all" || item.module === moduleFilter;
    const statusPass = statusFilter === "all" || (statusFilter === "active"
      ? ["running", "waiting", "observing", "blocked", "degraded", "partial"].includes(item.status)
      : item.status === statusFilter);
    const scopePass = statusFilter !== "active" || item.activity_scope !== "history";
    return modulePass && statusPass && scopePass;
  }), [data, moduleFilter, statusFilter]);
  const selected = data?.tasks.find((item) => item.id === selectedId) ?? visibleTasks[0] ?? data?.tasks[0];
  const logs = useMemo(() => {
    const hints = selected ? moduleLoggerHints[selected.module] ?? [] : [];
    const query = logQuery.trim().toLowerCase();
    const filtered = (data?.logs ?? []).filter((item) => {
      const haystack = `${item.logger} ${item.message} ${JSON.stringify(item.context)}`.toLowerCase();
      const modulePass = !selected || hints.length === 0 || hints.some((hint) => haystack.includes(hint));
      const levelPass = logLevel === "all" || item.level.toLowerCase() === logLevel;
      return modulePass && levelPass && (!query || haystack.includes(query));
    });
    const grouped = new Map<string, typeof filtered[number] & { repeat_count: number }>();
    filtered.forEach((item) => {
      const key = `${item.level}|${item.logger}|${item.message}`;
      const existing = grouped.get(key);
      if (existing) existing.repeat_count += 1;
      else grouped.set(key, { ...item, repeat_count: 1 });
    });
    return Array.from(grouped.values()).slice(0, showAllLogs ? 200 : 50);
  }, [data, selected, logQuery, logLevel, showAllLogs]);

  if (!data && !error) return <LoadingState variant="page" title="正在打开任务中心" detail="汇总数据、因子、模型、策略、组合、执行和复盘过程" rows={6} />;

  return <main className="task-center-page">
    <header className="task-center-head">
      <div><span>ALIS TASK CONTROL</span><h1>任务中心</h1><p>所有模块、策略迭代、下一步、精确时间和运行日志统一在这里。</p></div>
      <div><b className={data?.summary.running ? "running" : "idle"}>{data?.summary.running ? `${data.summary.running} 项运行中` : "当前无计算任务"}</b><time>实时快照 {exactTime(data?.generated_at)}</time></div>
    </header>
    {error && <div className="alert">{error}；保留最近一次成功快照。</div>}

    <section className="task-center-summary">
      <article><span>运行中</span><strong>{data?.summary.running ?? 0}</strong><small>正在占用 worker</small></article>
      <article><span>等待 / 冷却</span><strong>{data?.summary.waiting ?? 0}</strong><small>到达条件后自动继续</small></article>
      <article><span>当前未解决内部错误</span><strong>{data?.summary.current_internal_errors ?? data?.summary.failed ?? 0}</strong><small>只统计每类任务的最新状态</small></article>
      <article><span>最近失败记录</span><strong>{data?.summary.recent_failed_records ?? 0}</strong><small>历史证据，不代表仍未解决</small></article>
    </section>

    <section className="task-module-grid">
      {(data?.modules ?? []).map((item) => <button type="button" key={item.key} className={`task-module-card status-${item.status} ${moduleFilter === item.key ? "selected" : ""}`} onClick={() => { setModuleFilter(item.key); if (item.current_task_id) setSelectedId(item.current_task_id); }}>
        <div><i /><span>{item.name}</span><b>{statusLabel[item.status] || item.status}</b></div>
        <strong>{item.current_step}</strong>
        <dl><div><dt>下一步</dt><dd>{item.next_step}</dd></div><div><dt>精确时间</dt><dd>{exactTime(item.next_run_at, "等待条件")}</dd></div></dl>
        <small>{item.next_run_at ? countdown(item.next_run_at) : `更新于 ${exactTime(item.updated_at)}`}</small>
      </button>)}
    </section>

    <section className="task-center-workspace">
      <div className="task-list-panel">
        <header><div><span>任务流水</span><h2>模块与策略过程</h2></div><button type="button" onClick={() => setModuleFilter("all")}>全部模块</button></header>
        <div className="task-center-filters">
          <select value={moduleFilter} onChange={(event) => setModuleFilter(event.target.value)} aria-label="筛选模块"><option value="all">全部模块</option>{(data?.modules ?? []).map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}</select>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="筛选状态"><option value="active">当前运行、观察与阻断</option><option value="all">全部状态与历史</option><option value="running">仅运行中</option><option value="observing">仅观察中</option><option value="failed">仅失败记录</option><option value="completed">仅已完成</option></select>
        </div>
        <div className="task-center-list">
          {visibleTasks.map((item) => <button type="button" key={item.id} className={`${selected?.id === item.id ? "selected" : ""} status-${item.status}`} onClick={() => setSelectedId(item.id)}>
            <div><i /><strong>{item.name}</strong><b>{statusLabel[item.status] || item.status}</b></div>
            <p>{item.current_step}</p>
            <span><em>{progress(item.progress)}</em><small>{exactTime(item.updated_at || item.started_at)}</small></span>
          </button>)}
          {!visibleTasks.length && <div className="empty">当前筛选条件下没有任务。</div>}
        </div>
      </div>

      <TaskDetail task={selected} />
    </section>

    <section className="task-live-logs">
      <header><div><span>实时结构化日志</span><h2>{selected ? `${selected.name} · ${selected.module}` : "全部系统日志"}</h2></div><div className="task-log-controls"><select value={logLevel} onChange={(event) => setLogLevel(event.target.value)} aria-label="日志级别"><option value="all">全部级别</option><option value="error">ERROR</option><option value="warning">WARNING</option><option value="info">INFO</option></select><input value={logQuery} onChange={(event) => setLogQuery(event.target.value)} placeholder="搜索日志、错误或证券代码" /><button type="button" onClick={() => setShowAllLogs((value) => !value)}>{showAllLogs ? "收起到 50 条" : "展开全部"}</button></div></header>
      <div>{logs.map((item) => <article key={`${item.id}-${item.message}`} className={`level-${item.level.toLowerCase()}`}><time>{exactTime(item.created_at)}</time><b>{item.level}</b><strong>{item.logger}</strong><span>{item.message}{item.repeat_count > 1 && <em>重复 {item.repeat_count} 次</em>}</span>{Object.keys(item.context || {}).length > 0 && <code>{JSON.stringify(item.context)}</code>}</article>)}{!logs.length && <p>当前模块没有匹配的近期日志；可清空任务选择或搜索条件查看全部。</p>}</div>
    </section>
  </main>;
}

function TaskDetail({ task }: { task?: TaskCenterTask }) {
  if (!task) return <section className="task-detail-panel"><div className="empty">请选择一项任务查看过程。</div></section>;
  return <section className={`task-detail-panel status-${task.status}`}>
    <header><div><span>{task.source} · #{task.source_id || "—"}</span><h2>{task.name}</h2><p>{task.message || "暂无过程说明"}</p></div><b>{statusLabel[task.status] || task.status}</b></header>
    <div className="task-detail-progress"><span><i style={{ width: progress(task.progress) }} /></span><strong>{progress(task.progress)}</strong></div>
    <div className="task-step-flow"><article><span>当前步骤</span><strong>{task.current_step}</strong><small>{task.phase || "等待调度"}</small></article><i>→</i><article><span>下一步</span><strong>{task.next_step}</strong><small>{task.next_run_at ? `${exactTime(task.next_run_at)} · ${countdown(task.next_run_at)}` : "由条件和调度器决定"}</small></article></div>
    <dl className="task-time-grid">
      <div><dt>创建</dt><dd>{exactTime(task.created_at)}</dd></div><div><dt>开始</dt><dd>{exactTime(task.started_at)}</dd></div>
      <div><dt>最近心跳</dt><dd>{exactTime(task.heartbeat_at)}</dd></div><div><dt>最后更新</dt><dd>{exactTime(task.updated_at)}</dd></div>
      <div><dt>完成</dt><dd>{exactTime(task.finished_at)}</dd></div><div><dt>尝试次数</dt><dd>{task.attempt}/{task.max_attempts || 1}</dd></div>
    </dl>
    {task.duration_seconds != null && <p className="task-time-source">步骤耗时 {task.duration_seconds.toFixed(3)} 秒 · {task.time_source === "recorded" ? "精确记录时间" : "历史时间按周期耗时推导"}</p>}
    {task.error && <div className="task-detail-error"><strong>错误证据</strong><p>{task.error}</p></div>}
    {task.checkpoint && Object.keys(task.checkpoint).length > 0 && <details><summary>检查点与续跑证据</summary><pre>{JSON.stringify(task.checkpoint, null, 2)}</pre></details>}
  </section>;
}
