import { Activity, BriefcaseBusiness, ChartNoAxesCombined, Database, FileSearch, FlaskConical, Gauge, LayoutDashboard, LogOut, Menu, RefreshCw, X } from "lucide-react";
import { useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ConsoleLoading, ErrorStrip } from "./components";
import { DataAssets, EvidenceChain, Evolution, ModelGovernance, Operations, Overview, Portfolio, Research, TodayDecision } from "./pages";
import { useConsoleData } from "./useConsoleData";
import { AuthPage, RequireAuth, useAuth } from "./auth";

const nav = [
  ["/overview", "总览", LayoutDashboard], ["/decision", "今日决策", Activity], ["/portfolio", "账户与组合", BriefcaseBusiness],
  ["/evolution", "研究与进化", ChartNoAxesCombined], ["/research", "研究中心", FlaskConical],
  ["/models", "模型治理", Gauge], ["/data", "数据资产", Database],
  ["/evidence", "证据链", FileSearch], ["/operations", "系统运行", Activity],
] as const;

const pageNames: Record<string, string> = {
  "/overview": "投资系统总览", "/decision": "今日决策", "/portfolio": "账户与组合",
  "/evolution": "研究与进化", "/research": "研究中心", "/operations": "系统运行",
  "/factors": "研究与进化", "/models": "模型治理", "/data": "数据资产", "/evidence": "证据链",
};

function ConsoleApp() {
  const location = useLocation();
  const { user, logout } = useAuth();
  const { data, errors, loading, progress, refresh } = useConsoleData(location.pathname); const [open,setOpen]=useState(false);
  return <div className="app-shell">
    <aside className={open?"sidebar open":"sidebar"}><div className="brand"><div>AL</div><span><strong>ALIS</strong><small>模拟投资控制台</small></span><button onClick={()=>setOpen(false)} aria-label="关闭导航"><X size={19}/></button></div><nav>{nav.map(([path,label,Icon])=><NavLink key={path} to={path} onClick={()=>setOpen(false)}><Icon size={18}/><span>{label}</span></NavLink>)}</nav><div className="sidebar-foot"><span className={loading?"pulse":""}/><div><strong>{loading?"正在同步":"只读控制台"}</strong><small>写操作由自治系统执行</small></div></div></aside>
    <div className="app-body"><header className="topbar"><button className="mobile-menu" onClick={()=>setOpen(true)} aria-label="打开导航"><Menu size={20}/></button><div><span>{user?.username || "模拟账户"}</span><strong>¥200,000 基准权益</strong></div><div className="topbar-actions"><button className="icon-button" onClick={refresh} disabled={loading} title="刷新只读数据" aria-label="刷新数据"><RefreshCw size={18} className={loading?"spin":""}/></button><button className="icon-button" onClick={logout} title="退出登录" aria-label="退出登录"><LogOut size={17}/></button></div></header><main>{loading ? <ConsoleLoading page={pageNames[location.pathname] || "控制台"} completed={progress.completed} total={progress.total} /> : <><ErrorStrip errors={errors}/><Routes><Route path="/overview" element={<Overview data={data}/>}/><Route path="/decision" element={<TodayDecision data={data}/>}/><Route path="/portfolio" element={<Portfolio data={data}/>}/><Route path="/evolution" element={<Evolution data={data}/>}/><Route path="/research" element={<Research data={data}/>}/><Route path="/factors" element={<Navigate to="/evolution" replace/>}/><Route path="/models" element={<ModelGovernance data={data}/>}/><Route path="/data" element={<DataAssets data={data}/>}/><Route path="/evidence" element={<EvidenceChain data={data}/>}/><Route path="/operations" element={<Operations data={data}/>}/><Route path="*" element={<Navigate to="/overview" replace/>}/></Routes></>}</main></div>{open&&<button className="scrim" aria-label="关闭导航" onClick={()=>setOpen(false)}/>}</div>;
}

export default function App(){return <Routes><Route path="/login" element={<AuthPage mode="login"/>}/><Route path="/register" element={<AuthPage mode="register"/>}/><Route path="*" element={<RequireAuth><ConsoleApp/></RequireAuth>}/></Routes>}
