import { Activity, BriefcaseBusiness, ChartNoAxesCombined, FlaskConical, Gauge, LayoutDashboard, LogOut, Menu, RefreshCw, X } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthPage, RequireAuth, useAuth } from "./auth";
import { ConsoleLoading, ErrorStrip } from "./components";
import { DataAssets, EvidenceChain, Evolution, FactorGuidance, ModelGovernance, Operations, Overview, Portfolio, Research, ResearchEfficiency, ResearchWaves, SystemObservatory, SystemVersions, TodayDecision } from "./pages";
import { preloadConsoleRoutes, useConsoleData } from "./useConsoleData";

type NavItem = {
  key: string;
  path: string;
  label: string;
  Icon: LucideIcon;
  children?: ReadonlyArray<readonly [string, string]>;
};

const consoleNav: NavItem[] = [
  { key: "overview", path: "/overview", label: "总览", Icon: LayoutDashboard },
  { key: "decision", path: "/decision", label: "今日决策", Icon: Activity },
  { key: "portfolio", path: "/portfolio", label: "账户与组合", Icon: BriefcaseBusiness },
  { key: "research", path: "/research", label: "公司与行业研究", Icon: FlaskConical },
  { key: "models", path: "/evolution", label: "模型与因子", Icon: ChartNoAxesCombined, children: [
    ["/evolution", "策略进化"], ["/models", "模型治理"],
    ["/evolution/factors", "因子关系"], ["/evolution/metrics", "进化效率"], ["/evolution/waves", "实验批次"],
  ] },
  { key: "system", path: "/system", label: "系统", Icon: Gauge, children: [
    ["/system", "运行概览"], ["/system/versions", "版本与发布"], ["/operations", "任务与链路"], ["/data", "数据与日更"], ["/evidence", "证据与审计"],
  ] },
];
const pageNames: Record<string, string> = {
  "/overview": "投资系统总览", "/decision": "今日决策", "/portfolio": "账户与组合",
  "/system": "系统运行",
  "/system/versions": "版本与发布",
  "/evolution": "策略进化", "/research": "公司与行业研究",
  "/evolution/factors": "因子关系与指导", "/evolution/metrics": "进化效率",
  "/evolution/waves": "实验批次与专家",
  "/operations": "任务与链路", "/models": "模型治理",
  "/data": "数据与日更", "/evidence": "证据与审计",
};

function ConsoleApp() {
  const location = useLocation();
  const { user, logout } = useAuth();
  const { data, errors, loading, loadingMessage, refreshing, progress, refresh } = useConsoleData(location.pathname);
  const [open, setOpen] = useState(false);
  const [expandedNav, setExpandedNav] = useState<string | null>(null);
  const currentPage = pageNames[location.pathname] || "控制台";
  return <div className="app-shell">
    <aside className={open ? "sidebar open" : "sidebar"}>
      <div className="brand"><div><ChartNoAxesCombined size={21} strokeWidth={2.2}/></div><span><strong>ALIS</strong><small>量化研究与模拟交易</small></span><button onClick={() => setOpen(false)} aria-label="关闭导航"><X size={19}/></button></div>
      <nav>{consoleNav.map((item) => {
        const groupActive = Boolean(item.children?.some(([path]) => location.pathname === path));
        const childrenVisible = Boolean(item.children && (groupActive || expandedNav === item.key));
        return <div className="nav-entry" key={item.key}><NavLink to={item.path} end className={({ isActive }) => isActive || groupActive ? "active" : undefined} onPointerEnter={()=>void preloadConsoleRoutes([item.path])} onFocus={()=>void preloadConsoleRoutes([item.path])} onClick={() => { setOpen(false); if (item.children) setExpandedNav(item.key); }}><item.Icon size={18}/><span>{item.label}</span></NavLink>{childrenVisible && <div className="nav-children">{item.children!.map(([path, label]) => <NavLink key={path} to={path} end onPointerEnter={()=>void preloadConsoleRoutes([path])} onFocus={()=>void preloadConsoleRoutes([path])} onClick={()=>setOpen(false)}>{label}</NavLink>)}</div>}</div>;
      })}</nav>
      <div className="sidebar-foot"><span className={loading || refreshing ? "pulse" : ""}/><div><strong>{loading ? "正在载入" : refreshing ? "正在刷新" : "数据已同步"}</strong><small>更新于当前会话</small></div></div>
    </aside>
    <div className="app-body">
      <header className="topbar"><button className="mobile-menu" onClick={() => setOpen(true)} aria-label="打开导航"><Menu size={20}/></button><div className="topbar-context"><span>{user?.username || "模拟账户"}</span><strong>{currentPage}</strong></div><div className="topbar-actions"><button className="icon-button" onClick={refresh} disabled={loading || refreshing} title="刷新数据" aria-label="刷新数据"><RefreshCw size={18} className={loading || refreshing ? "spin" : ""}/></button><button className="icon-button" onClick={logout} title="退出登录" aria-label="退出登录"><LogOut size={17}/></button></div></header>
      <main>{loading ? <ConsoleLoading page={currentPage} completed={progress.completed} total={progress.total} message={loadingMessage}/> : <><ErrorStrip errors={errors}/><Routes>
        <Route path="/overview" element={<Overview data={data}/>}/><Route path="/system" element={<SystemObservatory data={data}/>}/><Route path="/system/versions" element={<SystemVersions data={data}/>}/><Route path="/decision" element={<TodayDecision data={data}/>}/><Route path="/portfolio" element={<Portfolio data={data}/>}/><Route path="/evolution" element={<Evolution data={data}/>}/><Route path="/evolution/factors" element={<FactorGuidance data={data}/>}/><Route path="/evolution/metrics" element={<ResearchEfficiency data={data}/>}/><Route path="/evolution/waves" element={<ResearchWaves data={data}/>}/><Route path="/research" element={<Research data={data}/>}/><Route path="/factors" element={<Navigate to="/evolution" replace/>}/>
        <Route path="/operations" element={<Operations data={data}/>}/><Route path="/data" element={<DataAssets data={data}/>}/><Route path="/models" element={<ModelGovernance data={data}/>}/><Route path="/evidence" element={<EvidenceChain data={data}/>}/>
        <Route path="/admin" element={<Navigate to="/overview" replace/>}/><Route path="/admin/operations" element={<Navigate to="/operations" replace/>}/><Route path="/admin/data" element={<Navigate to="/data" replace/>}/><Route path="/admin/models" element={<Navigate to="/models" replace/>}/><Route path="/admin/evidence" element={<Navigate to="/evidence" replace/>}/><Route path="*" element={<Navigate to="/overview" replace/>}/>
      </Routes></>}</main>
    </div>{open && <button className="scrim" aria-label="关闭导航" onClick={() => setOpen(false)}/>}
  </div>;
}

export default function App(){return <Routes><Route path="/login" element={<AuthPage mode="login"/>}/><Route path="/register" element={<AuthPage mode="register"/>}/><Route path="*" element={<RequireAuth><ConsoleApp/></RequireAuth>}/></Routes>}
