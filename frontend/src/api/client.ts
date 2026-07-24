import axios from "axios";

export const TOKEN_KEY = "quant_token";
// PostgreSQL-backed aggregate reads can exceed 15 seconds on a cold cache,
// especially while model training is using the same local machine. Keep a
// sensible global ceiling while allowing heavier endpoints to override it.
const REQUEST_TIMEOUT_MS = 30_000;

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
  timeout: REQUEST_TIMEOUT_MS,
});

export function apiErrorMessage(error: unknown, fallback: string): string {
  if (!axios.isAxiosError(error)) return error instanceof Error ? error.message : fallback;
  const detail = error.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const item = detail as { message?: unknown; hint?: unknown };
    const message = typeof item.message === "string" ? item.message : fallback;
    const hint = typeof item.hint === "string" ? item.hint : "";
    return hint ? `${message}：${hint}` : message;
  }
  if (error.code === "ECONNABORTED") return "请求超时，请检查后端服务后重试";
  return fallback;
}

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      window.dispatchEvent(new Event("alis:session-expired"));
    }
    return Promise.reject(error);
  },
);

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const abortFromCaller = () => controller.abort();
  if (init.signal) {
    if (init.signal.aborted) controller.abort();
    else init.signal.addEventListener("abort", abortFromCaller, { once: true });
  }
  const headers = new Headers(init.headers);
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers.set("Authorization", `Bearer ${token}`);

  try {
    const response = await fetch(input, { ...init, headers, signal: controller.signal });
    if (response.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      window.dispatchEvent(new Event("alis:session-expired"));
    }
    return response;
  } finally {
    window.clearTimeout(timer);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}
