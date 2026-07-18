const SHANGHAI_TZ = "Asia/Shanghai";

function parseServerTime(value?: string | null): Date | null {
  if (!value) return null;
  const text = String(value).trim();
  if (!text) return null;
  // Backend stores naive timestamps in the system timezone (Asia/Shanghai).
  // Treating them as UTC shifts them eight hours into the future and makes
  // elapsed/ETA calculations stay at zero.
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text}+08:00`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatGmt8Time(value?: string | null, fallback = "—"): string {
  const date = parseServerTime(value);
  if (!date) return fallback;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date).replace(/\//g, "-");
}

export function formatGmt8Date(value?: string | null, fallback = "暂无"): string {
  if (!value) return fallback;
  const text = String(value).trim();
  if (/^\d{8}$/.test(text)) return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
  const date = parseServerTime(text);
  if (!date) return fallback;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: SHANGHAI_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date).replace(/\//g, "-");
}

export function elapsedMinutesFromServerTime(value?: string | null): number {
  const date = parseServerTime(value);
  if (!date) return 0;
  return Math.max(0, (Date.now() - date.getTime()) / 60000);
}
