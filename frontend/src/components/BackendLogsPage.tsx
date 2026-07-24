import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type Source = "stdout" | "stderr";
type RawLogChunk = {
  source: Source;
  text: string;
  offset: number;
  next_offset: number;
  size: number;
  eof: boolean;
};

const EMPTY = { stdout: "", stderr: "" };
const ZERO = { stdout: 0, stderr: 0 };

export default function BackendLogsPage() {
  const [source, setSource] = useState<Source>("stdout");
  const [content, setContent] = useState<Record<Source, string>>(EMPTY);
  const [offsets, setOffsets] = useState<Record<Source, number>>(ZERO);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState("");
  const offsetsRef = useRef<Record<Source, number>>(ZERO);
  const outputRef = useRef<HTMLPreElement | null>(null);

  const readNewContent = useCallback(async (target: Source, fromStart = false) => {
    let offset = fromStart ? 0 : offsetsRef.current[target];
    const requestedOffset = offset;
    let appended = "";
    let resetDetected = false;
    let chunk: RawLogChunk;
    do {
      const response = await api.get<RawLogChunk>("/api/logs/backend", {
        params: { source: target, offset, limit: 4 * 1024 * 1024 },
        timeout: 30_000,
      });
      chunk = response.data;
      if (chunk.offset !== offset) resetDetected = true;
      appended += chunk.text;
      offset = chunk.next_offset;
    } while (!chunk.eof);
    offsetsRef.current = { ...offsetsRef.current, [target]: offset };
    setOffsets(offsetsRef.current);
    if (fromStart || resetDetected || (requestedOffset > 0 && offset < requestedOffset)) setContent((current) => ({ ...current, [target]: appended }));
    else if (appended) setContent((current) => ({ ...current, [target]: current[target] + appended }));
  }, []);

  useEffect(() => {
    let stopped = false;
    async function initialize() {
      try {
        await Promise.all([readNewContent("stdout", true), readNewContent("stderr", true)]);
        if (!stopped) {
          setConnected(true);
          setError("");
        }
      } catch (reason) {
        if (!stopped) setError(reason instanceof Error ? reason.message : "后端日志读取失败");
      }
    }
    void initialize();
    const timer = window.setInterval(async () => {
      try {
        await Promise.all([readNewContent("stdout"), readNewContent("stderr")]);
        if (!stopped) {
          setConnected(true);
          setError("");
        }
      } catch (reason) {
        if (!stopped) {
          setConnected(false);
          setError(reason instanceof Error ? reason.message : "后端日志读取失败");
        }
      }
    }, 2000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [readNewContent]);

  useEffect(() => {
    const output = outputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [content, source]);

  return (
    <section id="backendLogs" className="dashboard-section backend-logs-page" data-section="backendLogs">
      <div className="backend-logs-head">
        <div>
          <h2>后端原始日志</h2>
          <p>直接展示后端进程输出，不做解析、过滤或改写。</p>
        </div>
        <span className={connected ? "connected" : "disconnected"}>{connected ? "实时连接" : "连接中断"}</span>
      </div>
      <div className="backend-logs-tabs">
        <button type="button" className={source === "stdout" ? "active" : ""} onClick={() => setSource("stdout")}>标准输出 stdout</button>
        <button type="button" className={source === "stderr" ? "active" : ""} onClick={() => setSource("stderr")}>错误输出 stderr</button>
        <small>{offsets[source].toLocaleString()} bytes</small>
      </div>
      {error && <div className="alert">{error}</div>}
      <pre ref={outputRef} className="backend-logs-output">{content[source] || "等待后端输出…"}</pre>
    </section>
  );
}
