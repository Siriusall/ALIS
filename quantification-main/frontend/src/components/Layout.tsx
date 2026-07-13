import React, { ReactNode, useEffect, useState } from "react";
import type { UserInfo } from "../api/auth";

const NAV_ITEMS = [
  { id: "market", label: "市场总览", group: "研究" },
  { id: "dataCenter", label: "数据中台", group: "研究" },
  { id: "stockResearch", label: "股票研究", group: "研究" },
  { id: "industryResearch", label: "行业研究", group: "研究" },
  { id: "events", label: "公告事件", group: "研究" },
  { id: "sentiment", label: "市场情绪", group: "动态" },
  { id: "themes", label: "主题叙事", group: "动态" },
  { id: "expectation", label: "预期变化", group: "动态" },
  { id: "valuation", label: "估值中心", group: "动态" },
  { id: "stockPool", label: "股票池", group: "决策" },
  { id: "thesis", label: "投资逻辑", group: "决策" },
  { id: "portfolio", label: "组合管理", group: "决策" },
  { id: "positionRisk", label: "持仓风险", group: "决策" },
  { id: "validation", label: "回测验证", group: "治理" },
  { id: "decisionLogs", label: "决策日志", group: "治理" },
  { id: "review", label: "复盘中心", group: "治理" },
  { id: "governance", label: "系统治理", group: "治理" },
  { id: "strategy", label: "策略工作台", group: "治理" },
] as const;

export type SectionId = (typeof NAV_ITEMS)[number]["id"];

interface LayoutProps {
  children: ReactNode;
  header: ReactNode;
  /** 当前选中的板块，由父组件控制 */
  currentSection: SectionId;
  onSectionChange: (id: SectionId) => void;
  /** 当前用户，null 表示未登录 */
  user: UserInfo | null;
  onLogout: () => void;
  onLoginClick: () => void;
  onRegisterClick: () => void;
}

/** 子组件顺序须与 NAV_ITEMS 一致。 */
export default function Layout({
  children,
  header,
  currentSection,
  onSectionChange,
  user,
  onLogout,
  onLoginClick,
  onRegisterClick,
}: LayoutProps) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const childArray = React.Children.toArray(children);
  const index = NAV_ITEMS.findIndex((item) => item.id === currentSection);
  const currentChild = index >= 0 ? childArray[index] : childArray[0];

  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth >= 768) setSidebarOpen(false);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    document.body.style.overflow = sidebarOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [sidebarOpen]);

  function handleSectionChange(id: SectionId) {
    onSectionChange(id);
    if (window.innerWidth < 768) setSidebarOpen(false);
  }

  return (
    <div className={`dashboard-root${sidebarOpen ? " dashboard-sidebar-open" : ""}`}>
      <header className="dashboard-topbar">
        <div className="dashboard-topbar-left">
          <button
            type="button"
            className="dashboard-sidebar-toggle"
            aria-label="打开导航菜单"
            aria-expanded={sidebarOpen}
            onClick={() => setSidebarOpen((open) => !open)}
          >
            <span className="dashboard-sidebar-toggle-bar" />
            <span className="dashboard-sidebar-toggle-bar" />
            <span className="dashboard-sidebar-toggle-bar" />
          </button>
          <span className="dashboard-topbar-title">ALIS 长线投资系统</span>
        </div>
        <div className="dashboard-topbar-right">
          {user ? (
            <>
              <span className="dashboard-topbar-username">{user.username}</span>
              <button type="button" className="dashboard-topbar-logout" onClick={onLogout}>
                登出
              </button>
            </>
          ) : (
            <>
              <button type="button" className="dashboard-topbar-btn" onClick={onLoginClick}>
                登录
              </button>
              <button type="button" className="dashboard-topbar-btn dashboard-topbar-btn-primary" onClick={onRegisterClick}>
                注册
              </button>
            </>
          )}
        </div>
      </header>
      <button
        type="button"
        className="dashboard-sidebar-backdrop"
        aria-label="关闭导航菜单"
        tabIndex={sidebarOpen ? 0 : -1}
        onClick={() => setSidebarOpen(false)}
      />
      <div className="dashboard-root-inner">
        <aside className="dashboard-sidebar" aria-label="主导航">
          <nav className="dashboard-nav">
            {NAV_ITEMS.map(({ id, label, group }, itemIndex) => (
              <React.Fragment key={id}>
                {(itemIndex === 0 || NAV_ITEMS[itemIndex - 1].group !== group) && <div className="dashboard-nav-group">{group}</div>}
                <button type="button" className={`dashboard-nav-item ${currentSection === id ? "active" : ""}`} onClick={() => handleSectionChange(id)}>
                  <span className="dashboard-nav-icon" aria-hidden="true">{label.slice(0, 1)}</span>{label}
                </button>
              </React.Fragment>
            ))}
          </nav>
        </aside>
        <div className="dashboard-main">
          <header className="dashboard-header">{header}</header>
          <div className="dashboard-content">{currentChild}</div>
        </div>
      </div>
    </div>
  );
}
