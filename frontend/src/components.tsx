import { ChevronRight, Database, RefreshCw, ShieldAlert } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

export type Tone = "good" | "watch" | "danger" | "info" | "neutral";

export function toneOf(value: unknown): Tone {
  const text = String(value ?? "").toLowerCase();
  if (/failed|blocked|error|critical|owner_required|阻断|失败|需授权/.test(text)) return "danger";
  if (/waiting|partial|degraded|observ|pending|insufficient|隔离|等待|观察/.test(text)) return "watch";
  if (/ready|passed|healthy|completed|normal|正常|通过|完整/.test(text)) return "good";
  if (/running|active|info|执行中/.test(text)) return "info";
  return "neutral";
}

export function Status({ children, tone }: { children: ReactNode; tone?: Tone }) {
  const objectValue = children as unknown as Record<string, unknown>;
  const value = children && typeof children === "object" && !Array.isArray(children) && !("$$typeof" in objectValue)
    ? String(objectValue.label ?? objectValue.state ?? objectValue.status ?? "证据已记录")
    : children;
  const labels: Record<string, string> = { ok: "正常", ready: "就绪", healthy: "健康", running: "运行中", waiting: "等待", observing: "观察中", degraded: "降级", blocked: "阻断", failed: "失败", completed: "完成", passed: "通过", owner: "本机持有", follower: "跟随实例", disabled: "已委托" };
  const label = typeof value === "string" ? labels[value.toLowerCase()] || value : value;
  return <span className={`status status-${tone || toneOf(value)}`}><i />{label}</span>;
}

export function PageHeader({ eyebrow, title, summary, actions }: { eyebrow?: string; title: string; summary: string; actions?: ReactNode }) {
  return <header className="page-header"><div>{eyebrow && <span className="page-eyebrow">{eyebrow}</span>}<h1>{title}</h1><p>{summary}</p></div>{actions}</header>;
}

export function Section({ title, detail, children, className = "" }: { title: string; detail?: string; children: ReactNode; className?: string }) {
  return <section className={`section ${className}`}><header className="section-head"><div><h2>{title}</h2>{detail && <p>{detail}</p>}</div></header>{children}</section>;
}

export function Metric({ label, value, note, tone = "neutral", icon: Icon }: { label: string; value: ReactNode; note?: ReactNode; tone?: Tone; icon?: LucideIcon }) {
  return <div className={`metric metric-${tone}`}>
    <div className="metric-top"><span>{label}</span>{Icon && <i className="metric-icon"><Icon size={17} strokeWidth={1.8}/></i>}</div>
    <strong>{value}</strong>
    {note && <small>{note}</small>}
  </div>;
}

export function Empty({ title = "暂无记录", detail }: { title?: string; detail?: string }) {
  return <div className="empty"><Database size={20} /><div><strong>{title}</strong>{detail && <p>{detail}</p>}</div></div>;
}

export function ErrorStrip({ errors }: { errors: Record<string, string> }) {
  const items = Object.entries(errors);
  if (!items.length) return null;
  return <div className="error-strip"><ShieldAlert size={17} /><span>{items.length} 组数据暂不可用：{items.map(([key, message]) => `${key}（${message}）`).join("；")}</span></div>;
}

export function ConsoleLoading({ page, completed, total, message }: { page: string; completed: number; total: number; message?: string }) {
  const percent = total > 0 ? Math.round(completed / total * 100) : 0;
  return <div className="console-loading" role="status" aria-live="polite">
    <div className="loading-mark"><RefreshCw size={22} /></div>
    <h1>正在加载{page}</h1>
    <p>{message || "正在同步数据"}</p>
    <div className="loading-progress" aria-label={`加载进度 ${percent}%`}><i style={{ width: `${percent}%` }} /></div>
    <div className="loading-meta"><span>{completed}/{total} 个数据项</span><strong>{percent}%</strong></div>
    <div className="loading-skeleton" aria-hidden="true"><i/><i/><i/><i/></div>
  </div>;
}

export function Progress({ label, current, target, suffix = "" }: { label: string; current: number; target: number; suffix?: string }) {
  const percent = target > 0 ? Math.min(100, Math.max(0, current / target * 100)) : 0;
  return <div className="progress-row"><div><span>{label}</span><strong>{current}/{target}{suffix}</strong></div><div className="progress-track"><i style={{ width: `${percent}%` }} /></div><small>{current >= target ? "已达到样本门" : `还差 ${Math.max(0, target - current)}${suffix}`}</small></div>;
}

export function EvidenceDetails({ title = "查看证据", children }: { title?: string; children: ReactNode }) {
  return <details className="evidence"><summary>{title}<ChevronRight size={16} /></summary><div>{children}</div></details>;
}

export function fmtMoney(value: unknown) {
  if (value === null || value === undefined || value === "") return "未记录金额";
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", maximumFractionDigits: 0 }).format(number) : "金额不可用";
}

export function fmtPct(value: unknown, signed = true) {
  if (value === null || value === undefined || value === "") return "未记录比例";
  const number = Number(value);
  if (!Number.isFinite(number)) return "比例不可用";
  const pct = Math.abs(number) <= 1 ? number * 100 : number;
  return `${signed && pct > 0 ? "+" : ""}${pct.toFixed(2)}%`;
}

export function fmtPoints(value: unknown, signed = true) {
  if (value === null || value === undefined || value === "") return "未记录百分点";
  const number = Number(value);
  if (!Number.isFinite(number)) return "百分点不可用";
  return `${signed && number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}
