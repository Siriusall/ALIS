import { useEffect, useRef, useState } from "react";
import { fetchBackfillStatus, type BackfillStatus } from "../api/dataCenter";
import type { SectionId } from "./Layout";
import { elapsedMinutesFromServerTime } from "../utils/time";

interface Props {
  onOpenDataCenter: (id: SectionId) => void;
  variant?: "top" | "sidebar";
}

export default function BackfillStatusBar({ onOpenDataCenter, variant = "top" }: Props) {
  const [status, setStatus] = useState<BackfillStatus | null>(null);
  const [error, setError] = useState("");
  const polling = useRef(false);

  async function refresh() {
    if (polling.current) return;
    polling.current = true;
    try {
      const next = await fetchBackfillStatus();
      setStatus(next);
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      polling.current = false;
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const failed = Boolean(error && !status) || (!status?.auto_resume && (status?.phase === "failed" || status?.phase === "interrupted" || status?.ok === false));
  const running = Boolean(status?.running);
  const step = status?.current_step;
  const noGaps = !running && status?.phase === "completed" && Number(status?.plan_summary?.steps_needed ?? 0) <= 0;
  const gap = (step?.gap ?? {}) as Record<string, unknown>;
  const checked = Number(gap.checked_symbols ?? gap.have_symbols ?? gap.checked_dates ?? gap.have ?? 0);
  const need = Number(gap.need_symbols ?? gap.need_dates ?? gap.need ?? 0);
  const title = failed
    ? status?.message || "补全状态读取失败"
    : running
      ? status?.message || step?.title || "历史补全运行中"
      : status
        ? noGaps ? "历史补全已完成" : status.message || status.plan_summary?.next_action || "历史补全计划待检查"
        : "正在读取历史补全计划";
  const progress = Math.max(0, Math.min(100, status?.progress ?? 0));
  const eta = running && status ? dynamicEta(status) : status ? "空闲" : "读取中";
  const meta = step?.id || status?.phase || (running ? "运行中" : "常驻计划");
  const quantityText = need > 0 ? `${num(checked)} / ${num(need)}` : status?.plan_summary ? `${status.plan_summary.steps_done}/${status.plan_summary.steps_needed} 步` : "等待缺口统计";
  const activityText = noGaps
    ? "无需补全"
    : status?.auto_resume
    ? `${status.auto_resume_delay_seconds ?? "稍后"} 秒后自动继续 · 第 ${status.auto_resume_attempt ?? 1}/${status.auto_resume_max_attempts ?? 5} 次`
    : status?.activity?.text || (error ? error : running ? "等待实时日志…" : "点击查看生成/继续历史补全计划");

  const className = variant === "sidebar" ? "sidebar-backfill-card" : "global-backfill-bar";

  return (
    <div className={`${className} ${failed ? `${className}-error` : ""}`}>
      <span className="global-backfill-dot" />
      <div className="global-backfill-main">
        <strong>{title}</strong>
        <span>{meta} · {quantityText} · {running ? "剩余" : "状态"} {eta}</span>
      </div>
      <div className="global-backfill-progress" aria-label="历史补全进度">
        <i style={{ width: `${progress}%` }} />
      </div>
      <span className="global-backfill-activity">{activityText}</span>
      <button type="button" onClick={() => onOpenDataCenter("dataCenter")}>{failed ? "处理" : "查看计划"}</button>
    </div>
  );
}

const num = (v: number) => new Intl.NumberFormat("zh-CN").format(v || 0);
const dynamicEta = (s: BackfillStatus) => {
  const p = Math.max(1, Math.min(99, Number(s.progress ?? 0)));
  const elapsed = elapsedMinutesFromServerTime(s.started_at);
  if (elapsed <= 0) return "计算中";
  const minutes = elapsed * (100 - p) / p;
  return minutes >= 60 ? `${(minutes / 60).toFixed(1)}h` : `${Math.max(1, Math.round(minutes))}分钟`;
};
