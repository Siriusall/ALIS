import { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

interface ProtectedRouteProps {
  children: ReactNode;
}

/** 未登录时重定向到 /login，登录后显示子内容 */
export default function ProtectedRoute({ children }: ProtectedRouteProps) {
  const { user, loading, serviceError } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="auth-loading">
        <span>加载中…</span>
      </div>
    );
  }

  if (serviceError) {
    return (
      <div className="auth-loading">
        <div>
          <strong>系统服务未就绪</strong>
          <p>{serviceError}</p>
          <button type="button" className="button primary" onClick={() => window.location.reload()}>
            重新连接
          </button>
        </div>
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}
