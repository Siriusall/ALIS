import React, { ReactNode, Suspense, useEffect, useState } from "react";
import type { UserInfo } from "../api/auth";
import BackfillStatusBar from "./BackfillStatusBar";
import { useStockContext } from "../context/StockContext";

export const ALL_SECTION_IDS = [
  "market",
  "research",
  "stockResearch",
  "industryResearch",
  "events",
  "sentiment",
  "themes",
  "expectation",
  "valuation",
  "portfolioWorkbench",
  "stockPool",
  "thesis",
  "portfolio",
  "executionLedger",
  "positionRisk",
  "reviewWorkbench",
  "decisionLogs",
  "review",
  "validation",
  "dataCenter",
  "governance",
  "strategy",
] as const;

export const NAV_ITEMS = [
  { id: "market", label: "今日调仓建议", icon: "decision" },
  { id: "research", label: "研究中心", icon: "research" },
  { id: "portfolioWorkbench", label: "组合与执行", icon: "portfolio" },
  { id: "reviewWorkbench", label: "人工复核", icon: "review" },
  { id: "dataCenter", label: "数据中台", icon: "data" },
  { id: "governance", label: "系统治理", icon: "governance" },
] as const;

export type SectionId = (typeof ALL_SECTION_IDS)[number];

const SECTION_PARENT: Partial<Record<SectionId, SectionId>> = {
  stockResearch: "research",
  industryResearch: "research",
  events: "research",
  sentiment: "research",
  themes: "research",
  expectation: "research",
  valuation: "research",
  stockPool: "portfolioWorkbench",
  thesis: "portfolioWorkbench",
  portfolio: "portfolioWorkbench",
  executionLedger: "portfolioWorkbench",
  positionRisk: "portfolioWorkbench",
  decisionLogs: "reviewWorkbench",
  review: "reviewWorkbench",
  validation: "reviewWorkbench",
  strategy: "governance",
};

export function isSectionId(value: string | undefined): value is SectionId {
  return ALL_SECTION_IDS.some((item) => item === value);
}

function getNavSection(section: SectionId): SectionId {
  return SECTION_PARENT[section] ?? section;
}

type NavIconName = (typeof NAV_ITEMS)[number]["icon"];

function NavIcon({ name }: { name: NavIconName }) {
  const content = {
    decision: <><path d="M4 12h16" /><path d="m14 6 6 6-6 6" /><path d="M4 6h6" /></>,
    research: <><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /><path d="M8 11h6M11 8v6" /></>,
    portfolio: <><path d="M4 7h16v12H4z" /><path d="M8 7V5h8v2" /><path d="M4 12h16" /></>,
    review: <><path d="M5 4h14v16H5z" /><path d="m8 12 2 2 5-5" /></>,
    data: <><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v7c0 1.7 3.1 3 7 3s7-1.3 7-3V5" /><path d="M5 12v7c0 1.7 3.1 3 7 3s7-1.3 7-3v-7" /></>,
    governance: <><path d="M4 7h10M18 7h2M4 17h2M10 17h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></>,
  }[name];

  return (
    <svg className="dashboard-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {content}
    </svg>
  );
}

interface LayoutProps {
  sections: Partial<Record<SectionId, ReactNode>>;
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
  sections,
  header,
  currentSection,
  onSectionChange,
  user,
  onLogout,
  onLoginClick,
  onRegisterClick,
}: LayoutProps) {
  const { activeStock, setActiveStock } = useStockContext();
  const [stockDraft, setStockDraft] = useState(activeStock);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const currentChild = sections[currentSection] ?? sections.market;
  const navSection = getNavSection(currentSection);

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

  useEffect(() => {
    setStockDraft(activeStock);
  }, [activeStock]);

  function handleSectionChange(id: SectionId) {
    onSectionChange(id);
    if (window.innerWidth < 768) setSidebarOpen(false);
  }

  function openDataCenter() {
    onSectionChange("dataCenter");
    if (window.innerWidth < 768) setSidebarOpen(false);
  }

  function openStockResearch() {
    setActiveStock(stockDraft);
    onSectionChange("stockResearch");
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
          <span className="dashboard-topbar-title">ALIS</span>
        </div>
        <div className="dashboard-stock-context" role="search">
          <span>研究标的</span>
          <input
            aria-label="当前研究股票代码"
            value={stockDraft}
            onChange={(event) => setStockDraft(event.target.value.toUpperCase())}
            onKeyDown={(event) => event.key === "Enter" && openStockResearch()}
            placeholder="000001.SZ"
          />
          <button type="button" onClick={openStockResearch}>打开研究</button>
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
            {NAV_ITEMS.map(({ id, label, icon }) => (
              <button key={id} type="button" className={`dashboard-nav-item ${navSection === id ? "active" : ""}`} onClick={() => handleSectionChange(id)}>
                <NavIcon name={icon} />
                <span>{label}</span>
              </button>
            ))}
          </nav>
        </aside>
        <div className="dashboard-main">
          <header className="dashboard-header">{header}</header>
          <BackfillStatusBar onOpenDataCenter={openDataCenter} />
          <div className="dashboard-content">
            <Suspense fallback={<div className="auth-loading">模块加载中…</div>}>
              {currentChild}
            </Suspense>
          </div>
        </div>
      </div>
    </div>
  );
}
