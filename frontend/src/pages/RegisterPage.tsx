import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import "./AuthPage.css";

export default function RegisterPage() {
  const { user, register } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (user) navigate("/", { replace: true });
  }, [user, navigate]);
  if (user) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!username.trim() || !password) {
      setError("请填写用户名和密码");
      return;
    }
    if (password.length < 6) {
      setError("密码至少 6 位");
      return;
    }
    setLoading(true);
    try {
      await register(username.trim(), password);
      navigate("/", { replace: true });
    } catch (err: unknown) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : "注册失败";
      setError(typeof msg === "string" ? msg : "注册失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-page-card">
        <h1>注册</h1>
        <form onSubmit={handleSubmit} className="auth-form">
          <label>
            <span>用户名</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="2～32 个字符"
              autoComplete="off"
              disabled={loading}
            />
          </label>
          <label>
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 6 位"
              autoComplete="new-password"
              disabled={loading}
            />
          </label>
          {error && <div className="auth-error">{error}</div>}
          <button type="submit" disabled={loading}>
            {loading ? "注册中…" : "注册"}
          </button>
        </form>
        <p className="auth-page-switch">
          已有账号？<Link to="/login">去登录</Link>
        </p>
      </div>
    </div>
  );
}
