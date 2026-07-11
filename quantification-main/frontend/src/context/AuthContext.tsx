import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { UserInfo } from "../api/auth";
import {
  fetchCurrentUser,
  logout as apiLogout,
  setAuthHeader,
  setStoredToken,
} from "../api/auth";
import { login as apiLogin, register as apiRegister } from "../api/auth";

interface AuthContextValue {
  user: UserInfo | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    fetchCurrentUser()
      .then((u) => {
        if (active) setUser(u ?? null);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await apiLogin(username.trim(), password);
    setStoredToken(res.access_token);
    setAuthHeader(res.access_token);
    setUser(res.user);
  }, []);

  const register = useCallback(async (username: string, password: string) => {
    const res = await apiRegister(username.trim(), password);
    setStoredToken(res.access_token);
    setAuthHeader(res.access_token);
    setUser(res.user);
  }, []);

  const logout = useCallback(() => {
    apiLogout();
    setUser(null);
  }, []);

  const value: AuthContextValue = { user, loading, login, register, logout };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
