import { Activity, BriefcaseBusiness, ChartNoAxesCombined, Database, FileSearch, FlaskConical, Gauge, LayoutDashboard, LogOut, Menu, RefreshCw, Settings, X } from "lucide-react";
import { useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthPage, RequireAuth, useAuth } from "./auth";
import { ConsoleLoading, ErrorStrip } from "./components";
import { AdminOverview, DataAssets, EvidenceChain, Evolution, FactorGuidance, ModelGovernance, Operations, Overview, Portfolio, Research, ResearchEfficiency, ResearchWaves, TodayDecision } from "./pages";
import { preloadConsoleRoutes, useConsoleData } from "./useConsoleData";

const ownerNav = [
  ["/overview", "总览", LayoutDashboard], ["/decision", "今日决策", Activity],
  ["/portfolio", "账户与组合", BriefcaseBusiness], ["/evolution", "研究与进化", ChartNoAxesCombined],
  ["/research", "研究中心", FlaskConical],
] as const;
const adminNav = [
  ["/admin", "运行总览", LayoutDashboard], ["/admin/operations", "任务与链路", Activity],
  ["/admin/data", "数据与日更", Database], ["/admin/models", "模型治理", Gauge],
  ["/admin/evidence", "证据与审计", FileSearch],
] as const;
const pageNames: Record<string, string> = {
  "/overview": "投资系统总览", "/decision": "今日决策", "/portfolio": "账户与组合",
  "/evolution": "研究与进化", "/research": "研究中心", "/admin": "管理后台",
  "/evolution/factors": "因子关系与指导", "/evolution/metrics": "研究效率",
  "/evolution/waves": "批次与专家治理",
  "/admin/operations": "任务与链路", "/admin/models": "模型治理",
  "/admin/data": "数据与日更", "/admin/evidence": "证据与审计",
};

function ConsoleApp() {
  const location = useLocation();
  const { user, logout } = useAuth();
  const { data, errors, loading, refreshing, progress, refresh } = useConsoleData(location.pathname);
  const [open, setOpen] = useState(false);
  const admin = location.pathname.startsWith("/admin");
  const nav = admin ? adminNav : ownerNav;
  return <div className="app-shell">
    <aside className={open ? "sidebar open" : "sidebar"}>
      <div className="brand"><div>AL</div><span><strong>ALIS</strong><small>{admin ? "系统管理后台" : "模拟投资控制台"}</small></span><button onClick={() => setOpen(false)} aria-label="关闭导航"><X size={19}/></button></div>
      <nav>{nav.map(([path,label,Icon]) => <div className="nav-entry" key={path}><NavLink to={path} end={path === "/admin" || path === "/evolution"} onPointerEnter={()=>void preloadConsoleRoutes([path])} onFocus={()=>void preloadConsoleRoutes([path])} onClick={() => setOpen(false)}><Icon size={18}/><span>{label}</span></NavLink>{!admin&&path==="/evolution"&&<div className="nav-children"><NavLink to="/evolution" end onPointerEnter={()=>void preloadConsoleRoutes(["/evolution"])} onFocus={()=>void preloadConsoleRoutes(["/evolution"])} onClick={()=>setOpen(false)}>当前页面</NavLink><NavLink to="/evolution/factors" onPointerEnter={()=>void preloadConsoleRoutes(["/evolution/factors"])} onFocus={()=>void preloadConsoleRoutes(["/evolution/factors"])} onClick={()=>setOpen(false)}>因子关系与指导</NavLink><NavLink to="/evolution/metrics" onPointerEnter={()=>void preloadConsoleRoutes(["/evolution/metrics"])} onFocus={()=>void preloadConsoleRoutes(["/evolution/metrics"])} onClick={()=>setOpen(false)}>研究效率</NavLink><NavLink to="/evolution/waves" onPointerEnter={()=>void preloadConsoleRoutes(["/evolution/waves"])} onFocus={()=>void preloadConsoleRoutes(["/evolution/waves"])} onClick={()=>setOpen(false)}>批次与专家治理</NavLink></div>}</div>)}</nav>
      <NavLink className="console-switch" to={admin ? "/overview" : "/admin"} onPointerEnter={()=>void preloadConsoleRoutes([admin?"/overview":"/admin"])} onFocus={()=>void preloadConsoleRoutes([admin?"/overview":"/admin"])}>{admin ? <LayoutDashboard size={17}/> : <Settings size={17}/>}<span>{admin ? "返回投资前台" : "进入管理后台"}</span></NavLink>
      <div className="sidebar-foot"><span className={loading || refreshing ? "pulse" : ""}/><div><strong>{loading ? "正在载入" : refreshing ? "后台刷新" : "只读控制台"}</strong><small>写操作由自治系统执行</small></div></div>
    </aside>
    <div className="app-body">
      <header className="topbar"><button className="mobile-menu" onClick={() => setOpen(true)} aria-label="打开导航"><Menu size={20}/></button><div><span>{admin ? "系统管理" : user?.username || "模拟账户"}</span><strong>{admin ? "全链路只读监控" : "¥200,000 基准权益"}</strong></div><div className="topbar-actions"><button className="icon-button" onClick={refresh} disabled={loading || refreshing} title="刷新只读数据" aria-label="刷新数据"><RefreshCw size={18} className={loading || refreshing ? "spin" : ""}/></button><button className="icon-button" onClick={logout} title="退出登录" aria-label="退出登录"><LogOut size={17}/></button></div></header>
      <main>{loading ? <ConsoleLoading page={pageNames[location.pathname] || "控制台"} completed={progress.completed} total={progress.total}/> : <><ErrorStrip errors={errors}/><Routes>
        <Route path="/overview" element={<Overview data={data}/>}/><Route path="/decision" element={<TodayDecision data={data}/>}/><Route path="/portfolio" element={<Portfolio data={data}/>}/><Route path="/evolution" element={<Evolution data={data}/>}/><Route path="/evolution/factors" element={<FactorGuidance data={data}/>}/><Route path="/evolution/metrics" element={<ResearchEfficiency data={data}/>}/><Route path="/evolution/waves" element={<ResearchWaves data={data}/>}/><Route path="/research" element={<Research data={data}/>}/><Route path="/factors" element={<Navigate to="/evolution" replace/>}/>
        <Route path="/admin" element={<AdminOverview data={data}/>}/><Route path="/admin/operations" element={<Operations data={data}/>}/><Route path="/admin/data" element={<DataAssets data={data}/>}/><Route path="/admin/models" element={<ModelGovernance data={data}/>}/><Route path="/admin/evidence" element={<EvidenceChain data={data}/>}/>
        <Route path="/operations" element={<Navigate to="/admin/operations" replace/>}/><Route path="/data" element={<Navigate to="/admin/data" replace/>}/><Route path="/models" element={<Navigate to="/admin/models" replace/>}/><Route path="/evidence" element={<Navigate to="/admin/evidence" replace/>}/><Route path="*" element={<Navigate to="/overview" replace/>}/>
      </Routes></>}</main>
    </div>{open && <button className="scrim" aria-label="关闭导航" onClick={() => setOpen(false)}/>}
  </div>;
}

export default function App(){return <Routes><Route path="/login" element={<AuthPage mode="login"/>}/><Route path="/register" element={<AuthPage mode="register"/>}/><Route path="*" element={<RequireAuth><ConsoleApp/></RequireAuth>}/></Routes>}
