import React, { ReactNode, Suspense, useEffect, useState } from "react";
import type { UserInfo } from "../api/auth";
import BackfillStatusBar from "./BackfillStatusBar";
import LoadingState from "./LoadingState";
import {
  DATA_CENTER_SECTIONS,
  DATA_CENTER_SECTION_EVENT,
  DATA_CENTER_SECTION_STORAGE_KEY,
} from "./dataCenterSections";
import type { SectionKey as DataCenterSectionKey } from "./DataCenterTechnicalConsole";
import {
  GOVERNANCE_SECTIONS,
  GOVERNANCE_SECTION_EVENT,
  GOVERNANCE_SECTION_STORAGE_KEY,
  type GovernanceSectionKey,
} from "./governanceSections";

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
  "backendLogs",
] as const;

export const NAV_ITEMS = [
  { id: "market", label: "今日调仓建议", icon: "decision" },
  { id: "research", label: "研究中心", icon: "research" },
  { id: "portfolioWorkbench", label: "组合与执行", icon: "portfolio" },
  { id: "reviewWorkbench", label: "人工复核", icon: "review" },
  { id: "dataCenter", label: "数据中台", icon: "data" },
  { id: "governance", label: "系统治理", icon: "governance" },
  { id: "backendLogs", label: "后端日志", icon: "logs" },
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
    logs: <><path d="M5 4h14v16H5z" /><path d="M8 8h8M8 12h8M8 16h5" /></>,
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
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dataCenterOpen, setDataCenterOpen] = useState(currentSection === "dataCenter");
  const [governanceOpen, setGovernanceOpen] = useState(currentSection === "governance" || currentSection === "strategy");
  const [dataCenterSection, setDataCenterSection] = useState<DataCenterSectionKey>(() => {
    return (window.localStorage.getItem(DATA_CENTER_SECTION_STORAGE_KEY) as DataCenterSectionKey | null) || "overview";
  });
  const [governanceSection, setGovernanceSection] = useState<GovernanceSectionKey>(() => {
    return (window.localStorage.getItem(GOVERNANCE_SECTION_STORAGE_KEY) as GovernanceSectionKey | null) || "overview";
  });
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
    if (navSection === "dataCenter") setDataCenterOpen(true);
    if (navSection === "governance") setGovernanceOpen(true);
  }, [navSection]);

  useEffect(() => {
    const onDataCenterSection = (event: Event) => {
      const section = (event as CustomEvent<DataCenterSectionKey>).detail;
      if (section) setDataCenterSection(section);
    };
    window.addEventListener(DATA_CENTER_SECTION_EVENT, onDataCenterSection);
    return () => window.removeEventListener(DATA_CENTER_SECTION_EVENT, onDataCenterSection);
  }, []);

  useEffect(() => {
    const onGovernanceSection = (event: Event) => {
      const section = (event as CustomEvent<GovernanceSectionKey>).detail;
      if (section) setGovernanceSection(section);
    };
    window.addEventListener(GOVERNANCE_SECTION_EVENT, onGovernanceSection);
    return () => window.removeEventListener(GOVERNANCE_SECTION_EVENT, onGovernanceSection);
  }, []);

  function handleSectionChange(id: SectionId) {
    onSectionChange(id);
    if (id !== "dataCenter") setDataCenterOpen(false);
    if (id !== "governance" && id !== "strategy") setGovernanceOpen(false);
    if (window.innerWidth < 768) setSidebarOpen(false);
  }

  function handleGovernanceParent() {
    if (navSection !== "governance") {
      onSectionChange("governance");
      setGovernanceOpen(true);
      return;
    }
    setGovernanceOpen((open) => !open);
  }

  function handleGovernanceSection(section: GovernanceSectionKey) {
    setGovernanceSection(section);
    window.localStorage.setItem(GOVERNANCE_SECTION_STORAGE_KEY, section);
    onSectionChange("governance");
    window.dispatchEvent(new CustomEvent(GOVERNANCE_SECTION_EVENT, { detail: section }));
    if (window.innerWidth < 768) setSidebarOpen(false);
  }

  function openDataCenter() {
    onSectionChange("dataCenter");
    if (window.innerWidth < 768) setSidebarOpen(false);
  }

  function handleDataCenterParent() {
    if (navSection !== "dataCenter") {
      onSectionChange("dataCenter");
      setDataCenterOpen(true);
      return;
    }
    setDataCenterOpen((open) => !open);
  }

  function handleDataCenterSection(section: DataCenterSectionKey) {
    setDataCenterSection(section);
    window.localStorage.setItem(DATA_CENTER_SECTION_STORAGE_KEY, section);
    onSectionChange("dataCenter");
    window.dispatchEvent(new CustomEvent(DATA_CENTER_SECTION_EVENT, { detail: section }));
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
          <span className="dashboard-topbar-title">ALIS</span>
          <span className="dashboard-topbar-section">{NAV_ITEMS.find((item) => item.id === navSection)?.label || "决策系统"}</span>
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
            {NAV_ITEMS.map(({ id, label, icon }) => id === "dataCenter" ? (
              <div className={`dashboard-nav-group ${dataCenterOpen ? "open" : ""}`} key={id}>
                <button type="button" className={`dashboard-nav-item dashboard-nav-parent ${navSection === id ? "active" : ""}`} onClick={handleDataCenterParent} aria-expanded={dataCenterOpen}>
                  <NavIcon name={icon} />
                  <span>{label}</span>
                  <i className="dashboard-nav-caret" aria-hidden="true" />
                </button>
                <div className="dashboard-subnav" aria-label="数据中台子导航">
                  {DATA_CENTER_SECTIONS.map((item) => (
                    <button key={item.key} type="button" className={navSection === "dataCenter" && dataCenterSection === item.key ? "active" : ""} onClick={() => handleDataCenterSection(item.key)}>
                      <span>{item.label}</span>
                      <small>{item.hint}</small>
                    </button>
                  ))}
                </div>
              </div>
            ) : id === "governance" ? (
              <div className={`dashboard-nav-group ${governanceOpen ? "open" : ""}`} key={id}>
                <button type="button" className={`dashboard-nav-item dashboard-nav-parent ${navSection === id ? "active" : ""}`} onClick={handleGovernanceParent} aria-expanded={governanceOpen}>
                  <NavIcon name={icon} />
                  <span>{label}</span>
                  <i className="dashboard-nav-caret" aria-hidden="true" />
                </button>
                <div className="dashboard-subnav" aria-label="系统治理子导航">
                  {GOVERNANCE_SECTIONS.map((item) => (
                    <button key={item.key} type="button" className={navSection === "governance" && governanceSection === item.key ? "active" : ""} onClick={() => handleGovernanceSection(item.key)}>
                      <span>{item.label}</span>
                      <small>{item.hint}</small>
                    </button>
                  ))}
                </div>
              </div>
            ) : (
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
            <Suspense fallback={<LoadingState variant="page" title="正在打开模块" detail="正在准备页面结构和数据接口" rows={4} />}>
              {currentChild}
            </Suspense>
          </div>
        </div>
      </div>
    </div>
  );
}
