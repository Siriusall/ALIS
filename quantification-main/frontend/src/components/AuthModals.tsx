import { useState } from "react";
import type { UserInfo } from "../api/auth";
import { login as apiLogin, register as apiRegister } from "../api/auth";
import "./AuthModals.css";

type Mode = "login" | "register";

interface AuthModalsProps {
  onClose: () => void;
  onSuccess: (user: UserInfo, token: string) => void;
  initialMode?: Mode;
}

export default function AuthModals({ onClose, onSuccess, initialMode = "login" }: AuthModalsProps) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!username.trim() || !password) {
      setError("请填写用户名和密码");
      return;
    }
    setLoading(true);
    try {
      if (mode === "register") {
        const res = await apiRegister(username.trim(), password);
        onSuccess(res.user, res.access_token);
      } else {
        const res = await apiLogin(username.trim(), password);
        onSuccess(res.user, res.access_token);
      }
      onClose();
    } catch (err: unknown) {
      const msg = err && typeof err === "object" && "response" in err
        ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
        : "请求失败";
      setError(typeof msg === "string" ? msg : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-modal-overlay" onClick={onClose}>
      <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
        <div className="auth-modal-header">
          <h3>{mode === "login" ? "登录" : "注册"}</h3>
          <button type="button" className="auth-modal-close" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <form onSubmit={handleSubmit} className="auth-form">
          <label>
            <span>用户名</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="2～32 个字符"
              autoComplete={mode === "login" ? "username" : "off"}
              disabled={loading}
            />
          </label>
          <label>
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === "register" ? "至少 6 位" : "密码"}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              disabled={loading}
            />
          </label>
          {error && <div className="auth-error">{error}</div>}
          <div className="auth-actions">
            <button type="submit" disabled={loading}>
              {loading ? "提交中…" : mode === "login" ? "登录" : "注册"}
            </button>
            <button
              type="button"
              className="auth-switch"
              onClick={() => {
                setMode(mode === "login" ? "register" : "login");
                setError("");
              }}
            >
              {mode === "login" ? "没有账号？去注册" : "已有账号？去登录"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
