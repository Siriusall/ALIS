import axios from "axios";

import { api, TOKEN_KEY } from "./client";

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
  setAuthHeader(token);
  try {
    const { data } = await api.get<UserInfo | null>("/api/auth/me");
    return data ?? null;
  } catch (error) {
    // A production visitor without a session is simply logged out. During
    // localhost debugging the backend returns the synthetic debug user here.
    if (axios.isAxiosError(error) && error.response?.status === 401) return null;
    throw error;
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
