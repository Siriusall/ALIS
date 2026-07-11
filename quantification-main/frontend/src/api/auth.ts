import axios from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "",
});

export interface UserInfo {
  id: number;
  username: string;
  created_at?: string;
}

export interface AuthResult {
  access_token: string;
  token_type: string;
  user: UserInfo;
}

const TOKEN_KEY = "quant_token";

export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setStoredToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

/** 请求时在 Header 中附带 token */
export function setAuthHeader(token: string | null): void {
  if (token) api.defaults.headers.common["Authorization"] = `Bearer ${token}`;
  else delete api.defaults.headers.common["Authorization"];
}

export async function register(username: string, password: string): Promise<AuthResult> {
  const { data } = await api.post<AuthResult>("/api/auth/register", { username, password });
  return data;
}

export async function login(username: string, password: string): Promise<AuthResult> {
  const { data } = await api.post<AuthResult>("/api/auth/login", { username, password });
  return data;
}

export async function fetchCurrentUser(): Promise<UserInfo | null> {
  const token = getStoredToken();
  if (!token) return null;
  setAuthHeader(token);
  try {
    const { data } = await api.get<UserInfo | null>("/api/auth/me");
    return data ?? null;
  } catch {
    setStoredToken(null);
    setAuthHeader(null);
    return null;
  }
}

export async function logout(): Promise<void> {
  setStoredToken(null);
  setAuthHeader(null);
  try {
    await api.post("/api/auth/logout");
  } catch {
    // 忽略
  }
}
