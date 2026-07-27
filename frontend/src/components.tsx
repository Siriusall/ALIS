import { ChevronRight, Database, RefreshCw, ShieldCheck, ShieldAlert } from "lucide-react";
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
  return <span className={`status status-${tone || toneOf(value)}`}><i />{value}</span>;
}

export function PageHeader({ eyebrow, title, summary, actions }: { eyebrow: string; title: string; summary: string; actions?: ReactNode }) {
  return <header className="page-header"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{summary}</p></div>{actions}</header>;
}

export function Section({ title, detail, children, className = "" }: { title: string; detail?: string; children: ReactNode; className?: string }) {
  return <section className={`section ${className}`}><header className="section-head"><div><h2>{title}</h2>{detail && <p>{detail}</p>}</div></header>{children}</section>;
}

export function Metric({ label, value, note, tone = "neutral" }: { label: string; value: ReactNode; note?: ReactNode; tone?: Tone }) {
  return <div className={`metric metric-${tone}`}><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</div>;
}

export function Empty({ title = "暂无可用证据", detail = "后端未返回该项数据。" }: { title?: string; detail?: string }) {
  return <div className="empty"><Database size={20} /><div><strong>{title}</strong><p>{detail}</p></div></div>;
}

export function ErrorStrip({ errors }: { errors: Record<string, string> }) {
  const items = Object.entries(errors);
  if (!items.length) return null;
  return <div className="error-strip"><ShieldAlert size={17} /><span>{items.length} 组只读数据暂时无法获取：{items.map(([key, message]) => `${key} (${message})`).join("；")}。相关区域不会用推测值填充。</span></div>;
}

export function ConsoleLoading({ page, completed, total }: { page: string; completed: number; total: number }) {
  const percent = total > 0 ? Math.round(completed / total * 100) : 0;
  return <div className="console-loading" role="status" aria-live="polite">
    <div className="loading-mark"><RefreshCw size={22} /></div>
    <p className="eyebrow">ALIS OWNER CONSOLE</p>
    <h1>正在加载{page}</h1>
    <p>正在汇总决策、账户和运行证据</p>
    <div className="loading-progress" aria-label={`加载进度 ${percent}%`}><i style={{ width: `${percent}%` }} /></div>
    <div className="loading-meta"><span>{completed}/{total} 个数据域</span><strong>{percent}%</strong></div>
    <div className="loading-policy"><ShieldCheck size={16} /><span>只读同步，不会生成计划或触发交易</span></div>
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
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", maximumFractionDigits: 0 }).format(number) : "--";
}

export function fmtPct(value: unknown, signed = true) {
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const pct = Math.abs(number) <= 1 ? number * 100 : number;
  return `${signed && pct > 0 ? "+" : ""}${pct.toFixed(2)}%`;
}

export function fmtPoints(value: unknown, signed = true) {
  if (value === null || value === undefined || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${signed && number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}
