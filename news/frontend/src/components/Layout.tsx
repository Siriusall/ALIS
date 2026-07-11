import React, { ReactNode, useEffect, useState } from "react";

const NAV_ITEMS = [
  { id: "factors", label: "因子查询" },
  { id: "crawl", label: "数据爬取" },
  { id: "verify", label: "数据校验" },
] as const;

export type SectionId = (typeof NAV_ITEMS)[number]["id"];

interface LayoutProps {
  children: ReactNode;
  header: ReactNode;
  currentSection: SectionId;
  onSectionChange: (id: SectionId) => void;
}

export default function Layout({
  children,
  header,
  currentSection,
  onSectionChange,
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
          <span className="dashboard-topbar-title">因子系统</span>
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
            {NAV_ITEMS.map(({ id, label }) => (
              <button
                key={id}
                type="button"
                className={`dashboard-nav-item ${currentSection === id ? "active" : ""}`}
                onClick={() => handleSectionChange(id)}
              >
                {label}
              </button>
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
