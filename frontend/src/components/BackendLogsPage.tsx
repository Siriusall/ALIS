import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { fetchDataObservability, fetchRuntimeLogsAfter, type RuntimeLogItem } from "../api/dataCenter";
import { formatGmt8Time } from "../utils/time";

type Source = "stdout" | "stderr";
type View = "runtime" | Source;
type RawLogChunk = {
  source: Source;
  text: string;
  offset: number;
  next_offset: number;
  size: number;
  eof: boolean;
  current_pid: number;
  file_updated_at: string | null;
  provenance: string;
  provenance_note?: string;
};

const EMPTY = { stdout: "", stderr: "" };
const ZERO = { stdout: 0, stderr: 0 };

export default function BackendLogsPage() {
  const [view, setView] = useState<View>("runtime");
  const [runtime, setRuntime] = useState<RuntimeLogItem[]>([]);
  const [content, setContent] = useState<Record<Source, string>>(EMPTY);
  const [offsets, setOffsets] = useState<Record<Source, number>>(ZERO);
  const [rawMeta, setRawMeta] = useState<Partial<Record<Source, RawLogChunk>>>({});
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const offsetsRef = useRef<Record<Source, number>>(ZERO);
  const latestRuntimeId = useRef(0);
  const outputRef = useRef<HTMLPreElement | null>(null);

  const readRaw = useCallback(async (target: Source, fromStart = false) => {
    let offset = fromStart ? 0 : offsetsRef.current[target];
    const response = await api.get<RawLogChunk>("/api/logs/backend", {
      params: { source: target, offset, limit: 4 * 1024 * 1024 }, timeout: 30_000,
    });
    const chunk = response.data;
    offsetsRef.current = { ...offsetsRef.current, [target]: chunk.next_offset };
    setOffsets(offsetsRef.current);
    setRawMeta((current) => ({ ...current, [target]: chunk }));
    setContent((current) => ({
      ...current,
      [target]: fromStart || chunk.offset !== offset ? chunk.text : current[target] + chunk.text,
    }));
  }, []);

  useEffect(() => {
    let stopped = false;
    async function initialize() {
      try {
        const initial = await fetchDataObservability(200);
        if (stopped) return;
        const rows = [...(initial.runtime_logs ?? [])].sort((a, b) => a.id - b.id);
        setRuntime(rows.slice(-500));
        latestRuntimeId.current = Math.max(0, ...rows.map((item) => item.id));
        await Promise.all([readRaw("stdout", true), readRaw("stderr", true)]);
        if (!stopped) { setConnected(true); setError(""); }
      } catch (reason) {
        if (!stopped) setError(reason instanceof Error ? reason.message : "系统日志读取失败");
      }
    }
    void initialize();
    const timer = window.setInterval(async () => {
      try {
        const result = await fetchRuntimeLogsAfter(latestRuntimeId.current, 200, 0);
        if (result.items.length) {
          latestRuntimeId.current = Math.max(latestRuntimeId.current, ...result.items.map((item) => item.id));
          setRuntime((current) => [...current, ...result.items].slice(-500));
        }
        await Promise.all([readRaw("stdout"), readRaw("stderr")]);
        if (!stopped) { setConnected(true); setError(""); }
      } catch (reason) {
        if (!stopped) { setConnected(false); setError(reason instanceof Error ? reason.message : "系统日志读取失败"); }
      }
    }, 3000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [readRaw]);

  const runtimeText = useMemo(() => runtime.map((item) => {
    const context = item.context && Object.keys(item.context).length ? ` ${JSON.stringify(item.context)}` : "";
    return `${formatGmt8Time(item.created_at)}  ${item.level.padEnd(7)}  ${item.logger}  ${item.message}${context}`;
  }).join("\n"), [runtime]);
  const visibleText = view === "runtime" ? runtimeText : content[view];
  const meta = view === "runtime" ? null : rawMeta[view];

  useEffect(() => {
    const output = outputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [visibleText, view]);

  return (
    <section id="backendLogs" className="dashboard-section backend-logs-page" data-section="backendLogs">
      <div className="backend-logs-head">
        <div><h2>系统运行日志</h2><p>默认展示数据库持久化的实时任务证据；进程 stdout/stderr 仅作为历史文件辅助排查。</p></div>
        <span className={connected ? "connected" : "disconnected"}>{connected ? "实时日志已连接" : "日志连接中断"}</span>
      </div>
      <div className="backend-logs-tabs">
        <button type="button" className={view === "runtime" ? "active" : ""} onClick={() => setView("runtime")}>实时结构化日志</button>
        <button type="button" className={view === "stdout" ? "active" : ""} onClick={() => setView("stdout")}>历史 stdout</button>
        <button type="button" className={view === "stderr" ? "active" : ""} onClick={() => setView("stderr")}>历史 stderr</button>
        <small>{view === "runtime" ? `${runtime.length} 条 · 最新 ID ${latestRuntimeId.current || "—"}` : `${offsets[view].toLocaleString()} bytes`}</small>
      </div>
      {meta && <div className="backend-log-provenance">历史文件更新于 {formatGmt8Time(meta.file_updated_at)} · 当前服务 PID {meta.current_pid} · 无法证明该文件属于当前进程</div>}
      {error && <div className="alert">{error}</div>}
      <pre ref={outputRef} className="backend-logs-output">{visibleText || (view === "runtime" ? "等待系统写入结构化运行证据…" : "该历史进程文件暂无输出")}</pre>
    </section>
  );
}
