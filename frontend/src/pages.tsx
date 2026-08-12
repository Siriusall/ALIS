import { Activity, ArrowUpRight, Banknote, BookOpen, Boxes, BrainCircuit, Building2, ChartNoAxesCombined, ChartPie, ChevronLeft, ChevronRight, CirclePlus, Clock3, Database, FileSearch, FlaskConical, Gauge, GitBranch, Layers3, ListTree, LogOut, Pause, Radio, ScanSearch, Search, ServerCog, Shield, ShieldCheck, Sigma, Sparkles, Telescope, TrendingDown, TrendingUp, TriangleAlert, WalletCards, Workflow } from "lucide-react";
import { endpoints, firstArray, number, pick, readApi, Json, unwrapOwner } from "./api";
import { useEffect, useRef, useState } from "react";
import { Empty, EvidenceDetails, fmtMoney, fmtPct, fmtPoints, Metric, PageHeader, Progress, Section, Status, toneOf } from "./components";
import type { ConsoleData } from "./useConsoleData";

const actions = ["buy_candidate", "add_position", "reduce_position", "sell_exit", "research_only", "hold", "defensive_switch"];
const actionNames: Record<string, string> = { buy_candidate: "买入候选", add_position: "加仓", reduce_position: "减仓", sell_exit: "退出", research_only:"研究观察", hold: "保持", defensive_switch: "防御切换" };
const actionIcons: Record<string, typeof Activity> = { buy_candidate: CirclePlus, add_position: TrendingUp, reduce_position: TrendingDown, sell_exit: LogOut, research_only: Telescope, hold: Pause, defensive_switch: Shield };
function canonicalAction(value: unknown){const raw=String(value||"");return ({buy:"buy_candidate",add:"add_position",reduce:"reduce_position",sell:"sell_exit",observe:"research_only"} as Record<string,string>)[raw]||raw;}
function displayedAction(row:Json){if(Number(row.planned_quantity||0)<=0)return String(row.controller_intent||"")==="research_only"?"research_only":"hold";return canonicalAction(row.action||row.action_type);}

function ownerState(owner?: Json, tradingReady?: Json) {
  const contract = pick(owner, [
    "questions.system_changes.champion_execution_gate",
    "champion_execution_gate",
    "execution_gate",
  ], null);
  const status = String(pick(contract, ["status"], pick(owner, ["execution_permission", "execution_mode"], "")));
  const mode = String(pick(contract, ["mode"], pick(owner, ["execution_mode"], "")));
  const reason = String(pick(contract, ["reason"], ""));
  const isolated = status === "quarantined" || mode === "research_only_sell_reduce";
  const oosWarning = !isolated && Boolean(
    pick(contract, ["formal_oos_warning_active"], false)
      || reason === "active_champion_failed_formal_oos_warning_only",
  );
  const defensiveContinuity = Boolean(pick(contract, ["defensive_continuity.enabled"], false));
  const defensiveMaximum = number(contract, ["defensive_continuity.maximum_total_weight"], 0);
  const readinessStatus = String(pick(tradingReady, ["status"], ""));
  const readinessKnown = new Set(["ready", "waiting", "blocked", "running"]).has(readinessStatus);
  const tradingBlocked = readinessKnown && readinessStatus === "blocked";
  const label = isolated
    ? defensiveContinuity
      ? `研究隔离 · 防御 ETF 上限 ${fmtPct(defensiveMaximum)}`
      : "研究隔离 · 仅允许减仓和卖出"
    : readinessStatus === "blocked"
      ? "今日执行已阻断"
      : readinessStatus === "waiting"
        ? "等待执行窗口"
        : readinessStatus === "running"
          ? "执行进行中"
          : readinessStatus === "ready"
            ? oosWarning ? "允许按冻结计划执行 · OOS 风险告警" : "允许按冻结计划执行"
            : readinessKnown ? "执行状态待确认" : "执行状态证据未返回";
  const statusLabel = isolated ? "研究隔离" : readinessStatus === "blocked" ? "今日阻断" : readinessStatus === "ready" ? "允许执行" : readinessStatus === "waiting" ? "等待执行窗口" : readinessStatus === "running" ? "执行中" : "证据缺失";
  return { status, mode, reason, isolated, oosWarning, defensiveContinuity, defensiveMaximum, readinessStatus, tradingBlocked, label, statusLabel };
}

function acceptance(owner?: Json) {
  const raw = pick(owner, ["acceptance_progress", "acceptance", "progress"], {});
  const items = firstArray(raw, ["items", "stages"]);
  if (items.length) return items;
  return [
    { name: "候选因子评价", current: number(raw, ["factor.current", "factor_evaluations.current"]), target: number(raw, ["factor.target", "factor_evaluations.target"], 20) },
    { name: "成本候选", current: number(raw, ["cost_candidate.current", "trades.total"]), target: number(raw, ["cost_candidate.target"], 20) },
    { name: "成本自动激活", current: number(raw, ["cost_activation.current", "trades.total"]), target: number(raw, ["cost_activation.target"], 50) },
    { name: "跨日自治", current: number(raw, ["autonomy.current", "autonomous_days"]), target: number(raw, ["autonomy.target"], 20) },
  ];
}

function metricTone(value: unknown) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "neutral" as const;
  return Number(value) >= 0 ? "good" as const : "danger" as const;
}

function researchStatus(row: Json) {
  const value = pick(row, ["status.status", "status.lifecycle", "lifecycle.status", "lifecycle.lifecycle", "state", "stage", "status", "lifecycle"], "已记录");
  return typeof value === "string" || typeof value === "number" ? String(value) : "已记录";
}

function scalarLabel(value: unknown, fallback = "证据缺失") {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

const environmentTypeNames:Record<string,string>={
  historical_rolling:"历史滚动窗口",
  extreme_historical_replay:"极端历史回放",
  observed_state_slice:"市场状态切片",
  calibrated_stress:"校准压力场景",
};
const environmentNameNames:Record<string,string>={
  "crowding:rising":"拥挤度上升",
  "crowding:unwinding":"拥挤度消退",
  "liquidity:contracting":"流动性收缩",
  "liquidity:expanding":"流动性扩张",
  "style:growth":"成长风格",
  "style:large_cap":"大盘风格",
  "style:small_cap":"小盘风格",
  "style:value":"价值风格",
  "trend:down":"下跌趋势",
  "trend:sideways":"震荡行情",
  "trend:up":"上涨趋势",
  "volatility:high":"高波动",
  "volatility:low":"低波动",
  liquidity_contraction:"流动性收缩压力",
  transaction_cost_jump:"交易成本跳升",
  rapid_regime_switch:"市场状态快速切换",
  correlation_spike:"相关性骤升",
  state_misclassification:"市场状态误判",
};
function environmentTypeLabel(value:unknown){const raw=String(value||"");return environmentTypeNames[raw]||raw||"环境实例";}
function environmentNameLabel(value:unknown){
  const raw=String(value||"");
  if(environmentNameNames[raw])return environmentNameNames[raw];
  if(raw.startsWith("historical:"))return `历史窗口 ${raw.slice("historical:".length).split(":").join(" 至 ")}`;
  if(raw.startsWith("extreme:"))return `极端窗口 ${raw.slice("extreme:".length).split(":").join(" 至 ")}`;
  return raw||"未命名环境";
}
function researchResultStatusLabel(value:unknown){
  const raw=String(value||"");
  return ({
    queued:"等待回测",running:"回测中",completed:"已完成",
    insufficient_observed_slice_evidence:"切片证据不足",
    blocked_by_missing_calibration:"缺少校准",
    blocked_by_missing_environment_adapter:"缺少环境适配",
    blocked_by_missing_historical_baseline:"缺少历史基线",
  } as Record<string,string>)[raw]||raw||"等待";
}
function researchActivationLabel(value:unknown){return ({enabled:"已启用",disabled:"已停用"} as Record<string,string>)[String(value||"")]||String(value||"未加载");}

function ResearchDrilldown({pools,industries,initialStockEvidence,initialIndustryEvidence}:{pools:Json[];industries:Json[];initialStockEvidence?:Json;initialIndustryEvidence?:Json}){
  const initialStock=String(pick(pools[0],["ts_code","symbol"],""));const initialIndustry=String(pick(industries[0],["industry_code","code","name"],""));const [stock,setStock]=useState(initialStock);const [industry,setIndustry]=useState(initialIndustry);const [stockEvidence,setStockEvidence]=useState<Json>(initialStockEvidence||{});const [industryEvidence,setIndustryEvidence]=useState<Json>(initialIndustryEvidence||{});const firstStockRequest=useRef(true);const firstIndustryRequest=useRef(true);
  useEffect(()=>{if(!stock)return;if(firstStockRequest.current){firstStockRequest.current=false;if(stock===initialStock&&initialStockEvidence!==undefined)return}const controller=new AbortController();readApi(`/api/research/stocks/${encodeURIComponent(stock)}/console-evidence`,controller.signal).then(setStockEvidence).catch(error=>{if(error?.name!=="AbortError")setStockEvidence({status:"unavailable",reason:String(error)})});return()=>controller.abort()},[stock,initialStock,initialStockEvidence]);
  useEffect(()=>{if(!industry)return;if(firstIndustryRequest.current){firstIndustryRequest.current=false;if(industry===initialIndustry&&initialIndustryEvidence!==undefined)return}const controller=new AbortController();readApi(`/api/research/industries/${encodeURIComponent(industry)}`,controller.signal).then(setIndustryEvidence).catch(error=>setIndustryEvidence({status:"unavailable",reason:String(error)}));return()=>controller.abort()},[industry,initialIndustry,initialIndustryEvidence]);
  return <div className="two-column"><Section title="股票研究详情"><label className="detail-select"><span>研究对象</span><select value={stock} onChange={event=>setStock(event.target.value)}>{pools.map((row,i)=>{const code=String(pick(row,["ts_code","symbol"],""));const name=pick(row,["name","symbol_name"],"");return <option value={code} key={`${code}-${i}`}>{name ? `${name} ${code}` : code}</option>})}</select></label><EvidenceDetails title="公司、估值、预期、准入与财务完整度"><pre>{JSON.stringify(stockEvidence,null,2)}</pre></EvidenceDetails></Section><Section title="行业研究详情"><label className="detail-select"><span>行业</span><select value={industry} onChange={event=>setIndustry(event.target.value)}>{industries.map((row,i)=>{const code=String(pick(row,["industry_code","code","name"],""));return <option value={code} key={`${code}-${i}`}>{pick(row,["industry_name","name"],code)}</option>})}</select></label><EvidenceDetails title="行业状态与成分证据"><pre>{JSON.stringify(industryEvidence,null,2)}</pre></EvidenceDetails></Section></div>;
}

function ThemeEvidence({themeCode,asOfDate}:{themeCode:string;asOfDate:string}){
  const [open,setOpen]=useState(false);const [state,setState]=useState<Json>({});const [loading,setLoading]=useState(false);const [error,setError]=useState("");
  useEffect(()=>{if(!open||state.item)return;const controller=new AbortController();setLoading(true);setError("");readApi(`/api/research/themes/${encodeURIComponent(themeCode)}?as_of_date=${encodeURIComponent(asOfDate)}`,controller.signal).then(setState).catch(reason=>{if(reason?.name!=="AbortError")setError(String(reason?.message||reason))}).finally(()=>{if(!controller.signal.aborted)setLoading(false)});return()=>controller.abort()},[open,state.item,themeCode,asOfDate]);
  return <details className="evidence-details" open={open} onToggle={event=>setOpen((event.currentTarget as HTMLDetailsElement).open)}><summary>主题公司与评分证据</summary>{loading?<p>读取证据中...</p>:error?<p>{error}</p>:state.item?<pre>{JSON.stringify(state.item,null,2)}</pre>:<p>展开后按主题读取完整点时证据。</p>}</details>;
}

function rebalanceConclusion(rebalance: Json, isolated: boolean, tradingBlocked = false) {
  if (isolated) return "研究隔离，控制风险敞口";
  if (tradingBlocked) return "交易业务阻断，暂不执行";
  if (rebalance.execution_authorized) {
    const frozenOrders = firstArray(rebalance, ["proposal", "actions"])
      .filter(item => Number(item.planned_quantity || 0) > 0).length;
    const orderCount = Number(rebalance.proposal_count ?? frozenOrders);
    return orderCount > 0 ? "正式调仓计划已授权" : "今日保持当前组合";
  }
  const status = String(rebalance.status || "");
  if (status === "not_generated" || !status) return "等待正式计划";
  return "正式计划禁止执行";
}

function rebalanceReason(rebalance: Json, readinessBlocker = "") {
  if (rebalance.blocked_reason) return `阻断原因：${rebalance.blocked_reason}`;
  if (readinessBlocker) return `阻断原因：${readinessBlocker}`;
  if (rebalance.execution_authorized) return `计划执行日 ${rebalance.planned_execute_date || "待确认"}`;
  return "等待执行授权。";
}

function livePortfolioPerformance(portfolio: Json | undefined) {
  const series = firstArray(portfolio, ["series", "curve", "items"])
    .map(row => ({ date: String(pick(row, ["date", "trade_date"], "")), close: Number(pick(row, ["close", "strategy_equity", "equity", "value"], NaN)) }))
    .filter(row => row.date && Number.isFinite(row.close));
  const latest = pick(portfolio, ["latest_comparison"], {}) as Json;
  const month = series.length ? series[series.length - 1].date.slice(0, 6) : "";
  const monthRows = series.filter(row => row.date.slice(0, 6) === month);
  const monthly = monthRows.length > 1 && monthRows[0].close > 0
    ? (monthRows[monthRows.length - 1].close / monthRows[0].close - 1) * 100
    : null;
  return {
    daily: pick(latest, ["portfolio_daily_return_pct"], null),
    monthly,
    excess: pick(latest, ["excess_return_pct"], null),
    benchmark: pick(latest, ["benchmark_return_pct"], null),
  };
}

function frozenDecisionRationale(rebalance: Json, items: Json[]) {
  const controller = pick(rebalance, ["pretrade_controller"], {}) as Json;
  const contracts = firstArray(controller, ["actions", "contracts"]);
  const expected = pick(rebalance, ["expected_return_contract"], {}) as Json;
  const rawValues = (field: string) => [...new Set(contracts.flatMap((item) => {
    const value = item[field];
    return Array.isArray(value) ? value : value === undefined || value === null || value === "" ? [] : [value];
  }))];
  const textValues = (field: string) => rawValues(field).map(String);
  const expectedSource = expected.schema_version
    ? `${expected.schema_version} · ${expected.horizon_sessions || "未记录"} 个交易日`
    : items[0]?.expected_return_contract?.schema_version
      ? `${items[0].expected_return_contract.schema_version} · 20 个交易日`
      : "冻结计划未提供收益合同";
  const riskBudgetText = (() => {
    const budget = pick(rebalance, ["risk_budget"], {}) as Json;
    const actionBudgets = rawValues("risk_budget").filter(value => value && typeof value === "object") as Json[];
    const singleAssetLimits = actionBudgets.map(value => Number(value.max_single_position_pct)).filter(Number.isFinite);
    const preTradeBlocked = String(budget.status || "") === "blocked";
    const mandatoryRiskReduction = items.some(item =>
      Boolean(pick(item, ["controller_obligation.required"], false))
      && ["reduce_position", "sell_exit"].includes(String(pick(item, ["controller_action", "controller_intent"], "")))
      && Number(pick(item, ["planned_quantity"], 0)) > 0
    );
    const parts = [
      preTradeBlocked
        ? mandatoryRiskReduction && rebalance.execution_authorized
          ? "执行前风险告警，已由本计划强制减仓处理"
          : "执行前风险门阻断"
        : budget.status ? `状态 ${budget.status}` : "",
      Number.isFinite(Number(budget.equity_budget)) ? `权益预算 ${(Number(budget.equity_budget) * 100).toFixed(2)}%` : "",
      Number.isFinite(Number(budget.cash_target)) ? `现金目标 ${(Number(budget.cash_target) * 100).toFixed(2)}%` : "",
      singleAssetLimits.length ? `单资产上限 ${(Math.max(...singleAssetLimits) * 100).toFixed(2)}%` : "",
    ].filter(Boolean);
    return parts.join(" · ") || "冻结计划未提供风险预算";
  })();
  return {
    expectedReturn: expectedSource,
    triggerModels: textValues("trigger_models").join("、") || "冻结计划未提供触发模型",
    riskBudget: riskBudgetText,
    holdingPeriod: textValues("holding_period").join("、") || "冻结计划未提供持有期",
    exitConditions: textValues("exit_conditions").join("、") || "冻结计划未提供退出条件",
    forbiddenConditions: textValues("forbidden_conditions").join("、") || "冻结计划未提供禁止条件",
  };
}

function ownerIncidents(owner?: Json) {
  const supervised = firstArray(owner, ["questions.incident_supervision.cases"]);
  if (supervised.length) return supervised;
  return firstArray(owner, ["questions.anomalies", "incidents", "exceptions"]);
}

function HomeRuntimeSummary({ data }: { data: ConsoleData }) {
  const health = data.health || {};
  const readiness = data.tradingReady || {};
  const tasks = firstArray(data.adminTasksLive || data.tasks, ["tasks", "items"]);
  const watermarks = rowsFrom(data.watermarks, ["items", "assets", "datasets"]);
  const staleAssets = watermarks.filter((row) => /stale|failed|blocked|degraded/.test(String(pick(row, ["status", "readiness"], ""))));
  const activeTasks = tasks.filter((row) => pick(row, ["source"], "") !== "runtime" && /running|queued|retry_wait|waiting|observing|blocked|degraded/.test(String(pick(row, ["status", "state"], ""))));
  const dailyRunning = Boolean(pick(data.dailyUpdateStatus, ["running"], false));
  const dailyStatus = dailyRunning ? "running" : String(pick(data.dailyUpdateStatus, ["phase"], "")) === "failed" ? "failed" : pick(data.dailyUpdateStatus, ["latest_decision_ready_trade_date"], null) ? "ready" : "waiting";
  const ledgerStatus = String(pick(data.ledgerAudit, ["status"], pick(data.ledgerAudit, ["ok"], null) === true ? "ready" : "unknown"));
  const rows = [
    { label: "基础服务", status: pick(health, ["status"], "unknown"), detail: pick(health, ["summary", "detail"], "双库、Redis 与运行组件") },
    { label: "交易业务", status: pick(readiness, ["status"], "unknown"), detail: pick(readiness, ["summary"], "等待交易业务硬门") },
    { label: "日更链路", status: dailyStatus, detail: dailyRunning ? pick(data.dailyUpdateStatus, ["message"], "正在更新") : pick(data.dailyUpdateStatus, ["latest_decision_ready_trade_date"], "等待下一个完整数据日") },
    { label: "模拟账本", status: ledgerStatus, detail: pick(data.ledgerAudit, ["reason", "summary", "checked_at"], "账本一致性审计") },
    { label: "数据资产", status: staleAssets.length ? "degraded" : watermarks.length ? "ready" : "unknown", detail: `${watermarks.length - staleAssets.length}/${watermarks.length} 项无异常标记` },
    { label: "OOS / 密封集", status: /failed|blocked/.test(String(pick(data.oos, ["status"], ""))) ? pick(data.oos, ["status"], "unknown") : pick(data.sealed, ["status"], pick(data.oos, ["status"], "unknown")), detail: `${pick(data.oos, ["eligible_sessions"], "未记录")}/1537 · ${pick(data.sealed, ["eligible_sessions"], "未记录")}/126` },
  ];
  return <>
    <Section title="运行与数据状态"><div className="health-chain overview-health-grid">{rows.map((row) => <article key={row.label}><Status>{row.status}</Status><strong>{row.label}</strong><p>{String(row.detail)}</p></article>)}</div></Section>
    <Section title="当前业务推进">{activeTasks.length ? <TaskCategory rows={activeTasks.slice(0, 8)} /> : <Empty title="当前没有业务任务" detail="等待下一次调度。" />}<div className="overview-links"><a className="text-link" href="/operations">任务与链路 →</a><a className="text-link" href="/data">数据与日更 →</a><a className="text-link" href="/evidence">证据与审计 →</a></div></Section>
  </>;
}

export function Overview({ data }: { data: ConsoleData }) {
  const owner = unwrapOwner(data.owner); const ledger = data.ledger; const state = ownerState(owner, data.tradingReady);
  const rebalance = data.latestPlan || pick(owner, ["questions.today_rebalance"], {});
  const profit = pick(owner, ["questions.profit"], {});
  const livePerformance = livePortfolioPerformance(data.portfolio);
  const readinessGates = firstArray(data.tradingReady, ["gates"]);
  const readinessBlocker = String(firstArray(data.tradingReady, ["hard_blockers"])[0] || readinessGates.find((gate) => String(gate.status) === "blocked")?.summary || "");
  const equity = pick(ledger, ["total_equity"], pick(owner, ["account.total_equity", "total_equity"]));
  const actionsList = firstArray(rebalance, ["proposal", "actions"]);
  const incidents = ownerIncidents(owner);
  const dailyTone = metricTone(livePerformance.daily);
  const excessTone = metricTone(livePerformance.excess);
  const monthlyTone = metricTone(livePerformance.monthly);
  const decisionBlocked = state.isolated || state.tradingBlocked;
  const healthStatus = pick(data.health, ["status"], "unknown");
  const tradingStatus = pick(data.tradingReady, ["status"], "unknown");
  const totalEquity = number(ledger, ["total_equity"], 0);
  const cashBalance = number(ledger, ["cash_balance"], 0);
  const positionValue = number(ledger, ["position_market_value"], 0);
  const positionRatio = totalEquity > 0 ? Math.max(0, Math.min(1, positionValue / totalEquity)) : null;
  return <>
    <PageHeader eyebrow="OWNER CONSOLE" title="投资系统总览" summary="今日运行、账户表现与需要关注的异常。" />
    <div className="overview-dashboard-layout">
      <section className="overview-flow-panel">
        <div className="metric-grid overview-mini-metrics"><Metric label="账户日收益" value={fmtPoints(livePerformance.daily)} tone={dailyTone} note="最新完整交易日" icon={Activity}/><Metric label="账户月收益" value={fmtPoints(livePerformance.monthly)} tone={monthlyTone} note="当前月份已观测区间" icon={ChartNoAxesCombined}/><Metric label="累计超额" value={fmtPoints(livePerformance.excess)} tone={excessTone} note="相对沪深300，最新比较" icon={Sparkles}/></div>
        <div className="overview-curve-heading"><div><span>资金表现</span><h2>账户净值与基准</h2></div><small>共同首日归一化</small></div>
        <Curve data={data.portfolio}/>
      </section>
      <aside className="overview-account-stack">
        <article className="overview-account-card">
          <header><div><span>模拟账户</span><h3>资产总览</h3></div><Status tone="info">{pick(ledger,["valuation_date"],"时点未记录")}</Status></header>
          <div className="overview-account-balance"><span>账户权益</span><strong>{fmtMoney(equity)}</strong><small>模拟交易账户</small></div>
          <div className="overview-account-split"><div><span>持仓市值</span><strong>{fmtMoney(positionValue)}</strong><small>{positionRatio===null?"持仓占比未记录":`占比 ${fmtPct(positionRatio,false)}`}</small></div><div><span>可用现金</span><strong>{fmtMoney(cashBalance)}</strong><small>{totalEquity>0?`占比 ${fmtPct(cashBalance/totalEquity,false)}`:"现金占比未记录"}</small></div></div>
        </article>
        <section className="overview-account-detail"><header><div><span>账户结构</span><h3>权益与基准</h3></div></header><div className="overview-account-detail-body"><div className="overview-account-ring" style={{background:`conic-gradient(#7258f4 0deg ${Math.round((positionRatio || 0) * 360)}deg, #e8eaf1 ${Math.round((positionRatio || 0) * 360)}deg 360deg)`}}><div><strong>{positionRatio===null?"未记录":fmtPct(positionRatio,false)}</strong><span>持仓</span></div></div><dl><div><dt>累计盈亏</dt><dd className={number(ledger,["equity_delta"])>=0?"positive":"negative"}>{fmtMoney(pick(ledger,["equity_delta"]))}</dd></div><div><dt>沪深300同期</dt><dd>{fmtPoints(livePerformance.benchmark)}</dd></div></dl></div></section>
      </aside>
    </div>
    <div className="overview-command-grid">
      <article className={`overview-decision-card ${decisionBlocked?"is-blocked":""}`}><header><span className="overview-decision-icon"><Activity size={18}/></span><Status tone={decisionBlocked?"danger":state.oosWarning?"watch":"good"}>{state.isolated?"研究隔离":state.tradingBlocked?"今日阻断":rebalance.execution_authorized?"允许执行":"等待执行"}</Status></header><span>今日交易决策</span><h2>{rebalanceConclusion(rebalance,state.isolated,state.tradingBlocked)}</h2><p>{rebalanceReason(rebalance,readinessBlocker)}</p><footer><div><span>冻结动作</span><strong>{pick(rebalance,["proposal_count"],actionsList.length)}</strong></div><div><span>计划日期</span><strong>{pick(rebalance,["trade_date","plan_date"],"未记录")}</strong></div><div><span>执行日</span><strong>{pick(rebalance,["planned_execute_date"],"未记录")}</strong></div><a href="/decision">查看计划 <ArrowUpRight size={15}/></a></footer></article>
      <article className="overview-system-card"><header><div><span>运行状态</span><h3>{pick(owner,["healthy"],false)?"系统稳定运行":"系统处于保护运行"}</h3></div><Status tone={decisionBlocked?"danger":"good"}>{state.label}</Status></header><dl><div><dt>基础服务</dt><dd><Status>{healthStatus}</Status></dd></div><div><dt>交易业务</dt><dd><Status>{tradingStatus}</Status></dd></div><div><dt>最近完整周期</dt><dd>{formatSystemTime(pick(data.owner,["finished_at"],pick(owner,["generated_at"],"")),"等待周期完成")}</dd></div></dl></article>
    </div>
    {(state.isolated||state.oosWarning)&&<div className="overview-risk-note"><TriangleAlert size={18}/><div><strong>{state.isolated?"Champion 正式 OOS 失效，当前处于研究隔离":"Champion 正式 OOS 失败，策略有效性仍处于风险告警"}</strong><span>{state.isolated?(state.defensiveContinuity?`仅允许减仓、卖出及最多 ${fmtPct(state.defensiveMaximum)} 的登记防御 ETF。`:"仅允许减仓、卖出、账本审计和候选研究。") : "该结果用于研究治理；今日执行仍由交易就绪门和冻结合同决定。"}</span></div></div>}
    <HomeRuntimeSummary data={data}/>
    <Section title="距离下一次验收还差多少" detail="进度取各硬门的最短板，不用平均值掩盖缺口。"><div className="progress-grid">{acceptance(owner).map((item, index) => <Progress key={index} label={String(item.name || item.label || "验收项")} current={Number(item.current ?? item.value ?? 0)} target={Number(item.target ?? 1)} suffix={String(item.unit || "")} />)}</div></Section>
    <Section title="历史周期异常与系统自行处理事项" detail="周期证据与当前实时 readiness 分开显示；只有需要所有者授权的事项会提升为强通知。">{incidents.length ? <div className="incident-list">{incidents.slice(0, 6).map((item, i) => {const historical=String(pick(item,["evidence_scope"],""))==="completed_cycle";return <div key={i}><Status>{pick(item, ["disposition", "status"], "观察")}</Status><div><strong>{historical ? "历史周期证据 · " : ""}{pick(item, ["title", "summary", "name"], "运行事项")}</strong><p>{pick(item, ["next_review", "reason", "detail"], "等待下一周期复核")}{historical && pick(item,["observed_at"],"") ? ` · 观测于 ${String(pick(item,["observed_at"],""))}` : ""}</p></div></div>})}</div> : <Empty title="没有需要所有者处理的事项" detail="等待未来样本和冷却期不计为故障。" />}<a className="text-link" href="/operations">查看系统运行证据 →</a></Section>
  </>;
}

export function TodayDecision({ data }: { data: ConsoleData }) {
  const decision = data.decision; const owner = unwrapOwner(data.owner); const state = ownerState(owner, data.tradingReady);
  const rebalance = data.latestPlan || pick(owner, ["questions.today_rebalance"], {});
  const readinessGates = firstArray(data.tradingReady, ["gates"]);
  const readinessBlocker = String(firstArray(data.tradingReady, ["hard_blockers"])[0] || readinessGates.find((gate) => String(gate.status) === "blocked")?.summary || "");
  const items = firstArray(rebalance, ["proposal", "actions"]);
  const rationale = frozenDecisionRationale(rebalance, items);
  return <><PageHeader eyebrow="TODAY'S PLAN" title="今日决策" summary="围绕正式调仓计划查看动作、权重、执行状态与冻结证据。" />
    <div className={`decision-status ${state.isolated || state.tradingBlocked ? "danger-zone" : ""}`}><div><span>当前结论</span><h2>{rebalanceConclusion(rebalance,state.isolated,state.tradingBlocked)}</h2><p>{rebalanceReason(rebalance, readinessBlocker)}</p></div><Status tone={state.isolated || state.tradingBlocked ? "danger" : state.oosWarning ? "watch" : rebalance.execution_authorized ? "good" : "watch"}>{state.isolated ? "研究隔离" : state.tradingBlocked ? "今日阻断" : state.oosWarning ? "允许按计划执行 · OOS 告警" : rebalance.execution_authorized ? "允许执行" : "禁止执行"}</Status></div>
    <Section title="动作清单" detail="按 StrategyController 统一动作合同分组。">{items.length ? <div className="action-groups">{actions.map((action) => { const rows = items.filter((x) => displayedAction(x) === action); const Icon=actionIcons[action]||Activity; return <div className={`action-group action-group-${action}`} key={action}><header><div><span className="action-group-icon"><Icon size={15}/></span><strong>{actionNames[action]}</strong></div><span>{rows.length}</span></header>{rows.length ? rows.map((row, i) => <article key={i}><div><b>{pick(row, ["name", "symbol_name", "ts_code", "symbol"], "未命名资产")}</b><small>{pick(row, ["symbol", "ts_code"], "")}</small></div><p>{pick(row, ["reason", "action_reason", "thesis", "controller_intent"], "冻结合同未记录文字原因")}</p><strong>{fmtPct(pick(row, ["target_weight", "weight"]), false)}</strong></article>) : <small className="muted">当前无动作</small>}</div>; })}</div> : <Empty title={pick(rebalance,["status"],"")==="not_generated"?"当前没有正式冻结计划":"该冻结计划没有逐笔调仓动作"} />}</Section>
    <div className="two-column decision-layout"><Section title="权重、数量与成本" detail="逐项展示冻结计划中的真实可执行订单；零数量只显示保持或观察。"><DecisionTable items={items} /></Section><Section title="执行状态" detail="计划与成交严格分离。"><dl className="fact-list"><div><dt>计划状态</dt><dd><Status>{pick(rebalance,["status"],"未生成")}</Status></dd></div><div><dt>计划日期</dt><dd>{pick(rebalance,["trade_date","plan_date"],"计划日期未记录")}</dd></div><div><dt>计划执行日</dt><dd>{pick(rebalance,["planned_execute_date"],"执行日未记录")}</dd></div><div><dt>执行授权</dt><dd>{rebalance.execution_authorized ? "是" : "否"}</dd></div><div><dt>单资产硬上限</dt><dd>{fmtPct(pick(rebalance,["risk_limit_contract.global_single_asset_limit"],null),false)}</dd></div><div><dt>校验错误</dt><dd>{pick(rebalance,["validation_error_code"],"无")}</dd></div><div><dt>阻断原因</dt><dd>{pick(rebalance,["blocked_reason"],readinessBlocker || "无")}</dd></div></dl></Section></div>
    <Section title="决策原因" detail="收益来源、风险预算和退出合同均读取 T 日冻结证据。"><div className="reason-grid">{[["收益来源假设", rationale.expectedReturn], ["触发模型", rationale.triggerModels], ["风险预算", rationale.riskBudget], ["持有周期", rationale.holdingPeriod], ["退出条件", rationale.exitConditions], ["禁止条件", rationale.forbiddenConditions]].map(([label, value]) => <div key={label}><span>{label}</span><p>{value}</p></div>)}</div><EvidenceDetails title="完整决策证据"><pre>{JSON.stringify({decision,rebalance}, null, 2)}</pre></EvidenceDetails></Section>
  </>;
}

function DecisionTable({ items }: { items: Json[] }) {
  const decisionLabel=(row:Json)=>{const quantity=Number(row.planned_quantity||0);const intent=String(row.controller_intent||"");const required=Boolean(pick(row,["controller_obligation.required"],false));if(quantity>0)return `${required?"强制":"建议"} · ${String(row.planned_side||"")==="SELL"?"卖出":"买入"}`;if(intent==="research_only")return "研究观察 · 禁止买入";if(["advisory_entry","advisory_add"].includes(intent))return "允许，但组合未选择";return "保持 · 无执行单";};
  return items.length ? <div className="table-wrap decision-orders"><table><thead><tr><th>资产</th><th>授权与动作</th><th>冻结数量</th><th>当前 / 目标</th><th>金额与成本</th></tr></thead><tbody>{items.map((row, i) => {const cost=pick(row,["planned_cost"],{}) as Json;const quantity=Number(row.planned_quantity||0);return <tr key={i}><td data-label="资产"><strong>{pick(row,["name","symbol_name","symbol","ts_code"],"未命名资产")}</strong><small>{pick(row,["symbol","ts_code"],"")}</small></td><td data-label="授权与动作"><strong>{decisionLabel(row)}</strong><small>{pick(row,["reason"],"")}</small></td><td data-label="冻结数量">{quantity>0?<><strong>{quantity} 股</strong><small>{pick(row,["planned_lots"],0)} 手 · 参考价 {fmtMoney(pick(row,["reference_close"]))}</small></>:"0 股"}</td><td data-label="当前 / 目标">{fmtPct(pick(row,["current_weight"]),false)} / {fmtPct(pick(row,["target_weight","weight"]),false)}</td><td data-label="金额与成本">{quantity>0?<><strong>{fmtMoney(pick(row,["estimated_execution_amount"]))}</strong><small>费用 {fmtMoney(Number(cost.commission||0)+Number(cost.transfer_fee||0)+Number(cost.stamp_tax||0))} · 滑点 {fmtMoney(cost.slippage_cost)} · 缓冲 {fmtMoney(cost.calibration_buffer)}</small></>:"无交易成本"}</td></tr>})}</tbody></table></div> : <Empty title="暂无正式执行单" detail="当前没有冻结执行单，可能是保持、数据门阻断或计划尚未生成。" />;
}

export function Portfolio({ data }: { data: ConsoleData }) {
  const ledger = data.ledger; const positions = firstArray(data.positions, ["positions"]);
  const competition = pick(data.research, ["portfolio_optimization.optimizer.method_competition","portfolio_optimization.method_competition"], {});
  const methods = firstArray(competition, ["evaluations"]);
  const totalEquity=number(ledger,["total_equity"],0);const cashBalance=number(ledger,["cash_balance"],0);const positionValue=number(ledger,["position_market_value"],0);const valuationDate=String(pick(ledger,["valuation_date"],"未记录"));
  const cashRatio=totalEquity>0?cashBalance/totalEquity:null;const positionRatio=totalEquity>0?positionValue/totalEquity:null;
  const underlyingRisk=pick(data.risk,["underlying_risk_exposure"],{}) as Json;
  const riskExposures=firstArray(data.risk,["exposures"]);
  const breaches=firstArray(data.risk,["breaches"]);
  const hardBreaches=breaches.filter(row=>String(pick(row,["severity"],"")).toLowerCase()==="error");
  const advisoryBreaches=breaches.filter(row=>String(pick(row,["severity"],"")).toLowerCase()!=="error");
  const latestPlan=data.latestPlan||{};
  const plannedActions=firstArray(latestPlan,["proposal","actions"]);
  const remediatedHardBreaches=hardBreaches.filter(breach=>plannedActions.some(action=>
    String(pick(action,["symbol"],""))===String(pick(breach,["symbol"],""))
    && Boolean(pick(action,["controller_obligation.required"],false))
    && ["reduce_position","sell_exit"].includes(String(pick(action,["controller_action","controller_intent"],"")))
    && Number(pick(action,["planned_quantity"],0))>0
  ));
  const allHardBreachesPlanned=hardBreaches.length>0&&remediatedHardBreaches.length===hardBreaches.length&&Boolean(latestPlan.execution_authorized);
  const riskDisplay=allHardBreachesPlanned?"待本次计划修复":pick(data.risk,["status"],"证据缺失");
  return <><PageHeader eyebrow="ACCOUNT & PORTFOLIO" title="账户与组合" summary="资产、收益、风险与账本。" />
    <div className="metric-grid headline-metrics portfolio-metrics"><Metric label="账户权益" value={fmtMoney(totalEquity)} note={`估值日 ${valuationDate}`} icon={WalletCards}/><Metric label="真实权益暴露" value={fmtPct(pick(underlyingRisk,["equity_exposure"]),false)} note={`股票 ${fmtPct(pick(underlyingRisk,["stock_exposure"]),false)} · 权益型 ETF ${fmtPct(pick(underlyingRisk,["equity_like_etf_exposure"]),false)}`} icon={ChartPie}/><Metric label="真实防御资产" value={fmtPct(pick(underlyingRisk,["true_defensive_exposure"]),false)} note="货币、国债与黄金；不含红利类 ETF" icon={Shield}/><Metric label="现金" value={fmtMoney(cashBalance)} note={cashRatio===null?"占比未记录":`账户占比 ${fmtPct(cashRatio,false)}`} icon={Banknote}/></div>
    <div className="portfolio-hero-grid"><Section title="净值与基准" detail="账户与沪深300共同首日归一化；缺失区间不补值。" className="portfolio-curve-section"><Curve data={data.portfolio || data.performance} /></Section><Section title="风险概览" className="portfolio-risk-section"><dl className="fact-list"><div><dt>经验 CVaR</dt><dd>{fmtPct(pick(data.risk,["tail_risk.cvar","cvar","portfolio_cvar"]),false)}</dd></div><div><dt>风险状态</dt><dd><Status tone={allHardBreachesPlanned?"watch":undefined}>{riskDisplay}</Status></dd></div><div><dt>硬风险</dt><dd>{hardBreaches.length}{allHardBreachesPlanned?"（已冻结减仓）":""}</dd></div><div><dt>资料提醒</dt><dd>{advisoryBreaches.length}</dd></div><div><dt>压力场景</dt><dd>{firstArray(data.risk,["stress_tests"]).length}</dd></div><div><dt>账本一致性</dt><dd><Status>{pick(unwrapOwner(data.owner),["questions.system_changes.ledger_status"],"证据缺失")}</Status></dd></div></dl></Section></div>
    <Section title="当前持仓" detail={`组合内最早价格时间 ${pick(data.positions,["data_source_time"],"未记录")}；个别标的可能已使用更晚的可用收盘价。`}>{positions.length ? <div className="table-wrap"><table><thead><tr><th>持仓</th><th>数量</th><th>当前市值</th><th>真实风险分类</th><th>盈亏</th><th>策略下一步</th></tr></thead><tbody>{positions.map((row,i)=>{const exposure=riskExposures.find(item=>String(pick(item,["symbol"],""))===String(pick(row,["symbol"],"")));const group=String(pick(exposure,["risk_group"],"unknown"));const label=group==="true_defensive"?"真实防御":group==="equity_like"?"权益型 ETF":group==="equity"?"股票权益":"待识别";return <tr key={i}><td><strong>{pick(row,["name","symbol"],"未记录")}</strong><small>{pick(row,["symbol"],"")}</small></td><td>{pick(row,["quantity"],"未记录")}</td><td>{fmtMoney(pick(row,["current_value"]))}</td><td><strong>{label}</strong><small>权益暴露 {fmtPct(pick(exposure,["equity_exposure_factor"]),false)}</small></td><td className={metricTone(pick(row,["profit_loss"]))==="good"?"positive":metricTone(pick(row,["profit_loss"]))==="danger"?"negative":""}>{fmtMoney(pick(row,["profit_loss"]))}</td><td>{pick(row,["strategy_next"],"未记录")}</td></tr>})}</tbody></table></div>:<Empty />}</Section>
    <Section title="组合方案竞赛" detail="同一预算、交易单位与成本假设下的比较。">{methods.length?<div className="table-wrap"><table><thead><tr><th>方案</th><th>目标值</th><th>CVaR</th><th>换手</th><th>预计总成本</th><th>结论</th></tr></thead><tbody>{methods.map((row,i)=><tr key={i}><td><strong>{String(pick(row,["method"],""))==="minimum_turnover_feasible"?"最小换手可行方案":pick(row,["method"],"未记录")}</strong></td><td>{pick(row,["objective"],"未记录")}</td><td>{fmtPct(pick(row,["cvar_20d","cvar"]),false)}</td><td>{fmtPct(pick(row,["turnover"]),false)}</td><td>{fmtMoney(pick(row,["estimated_total_cost_cny"]))}</td><td><Status>{String(pick(row,["method"],""))===String(pick(competition,["selected_method"],""))?"选择":"拒绝"}</Status></td></tr>)}</tbody></table></div>:<Empty title="暂无组合竞赛结果" />}</Section>
  </>;
}

function Curve({ data }: { data?: Json }) {
  const series=firstArray(data,["series","curve","items"]).slice(-60);if(!series.length)return <Empty title="暂无连续净值曲线" detail="更长历史将随模拟成交自然积累。"/>;
  const rows=series.map(row=>({date:String(pick(row,["date","trade_date"],"")),portfolio:Number(pick(row,["close","strategy_equity","equity","value"],NaN)),benchmark:Number(pick(row,["benchmark_close","benchmark","benchmark_value"],NaN))})).filter(row=>Number.isFinite(row.portfolio));if(!rows.length)return <Empty title="暂无可绘制净值"/>;
  const all=rows.flatMap(row=>[row.portfolio,row.benchmark]).filter(Number.isFinite);const rawMin=Math.min(...all),rawMax=Math.max(...all);const padding=Math.max((rawMax-rawMin)*.15,.2);const min=rawMin-padding,max=rawMax+padding;const coordinate=(value:number,index:number)=>({x:5+index/Math.max(rows.length-1,1)*90,y:88-(value-min)/(max-min)*72});const portfolioCoordinates=rows.map((row,index)=>coordinate(row.portfolio,index));const portfolioPoints=portfolioCoordinates.map(point=>`${point.x},${point.y}`).join(" ");const portfolioAreaPoints=`${portfolioCoordinates[0].x},88 ${portfolioPoints} ${portfolioCoordinates[portfolioCoordinates.length-1].x},88`;const benchmarkRows=rows.map((row,index)=>({value:row.benchmark,index,date:row.date})).filter(row=>Number.isFinite(row.value));const benchmarkPoints=benchmarkRows.map(row=>{const point=coordinate(row.value,row.index);return `${point.x},${point.y}`}).join(" ");const portfolioReturn=rows.length>1?rows[rows.length-1].portfolio/rows[0].portfolio-1:0;const firstBenchmark=benchmarkRows[0]?.value,lastBenchmark=benchmarkRows[benchmarkRows.length-1]?.value;const benchmarkReturn=firstBenchmark&&lastBenchmark?lastBenchmark/firstBenchmark-1:null;const lastPoint=portfolioCoordinates[portfolioCoordinates.length-1];const benchmarkLast=benchmarkRows[benchmarkRows.length-1];const benchmarkCoverageIncomplete=Boolean(benchmarkLast&&benchmarkLast.index<rows.length-1);const benchmarkCutoff=benchmarkCoverageIncomplete?coordinate(rows[benchmarkLast.index].portfolio,benchmarkLast.index):null;
  return <div className="curve"><header className="curve-head"><div className="curve-legend"><span><i className="portfolio-line"/>账户净值</span><span><i className="benchmark-line"/>沪深300</span></div><div className="curve-performance"><span>账户区间收益</span><strong className={portfolioReturn>=0?"positive":"negative"}>{fmtPct(portfolioReturn)}</strong></div></header><div className="curve-plot"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="账户净值与沪深300曲线">{[24,46,68,88].map(y=><line key={y} x1="5" y1={y} x2="95" y2={y}/>)}{benchmarkCutoff&&<line className="curve-cutoff" x1={benchmarkCutoff.x} y1="8" x2={benchmarkCutoff.x} y2="90"/>}<polygon className="portfolio-area" points={portfolioAreaPoints}/><polyline className="portfolio-line" points={portfolioPoints}/>{benchmarkPoints&&<polyline className="benchmark-line" points={benchmarkPoints}/>}</svg><span className="portfolio-point" style={{left:`${lastPoint.x}%`,top:`${lastPoint.y}%`}} aria-hidden="true"/></div><footer className="curve-axis"><span>{rows[0].date}</span><strong>{benchmarkReturn===null?"基准未记录":`基准${benchmarkCoverageIncomplete?`截至 ${benchmarkLast.date} `:" "}${fmtPct(benchmarkReturn)}`}</strong><span>{rows[rows.length-1].date}</span></footer>{benchmarkCoverageIncomplete&&<small className="curve-coverage-note">沪深300数据截至 {benchmarkLast.date}，之后区间暂无可比数据</small>}</div>;
}

export function Evolution({ data }: { data: ConsoleData }) {
  const [liveResearch,setLiveResearch]=useState<Json|undefined>(data.factorResearchConsole||data.factorResearch||data.factorResearchCompact);
  const [liveTasks,setLiveTasks]=useState<Json|undefined>(data.evolutionTasks);
  const [updatedAt,setUpdatedAt]=useState("");
  const [matrixExpanded,setMatrixExpanded]=useState(false);
  const [matrixSort,setMatrixSort]=useState<{key:string;direction:"asc"|"desc"}>({key:"fitness",direction:"desc"});
  const [candidateAccountFilter,setCandidateAccountFilter]=useState<"observing"|"stopped"|"all">("observing");
  useEffect(()=>{
    let active=true;let inFlight=false;const controller=new AbortController();
    const poll=async()=>{
      if(inFlight)return;
      inFlight=true;
      const results=await Promise.allSettled([
        readApi("/api/factors/research/status?console=true",controller.signal,60_000),
        readApi("/api/model-strategy/evolution/tasks?limit=60",controller.signal,60_000),
      ]);
      inFlight=false;
      if(!active)return;
      if(results[0].status==="fulfilled")setLiveResearch(results[0].value);
      if(results[1].status==="fulfilled")setLiveTasks(results[1].value);
      if(results.some(result=>result.status==="fulfilled"))setUpdatedAt(new Date().toLocaleTimeString("zh-CN",{hour12:false}));
    };
    const timer=window.setInterval(()=>void poll(),5000);
    return()=>{active=false;controller.abort();window.clearInterval(timer)};
  },[]);
  useEffect(()=>{if(data.factorResearchConsole)setLiveResearch(data.factorResearchConsole)},[data.factorResearchConsole]);
  useEffect(()=>{if(data.evolutionTasks)setLiveTasks(data.evolutionTasks)},[data.evolutionTasks]);

  const research=liveResearch||data.factorResearchConsole||data.factorResearch||{};
  const v10=pick(research,["v10"],{}) as Json;
  const experts=firstArray(v10,["expert_pool"]);
  const combinations=firstArray(v10,["factor_combinations"]);
  const clusters=firstArray(research,["clusters"]);
  const tasks=rowsFrom(liveTasks||data.evolutionTasks,["items","tasks"]);
  const v10Events=firstArray(v10,["events"]);
  const researchLines=[
    {key:"candidate_generation",name:"候选生成线",taskTypes:["factor_research_cycle","strategy_candidate_generation","strategy_environment_backtest"]},
    {key:"cluster_distillation",name:"策略簇蒸馏线",taskTypes:["strategy_cluster_distillation"]},
    {key:"strategy_portfolio",name:"策略组合线",taskTypes:["strategy_portfolio_research","strategy_portfolio_formal_queue"]},
  ].map(line=>{
    const lineTasks=tasks.filter(row=>line.taskTypes.includes(String(row.task_type||"")));
    const active=lineTasks.find(row=>["queued","running","retry_wait"].includes(String(row.status||"")));
    const latest=active||lineTasks[0];
    const eventRows=v10Events.filter(row=>String(row.research_line||"")===line.key);
    return {
      ...line,
      active,
      latest,
      eventCount:eventRows.reduce((sum,row)=>sum+Number(row.count||0),0),
      completedCount:eventRows.filter(row=>String(row.status)==="completed").reduce((sum,row)=>sum+Number(row.count||0),0),
    };
  });
  const researchLogs=tasks.filter(row=>researchLines.some(line=>line.taskTypes.includes(String(row.task_type||"")))).sort((left,right)=>{
    const timeDelta=String(pick(right,["updated_at","created_at"],"")).localeCompare(String(pick(left,["updated_at","created_at"],"")));
    return timeDelta||Number(right.id||0)-Number(left.id||0);
  }).slice(0,20);
  const factorRows=rowsFrom(data.factors,["definitions","factors","registry","items"]);
  const factorLabels=Object.fromEntries(factorRows.map(row=>[
    String(pick(row,["factor_key"],"")),
    String(pick(row,["name_cn","name","factor_key"],"")),
  ]).filter(([key])=>key));
  const championExperts=experts.filter(row=>String(row.role)==="champion");
  const backupExperts=experts.filter(row=>String(row.role)==="backup");
  const matrixRows:Json[]=combinations.flatMap((candidate:Json):Json[]=>{
    const results=firstArray(candidate,["environment_results"]);
    return (results.length?results:[{}]).map((result:Json):Json=>({
      ...result,
      candidate_id:candidate.candidate_id,
      generation_mode:candidate.generation_mode,
      factor_weights:candidate.factor_weights,
      factor_count:candidate.factor_count,
      holding_period_sessions:candidate.holding_period_sessions,
      candidate_fitness:candidate.fitness,
      candidate_status:candidate.status,
    }));
  });
  const matrixSortValue=(row:Json,key:string):string|number|null=>{
    const metrics=(row.metrics&&typeof row.metrics==="object"?row.metrics:row) as Json;
    const sortableNumber=(value:unknown)=>value===null||value===undefined||value===""||!Number.isFinite(Number(value))?null:Number(value);
    if(key==="combination")return String(row.candidate_id||"");
    if(key==="generation")return generationModeLabel(row.generation_mode);
    if(key==="environment")return environmentNameLabel(row.environment_name||row.environment_id);
    if(key==="total_return")return sortableNumber(pick(metrics,["total_return"],null));
    if(key==="benchmark_return")return sortableNumber(pick(metrics,["benchmark_return"],null));
    if(key==="excess_return")return sortableNumber(pick(metrics,["excess_return"],null));
    if(key==="max_drawdown")return sortableNumber(pick(metrics,["max_drawdown"],null));
    if(key==="turnover_rate")return sortableNumber(pick(metrics,["turnover_rate"],null));
    if(key==="fitness")return sortableNumber(pick(row,["environment_fitness.fitness"],null));
    if(key==="status")return researchResultStatusLabel(pick(row,["status"],pick(row,["candidate_status"],"")));
    return "";
  };
  const sortedMatrixRows=[...matrixRows].sort((left,right)=>{
    const a=matrixSortValue(left,matrixSort.key);const b=matrixSortValue(right,matrixSort.key);
    if(a===null&&b===null)return 0;
    if(a===null)return 1;
    if(b===null)return -1;
    const compared=typeof a==="number"&&typeof b==="number"?a-b:String(a).localeCompare(String(b),"zh-CN");
    return matrixSort.direction==="asc"?compared:-compared;
  });
  const visibleMatrixRows=matrixExpanded?sortedMatrixRows:sortedMatrixRows.slice(0,8);
  const sortMatrix=(key:string)=>{
    setMatrixSort(current=>current.key===key?{key,direction:current.direction==="asc"?"desc":"asc"}:{key,direction:["combination","generation","environment","status"].includes(key)?"asc":"desc"});
  };
  const sortHeader=(key:string,label:string)=><button className="matrix-sort-button" type="button" onClick={()=>sortMatrix(key)}>{label}<span>{matrixSort.key===key?(matrixSort.direction==="asc"?"↑":"↓"):"↕"}</span></button>;
  const matrixSortLabels:Record<string,string>={combination:"因子组合",generation:"生成方式",environment:"环境",total_return:"组合收益",benchmark_return:"基准收益",excess_return:"超额收益",max_drawdown:"最大回撤",turnover_rate:"换手率",fitness:"环境适应度",status:"状态"};
  const environmentCount=firstArray(v10,["environments"]).reduce((sum,row)=>sum+Number(row.count||0),0);
  const candidateCenter=data.candidateObservations||{};
  const candidateSummary=pick(candidateCenter,["summary"],{}) as Json;
  const candidateAccounts=firstArray(candidateCenter,["items"]);
  const visibleCandidateAccounts=candidateAccounts.filter(row=>candidateAccountFilter==="all"||String(row.runtime_status)===candidateAccountFilter);
  const typeNames:Record<string,string>={strategy:"策略",model:"业务模型",controller:"策略控制器",factor:"因子策略包",portfolio:"组合方法"};
  const changeNames:Record<string,string>={bugfix:"程序修复",performance_tuning:"效果调优",structural_change:"结构变更"};
  const candidateStopReason=(row:Json)=>{const reason=String(row.stop_reason||"");if(reason==="human_review_rejected"&&String(row.reviewer)==="auto_optimizer")return "自动评估淘汰";return({quick_diagnostic_failed:"快速诊断失败",operator_rejected:"所有者否决",human_review_rejected:"评审否决",shadow_rejected:"观察效果未通过",rolled_back:"晋升后已回退",superseded:"已被新版本替代",promoted_to_active:"已晋升"} as Record<string,string>)[reason]||reason||"—"};
  return <><PageHeader eyebrow="MODEL & FACTOR EVOLUTION" title="策略进化" summary={`模型、因子与策略三线状态${updatedAt?` · ${updatedAt}`:""}`} />
    <section className="account-isolation-map">
      <article className="production-account-lane"><header><div><span>PRODUCTION ACCOUNT</span><h2>唯一正式模拟账户</h2></div><Status tone="good">拥有执行权</Status></header><dl><div><dt>当前权益</dt><dd>{fmtMoney(pick(data.ledger,["total_equity"]))}</dd></div><div><dt>现金</dt><dd>{fmtMoney(pick(data.ledger,["cash_balance"]))}</dd></div><div><dt>持仓市值</dt><dd>{fmtMoney(pick(data.ledger,["position_market_value"]))}</dd></div><div><dt>生产账户数</dt><dd>1</dd></div></dl><p>只有当前 Champion 可以生成正式模拟订单；候选账户的决策和成交不会进入此账本。</p></article>
      <div className="isolation-boundary"><ShieldCheck size={20}/><strong>交易隔离边界</strong><span>账户、现金、持仓、订单、成交和费用全部分表保存</span></div>
      <article className="candidate-account-lane"><header><div><span>CANDIDATE ACCOUNTS</span><h2>候选隔离账户池</h2></div><Status tone="watch">零执行权</Status></header><dl><div><dt>观察候选</dt><dd>{pick(candidateSummary,["observing"],0)}</dd></div><div><dt>已建立账户</dt><dd>{pick(candidateSummary,["with_isolated_account"],0)}</dd></div><div><dt>具备资格</dt><dd>{pick(candidateSummary,["production_eligible"],0)}</dd></div><div><dt>生产授权</dt><dd>{pick(candidateSummary,["production_authority"],0)}</dd></div></dl><p>每个版本从独立 20 万元起账；新版本不会继承父版本收益，晋升前不能影响正式账户。</p></article>
    </section>

    <Section title="候选隔离交易账户" detail="候选创建后立即观察。账户有完整模拟交易证据，但没有正式执行权。">
      {candidateAccounts.length?<><div className="candidate-account-toolbar"><div>{([["observing","观察中"],["stopped","已停止"],["all","全部"]] as const).map(([key,label])=><button type="button" className={candidateAccountFilter===key?"active":""} onClick={()=>setCandidateAccountFilter(key)} key={key}>{label}<span>{key==="all"?candidateAccounts.length:candidateAccounts.filter(row=>String(row.runtime_status)===key).length}</span></button>)}</div><small>所有候选均无正式执行权</small></div><div className="table-wrap candidate-account-table"><table><thead><tr><th>候选账户</th><th>类型</th><th>状态</th><th>独立权益</th><th>累计收益</th><th>现金 / 持仓</th><th>持仓 / 成交</th><th>费用 / 筛选拦截</th><th>观察日期 / 停止原因</th><th>证据</th></tr></thead><tbody>{visibleCandidateAccounts.map((row,index)=>{const account=pick(row,["account"],{}) as Json;const hasAccount=account.latest_equity!==null&&account.latest_equity!==undefined;const blocked=Array.isArray(row.blocked_reasons)?row.blocked_reasons:[];const observing=String(row.runtime_status)==="observing";return <tr key={String(row.model_version_id||index)}><td data-label="候选账户"><strong>{String(row.name||row.component||"未命名候选")}</strong><small>#{row.model_version_id} · {String(row.component||"")}</small></td><td data-label="类型"><strong>{typeNames[String(row.candidate_type)]||row.candidate_type||"候选"}</strong><small>{changeNames[String(row.change_type)]||row.change_type||"未登记"}</small></td><td data-label="状态"><Status tone={observing?"watch":"neutral"}>{runtimeStatusLabel(row.runtime_status)}</Status><small>{String(row.eligibility_status)==="eligible"?"具备生产资格":"无生产资格"}</small></td><td data-label="独立权益"><strong>{hasAccount?fmtMoney(account.latest_equity):"等待首次回放"}</strong></td><td data-label="累计收益" className={hasAccount?(Number(account.return)>=0?"positive":"negative"):""}>{hasAccount?fmtPct(account.return):"—"}</td><td data-label="现金 / 持仓">{hasAccount?<><strong>{fmtMoney(account.latest_cash)}</strong><small>{fmtMoney(account.stock_value)}</small></>:"—"}</td><td data-label="持仓 / 成交">{hasAccount?`${Number(account.position_count||0)} / ${Number(account.operation_count||0)}`:"—"}</td><td data-label="费用 / 筛选拦截">{hasAccount?<><strong>{fmtMoney(account.total_fees)}</strong><small>{Number(account.blocked_event_count||0)} 次策略筛选拦截</small></>:"—"}</td><td data-label="观察日期 / 停止原因"><strong>{observing?(account.latest_trade_date||"等待首个未来交易日"):candidateStopReason(row)}</strong>{!observing&&row.review_comment?<small>{String(row.review_comment)}</small>:null}</td><td data-label="证据"><EvidenceDetails title="查看"><pre>{JSON.stringify({运行状态:row.runtime_status,停止原因:candidateStopReason(row),生产资格:row.eligibility_status,阻断原因:blocked,问题族:row.problem_family,独立账户:account,下一动作:row.next_action,禁止生产变更:row.production_change_forbidden},null,2)}</pre></EvidenceDetails></td></tr>})}</tbody></table></div></>:<Empty title="暂无候选隔离账户" detail="新候选创建后会立即出现在这里；首次完整回放后形成独立权益。"/>}
    </Section>
    <Section title="三线进化状态">
      <div className="research-line-grid">{researchLines.map((line,index)=>{
        const task=line.latest as Json|undefined;const rawProgress=Number(pick(task,["progress"],0));const progress=Math.round((rawProgress<=1?rawProgress*100:rawProgress)*10)/10;
        return <article className={line.active?"active":""} key={line.key}><header><span>0{index+1}</span><div><h3>{line.name}</h3><small>{line.taskTypes.map(researchTaskTypeLabel).join(" / ")}</small></div><Status tone={String(task?.status)==="failed"?"danger":line.active?"watch":String(task?.status)==="completed"?"good":"neutral"}>{researchTaskStatusLabel(task?.status)}</Status></header><div className="line-task-message"><strong>{task?`#${pick(task,["id"],"任务号未记录")} ${pick(task,["message","phase"],"步骤摘要未记录")}`:"暂无任务"}</strong><small>{pick(task,["updated_at","finished_at","created_at"],"时间未记录")}</small></div><div className="line-progress"><span><i style={{width:`${progress}%`}}/></span><b>{progress}%</b></div><footer><span>事件 {line.eventCount}</span><span>完成 {line.completedCount}</span><span>{pick(task,["phase"],"尚未进入阶段")}</span></footer></article>
      })}</div>
    </Section>

    <Section title="进化任务日志">
      {researchLogs.length?<div className="research-live-logs">{researchLogs.map((row,index)=><div key={String(row.id||index)}><time>{formatLogTime(pick(row,["updated_at","created_at"],""))}</time><span>{researchLineName(row.task_type)}</span><code>#{pick(row,["id"],"任务号未记录")}</code><Status tone={String(row.status)==="failed"?"danger":String(row.status)==="completed"?"good":String(row.status)==="running"?"watch":"neutral"}>{researchTaskStatusLabel(row.status)}</Status><strong>{pick(row,["message","phase"],"步骤摘要未记录")}</strong><small>{Math.round(Number(row.progress||0)*100)}%</small></div>)}</div>:<Empty title="暂无进化任务日志"/>}
    </Section>

    <Section title="环境专家池">
      <div className="expert-summary"><div><span>环境冠军</span><strong>{championExperts.length}</strong></div><div><span>备选专家</span><strong>{backupExperts.length}</strong></div><div><span>环境实例</span><strong>{environmentCount}</strong></div><div><span>进化引擎</span><Status>{researchActivationLabel(pick(v10,["activation_status"],"未加载"))}</Status></div></div>
      {championExperts.length?<div className="expert-pool-grid">{championExperts.map((expert,index)=>{
        const backups=backupExperts.filter(row=>String(row.environment_id)===String(expert.environment_id)&&String(row.environment_version)===String(expert.environment_version));
        return <article key={`${expert.environment_id}-${expert.environment_version}-${index}`}><header><div><span>{environmentTypeLabel(pick(expert,["environment_type"],""))}</span><h3>{environmentNameLabel(pick(expert,["environment_name"],String(expert.environment_id||"")))}</h3></div><Status tone={Number(expert.fitness)>=0?"good":"watch"}>冠军</Status></header><div className="expert-fitness"><strong>{Number(expert.fitness||0).toFixed(5)}</strong><span>环境适应度</span></div><dl><div><dt>环境版本</dt><dd>{String(expert.environment_version||"版本未记录").slice(0,12)}</dd></div><div><dt>策略原型</dt><dd>{String(expert.prototype_id||"原型未记录").slice(-12)}</dd></div><div><dt>因子数</dt><dd>{pick(expert,["factor_count"],Object.keys((expert.factor_weights||{}) as Json).length)}</dd></div><div><dt>备选</dt><dd>{backups.length}</dd></div></dl><FactorWeights weights={expert.factor_weights} labels={factorLabels}/>{backups.length>0&&<footer>备选：{backups.map(row=>`${String(row.prototype_id||"").slice(-8)} (${Number(row.fitness||0).toFixed(4)})`).join(" · ")}</footer>}</article>
      })}</div>:<Empty title="暂无环境专家"/>}
    </Section>

    <Section title="因子组合 × 环境回测矩阵">
      {matrixRows.length?<><div className="matrix-toolbar"><span>共 {matrixRows.length} 条，当前按“{matrixSortLabels[matrixSort.key]}”{matrixSort.direction==="asc"?"升序":"降序"}</span>{matrixRows.length>8&&<button type="button" onClick={()=>setMatrixExpanded(value=>!value)}>{matrixExpanded?"收起矩阵":`展开全部（${matrixRows.length}）`}</button>}</div><div className="table-wrap research-matrix"><table><thead><tr><th>{sortHeader("combination","因子组合")}</th><th>{sortHeader("generation","生成方式")}</th><th>{sortHeader("environment","环境 / 版本")}</th><th>{sortHeader("total_return","组合收益")}</th><th>{sortHeader("benchmark_return","基准收益")}</th><th>{sortHeader("excess_return","超额")}</th><th>{sortHeader("max_drawdown","最大回撤")}</th><th>{sortHeader("turnover_rate","换手")}</th><th>{sortHeader("fitness","环境适应度")}</th><th>{sortHeader("status","状态")}</th></tr></thead><tbody>{visibleMatrixRows.map((row,index)=>{
        const metrics=(row.metrics&&typeof row.metrics==="object"?row.metrics:row) as Json;
        const fitness=pick(row,["environment_fitness.fitness"],null);
        return <tr key={`${row.candidate_id}-${row.environment_id}-${index}`}><td><div className="matrix-factor-set">{Object.entries((row.factor_weights||{}) as Json).sort((a,b)=>Math.abs(Number(b[1]))-Math.abs(Number(a[1]))).map(([key,value])=><span key={key}><b>{factorLabels[key]||factorFallbackLabel(key)}</b><small>{Number(value)>0?"+":""}{Number(value).toFixed(3)}</small></span>)}</div><small>{pick(row,["factor_count"],"因子数未记录")} 因子 · 持有 {pick(row,["holding_period_sessions"],"周期未记录")} 日</small></td><td>{generationModeLabel(row.generation_mode)}</td><td><strong>{environmentNameLabel(pick(row,["environment_name"],String(row.environment_id||"")))}</strong><small>{environmentTypeLabel(row.environment_type)} · {String(row.environment_version||"版本未记录").slice(0,12)}</small></td><td>{fmtPct(pick(metrics,["total_return"]))}</td><td>{fmtPct(pick(metrics,["benchmark_return"]))}</td><td className={metricTone(pick(metrics,["excess_return"]))==="good"?"positive":"negative"}>{fmtPct(pick(metrics,["excess_return"]))}</td><td>{fmtPct(pick(metrics,["max_drawdown"]))}</td><td>{fmtPct(pick(metrics,["turnover_rate"]))}</td><td><strong>{fitness===null?"风险证据":Number(fitness).toFixed(5)}</strong></td><td><Status>{researchResultStatusLabel(pick(row,["status"],pick(row,["candidate_status"],"等待")))}</Status></td></tr>
      })}</tbody></table></div></>:<Empty title="暂无环境回测记录"/>}
    </Section>

    <Section title="因子基因簇">{clusters.length?<div className="research-cluster-grid">{clusters.map((cluster,index)=><article key={String(cluster.cluster_id||index)}><header><span>基因簇 {index+1}</span><Status>{pick(cluster,["member_count"],0)} 个组合</Status></header><strong>{Number(pick(cluster,["representative_fitness"],0)).toFixed(4)}</strong><small>代表 fitness</small><div><b>共同因子</b><p>{firstArray(pick(cluster,["factor_centroid"],{}) as Json,["intersection"]).map(value=>factorLabels[String(value)]||String(value)).join("、")||"尚无共同因子"}</p></div></article>)}</div>:<Empty title="暂无因子基因簇"/>}</Section>
  </>;
}

export function FactorGuidance({ data }: { data: ConsoleData }) {
  const [relationMode,setRelationMode]=useState<"all"|"positive"|"negative">("all");
  const [minimumObservations,setMinimumObservations]=useState(1);
  const [selectedFactor,setSelectedFactor]=useState("");
  const analytics=data.researchAnalytics||{};const guidance=pick(analytics,["guidance"],{}) as Json;
  const factors=firstArray(guidance,["factor_scores"]).slice(0,18);const edges=firstArray(guidance,["interactions"]).slice(0,40);
  const factorDetail=data.researchFactorAnalytics||{};const heatmapRows=firstArray(factorDetail,["heatmap"]);const trendRows=firstArray(factorDetail,["trend"]);
  const registry=rowsFrom(data.factors,["definitions","factors","registry","items"]);
  const labels=Object.fromEntries(registry.map(row=>[String(row.factor_key||""),String(pick(row,["name_cn","name","factor_key"],""))]));
  const contributionRows=factors.map(row=>({
    ...row,
    factor_key_cn:labels[String(row.factor_key)]||factorFallbackLabel(String(row.factor_key)),
    mean_contribution:Number(row.mean_contribution||0).toFixed(6),
    mean_absolute_contribution:Number(row.mean_absolute_contribution||0).toFixed(6),
  }));
  const nodes:Json[]=factors.map((row,index)=>{const angle=(Math.PI*2*index)/Math.max(1,factors.length);return {...row,x:400+Math.cos(angle)*270,y:210+Math.sin(angle)*155}});
  const nodeMap=Object.fromEntries(nodes.map(row=>[String(row.factor_key),row]));
  const visibleEdges=edges.filter(row=>nodeMap[String(row.factor_left)]&&nodeMap[String(row.factor_right)]&&Number(row.observation_count||0)>=minimumObservations&&(relationMode==="all"||(relationMode==="positive"&&Number(row.complementarity)>0)||(relationMode==="negative"&&Number(row.complementarity)<0)));
  const positive=visibleEdges.filter(row=>Number(row.complementarity)>0).slice(0,12);const conflicts=[...visibleEdges].reverse().filter(row=>Number(row.complementarity)<0).slice(0,12);
  const heatFactors=[...new Set(heatmapRows.map(row=>String(row.factor_key)))].slice(0,12);
  const heatEnvironments=[...new Set(heatmapRows.map(row=>String(row.environment_name||row.environment_id)))].slice(0,10);
  const heatValue=(factor:string,environment:string)=>Number(heatmapRows.find(row=>String(row.factor_key)===factor&&String(row.environment_name||row.environment_id)===environment)?.mean_contribution||0);
  const heatMaximum=Math.max(.000001,...heatmapRows.map(row=>Math.abs(Number(row.mean_contribution||0))));
  const selectedRows=selectedFactor?heatmapRows.filter(row=>String(row.factor_key)===selectedFactor):[];
  const selectedEdges=selectedFactor?edges.filter(row=>String(row.factor_left)===selectedFactor||String(row.factor_right)===selectedFactor):[];
  const selectedTrend=selectedFactor?trendRows.filter(row=>String(row.factor_key)===selectedFactor):[];
  return <><PageHeader eyebrow="FACTOR GUIDANCE" title="因子关系与组合指导" summary="实时贡献、交互关系与抽样状态"/>
    <div className="metric-grid headline-metrics"><Metric label="已学习因子" value={factors.length} icon={Sigma}/><Metric label="交互关系" value={edges.length} icon={GitBranch}/><Metric label="指导抽样" value={`${Math.round(Number(pick(guidance,["policy.guided_share"],.7))*100)}%`} icon={ScanSearch}/><Metric label="纯随机探索" value={`${Math.round(Number(pick(guidance,["policy.pure_random_share"],.3))*100)}%`} icon={FlaskConical}/></div>
    <Section title="因子交互图"><div className="research-filterbar"><button className={relationMode==="all"?"active":""} onClick={()=>setRelationMode("all")}>全部关系</button><button className={relationMode==="positive"?"active":""} onClick={()=>setRelationMode("positive")}>只看互补</button><button className={relationMode==="negative"?"active":""} onClick={()=>setRelationMode("negative")}>只看冲突</button><label>最低样本 <input type="number" min="1" value={minimumObservations} onChange={event=>setMinimumObservations(Math.max(1,Number(event.target.value)||1))}/></label></div>{nodes.length?<div className="factor-graph"><svg viewBox="0 0 800 420">{visibleEdges.map((edge,index)=>{const left=nodeMap[String(edge.factor_left)],right=nodeMap[String(edge.factor_right)];return <line key={index} className={Number(edge.complementarity)<0?"negative":""} x1={left.x} y1={left.y} x2={right.x} y2={right.y} strokeWidth={Math.min(4,1+Math.abs(Number(edge.complementarity))*20)}/>})}{nodes.map((node,index)=>{const radius=Math.min(22,8+Math.abs(Number(node.mean_absolute_contribution||0))*100);const key=String(node.factor_key);return <g className={`factor-node ${selectedFactor===key?"selected":""}`} key={key||String(index)} onClick={()=>setSelectedFactor(key)}><circle cx={node.x} cy={node.y} r={radius}/><text x={node.x} y={node.y+radius+12} textAnchor="middle">{labels[key]||key}</text></g>})}</svg></div>:<Empty title="等待首批因子贡献证据"/>}</Section>
    <Section title="因子 × 环境贡献热力图">{heatFactors.length&&heatEnvironments.length?<div className="heatmap-wrap"><table><thead><tr><th>因子</th>{heatEnvironments.map(environment=><th key={environment}>{environmentNameLabel(environment)}</th>)}</tr></thead><tbody>{heatFactors.map(factor=><tr key={factor}><th><button onClick={()=>setSelectedFactor(factor)}>{labels[factor]||factorFallbackLabel(factor)}</button></th>{heatEnvironments.map(environment=>{const value=heatValue(factor,environment);const opacity=Math.min(.85,.12+Math.abs(value)/heatMaximum*.73);return <td key={environment} title={`${value.toFixed(6)}`} style={{background:value>=0?`rgba(182,59,59,${opacity})`:`rgba(23,113,83,${opacity})`}}>{value.toFixed(4)}</td>})}</tr>)}</tbody></table></div>:<Empty title="等待环境贡献证据"/>}</Section>
    <div className="two-column"><Section title="因子边际贡献账本"><CompactTable rows={contributionRows} columns={[["因子","factor_key_cn"],["样本","observations"],["平均贡献","mean_contribution"],["绝对贡献","mean_absolute_contribution"]]}/></Section><Section title="当前指导概率"><CompactTable rows={Object.entries((guidance.sampling_probabilities||{}) as Json).sort((a,b)=>Number(b[1])-Number(a[1])).map(([factor_key,probability])=>({factor_key:labels[factor_key]||factorFallbackLabel(factor_key),probability:Number(probability).toFixed(4)}))} columns={[["因子","factor_key"],["抽样概率","probability"]]}/></Section></div>
    <div className="guidance-grid"><article><h3>优先互补</h3><p>{positive.map(row=>`${labels[String(row.factor_left)]||row.factor_left} × ${labels[String(row.factor_right)]||row.factor_right}`).join("；")||"等待证据"}</p></article><article><h3>限制冲突</h3><p>{conflicts.map(row=>`${labels[String(row.factor_left)]||row.factor_left} × ${labels[String(row.factor_right)]||row.factor_right}`).join("；")||"等待证据"}</p></article></div>
    {selectedFactor&&<aside className="research-detail-drawer"><header><div><small>因子详情</small><h2>{labels[selectedFactor]||factorFallbackLabel(selectedFactor)}</h2></div><button onClick={()=>setSelectedFactor("")}>关闭</button></header><div className="metric-grid"><Metric label="环境证据" value={selectedRows.length}/><Metric label="交互关系" value={selectedEdges.length}/><Metric label="历史 Wave" value={selectedTrend.length}/><Metric label="平均贡献" value={(selectedRows.reduce((sum,row)=>sum+Number(row.mean_contribution||0),0)/Math.max(1,selectedRows.length)).toFixed(6)}/></div><CompactTable rows={selectedRows} columns={[["环境","environment_name","environment_id"],["平均贡献","mean_contribution"],["样本","observations"]]}/><CompactTable rows={selectedEdges.slice(0,20)} columns={[["左因子","factor_left"],["右因子","factor_right"],["互补度","complementarity"],["样本","observation_count"]]}/></aside>}
  </>;
}

export function ResearchEfficiency({ data }: { data: ConsoleData }) {
  const analytics=data.researchAnalytics||{};const metrics=firstArray(analytics,["runtime_metrics"]);const cache=pick(analytics,["cache"],{}) as Json;
  const timeseries=data.researchTimeseries||{};const metricItems=firstArray(timeseries,["items"]);const earlyStops=firstArray(timeseries,["early_stops"]);const slowCandidates=firstArray(timeseries,["slow_candidates"]);const efficiency=firstArray(timeseries,["efficiency"]);
  const waves=firstArray(analytics,["waves"]);const tasks=rowsFrom(data.evolutionTasks,["items","tasks"]);const activeTasks=tasks.filter(row=>["queued","running","retry_wait"].includes(String(row.status)));
  const activeWave=activeTasks.length?waves.find(row=>["queued","running"].includes(String(row.status||row.wave_status))):undefined;const completed=tasks.filter(row=>String(row.status)==="completed");const failed=tasks.filter(row=>["failed","cancelled"].includes(String(row.status)));
  const metricPresentation=(metricType:unknown,value:unknown)=>{
    const key=String(metricType||"");const numberValue=Number(value||0);
    if(key==="cache_hit_ratio")return{label:"缓存命中率",description:"已有回测结果被直接复用的比例，越高越节省计算",display:`${(numberValue*100).toFixed(2)}%`,width:Math.min(100,Math.max(0,numberValue*100))};
    if(key==="candidate_runtime_seconds")return{label:"单个候选平均耗时",description:"完成一个因子组合环境回测的平均时间，越低越快",display:`${numberValue.toFixed(1)} 秒`,width:Math.min(100,Math.max(0,numberValue/600*100))};
    return{label:key||"未知指标",description:"研究引擎记录的运行观测值",display:numberValue.toFixed(4),width:Math.min(100,Math.abs(numberValue))};
  };
  const runtimeTrend=metricItems.filter(row=>String(row.metric_type)==="candidate_runtime_seconds").sort((a,b)=>String(a.observed_at).localeCompare(String(b.observed_at)));
  const cacheTrend=metricItems.filter(row=>String(row.metric_type)==="cache_hit_ratio").sort((a,b)=>String(a.observed_at).localeCompare(String(b.observed_at)));
  const quota=(activeWave?.quota||{}) as Json;const checkpoint=(activeWave?.checkpoint||{}) as Json;
  const waveElapsed=activeWave?Math.max(0,(Date.now()-new Date(String(activeWave.wave_created_at||activeWave.created_at)).getTime())/1000):0;
  return <><PageHeader eyebrow="EVOLUTION EFFICIENCY" title="进化效率" summary="模型与因子实验的计算、缓存和任务状态"/>
    <div className="metric-grid headline-metrics"><Metric label="可复用回测结果" value={Number(cache.entries||0)} note="缓存中保存的结果数量"/><Metric label="累计复用次数" value={Number(cache.hits||0)} note="避免重复回测的次数"/><Metric label="已完成实验任务" value={completed.length}/><Metric label="失败 / 取消任务" value={failed.length}/></div>
    <Section title="运行指标">{metrics.length?<div className="metric-bars explained">{metrics.map((row,index)=>{const metric=metricPresentation(row.metric_type,row.average);return <div key={String(row.metric_type||index)} title={metric.description}><b>{metric.label}<small>{metric.description}</small></b><span><i style={{width:`${metric.width}%`}}/></span><strong>{metric.display}</strong></div>})}</div>:<Empty title="等待首批新版运行指标"/>}</Section>
    <div className="two-column"><Section title="单个候选回测耗时趋势"><MiniTrend rows={runtimeTrend} valueKey="value" format="seconds"/></Section><Section title="缓存命中率趋势"><MiniTrend rows={cacheTrend} valueKey="value" format="percent"/></Section></div>
    <Section title="单位计算收益"><CompactTable rows={efficiency.slice(-30).reverse()} columns={[["候选","candidate_id"],["环境","environment_count"],["平均适应度","mean_fitness"],["耗时（秒）","runtime_seconds"],["单位计算收益","fitness_per_compute_second"],["时间","observed_at"]]}/></Section>
    <div className="two-column"><Section title="缓存与复用"><dl className="fact-list"><div><dt>候选数</dt><dd>{Number(cache.candidates||0)}</dd></div><div><dt>环境版本</dt><dd>{Number(cache.environment_versions||0)}</dd></div><div><dt>缓存键</dt><dd>候选 × 环境 × 快照 × 成本</dd></div></dl></Section><Section title="执行能力"><dl className="fact-list"><div><dt>分层预算</dt><dd>{pick(analytics,["execution.tiered_budget"],false)?"已启用":"未启用"}</dd></div><div><dt>失败早停</dt><dd>{pick(analytics,["execution.catastrophic_early_stop"],false)?"已启用":"未启用"}</dd></div><div><dt>环境并发上限</dt><dd>{pick(analytics,["execution.parallelism"],1)}</dd></div></dl></Section></div>
    <Section title="当前 Wave 资源预算">{activeWave?<div className="metric-grid"><Metric label="候选进度" value={`${Number(activeWave.completed_count||0)+Number(activeWave.failed_count||0)}/${Number(activeWave.expected_count||0)}`}/><Metric label="运行时间" value={`${Math.round(waveElapsed)} 秒`} note={`上限 ${Number(quota.maximum_runtime_seconds||0)} 秒`}/><Metric label="重试上限" value={Number(quota.maximum_retries_per_candidate||0)}/><Metric label="检查点候选" value={Object.keys((checkpoint.completed_environment_ids||{}) as Json).length}/></div>:<Empty title="暂无运行中 Wave"/>}</Section>
    <div className="two-column"><Section title="早停候选"><CompactTable rows={earlyStops} columns={[["候选","candidate_id"],["环境","environment_name","environment_id"],["状态","status"],["原因","early_stop_contract"],["时间","updated_at"]]}/></Section><Section title="慢任务排行"><CompactTable rows={slowCandidates} columns={[["候选","candidate_id"],["耗时（秒）","value"],["Wave","wave_id"],["时间","observed_at"]]}/></Section></div>
    <Section title="最近实验任务"><CompactTable rows={tasks.slice(0,40)} columns={[["任务","id"],["类型","task_type"],["状态","status"],["阶段","phase"],["消息","message"],["更新时间","updated_at"]]}/></Section>
  </>;
}

export function ResearchWaves({ data }: { data: ConsoleData }) {
  const [selectedWave,setSelectedWave]=useState("");
  const [waveDetail,setWaveDetail]=useState<Json>({});
  const [waveLoading,setWaveLoading]=useState(false);
  const [waveError,setWaveError]=useState("");
  const [waveOffset,setWaveOffset]=useState(0);
  const [historyOffset,setHistoryOffset]=useState(0);
  const analytics=data.researchAnalyticsConsole||data.researchAnalytics||{};const waves=firstArray(analytics,["waves"]);const experts=firstArray(analytics,["expert_governance"]);
  const expertHistory=firstArray(data.researchExpertHistory,["items"]);const similarities=firstArray(data.researchEnvironmentSimilarity,["items"]);const archive=data.researchArchive||{};
  const wavePageSize=8;const historyPageSize=20;const visibleWaves=waves.slice(waveOffset,waveOffset+wavePageSize);const visibleExpertHistory=expertHistory.slice(historyOffset,historyOffset+historyPageSize);
  useEffect(()=>{if(!selectedWave){setWaveDetail({});setWaveError("");return}let active=true;const controller=new AbortController();setWaveLoading(true);setWaveError("");readApi(`/api/factors/research/analytics/waves/${encodeURIComponent(selectedWave)}`,controller.signal,60_000).then(value=>{if(active)setWaveDetail(value)}).catch(error=>{if(active&&String(error?.name)!=="AbortError"){setWaveDetail({});setWaveError(`Wave 明细读取失败：${String(error?.message||error)}`)}}).finally(()=>{if(active)setWaveLoading(false)});return()=>{active=false;controller.abort()}},[selectedWave]);
  useEffect(()=>{if(waveOffset>=waves.length)setWaveOffset(0)},[waveOffset,waves.length]);
  useEffect(()=>{if(historyOffset>=expertHistory.length)setHistoryOffset(0)},[expertHistory.length,historyOffset]);
  const expiryLabel=(value:unknown)=>String(value)==="expired"?"已过期":String(value)==="fresh"?"有效":String(value||"状态未记录");
  const expertRows=experts.map(row=>({...row,expiry_status_cn:expiryLabel(row.expiry_status)}));
  const similarityEnvironments=[...new Set(similarities.flatMap(row=>[String(row.environment_left),String(row.environment_right)]))].slice(0,12);
  const similarityValue=(left:string,right:string)=>left===right?1:Number(similarities.find(row=>(String(row.environment_left)===left&&String(row.environment_right)===right)||(String(row.environment_left)===right&&String(row.environment_right)===left))?.champion_jaccard||0);
  const detailCandidates=firstArray(waveDetail,["candidates"]);
  return <><PageHeader eyebrow="EXPERIMENT WAVES" title="实验批次与专家" summary="模型与因子实验的批次、检查点和专家状态"/>
    <div className="metric-grid headline-metrics"><Metric label="实验批次" value={waves.length}/><Metric label="运行中" value={waves.filter(row=>["queued","running"].includes(String(row.wave_status))).length}/><Metric label="已蒸馏" value={waves.filter(row=>String(row.wave_status)==="distilled").length}/><Metric label="过期专家" value={experts.filter(row=>String(row.expiry_status)==="expired").length}/></div>
    <Section title="实验 Wave">{waves.length?<><div className="wave-grid">{visibleWaves.map((wave,index)=><article className={selectedWave===String(wave.wave_id)?"selected":""} key={String(wave.wave_id||index)}><header><h3>{String(wave.wave_id||"Wave 编号未记录").slice(-16)}</h3><Status>{String(wave.wave_status||"unknown")}</Status></header><dl><div><dt>完成</dt><dd>{Number(wave.completed_count||0)}/{Number(wave.expected_count||0)}</dd></div><div><dt>失败</dt><dd>{Number(wave.failed_count||0)}</dd></div><div><dt>冻结哈希</dt><dd>{String(wave.freeze_hash||"哈希未记录").slice(0,8)}</dd></div></dl><button className="detail-button" onClick={()=>setSelectedWave(current=>current===String(wave.wave_id)?"":String(wave.wave_id))}>{selectedWave===String(wave.wave_id)?"收起候选":"查看候选与环境"}</button><EvidenceDetails title="冻结合同、配额与检查点"><pre>{JSON.stringify({freeze:wave.freeze,quota:wave.quota,checkpoint:wave.checkpoint},null,2)}</pre></EvidenceDetails></article>)}</div><ReadOnlyPager label="实验 Wave" offset={waveOffset} pageSize={wavePageSize} total={waves.length} hasMore={waveOffset+wavePageSize<waves.length} loading={false} onChange={offset=>{setSelectedWave("");setWaveOffset(offset)}}/></>:<Empty title="暂无 Wave 冻结证据"/>}</Section>
    {selectedWave&&<Section title="Wave 候选与环境检查点">{waveLoading?<Empty title="正在读取 Wave 明细"/>:waveError?<Empty title={waveError}/>:detailCandidates.length?<div className="wave-candidate-list">{detailCandidates.map((candidate,index)=><article key={String(candidate.candidate_id||index)}><header><div><strong>{String(candidate.candidate_id||"未记录").slice(-16)}</strong><small>{generationModeLabel(candidate.generation_mode)} · {candidate.factor_count} 因子</small></div><Status>{researchResultStatusLabel(candidate.status)}</Status></header><div className="candidate-environment-grid">{firstArray(candidate,["environments"]).map((environment,environmentIndex)=><div key={String(environment.environment_id||environmentIndex)}><Status tone={String(environment.status)==="completed"?"good":String(environment.status).includes("failed")?"danger":"neutral"}>{researchResultStatusLabel(environment.status)}</Status><strong>{environmentNameLabel(environment.environment_name||environment.environment_id)}</strong><small>{environment.cache_hit_count?`缓存复用 ${environment.cache_hit_count}`:"未命中缓存"} · {environment.fitness==null?"等待适应度":Number(environment.fitness).toFixed(5)}</small></div>)}</div></article>)}</div>:<Empty title="暂无候选明细"/>}</Section>}
    <Section title="环境专家稳定性与过期"><CompactTable rows={expertRows} columns={[["环境","environment_id"],["专家","prototype_id"],["版本","environment_version"],["适应度","fitness"],["稳定度","ranking_stability"],["年龄（日）","age_days"],["状态","expiry_status_cn"]]}/></Section>
    <Section title="专家跨版本排名" detail="按观测时间倒序读取最近 100 条，每页只渲染 20 条。"><CompactTable rows={visibleExpertHistory} columns={[["环境","environment_name","environment_id"],["版本","environment_version"],["专家","prototype_id"],["名次","rank_no"],["角色","role"],["适应度","fitness"],["时间","observed_at"]]}/>{expertHistory.length>0&&<ReadOnlyPager label="专家历史" offset={historyOffset} pageSize={historyPageSize} total={expertHistory.length} hasMore={historyOffset+historyPageSize<expertHistory.length} loading={false} onChange={setHistoryOffset}/>}</Section>
    <Section title="环境相似度矩阵">{similarityEnvironments.length?<div className="heatmap-wrap similarity-matrix"><table><thead><tr><th>环境</th>{similarityEnvironments.map(environment=><th key={environment}>{environment.slice(-6)}</th>)}</tr></thead><tbody>{similarityEnvironments.map(left=><tr key={left}><th>{left.slice(-10)}</th>{similarityEnvironments.map(right=>{const value=similarityValue(left,right);return <td key={right} title={`${left} × ${right}: ${value.toFixed(3)}`} style={{background:`rgba(37,99,169,${.08+value*.78})`}}>{value.toFixed(2)}</td>})}</tr>)}</tbody></table></div>:<Empty title="等待跨环境重合证据"/>}<CompactTable rows={similarities.slice(0,30).map(row=>({...row,recommendation_cn:String(row.recommendation)==="merge_review"?"合并复核":"降低重合权重"}))} columns={[["环境 A","environment_left"],["环境 B","environment_right"],["相似度","champion_jaccard"],["研究权重","research_weight_multiplier"],["建议","recommendation_cn"]]}/></Section>
    <Section title="归档状态"><div className="metric-grid"><Metric label="运行明细" value={Number(pick(archive,["runtime_detail.count"],0))} note={Number(pick(archive,["runtime_detail.count"],0))>0?`${pick(archive,["runtime_detail.earliest"],"起始日未记录")} → ${pick(archive,["runtime_detail.latest"],"结束日未记录")}`:"尚无运行明细"}/><Metric label="每日汇总" value={Number(pick(archive,["daily_rollups.count"],0))} note={Number(pick(archive,["daily_rollups.count"],0))>0?`${pick(archive,["daily_rollups.earliest"],"起始日未记录")} → ${pick(archive,["daily_rollups.latest"],"结束日未记录")}`:"尚无每日汇总"}/><Metric label="环境回测" value={Number(pick(archive,["environment_backtests.count"],0))} note={Number(pick(archive,["environment_backtests.count"],0))>0?`${pick(archive,["environment_backtests.earliest"],"起始日未记录")} → ${pick(archive,["environment_backtests.latest"],"结束日未记录")}`:"尚无环境回测"}/><Metric label="明细保留" value={`${Number(archive.retention_days||30)} 日`}/></div></Section>
    <Section title="可比性合同"><EvidenceDetails title="查看比较维度"><pre>{JSON.stringify(pick(analytics,["comparability_gate"],{}),null,2)}</pre></EvidenceDetails></Section>
  </>;
}


function FactorWeights({weights,labels={}}:{weights:unknown,labels?:Record<string,string>}){const rows=weights&&typeof weights==="object"&&!Array.isArray(weights)?Object.entries(weights as Json).sort((a,b)=>Math.abs(Number(b[1]))-Math.abs(Number(a[1]))):[];return rows.length?<div className="factor-weights">{rows.map(([key,value])=><div key={key}><span><b>{labels[key]||factorFallbackLabel(key)}</b><small>{key}</small></span><strong>{Number(value)>0?"+":""}{Number(value).toFixed(3)}</strong></div>)}</div>:<Empty title="暂无因子权重" detail="组合尚未返回权重合同。"/>}
function MiniTrend({rows,valueKey,format}:{rows:Json[],valueKey:string,format:"seconds"|"percent"}){if(!rows.length)return <Empty title="等待趋势数据"/>;const values=rows.map(row=>Number(row[valueKey]||0));const low=Math.min(...values),high=Math.max(...values);const latest=values[values.length-1];const display=(value:number)=>format==="percent"?`${(value*100).toFixed(2)}%`:`${value.toFixed(1)} 秒`;const coordinates=values.map((value,index)=>({x:20+index/Math.max(1,values.length-1)*560,y:170-(value-low)/Math.max(.0000001,high-low)*130}));const points=coordinates.map(point=>`${point.x},${point.y}`).join(" ");const firstPoint=coordinates[0],lastPoint=coordinates[coordinates.length-1];const areaPoints=`${firstPoint.x},170 ${points} ${lastPoint.x},170`;return <div className="mini-trend"><svg viewBox="0 0 600 190" role="img" aria-label={`最低 ${display(low)}，最新 ${display(latest)}，最高 ${display(high)}`}>{[40,105,170].map(y=><line className="trend-grid" key={y} x1="20" y1={y} x2="580" y2={y}/>) }<polygon className="trend-area" points={areaPoints}/><polyline className="trend-line" points={points}/><circle className="trend-point" cx={lastPoint.x} cy={lastPoint.y} r="4"/></svg><footer><span>最低 {display(low)}</span><strong>最新 {display(latest)}</strong><span>最高 {display(high)}</span></footer></div>}
function factorFallbackLabel(key:string){const words:Record<string,string>={earnings:"盈利",yield:"收益率",book:"账面",price:"价格",value:"价值",lowvol:"低波动",interaction:"交互",short:"短期",reversal:"反转",medium:"中期",momentum:"动量",intraday:"日内",position:"位置",total:"总",mv:"市值",volume:"成交量",turnover:"换手",volatility:"波动率",ratio:"比率",beta:"贝塔",size:"规模",range:"振幅",contraction:"收缩",open:"开盘",close:"收盘",corr:"相关"};return key.split("_").filter(part=>part!=="alpha"&&part!=="dsl").map(part=>words[part]||part).join("")||key}
function researchStageLabel(value:unknown){return ({quick_screen:"快速筛选",confirmation:"多窗口复筛",running:"研究运行中",budget_exhausted:"本批次完成",not_started:"尚未开始"} as Record<string,string>)[String(value)]||String(value||"未知")}
function generationModeLabel(value:unknown){return ({random_exploration:"随机探索",prototype_mutation:"原型变异",cross_cluster_crossover:"跨簇交叉",targeted_gap_fill:"缺口定向补全"} as Record<string,string>)[String(value)]||String(value||"未记录")}
function researchTaskStatusLabel(value:unknown){return ({queued:"排队",running:"运行中",retry_wait:"等待重试",completed:"完成",failed:"失败",cancelled:"已取消"} as Record<string,string>)[String(value)]||String(value||"未启动")}
function researchTaskTypeLabel(value:unknown){return ({factor_research_cycle:"候选研究",strategy_candidate_generation:"候选生成",strategy_environment_backtest:"环境回测",strategy_cluster_distillation:"策略簇蒸馏",strategy_portfolio_research:"策略组合",strategy_portfolio_formal_queue:"正式队列"} as Record<string,string>)[String(value)]||String(value||"未记录")}
function researchLineName(value:unknown){const type=String(value||"");if(["factor_research_cycle","strategy_candidate_generation","strategy_environment_backtest"].includes(type))return "候选生成线";if(type==="strategy_cluster_distillation")return "策略簇蒸馏线";if(["strategy_portfolio_research","strategy_portfolio_formal_queue"].includes(type))return "策略组合线";return researchTaskTypeLabel(type)}
function formatLogTime(value:unknown,fallback="未记录"){const text=String(value||"");if(!text)return fallback;return text.replace("T"," ").replace(/([+-]\\d\\d:\\d\\d|Z)$/,"").split(".")[0]}

function ScoreBar({value}:{value:unknown}){const score=Math.max(0,Math.min(100,Number(value)||0));return <div className="score-cell"><strong>{score.toFixed(1)}</strong><span><i style={{width:`${score}%`}}/></span></div>}
function industryConclusion(row:Json){if(row.eligible===true)return "已准入";if(Array.isArray(row.blockers)&&row.blockers.length)return "未准入";return scalarLabel(pick(row,["status","state.status"],"证据不足"),"证据不足")}

export function Research({ data }: { data: ConsoleData }) {
  const [view,setView]=useState<"industries"|"themes"|"pools"|"events">("industries");const theses=firstArray(data.theses,["items","theses"]);const validIndustry=(row:Json)=>{const label=String(pick(row,["industry","industry_name","industry_code"],"")).trim().toLowerCase();return Boolean(label)&&label!=="nan"};const industries=firstArray(data.industries,["items","industries"]).filter(validIndustry);const industryByName=new Map(industries.map(row=>[String(pick(row,["industry","industry_name"],"")),row]));const themes=firstArray(data.themes,["items","themes"]);const events=firstArray(data.events,["items","events"]);const pools=firstArray(data.pools,["items","members","pools"]);const allocation=firstArray(data.industryAllocation,["items"]).filter(validIndustry);const industryRows=(allocation.length?allocation:industries).map(row=>{const detail=industryByName.get(String(pick(row,["industry","industry_name"],"")))||{};return {...detail,...row,state_as_of_date:pick(row,["state_as_of_date"],pick(detail,["state_as_of_date","as_of_date","updated_at"],""))}}).sort((a,b)=>Number(pick(b,["combined_score","state.score"],0))-Number(pick(a,["combined_score","state.score"],0)));const themeRows=themes.slice().sort((a,b)=>Number(b.score||0)-Number(a.score||0));const poolRows:Json[]=pools.map(row=>{const match=String(row.reason||"").match(/research_score=([\d.]+)/);return {...row,research_score:Number(pick(row,["research_state.score","score"],match?.[1]||0))}}).sort((a,b)=>b.research_score-a.research_score);const eventRows=events.slice().sort((a,b)=>Number(pick(b,["impact.decayed_score","impact.score","score"],0))-Number(pick(a,["impact.decayed_score","impact.score","score"],0)));const marketState=pick(data.modelMarketLive,["market_state.label","market_state.state","state.label","state.state"],"证据缺失");const professionalMetrics=pick(data.researchDataAssets,["professional_operating_metric_candidates"],{}) as Json;const professionalGroups=firstArray(professionalMetrics,["candidate_groups"]);const consensusRows=rowsFrom(data.analystConsensus,["items","stocks","consensus","samples"]);
  const topIndustries=industryRows.slice(0,8);const topTheme=themeRows[0];const topPool=poolRows[0];const topEvent=eventRows[0];const industryScore=(row:Json)=>Math.max(0,Math.min(100,Number(pick(row,["combined_score","state.score"],0))));
  return <><PageHeader eyebrow="RESEARCH" title="研究中心" summary="比较行业、板块、主题和股票研究评分，并下钻到点时证据与失效条件。"/><div className="metric-grid headline-metrics research-headline-metrics"><Metric label="市场状态" value={<Status>{marketState}</Status>} note={pick(data.modelMarketLive,["trade_date","as_of_date"],"未记录")} icon={Gauge}/><Metric label="可评价行业" value={industries.filter(row=>pick(row,["state.status"],"")==="ready").length} note={`共 ${industries.length} 个行业`} icon={Building2}/><Metric label="可晋升主题" value={themes.filter(row=>Boolean(row.promotable)).length} note={`共 ${themes.length} 个主题`} icon={Sparkles}/><Metric label="研究股票池" value={pools.length} note={`${theses.length} 条投资逻辑`} icon={ListTree}/><Metric label="年报经营候选" value={Number(pick(professionalMetrics,["candidates"],0))} note={`${Number(pick(professionalMetrics,["documents"],0))} 份PDF；未审批不进选股`} icon={FileSearch}/></div><div className="research-dashboard-grid"><section className="research-ranking-panel"><header><div><span>行业强度</span><h2>领先行业与板块</h2></div><Status>{pick(data.modelMarketLive,["trade_date","as_of_date"],"时点未记录")}</Status></header>{topIndustries.length?<div className="research-ranking-bars">{topIndustries.map((row,index)=>{const score=industryScore(row);return <div key={String(pick(row,["industry","industry_code","industry_name"],index))}><span><b>{String(index+1).padStart(2,"0")}</b><strong>{pick(row,["industry","industry_name"],"未记录")}</strong></span><i><em style={{width:`${score}%`}}/></i><small>{score.toFixed(1)}</small></div>})}</div>:<Empty title="暂无行业评分"/>}</section><aside className="research-focus-panel"><header><span>当前焦点</span><Status>{marketState}</Status></header><article><span>领先主题</span><strong>{pick(topTheme||{},["theme_name","name","theme_code"],"暂无主题")}</strong><small>{topTheme?`${Number(topTheme.score||0).toFixed(1)} · ${pick(topTheme,["lifecycle"],"生命周期未记录")}`:"等待主题证据"}</small></article><article><span>股票池首位</span><strong>{pick(topPool||{},["name","symbol_name","ts_code","symbol"],"暂无股票")}</strong><small>{topPool?`${pick(topPool,["ts_code","symbol"],"")} · 研究分 ${Number(topPool.research_score||0).toFixed(1)}`:"等待股票池证据"}</small></article><article><span>高影响事件</span><strong>{pick(topEvent||{},["title","event_type"],"暂无事件")}</strong><small>{topEvent?`${pick(topEvent,["ts_code"],"证券未记录")} · ${Number(pick(topEvent,["impact.decayed_score","impact.score","score"],0)).toFixed(1)}`:"等待事件证据"}</small></article></aside></div><div className="research-tabs" role="tablist">{[["industries","行业与板块",Activity],["themes","主题与叙事",FlaskConical],["pools","股票池",BookOpen],["events","事件与反证",Clock3]].map(([key,label,Icon]:any)=><button key={key} className={view===key?"active":""} onClick={()=>setView(key)}><Icon size={16}/><span>{label}</span></button>)}</div>
    {view==="industries"&&<Section title="行业与板块评分" detail="综合分 = 叙事 60% + 行业质量 20% + 候选选择分 20%；预算只分配给通过准入门的行业。"><div className="table-wrap"><table><thead><tr><th>排名</th><th>行业 / 板块</th><th>综合分</th><th>景气分</th><th>叙事分</th><th>覆盖</th><th>生命周期</th><th>预算权重</th><th>更新时间</th><th>结论</th></tr></thead><tbody>{industryRows.map((row,i)=><tr key={String(pick(row,["industry","industry_code","industry_name"],i))}><td>{i+1}</td><td><strong>{pick(row,["industry","industry_name"],"未记录")}</strong><small>{pick(row,["industry_code"],"")}</small></td><td><ScoreBar value={pick(row,["combined_score","state.score"],0)}/></td><td>{Number(pick(row,["raw_score","state.score"],0)).toFixed(1)}</td><td>{Number(pick(row,["narrative_score"],0)).toFixed(1)}</td><td>{fmtPct(pick(row,["coverage","state.coverage"]),false)}</td><td>{pick(row,["narrative_lifecycle","lifecycle","state.lifecycle"],"证据不足")}</td><td>{fmtPct(pick(row,["target_weight"],0),false)}</td><td>{pick(row,["updated_at","as_of_date","state_as_of_date","trade_date","state.updated_at"],"未记录")}</td><td><Status>{industryConclusion(row)}</Status></td></tr>)}</tbody></table></div></Section>}
    {view==="themes"&&<Section title="主题与叙事评分" detail="按主题分数、生命周期、可晋升状态和公司映射判断主线强度。"><div className="research-score-grid">{themeRows.map((row,i)=><article key={String(row.theme_code||i)}><header><div><span>#{i+1}</span><h3>{pick(row,["theme_name","name","theme_code"],"未命名主题")}</h3><small>{row.theme_code}</small></div><Status>{row.promotable?"可晋升":pick(row,["lifecycle"],"观察")}</Status></header><ScoreBar value={row.score}/><dl><div><dt>生命周期</dt><dd>{pick(row,["lifecycle"],"未记录")}</dd></div><div><dt>映射公司</dt><dd>{Number(pick(row,["company_count"],Array.isArray(row.companies)?row.companies.length:0))}</dd></div><div><dt>行业</dt><dd>{pick(row,["payload.knowledge_graph.industry"],"未记录")}</dd></div><div><dt>时点</dt><dd>{pick(row,["as_of_date"],"未记录")}</dd></div></dl><ThemeEvidence themeCode={String(row.theme_code||"")} asOfDate={String(row.as_of_date||"")}/></article>)}</div></Section>}
    {view==="pools"&&<Section title="股票池研究评分" detail="观察池、核心候选和持仓池只读展示；评分不足不会因进入列表而自动升级。"><div className="table-wrap"><table><thead><tr><th>排名</th><th>股票</th><th>研究分</th><th>池类型</th><th>状态</th><th>准入理由</th><th>更新时间</th></tr></thead><tbody>{poolRows.map((row,i)=>{const code=String(pick(row,["ts_code","symbol"],""));const name=pick(row,["name","symbol_name"],"");return <tr key={String(row.id||i)}><td>{i+1}</td><td><strong>{name||code||"未记录"}</strong>{name&&code&&<small>{code}</small>}</td><td><ScoreBar value={row.research_score}/></td><td>{pick(row,["pool_type"],"未记录")}</td><td><Status>{researchStatus(row)}</Status></td><td>{pick(row,["reason"],"未记录")}</td><td>{pick(row,["updated_at","entered_at"],"未记录")}</td></tr>})}</tbody></table></div></Section>}
    {view==="events"&&<Section title="事件影响与反证" detail="按衰减后影响分排序；负面事件和到期复核必须回到投资逻辑。"><div className="table-wrap"><table><thead><tr><th>股票</th><th>事件</th><th>方向</th><th>影响分</th><th>置信度</th><th>跟踪状态</th><th>下一步</th></tr></thead><tbody>{eventRows.map((row,i)=><tr key={String(row.id||i)}><td>{pick(row,["ts_code"],"未记录")}</td><td><strong>{pick(row,["title","event_type"],"未记录")}</strong><small>{pick(row,["event_date"],"")}</small></td><td>{pick(row,["impact_direction"],"未记录")}</td><td><ScoreBar value={pick(row,["impact.decayed_score","impact.score","score"],0)}/></td><td>{fmtPct(pick(row,["confidence"]),false)}</td><td><Status>{pick(row,["follow_up.status","status"],"未跟踪")}</Status></td><td>{pick(row,["follow_up.next_action","summary"],"未记录")}</td></tr>)}</tbody></table></div></Section>}
    <Section title="专业经营指标候选" detail="巨潮年报原文抽取与校验状态。"><CompactTable rows={professionalGroups} columns={[["企业模板","template_kind"],["指标","metric_key"],["校验状态","validation_status","status"],["数量","count"]]}/></Section><ResearchDrilldown pools={pools} industries={industries} initialStockEvidence={data.researchStockEvidence} initialIndustryEvidence={data.researchIndustryEvidence}/><div className="two-column"><Section title="投资逻辑状态"><CompactTable rows={theses.slice(0,20)} columns={[["股票","ts_code"],["逻辑","title","hypothesis"],["状态","derived_status","status"],["置信度","derived_confidence","confidence"],["复核","review_date","next_review_at"]]}/></Section><Section title="一致预期">{consensusRows.length?<CompactTable rows={consensusRows.slice(0,20)} columns={[["股票","ts_code","name"],["状态","status"],["评级","rating","consensus_rating"],["目标价","target_price_avg","target_price"]]}/>:<Empty title="暂无多机构一致预期" detail={`${pick(data.analystConsensus,["with_consensus"],0)} 只股票达到当前口径。`}/>}</Section></div><Section title="研究治理与合同"><div className="two-column"><EvidenceDetails title="数据资产与能力缺口"><pre>{JSON.stringify({assets:data.researchDataAssets,governance:data.researchGovernance},null,2)}</pre></EvidenceDetails><EvidenceDetails title="持仓状态与评分合同"><pre>{JSON.stringify({positionState:data.researchPositionState,contracts:data.researchContracts},null,2)}</pre></EvidenceDetails></div></Section></>;
}

function rowsFrom(source: Json|undefined, keys:string[]):Json[]{const direct=firstArray(source,keys);if(direct.length)return direct;if(source&&typeof source==="object")return Object.entries(source).filter(([,v])=>v&&typeof v==="object").map(([key,value])=>({key,...value as Json}));return []}
function objectRows(source: unknown):Json[]{return source&&typeof source==="object"&&!Array.isArray(source)?Object.entries(source as Json).filter(([,value])=>value&&typeof value==="object").map(([key,value])=>({key,...value as Json})):[]}

function apiCatalogRows(source?:Json){const paths=source?.paths&&typeof source.paths==="object"?source.paths:{};const covered=new Set(Object.values(endpoints).map(value=>value.split("?")[0]));return Object.entries(paths).flatMap(([path,methods])=>Object.entries(methods as Json).filter(([method])=>["get","post","put","patch","delete"].includes(method)).map(([method,contract])=>({path,method:method.toUpperCase(),summary:pick(contract as Json,["summary","operationId"],"未记录"),covered:method==="get"&&covered.has(path)?"前端已读取":method==="get"?"待分类/聚合":"后台控制面"})));}

export function FactorLab({data}:{data:ConsoleData}){
  const rows=rowsFrom(data.factors,["definitions","factors","registry","items"]);
  return <><PageHeader eyebrow="FACTOR PLATFORM" title="因子平台" summary="查看注册版本、生产状态、点时物化和未来评价积累。"/><div className="metric-grid headline-metrics"><Metric label="注册因子" value={rows.length}/><Metric label="生产因子" value={pick(data.factors,["active_count"],rows.filter(x=>String(pick(x,["status","stage"]))==="production").length)}/><Metric label="候选因子" value={pick(data.factors,["candidate_count"],rows.filter(x=>String(pick(x,["status","stage"]))==="candidate").length)}/><Metric label="评价记录" value={pick(data.factors,["evaluation_count"],0)}/></div><Section title="注册表" detail="交易约束因子不会参与 Alpha IC 晋升。"><CompactTable rows={rows} columns={[["因子","name_cn","factor_key","name"],["状态","status","stage"],["版本","version","registry_version"],["分类","category"]]}/></Section><EvidenceDetails title="完整因子合同"><pre>{JSON.stringify({registry:data.factors,status:data.factorStatus},null,2)}</pre></EvidenceDetails></>;
}

function modelStatusLabel(value:unknown){return ({production:"生产中",production_current_standard:"生产中（当前硬门通过）",legacy_production:"运行兼容版（待新协议替换）",legacy_invalid_candidate:"旧评价已归档",rejected_candidate:"已驳回",evaluation_archived:"旧评价已归档",evaluation_removed:"旧评价已移除",runtime_compatibility_only:"仅保留推理兼容",outdated:"未按当前协议评价",not_evaluated:"尚未评价",not_started:"尚无训练任务",wait_for_data:"等待数据",wait_for_oos_cohort_advance:"等待样本外窗口自然前移",waiting_for_natural_oos_cohort_advance:"等待样本外窗口自然前移",adaptive_same_cohort_non_promotable:"同批窗口仅供诊断",fresh_unconsumed_formal_oos_cohort:"全新未消费样本外窗口",current:"当前评价协议",candidate:"候选",trainable:"可训练",proxy:"规则代理",queued:"等待训练",running:"训练中",completed:"已完成",failed:"失败",passed:"通过",blocked:"阻断",active:"启用",observing:"观察中",coverage:"覆盖检查",dataset:"数据集构造",training:"模型训练",validation:"样本外验证",failure_recovery:"故障恢复",info:"运行信息",shadow_failed:"影子验证失败",shadow_validation_failed:"影子验证失败",shadow_live_observation:"影子观察中",pending_strategy_ab:"等待冻结策略 A/B",pending_live_observation:"等待 20 日未来观察",pending_enrollment:"等待自治入组",awaiting_first_future_signal:"等待首个未来信号",awaiting_realized_future_return:"等待未来收益形成",extended_for_inconclusive_evidence:"证据不足，自动延长观察",cancelled_stale_contract:"旧合同已隔离",controlled_strategy_ab:"冻结策略 A/B",live_observation_20_sessions:"20 日未来观察",majority_windows_improved:"多数窗口改善",mean_return_delta_positive:"平均增量为正",paired_daily_bootstrap_lower_positive:"Bootstrap 下界为正",degraded:"已降级",ready:"就绪",current_gate_passed:"当前硬门通过",legacy_production_current_gate_not_passed:"运行兼容版未通过当前硬门",not_applicable:"不适用",not_enqueued:"未触发后续研究",queued_for_automatic_recovery:"已进入自动恢复队列",automatic_retry_started:"自动重试中",automatic_retry_not_started:"等待自治复核",resolved_by_existing_candidate_observation:"已有候选观察接管",delegated_to_data_backfill:"已委托数据补全",data_backfill_delegation_failed:"补全委托失败",automatic_retry_exhausted:"自动重试耗尽",escalated_owner_review:"已升级所有者异常",recovery_enqueue_failed:"自动恢复排队失败",online_limited:"限权在线",online_shadow:"在线旁路",not_available:"无可用产物",limited_rank_initial:"初始限权",limited_rank_deweighted:"已自动降权",limited_rank_increased:"观察通过，限权提高",shadow_only:"纯旁路",risk_only:"仅风险参考",full_production:"正式生产",idle:"空闲",paused:"已暂停",waiting:"等待",waiting_schedule:"等待收盘调度",recovery_pending:"等待安全恢复",unknown:"未知",owner:"主实例",follower:"从实例",healthy:"健康",normal:"正常",partial:"部分可用",error:"错误",retry_wait:"等待重试",stopping:"正在停止",alive:"存活"} as Record<string,string>)[String(value||"")]||String(value||"未知")}
const modelEvidenceKeyNames:Record<string,string>={
  training_contract:"训练合同",family:"模型族",objective:"训练目标",metric:"评价指标",tasks:"训练任务",eval_at:"排序评估位置",label_gain:"标签增益",params:"训练参数",
  learning_rate:"学习率",num_leaves:"叶子节点数",max_depth:"最大深度",n_estimators:"迭代轮数",min_child_samples:"叶节点最少样本",feature_fraction:"特征采样比例",bagging_fraction:"样本采样比例",bagging_freq:"样本采样频率",reg_alpha:"L1 正则",reg_lambda:"L2 正则",max_bin:"最大分箱数",random_state:"随机种子",early_stopping_rounds:"提前停止轮数",
  current_gate_audit:"当前硬门审计",schema_version:"数据结构版本",status:"状态",passed:"是否通过",gates:"硬门明细",blocked_reasons:"阻断原因",non_destructive:"不破坏当前生产绑定",
  task_baseline:"同任务基线",comparison_executed:"已执行比较",outperformed:"是否跑赢同任务基线",task_metrics:"任务原生指标",task_native_oos:"任务原生样本外",passed_windows:"通过窗口",baseline_outperformed_windows:"跑赢同任务基线窗口",minimum_passed_windows:"最低通过窗口",
  environment_coverage:"市场环境覆盖",parent_state_count:"父状态总数",covered_parent_states:"已覆盖父状态",profile_count:"市场分项总数",minimum_required_profiles:"最低分项覆盖要求",covered_profiles:"已覆盖市场分项",parent_states_complete:"父状态覆盖完整",profiles_complete:"市场分项覆盖达标",complete:"整体覆盖达标",contract:"评价合同",
  market_state_metrics:"四类市场状态指标",market_profile_metrics:"十二类市场分项指标",observations:"样本数",net_lift:"成本后提升",hit_rate:"命中率",rank_return_corr:"排序收益相关性",
  live_health:"生产运行观察",candidate_observation:"候选未来观察",production_observation:"生产运行观察",model_key:"模型标识",active_version:"生产激活版本",production_active:"是否生产激活",weight_multiplier:"健康权重系数",reason:"原因",evidence_maturity:"证据成熟度",mature_20d_outcomes:"成熟的20日结果",minimum_required:"最低要求",updated_at:"更新时间",
  validation:"验证结论",production_eligible:"可否进入生产",production_eligibility:"生产资格",eligible:"符合资格",artifact:"模型产物",artifact_ready:"产物可用",artifact_type:"产物类型",model_type:"模型类型",model_path_exists:"模型文件存在",features:"输入特征",training_dataset:"训练数据集",predict_contract:"预测合同",output_contract:"输出合同",training_protocol:"训练协议",controlled_strategy_ab:"冻结策略 A/B",fresh_unconsumed_formal_oos_cohort:"全新未消费样本外窗口",lifecycle_effect_authorized:"生命周期决策已获验证集授权",new_event_dates_after_source:"来源版本后新增有效事件日",minimum_new_event_dates:"完整新测试窗最低事件日",complete_new_test_window:"已形成完整新测试窗",event_research_resume_contract:"事件研究续接合同",live_observation_20_sessions:"20 日未来观察",human_review:"最终治理审计",majority_windows_improved:"多数窗口改善",mean_return_delta_positive:"平均增量为正",paired_daily_bootstrap_lower_positive:"Bootstrap 下界为正",five_complete_windows:"五个窗口完整",all_replays_completed:"全部回放完成",paired_dates_complete:"配对交易日完整",candidate_fold_model_actually_consumed:"逐窗实际消费模型",candidate_fold_model_actually_effective:"逐窗模型实际改变策略",candidate_fold_model_effect_contract_current:"模型效果合同为当前版本",worst_drawdown_not_materially_worse:"回撤没有显著恶化",turnover_not_explosive:"换手没有失控",
  fallback:"降级方案",input:"输入",output:"输出",score:"评分",scorer:"评分器",rows:"样本行数",train_rows:"训练行数",validation_rows:"验证行数",test_rows:"测试行数",horizon:"预测周期",start:"开始日期",end:"结束日期",train_end:"训练截止日",validation_start:"验证开始日",validation_end:"验证截止日",test_start:"测试开始日",
};
const modelEvidenceValueNames:Record<string,string>={
  true:"是",false:"否",lambdarank:"排序学习（LambdaRank）",ndcg:"归一化折损累计增益（NDCG）",healthy:"健康",observing:"观察中",degraded:"已降级",failed:"失败",passed:"通过",not_applicable:"不适用",
  current_gate_passed:"当前硬门通过",legacy_production_current_gate_not_passed:"历史生产版未通过当前硬门",legacy_model_evaluation_contract_invalid:"历史评价协议已失效",outdated_event_feature_contract_invalid:"事件特征合同已升级，等待按 V3 重评",legacy_truncated_training_dataset_invalid:"历史训练仅保留少量交易日，结果按当前协议无效",baseline_outperformed:"未跑赢基线",environment_coverage_complete:"市场环境覆盖不完整",walk_forward_windows:"滚动验证窗口不足",walk_forward_positive:"滚动样本外收益未转正",worst_window_positive:"最差窗口收益未转正",artifact_ready:"模型产物不可用",
};
function localizeModelEvidence(value:unknown):unknown{
  if(Array.isArray(value))return value.map(localizeModelEvidence);
  if(value&&typeof value==="object")return Object.fromEntries(Object.entries(value as Json).map(([key,item])=>[modelEvidenceKeyNames[key]||key,localizeModelEvidence(item)]));
  if(typeof value==="boolean")return value?"是":"否";
  if(typeof value==="string")return modelEvidenceValueNames[value]||modelStatusLabel(value);
  return value;
}
function evidenceMetric(value:unknown,kind:"number"|"percent"="number"){
  if(value===null||value===undefined||value==="")return "未记录";
  return kind==="percent"?fmtPct(value):String(value);
}
function ModelEvidencePanel({isHsmm,row,metrics,health,artifact,blockers,gateAudit,hsmmValidation}:{isHsmm:boolean,row:Json,metrics:Json,health:Json,artifact:Json,blockers:unknown[],gateAudit:Json,hsmmValidation:Json}){
  const contract=pick(row,["training_contract"],{}) as Json;const params=pick(contract,["params"],{}) as Json;const taskBaseline=pick(metrics,["task_baseline"],{}) as Json;const taskOos=pick(metrics,["task_native_oos"],{}) as Json;const environment=pick(metrics,["environment_coverage"],{}) as Json;const gates=pick(gateAudit,["gates"],{}) as Json;const validation=isHsmm?hsmmValidation:pick(row,["latest_version.validation"],{}) as Json;const evaluationStatus=String(pick(row,["evaluation_contract_status"],pick(metrics,["evaluation_status"],"not_evaluated")));const hsmmTask=pick(hsmmValidation,["task_native"],{}) as Json;const hsmmFolds=Array.isArray(hsmmTask.walk_forward_evidence)?hsmmTask.walk_forward_evidence as Json[]:[];
  const gateRows=Object.entries(gates).map(([key,value])=>({name:modelEvidenceKeyNames[key]||key,status:Boolean(value)?"通过":"未通过",passed:Boolean(value)}));
  const parameterRows=Object.entries(params).map(([key,value])=>({name:modelEvidenceKeyNames[key]||key,value:Array.isArray(value)?value.join("、"):String(value)}));
  const trainingRows=[
    {name:"模型族",value:pick(contract,["family"],pick(row,["family"],"未登记模型族"))},
    {name:"训练目标",value:modelEvidenceValueNames[String(pick(contract,["objective"],"未登记"))]||pick(contract,["objective"],"未登记")},
    {name:"评价指标",value:modelEvidenceValueNames[String(pick(contract,["metric"],"未登记"))]||pick(contract,["metric"],"未登记")},
    {name:"排序评估位置",value:(pick(contract,["eval_at"],[]) as unknown[]).join("、")||"该模型不使用排序位置"},
    {name:"标签增益",value:(pick(contract,["label_gain"],[]) as unknown[]).join("、")||"该模型不使用标签增益"},
  ];
  return <div className="model-evidence-panel">
    <div className="model-evidence-summary">
      <article><span>当前结论</span><strong>{modelStatusLabel(pick(gateAudit,["status"],pick(validation,["status"],"not_applicable")))}</strong><small>{blockers.length?`${blockers.length} 项仍阻断`:"没有记录到阻断项"}</small></article>
      <article><span>评价协议</span><strong>{modelStatusLabel(evaluationStatus)}</strong><small>{String(pick(metrics,["model_evaluation_contract"],pick(hsmmValidation,["evaluation_contract"],"尚未登记评价协议")))}</small></article>
      <article><span>{isHsmm?"HSMM 对 HMM":"同任务基线"}</span><strong>{isHsmm?`${hsmmFolds.filter(item=>Number(item.likelihood_gain_per_observation)>0).length}/${hsmmFolds.length||3} 窗占优`:taskBaseline.comparison_executed?taskBaseline.outperformed?"已跑赢":"未跑赢":"未执行"}</strong><small>{isHsmm?`样本外 ${hsmmValidation.oos_observations||"尚未形成"} 周`:`任务窗 ${taskOos.passed_windows||0}/${taskOos.window_count||0} 通过`}</small></article>
      <article><span>{isHsmm?"影子观察":"环境覆盖"}</span><strong>{isHsmm?`${health.mature_20d_outcomes||0}/${health.minimum_required||4} 周`:`${environment.covered_parent_states||0}/4 · ${environment.covered_profiles||0}/12`}</strong><small>{isHsmm?modelStatusLabel(health.status):environment.complete?"覆盖达标":"覆盖未达标"}</small></article>
      <article><span>模型产物</span><strong>{artifact.artifact_ready?"可用":"未就绪"}</strong><small>{artifact.model_type||artifact.artifact_type||"产物类型尚未登记"}</small></article>
    </div>
    {gateRows.length>0&&<section className="model-evidence-block"><h4>当前硬门</h4><div className="model-gate-list">{gateRows.map(item=><div key={item.name}><span>{item.name}</span><Status tone={item.passed?"good":"danger"}>{item.status}</Status></div>)}</div></section>}
    {blockers.length>0&&<section className="model-evidence-block"><h4>当前阻断</h4><ul className="model-blocker-list">{blockers.map((item,index)=><li key={index}>{modelEvidenceValueNames[String(item)]||String(item)}</li>)}</ul></section>}
    {!isHsmm&&<section className="model-evidence-block"><h4>训练合同</h4><CompactTable rows={trainingRows} columns={[["项目","name"],["当前设置","value"]]}/>{parameterRows.length>0&&<EvidenceDetails title="查看训练参数"><CompactTable rows={parameterRows} columns={[["参数","name"],["数值","value"]]}/></EvidenceDetails>}</section>}
    {isHsmm&&<section className="model-evidence-block"><h4>HSMM 任务原生验证</h4><div className="model-fact-grid"><div><span>样本外观察</span><strong>{hsmmValidation.oos_observations||"尚未形成"}</strong></div><div><span>滚动窗口</span><strong>{hsmmFolds.length}</strong></div><div><span>优于同发射 HMM</span><strong>{hsmmFolds.filter(item=>Number(item.likelihood_gain_per_observation)>0).length}</strong></div><div><span>任务结论</span><strong>{modelStatusLabel(hsmmTask.passed?"passed":"failed")}</strong></div></div><EvidenceDetails title="逐窗边际似然、时长与状态占用证据"><pre>{JSON.stringify(localizeModelEvidence(hsmmTask),null,2)}</pre></EvidenceDetails></section>}
    {!isHsmm&&metrics.task_metrics&&<section className="model-evidence-block"><h4>任务原生指标</h4><pre>{JSON.stringify(localizeModelEvidence(metrics.task_metrics),null,2)}</pre></section>}
  </div>
}
function modelProgress(run:Json){const raw=Number(pick(run,["progress"],0));return Math.max(0,Math.min(100,raw<=1?raw*100:raw))}

const governedPlanModelKeys=[
  "medium_horizon_lambdarank_v1",
  "capital_persistence_multitask_v1",
  "leader_lambdarank_v1",
  "event_impact_assessor_v2",
  "narrative_transition_multitask_v1",
];
const governedPlanModelNames:Record<string,string>={
  medium_horizon_lambdarank_v1:"中期排序",
  capital_persistence_multitask_v1:"资金持续",
  leader_lambdarank_v1:"龙头排序",
  event_impact_assessor_v2:"事件影响评估",
  narrative_transition_multitask_v1:"叙事生命周期",
};
function modelGateLabel(value:unknown){const key=String(value||"");return modelEvidenceKeyNames[key]||modelStatusLabel(key)}
function ModelStrategyImpactCard({row}:{row:Json}){
  const latest=pick(row,["latest_version"],{}) as Json;const metrics=pick(latest,["metrics"],{}) as Json;const taskOos=pick(metrics,["task_native_oos"],{}) as Json;const impact=pick(metrics,["strategy_impact_ab"],{}) as Json;const validation=pick(latest,["validation"],{}) as Json;const shadow=pick(validation,["model_shadow"],{}) as Json;const eligibility=pick(validation,["production_eligibility"],{}) as Json;const windows=Array.isArray(impact.windows)?impact.windows as Json[]:[];const improved=windows.filter(item=>Number(item.return_delta)>0).length;const eligibilityGates=pick(eligibility,["gates"],{}) as Json;const blocked=[...new Set([...(Array.isArray(impact.blocked_reasons)?impact.blocked_reasons:[]),...(Array.isArray(eligibility.blocked_reasons)?eligibility.blocked_reasons:[]),...Object.entries(eligibilityGates).filter(([,passed])=>passed===false).map(([key])=>key)])];const version=pick(latest,["id","version"],null);const impactStatus=String(impact.status||"");const validationStatus=String(validation.status||"not_evaluated");const shadowStatus=String(shadow.status||validationStatus);const shadowProgress=shadow.status?`${Number(shadow.observed_return_sessions||0)}/${Number(shadow.required_return_sessions||20)} 日`:"尚未入组";
  return <article className={`model-ab-card ${impactStatus==="failed"?"failed":impactStatus==="passed"?"passed":"pending"}`}><header><div><span>{row.model_key}</span><h3>{pick(row,["name"],governedPlanModelNames[String(row.model_key)]||"业务模型")}</h3><small>{version?`候选版本 #${version}`:"尚无当前协议候选版本"}</small></div><Status tone={impactStatus==="passed"?"good":impactStatus==="failed"?"danger":"watch"}>{impactStatus?modelStatusLabel(impactStatus):modelStatusLabel(validationStatus)}</Status></header><dl><div><dt>本职任务 OOS</dt><dd>{taskOos.window_count?`${taskOos.passed_windows||0}/${taskOos.window_count} 窗通过`:taskOos.passed?"通过":"尚未运行"}</dd></div><div><dt>A/B 改善窗口</dt><dd>{windows.length?`${improved}/${windows.length}`:"未运行"}</dd></div><div><dt>平均收益增量</dt><dd className={Number(impact.mean_return_delta)>=0?"positive":"negative"}>{impact.mean_return_delta===undefined?"未运行":fmtPct(impact.mean_return_delta)}</dd></div><div><dt>Bootstrap 95% 下界</dt><dd className={Number(impact.paired_bootstrap_95_lower)>=0?"positive":"negative"}>{impact.paired_bootstrap_95_lower===undefined?"未运行":fmtPct(impact.paired_bootstrap_95_lower)}</dd></div><div><dt>候选未来观察</dt><dd>{modelStatusLabel(shadowStatus)} · {shadowProgress}</dd></div><div><dt>生产资格</dt><dd>{validation.production_eligible?"允许上线":"不可上线"}</dd></div></dl>{impact.model_effect_contract&&<p className="model-ab-contract">效果合同：<code>{String(impact.model_effect_contract)}</code></p>}{blocked.length>0&&<div className="model-ab-blockers"><strong>当前阻断</strong><span>{blocked.slice(0,5).map(modelGateLabel).join("、")}{blocked.length>5?` 等 ${blocked.length} 项`:""}</span></div>}<EvidenceDetails title={`展开 ${windows.length||0} 个 A/B 窗口与全部硬门`}><div className="table-wrap model-ab-window-table"><table><thead><tr><th>窗口</th><th>测试区间</th><th>关闭模型</th><th>启用模型</th><th>收益增量</th><th>配对日</th><th>消费 / 生效日</th></tr></thead><tbody>{windows.map((item,index)=><tr key={String(item.fold||index)}><td>{item.fold||index+1}</td><td>{item.test_start||"日期未记录"} ～ {item.test_end||"日期未记录"}</td><td>{fmtPct(item.reference_total_return)}</td><td>{fmtPct(item.candidate_total_return)}</td><td className={Number(item.return_delta)>=0?"positive":"negative"}>{fmtPct(item.return_delta)}</td><td>{item.paired_trade_dates||0}</td><td>{item.candidate_model_consumed_days||0} / {item.candidate_model_effect_days||0}</td></tr>)}</tbody></table></div><div className="model-gate-list">{Object.entries(pick(impact,["gates"],{}) as Json).map(([key,passed])=><div key={key}><span>{modelGateLabel(key)}</span><Status tone={passed?"good":"danger"}>{passed?"通过":"未通过"}</Status></div>)}</div></EvidenceDetails></article>
}

export function ModelGovernance({data}:{data:ConsoleData}){
  const [livePlanModels,setLivePlanModels]=useState<Json|undefined>(data.planModels);
  const [liveTasks,setLiveTasks]=useState<Json|undefined>(data.modelTasksLive||data.tasks);
  const [liveOperations,setLiveOperations]=useState<Json|undefined>(data.modelOperations);
  const [liveResearch,setLiveResearch]=useState<Json|undefined>(data.factorResearch);
  const [liveEvolutionTasks,setLiveEvolutionTasks]=useState<Json|undefined>(data.evolutionTasks);
  const [details,setDetails]=useState<ConsoleData>({});
  const [logModel,setLogModel]=useState("all");
  const [logStatus,setLogStatus]=useState("all");
  const [onlyAbnormal,setOnlyAbnormal]=useState(false);
  const [logRefreshedAt,setLogRefreshedAt]=useState<string>("");
  const [logStreamHealth,setLogStreamHealth]=useState<"waiting"|"healthy"|"partial"|"degraded">("waiting");
  const pageData={...data,...details,factorResearch:liveResearch||details.factorResearch||data.factorResearch||data.factorResearchCompact,evolutionTasks:liveEvolutionTasks||details.evolutionTasks||data.evolutionTasks};
  useEffect(()=>setLivePlanModels(data.planModels),[data.planModels]);
  useEffect(()=>setLiveTasks(data.modelTasksLive||data.tasks),[data.modelTasksLive,data.tasks]);
  useEffect(()=>setLiveOperations(data.modelOperations),[data.modelOperations]);
  useEffect(()=>setLiveResearch(data.factorResearch||data.factorResearchCompact),[data.factorResearch,data.factorResearchCompact]);
  useEffect(()=>setLiveEvolutionTasks(data.evolutionTasks),[data.evolutionTasks]);
  useEffect(()=>{
    let active=true;let busy=false;
    const refresh=async()=>{if(busy)return;busy=true;let logSourcesUpdated=0;const markLogSourceUpdated=()=>{logSourcesUpdated+=1;if(active){setLogRefreshedAt(new Date().toLocaleString("sv-SE",{hour12:false}));setLogStreamHealth(logSourcesUpdated>=2?"healthy":"partial")}};try{await Promise.allSettled([
      readApi(endpoints.modelOperations).then(value=>{if(active){setLiveOperations(value);markLogSourceUpdated()}}),
      readApi(endpoints.modelTasksLive).then(value=>{if(active){setLiveTasks(value);markLogSourceUpdated()}}),
    ]);if(active&&logSourcesUpdated===0)setLogStreamHealth("degraded")}finally{busy=false}};
    const timer=window.setInterval(refresh,15000);
    return()=>{active=false;window.clearInterval(timer)};
  },[]);
  useEffect(()=>{if(details.planModels)setLivePlanModels(details.planModels)},[details.planModels]);
  const operationItems=firstArray(liveOperations,["items"]);const operationByKey=new Map(operationItems.map(row=>[String(row.model_key),row]));const modelsReady=Boolean(livePlanModels);const planModels=firstArray(livePlanModels,["items"]);const planModelByKey=new Map(planModels.map(row=>[String(row.model_key),row]));const candidateObservationFor=(modelKey:string)=>{const operation=operationByKey.get(modelKey)||{};const explicit=pick(operation,["candidate_observation"],{}) as Json;if(explicit.candidate_active||explicit.status&&explicit.status!=="not_applicable")return explicit;const family=planModelByKey.get(modelKey)||{};const latest=pick(family,["latest_version"],{}) as Json;const shadow=pick(latest,["validation.model_shadow"],{}) as Json;return shadow.status?{scope:"candidate_future_shadow",status:shadow.status,candidate_active:true,model_version_id:latest.id||null,version:latest.version||null,signal_start_trade_date:shadow.signal_start_trade_date||null,observed_return_sessions:Number(shadow.observed_return_sessions||0),required_return_sessions:Number(shadow.required_return_sessions||20),minimum_effect_days:Number(shadow.minimum_effect_days||4),last_observed_trade_date:shadow.last_observed_trade_date||null,autonomous_management:true,manager:"model_shadow_observation",production_change_forbidden:true}:{status:"not_applicable",candidate_active:false,observed_return_sessions:0,required_return_sessions:20}};const registryVersions=rowsFrom(pageData.models,["versions"]);const counts=(livePlanModels?.counts||{}) as Json;
  const hsmmGovernance=pick(pageData.modelGovernance,["hsmm"],{}) as Json;const hsmmEligible=Boolean(hsmmGovernance.production_eligible);const hsmmVersion=pick(hsmmGovernance,["latest_shadow_version"],null);const hsmmValidation=pick(hsmmGovernance,["validation"],{}) as Json;const hsmmValidationPassed=String(hsmmValidation.status||"")==="passed";const hsmmLiveWeeks=Number(pick(hsmmGovernance,["live_observation_weeks"],0));const hsmmMinimumWeeks=Number(pick(hsmmGovernance,["minimum_live_observation_weeks"],4));const hsmm={model_key:"market_regime_hsmm_v1",name:"市场状态 HSMM",role:"识别市场状态、风险预算和防御切换",family:"Student-t 显式时长隐半马尔可夫模型",status:hsmmEligible?"production_current_standard":pick(hsmmGovernance,["status"],"not_applicable"),active_version:hsmmEligible&&hsmmVersion?{version:hsmmVersion}:null,latest_version:hsmmVersion?{version:hsmmVersion,metrics:{validation:hsmmValidation}}:null,coverage:{effective_sessions:pick(pageData.modelGovernance,["market_training_history.observations"],"正在读取"),minimum_sessions:1512},blocked_reasons:hsmmEligible?[]:[pick(hsmmGovernance,["blocking_reason"],"尚未通过生产激活门")],current_gate_audit:{status:hsmmEligible?"current_gate_passed":hsmmValidationPassed?"shadow_live_observation":"shadow_validation_failed",passed:hsmmEligible,gates:{历史样本外验证:hsmmValidationPassed,真实影子观察:hsmmValidationPassed&&hsmmLiveWeeks>=hsmmMinimumWeeks,生产评审:Boolean(hsmmGovernance.review_completed)},blocked_reasons:hsmmEligible?[]:[pick(hsmmGovernance,["blocking_reason"],"尚未通过生产激活门")]},live_health:{status:hsmmEligible?"healthy":hsmmValidationPassed?"observing":"shadow_validation_failed",mature_20d_outcomes:hsmmLiveWeeks,minimum_required:hsmmMinimumWeeks,observation_unit:"周",updated_at:pick(pageData.modelMarketLive,["available_time","trade_date"],null)},active_artifact:{artifact_ready:Boolean(hsmmVersion),model_type:"Student-t explicit-duration HSMM",training_dataset:pick(pageData.modelGovernance,["market_training_history"],{})}};
  const families=[hsmm,...planModels];const runs:Json[]=operationItems.map(row=>{const latestRun=(row.latest_train_run||{}) as Json;if(latestRun.id||latestRun.status)return {...latestRun,model_key:row.model_key,model_name:row.name,trigger_reason:row.trigger_reason,next_check_at:row.next_check_at,training_failure_recovery:row.training_failure_recovery} as Json;const family=planModelByKey.get(String(row.model_key))||{};const coverage=pick(family,["coverage"],{}) as Json;const eventModel=String(row.model_key)==="event_impact_assessor_v2";const eventObserved=pick(coverage,["effective_sessions"],null);const eventRequired=pick(coverage,["formal_walk_forward_sessions","minimum_sessions","required_sessions"],null);const eventCoverageReady=eventModel&&Number(eventRequired)>0&&Number(eventObserved)>=Number(eventRequired);const waitingForEventData=eventModel&&!eventCoverageReady;const eventMessage=eventObserved!==null&&eventRequired!==null?`真实事件有效日 ${eventObserved} / ${eventRequired}（正式五窗要求），数据门未满足，保持 N/A。`:"正式事件历史门未满足，保持 N/A；完整覆盖返回后自动显示。";return{model_key:row.model_key,model_name:row.name,status:waitingForEventData?"wait_for_data":"not_started",phase:waitingForEventData?"等待真实事件历史":"尚无训练任务",message:waitingForEventData?eventMessage:eventCoverageReady?`真实事件有效日 ${eventObserved} / ${eventRequired}，数据门已满足，等待后台正式评价。`:"后台尚未生成该模型的训练任务。",trigger_reason:waitingForEventData?"真实事件历史未满足训练门":eventCoverageReady?"事件数据已满足正式五窗要求":row.trigger_reason,next_check_at:row.next_check_at} as Json});const activeRuns=runs.filter(row=>/running|queued/.test(String(row.status)));const handledFailureStatuses=new Set(["resolved_by_existing_candidate_observation","delegated_to_data_backfill","automatic_retry_started"]);const completedRecoveryStatuses=new Set(["resolved_by_existing_candidate_observation"]);const failedRuns=runs.filter(row=>row.status==="failed"&&!handledFailureStatuses.has(String(pick(row,["metrics.failure_recovery.status","training_failure_recovery.status"],""))));const candidateObservationItems=governedPlanModelKeys.map(model_key=>({model_key,candidate_observation:candidateObservationFor(model_key)})).filter(row=>Boolean(row.candidate_observation.candidate_active));const observingModels=candidateObservationItems.filter(row=>["awaiting_first_future_signal","observing","extended_for_inconclusive_evidence"].includes(String(row.candidate_observation.status||"")));const matureObservationCount=candidateObservationItems.reduce((sum,row)=>sum+Number(row.candidate_observation.observed_return_sessions||0),0);const requiredObservationCount=candidateObservationItems.reduce((sum,row)=>sum+Number(row.candidate_observation.required_return_sessions||0),0);
  const taskRows=firstArray(liveTasks,["tasks","items"]).filter(row=>/model|train/i.test(`${pick(row,["module"],"")} ${pick(row,["task_type"],"")} ${pick(row,["name","task_name"],"")}`));
  const taskLogs=firstArray(liveTasks,["logs"]).filter(row=>/model|train/i.test(`${pick(row,["module","logger"],"")} ${pick(row,["task_type"],"")} ${pick(row,["message"],"")}`));
  const ownerAttentionRecoveryStatuses=new Set(["automatic_retry_exhausted","escalated_owner_review","data_backfill_delegation_failed","recovery_enqueue_failed"]);
  const timeline:Json[]=runs.flatMap(run=>{const recoveryStatus=String(pick(run,["metrics.failure_recovery.status","training_failure_recovery.status"],""));return(Array.isArray(pick(run,["metrics.timeline"],[]))?pick(run,["metrics.timeline"],[]) as Json[]:[]).map(item=>({...item,model_key:run.model_key,model_name:run.model_name,run_id:run.id,training_protocol_status:run.training_protocol_status,labeled_trade_date_count:run.labeled_trade_date_count,run_recovery_status:recoveryStatus,recovery_handled:handledFailureStatuses.has(recoveryStatus)} as Json))}).sort((a,b)=>String(pick(b,["at"],"")).localeCompare(String(pick(a,["at"],""))));
  const logs:Json[]=[...timeline,...taskLogs].sort((a,b)=>String(pick(b,["at","created_at","timestamp"],"")).localeCompare(String(pick(a,["at","created_at","timestamp"],"")))).slice(0,120);
  const logIsUnhandled=(row:Json)=>{const phase=String(pick(row,["phase","level","status"],"")).toLowerCase();const message=String(pick(row,["message","summary"],"")).toLowerCase();const recoveryStatus=String(pick(row,["run_recovery_status","training_failure_recovery.status"],""));if(ownerAttentionRecoveryStatuses.has(recoveryStatus))return true;if(Boolean(row.recovery_handled)||handledFailureStatuses.has(recoveryStatus))return false;return/fail|error|blocked|degrad|失败/.test(`${phase} ${message}`)};
  const filteredLogs=logs.filter(row=>{const model=String(pick(row,["model_key","module","logger"],""));const status=String(pick(row,["phase","level","status"],"")).toLowerCase();if(logModel!=="all"&&model!==logModel)return false;if(logStatus!=="all"&&status!==logStatus)return false;if(onlyAbnormal&&!logIsUnhandled(row))return false;return true});
  const unhandledLogCount=logs.filter(logIsUnhandled).length;
  const logStreamLabel=logStreamHealth==="healthy"?"实时连接正常":logStreamHealth==="partial"?"部分日志源已更新":logStreamHealth==="degraded"?"实时日志刷新失败":"等待首次刷新";
  const logStreamTone=logStreamHealth==="healthy"?"good":logStreamHealth==="waiting"?"neutral":"watch";
  const productionCount=planModels.filter(row=>Boolean(pick(row,["active_version.id","active_version.version"],null))).length+(pick(hsmm.active_version as Json,["id","version"],null)?1:0);const onlineCount=operationItems.filter(row=>String(pick(row,["online_observation.status"],""))!=="not_available").length+(pick(hsmm,["latest_version.id","latest_version.version"],null)?1:0);const degradedCount=families.filter(row=>/degrad|artifact_missing|failed/.test(String(pick(row,["health.status","active_artifact.status","status"],"")))).length;const productionDegrees=Math.round(productionCount/Math.max(families.length,1)*360);
  const marketState=scalarLabel(pick(pageData.modelMarketLive,["market_state.label","market_state.state","market_state","state"],"正在读取最新市场状态"));const marketConfidence=pick(pageData.modelMarketLive,["market_state.confidence","confidence","probability"],null);const marketProfile=pick(pageData.modelMarketLive,["market_profile.label","market_profile.name","market_profile.code"],"正在读取环境分项");
  const productionUsage=planModels.filter(row=>Boolean(pick(row,["active_version.id","active_version.version"],null))).map(row=>{const operation=operationByKey.get(String(row.model_key))||{};const weights=pick(operation,["weight_breakdown"],{}) as Json;return{name:row.name,observation:`${modelStatusLabel(pick(operation,["observation.status"],"not_applicable"))} · ${pick(operation,["observation.mature_20d_outcomes"],0)}/${pick(operation,["observation.minimum_required"],5)}`,base:`${Math.round(Number(weights.base||0)*100)}%`,environment:`× ${Number(weights.environment||1).toFixed(2)}`,health:`× ${Number(weights.health||1).toFixed(2)}`,maturity:`× ${Number(weights.maturity||1).toFixed(2)}`,normalized:`${(Number(weights.normalized_model_weight||0)*100).toFixed(1)}%`,effective:`${(Number(weights.final_fusion_weight||0)*100).toFixed(1)}%`}});
  const abModels=governedPlanModelKeys.map(key=>planModels.find(row=>String(row.model_key)===key)||{model_key:key,name:governedPlanModelNames[key],status:"not_evaluated",latest_version:null});
  const factorResearch=pageData.factorResearch||{};const researchSnapshot=pick(factorResearch,["snapshot"],{}) as Json;const researchContract=pick(researchSnapshot,["contract"],{}) as Json;const researchPackage=(pick(factorResearch,["research_package"],null)||pick(researchContract,["research_package"],{})) as Json;const researchBindings=pick(researchPackage,["model_bindings"],{}) as Json;const modelCandidatePool=Array.isArray(researchContract.model_candidate_pool)?researchContract.model_candidate_pool as Json[]:[];const researchSnapshotId=String(pick(researchSnapshot,["snapshot_id"],""));const allEvolutionTasks=firstArray(pageData.evolutionTasks,["items","tasks"]);const staleContractTasks=allEvolutionTasks.filter(row=>String(pick(row,["phase","result.contract_invalidation.status"],""))==="cancelled_stale_contract");const staleContractTask=staleContractTasks.sort((a,b)=>Number(b.id||0)-Number(a.id||0))[0]||{};const invalidation=pick(staleContractTask,["result.contract_invalidation"],{}) as Json;const replacementTaskId=Number(pick(invalidation,["replacement_task_id"],0));const replacementSnapshotId=String(pick(invalidation,["replacement_snapshot_id"],researchSnapshotId));const replacementTask=allEvolutionTasks.find(row=>Number(row.id)===replacementTaskId)||{};const currentResearchTasks=allEvolutionTasks.filter(row=>{const id=Number(row.id||0);const predecessor=Number(pick(row,["payload.predecessor_task_id"],0));const snapshotId=String(pick(row,["payload.snapshot_id","result.snapshot_id","checkpoint.snapshot_id"],""));return id===replacementTaskId||predecessor===replacementTaskId||(Boolean(researchSnapshotId)&&snapshotId===researchSnapshotId)}).sort((a,b)=>Number(a.id||0)-Number(b.id||0));const bindingRows=governedPlanModelKeys.map(key=>{const binding=researchBindings[key];return{name:governedPlanModelNames[key],model_key:key,binding:binding&&typeof binding==="object"?`${binding.id?`#${binding.id}`:"版本号未记录"} · ${binding.version||binding.status||"已冻结"}`:"基础因子与确定性规则"}});const candidateRows=modelCandidatePool.map(row=>({name:governedPlanModelNames[String(row.model_key)]||row.model_key,version:`${row.id?`#${row.id}`:"版本号未记录"} · ${row.version||"版本名称未记录"}`,status:modelStatusLabel(pick(row,["validation.status","status"],"candidate"))}));const researchTaskRows=currentResearchTasks.map(row=>({id:row.id?`#${row.id}`:"任务号未记录",task:researchTaskTypeLabel(row.task_type),status:modelStatusLabel(row.status),phase:modelStatusLabel(row.phase),message:row.message||"未提供任务摘要"}));
  return <><PageHeader eyebrow="MODEL OPERATIONS" title="模型训练与治理" summary="先看训练任务、实时进度和日志，再查看生产版本、验证硬门与降级影响。"/>
    <div className="metric-grid headline-metrics model-headline-metrics"><Metric label="正在训练" value={liveOperations?activeRuns.length:"读取中"} note={liveOperations?`${runs.filter(row=>row.status==="queued").length} 个等待中`:"正在读取五模型运行合同"} icon={BrainCircuit}/><Metric label="在线推理" value={liveOperations?onlineCount:"读取中"} note={liveOperations?"限权、风险参考和纯旁路分别授权":"正在读取在线授权合同"} icon={Radio}/><Metric label="未处理训练故障" value={liveOperations?failedRuns.length:"读取中"} tone={failedRuns.length?"danger":"good"} note={liveOperations?"已接管、补数或重试中的故障不重复报警":"正在读取训练恢复证据"} icon={TriangleAlert}/><Metric label="候选 / 可训练" value={modelsReady?Number(counts.candidate||0)+Number(counts.trainable||0):"读取中"} note={modelsReady?`${counts.candidate||0} 候选 · ${counts.trainable||0} 可训练 · ${counts.legacy_invalid_candidate||0} 历史无效`:"正在读取候选版本"} icon={Boxes}/></div>
    <div className="model-overview-grid">
      <section className="model-observation-panel"><header><div><span>未来样本</span><h2>候选观察进度</h2></div><Status tone={observingModels.length?"watch":"neutral"}>{observingModels.length} 个观察中</Status></header>{candidateObservationItems.length?<div className="model-observation-list">{candidateObservationItems.slice(0,5).map(({model_key,candidate_observation})=>{const observed=Number(candidate_observation.observed_return_sessions||0);const required=Math.max(Number(candidate_observation.required_return_sessions||20),1);const family=planModelByKey.get(model_key)||{};return <article key={model_key}><div><strong>{pick(family,["name"],governedPlanModelNames[model_key]||model_key)}</strong><small>{modelStatusLabel(candidate_observation.status)}</small></div><span><i style={{width:`${Math.min(100,Math.max(0,observed/required*100))}%`}}/></span><b>{observed}/{required}</b></article>})}</div>:<Empty title="当前没有候选进入未来观察" detail="候选通过正式研究门后才会在此累计真实未来样本。"/>}</section>
      <aside className="model-permission-panel"><header><div><span>生产治理</span><h2>模型权限</h2></div><Status tone={productionCount?"good":"watch"}>{productionCount?"已激活":"未激活"}</Status></header><div className="model-permission-ring" style={{background:`conic-gradient(#8b73ff 0deg ${productionDegrees}deg, #3b4258 ${productionDegrees}deg 360deg)`}}><div><strong>{productionCount}/{families.length}</strong><span>生产激活</span></div></div><dl><div><dt>在线推理</dt><dd>{onlineCount}</dd></div><div><dt>候选观察</dt><dd>{candidateObservationItems.length}</dd></div><div><dt>未来样本</dt><dd>{matureObservationCount}/{requiredObservationCount}</dd></div><div><dt>未处理故障</dt><dd className={failedRuns.length?"negative":"positive"}>{failedRuns.length}</dd></div></dl></aside>
    </div>
    <Section title="模型训练任务" detail="每 5 秒刷新训练状态；展示当前阶段、心跳、进度和最近结果。">
      <div className="model-training-board">{runs.length?runs.map((run,i)=>{const legacy=run.training_protocol_status==="legacy_invalid";const currentFailed=run.training_protocol_status==="current_failed";const hasTask=Boolean(run.id);const recovery=pick(run,["metrics.failure_recovery","training_failure_recovery"],{}) as Json;const recoveryStatus=String(recovery.status||"");const autoHandled=handledFailureStatuses.has(recoveryStatus);return <article className={legacy||currentFailed&&!autoHandled?"legacy-invalid":/running|queued/.test(String(run.status))?"active":""} key={String(run.model_key||run.id||i)}><header><div><span>{run.model_name||run.model_key}</span><small>{hasTask?`任务 #${run.id}`:"尚未生成训练任务"} · {run.model_key}</small></div><Status tone={autoHandled?"watch":legacy||currentFailed||run.status==="failed"?"danger":run.status==="completed"?"good":/running|queued|wait_for_data/.test(String(run.status))?"watch":"neutral"}>{autoHandled?modelStatusLabel(recoveryStatus):legacy?"历史旧协议 · 结果无效":currentFailed?"当前协议 · 训练失败":modelStatusLabel(run.status)}</Status></header><div className="model-run-stage"><strong>{autoHandled?"历史故障已接管":modelStatusLabel(pick(run,["phase"],"queued"))}</strong><span>{Math.round(modelProgress(run))}%</span></div><div className="model-run-progress"><i style={{width:`${modelProgress(run)}%`}}/></div><p>{pick(run,["error","message"],"尚无训练任务")}</p><dl className="model-run-meta"><div><dt>{legacy?"旧任务有效交易日":"触发原因"}</dt><dd>{legacy?`${pick(run,["labeled_trade_date_count"],"未记录")} 日（不满足当前合同）`:pick(run,["trigger_reason"],"定期模型治理")}</dd></div><div><dt>自动处置</dt><dd>{recoveryStatus?modelStatusLabel(recoveryStatus):run.status==="failed"?"等待故障协调器接管":"无训练故障"}</dd></div><div><dt>下次检查</dt><dd>{formatLogTime(pick(run,["next_check_at"],""),"由自治日循环按证据触发")}</dd></div></dl><footer><span>开始 {formatLogTime(pick(run,["started_at","created_at"],""),hasTask?"时间未记录":"尚未启动")}</span><span>更新 {formatLogTime(pick(run,["updated_at","finished_at"],""),hasTask?"时间未记录":"等待数据门")}</span></footer></article>;}):<Empty title={liveOperations?"尚无模型训练任务":"正在读取模型训练任务"} detail={liveOperations?"后台未返回模型清单。":"模型状态返回后会自动显示，无需刷新。"}/>}</div>
      {taskRows.length>0&&<EvidenceDetails title="模型调度与自治任务"><CompactTable rows={taskRows.slice(0,20)} columns={[["任务","name","task_name","task_type"],["状态","status","state"],["当前步骤","current_step","message","phase"],["更新时间","heartbeat_at","updated_at","next_run_at"]]} fallback="该任务未记录此字段"/></EvidenceDetails>}
    </Section>
    <Section title="模型实时日志" detail="每 5 秒刷新当前连接和最近事件；列表保留真实时间戳，不用空闲心跳伪造新日志。">
      <div className="model-log-live-status"><Status tone={logStreamTone}>{logStreamLabel}</Status><span>最近刷新 {formatLogTime(logRefreshedAt,"等待首次刷新")}</span><span>当前训练 {activeRuns.length}</span><span>候选观察 {observingModels.length}</span><span className={unhandledLogCount?"danger-text":""}>未处理异常 {unhandledLogCount}</span></div>
      <div className="model-log-filters"><label>模型<select value={logModel} onChange={event=>setLogModel(event.target.value)}><option value="all">全部模型</option>{operationItems.map(row=><option key={String(row.model_key)} value={String(row.model_key)}>{row.name}</option>)}</select></label><label>状态<select value={logStatus} onChange={event=>setLogStatus(event.target.value)}><option value="all">全部状态</option>{["coverage","dataset","training","validation","failure_recovery","completed","failed","info"].map(status=><option key={status} value={status}>{modelStatusLabel(status)}</option>)}</select></label><label className="model-log-check"><input type="checkbox" checked={onlyAbnormal} onChange={event=>setOnlyAbnormal(event.target.checked)}/>仅看未处理异常</label><span>{filteredLogs.length} / {logs.length} 条</span></div>
      {filteredLogs.length ? (
        <div className="model-live-logs">{filteredLogs.map((row,i)=>{const legacy=row.training_protocol_status==="legacy_invalid";const phase=String(pick(row,["phase","level","status"],""));const recoveryStatus=String(pick(row,["run_recovery_status","training_failure_recovery.status"],""));const recoveryHandled=Boolean(row.recovery_handled)||handledFailureStatuses.has(recoveryStatus);const recoveryCompleted=completedRecoveryStatuses.has(recoveryStatus);const isRecovery=phase==="failure_recovery";const historicalFailure=/fail|error/i.test(phase)&&recoveryHandled;const unhandled=logIsUnhandled(row);const label=unhandled?`未处理 · ${modelStatusLabel(phase)}`:isRecovery&&recoveryStatus?modelStatusLabel(recoveryStatus):historicalFailure?"历史失败 · 已自动关闭":legacy?`历史日志 · ${modelStatusLabel(phase)}`:modelStatusLabel(phase);return <div className={unhandled?"unhandled":isRecovery&&recoveryCompleted?"resolved":isRecovery&&recoveryHandled?"recovering":""} key={`${pick(row,["run_id","id"],i)}-${i}`}><time>{formatLogTime(pick(row,["at","created_at","timestamp"],""),"时间未记录")}</time><code>{pick(row,["model_name","model_key","module","logger"],"模型系统")}</code><Status tone={unhandled?"danger":isRecovery&&recoveryCompleted||/complete|success/i.test(phase)?"good":isRecovery&&recoveryHandled?"watch":"neutral"}>{label}</Status><strong>{pick(row,["message","summary"],"未提供日志摘要")}</strong><small>{pick(row,["progress"],null)!==null?`${Math.round(Number(row.progress)*100)}%`:"无进度值"}</small></div>})}</div>
      ) : (
        <Empty title={onlyAbnormal?"没有未处理异常":"当前筛选下没有日志"} detail={onlyAbnormal?"已接管、补数或自动重试中的历史故障不会重复报警。":"调整模型或状态筛选后查看其他事件。"}/>
      )}
    </Section>
    <Section title="模型上线门与冻结策略 A/B" detail="A/B 只评价单个模型在同一冻结策略中的边际贡献；通过不等于模型已上线，也不等于完整策略有效。"><div className="model-ab-grid">{abModels.map(row=><ModelStrategyImpactCard key={String(row.model_key)} row={row}/>)}</div></Section>
    <Section title="当前研究模型合同" detail="新研究只能读取当前生产资格绑定；历史 Champion 模型 ID 只允许显式回放，不能重新进入新候选。"><div className="model-research-contract-summary"><article><span>当前快照</span><strong>{researchSnapshotId||"正在读取研究快照"}</strong><small>数据截止 {pick(researchSnapshot,["data_as_of"],"等待研究快照")} · {researchStageLabel(pick(factorResearch,["current_stage","status"],"not_started"))}</small></article><article><span>生产模型绑定</span><strong>{Object.values(researchBindings).filter(Boolean).length} / 5</strong><small>{Object.values(researchBindings).some(Boolean)?"仅展示通过当前生产资格审计的绑定":"全部归还基础因子与确定性规则"}</small></article><article><span>有效模型候选池</span><strong>{modelCandidatePool.length}</strong><small>{candidateRows.length?candidateRows.map(row=>row.version.split(" · ")[0]).join(" / "):"当前没有合格候选"}</small></article><article><span>旧合同隔离</span><strong>{staleContractTask.id?`#${staleContractTask.id}`:"正在读取隔离记录"}</strong><small>{staleContractTask.id?`${modelStatusLabel(pick(staleContractTask,["phase","result.contract_invalidation.status"],"cancelled_stale_contract"))} · ${replacementTaskId?`替换任务 #${replacementTaskId}`:"替换任务号未记录"}`:"任务合同返回后自动显示"}</small></article></div><div className="two-column model-contract-columns"><div><h3>五个生产模型绑定</h3><CompactTable rows={bindingRows} columns={[["模型","name"],["模型键","model_key"],["当前研究绑定","binding"]]} fallback="基础因子与确定性规则"/></div><div><h3>当前有效模型候选池</h3><CompactTable rows={candidateRows} columns={[["模型","name"],["候选版本","version"],["治理状态","status"]]} fallback="当前合同未记录"/></div></div>{staleContractTask.id&&<div className="model-contract-transition"><h3>旧合同隔离与替换链</h3><dl><div><dt>隔离任务</dt><dd>#{staleContractTask.id} · {modelStatusLabel(pick(staleContractTask,["phase"],"cancelled_stale_contract"))}</dd></div><div><dt>替换任务</dt><dd>{replacementTaskId||replacementTask.id?`#${replacementTaskId||replacementTask.id}`:"任务号未记录"} · {modelStatusLabel(pick(replacementTask,["status"],"completed"))}</dd></div><div><dt>替换快照</dt><dd>{replacementSnapshotId||researchSnapshotId||"快照号未记录"}</dd></div><div><dt>环境回测</dt><dd>{currentResearchTasks.filter(row=>row.task_type==="strategy_environment_backtest").length} 个任务</dd></div></dl>{researchTaskRows.length>0&&<CompactTable rows={researchTaskRows} columns={[["任务","id"],["类型","task"],["状态","status"],["阶段","phase"],["摘要","message"]]} fallback="任务合同未记录"/>}</div>}<EvidenceDetails title="查看当前研究包、候选池与合同隔离原始证据"><pre>{JSON.stringify({snapshot:{snapshot_id:researchSnapshotId,data_as_of:researchSnapshot.data_as_of},research_package:researchPackage,model_candidate_pool:modelCandidatePool,contract_invalidation:invalidation,current_research_tasks:currentResearchTasks},null,2)}</pre></EvidenceDetails></Section>
    <Section title="模型族与生产状态" detail="生产绑定、当前硬门和未来样本观察分别显示；历史规则版本不会冒充当前标准通过。"><div className="model-family-grid compact">{families.map((row,i)=>{
      const isHsmm=row.model_key==="market_regime_hsmm_v1";const active=pick(row,["active_version"],{}) as Json;const latest=pick(row,["latest_version"],{}) as Json;const metrics=pick(latest,["metrics"],{}) as Json;const coverage=pick(row,["coverage"],{}) as Json;const artifact=pick(row,["active_artifact"],{}) as Json;const operation=operationByKey.get(String(row.model_key))||{};const onlineObservation=pick(operation,["online_observation"],{}) as Json;const candidateObservation=isHsmm?{} as Json:candidateObservationFor(String(row.model_key));const productionObservation=pick(operation,["production_observation","observation"],{}) as Json;const health={...pick(row,["live_health"],{}) as Json,...productionObservation};const blockers=pick(row,["blocked_reasons"],[]) as unknown;const gate=pick(row,["current_gate_audit.status"],"not_applicable");const evaluationStatus=String(pick(row,["evaluation_contract_status"],isHsmm?pick(hsmmValidation,["evaluation_contract"],null)?"current":"not_evaluated":"not_evaluated"));const taskOos=pick(metrics,["task_native_oos"],{}) as Json;const taskBaseline=pick(metrics,["task_baseline"],{}) as Json;const environmentCoverage=pick(metrics,["environment_coverage"],{}) as Json;const observationUnit=String(health.observation_unit||"个成熟结果");const candidateObservationText=candidateObservation.candidate_active?`${modelStatusLabel(candidateObservation.status)} · ${candidateObservation.observed_return_sessions||0}/${candidateObservation.required_return_sessions||20} 个真实未来收益日`:"当前没有已入组候选";const productionObservationText=isHsmm&&!hsmmValidationPassed?"验证失败，未进入影子观察":health.status&&health.status!=="not_applicable"?`${modelStatusLabel(health.status)} · ${health.mature_20d_outcomes||0}/${health.minimum_required||5} ${observationUnit}`:"未激活，不适用";const onlineAuthorizationText=isHsmm?"在线旁路 · 不控制风险预算":onlineObservation.status?`${modelStatusLabel(onlineObservation.status)} · ${modelStatusLabel(onlineObservation.authorization_tier)} · ${onlineObservation.future_observed_sessions||0}/20 日`:"在线状态正在读取";const hsmmHealthFailed=/failed|失败/.test(String(health.status));
      return <article key={String(row.model_key||i)}><header><div><span>{pick(row,["family"],"MODEL")}</span><h3>{pick(row,["name","model_key"],"模型")}</h3><small>{row.model_key}</small></div><div className="model-status-stack"><Status tone={row.status==="legacy_production"?"watch":"neutral"}>{modelStatusLabel(pick(row,["status"],"unknown"))}</Status>{!isHsmm&&onlineObservation.status&&<Status tone={onlineObservation.trade_authorized?"watch":"neutral"}>{modelStatusLabel(onlineObservation.status)}</Status>}{isHsmm&&health.status&&health.status!=="not_applicable"&&<Status tone={hsmmHealthFailed?"danger":health.status==="observing"?"watch":health.status==="degraded"?"danger":"good"}>{modelStatusLabel(health.status)}</Status>}</div></header><p>{pick(row,["role"],"未登记模型职责")}</p><dl><div><dt>生产版本</dt><dd>{pick(active,["version","id"],"未激活")}</dd></div><div><dt>在线权限</dt><dd>{onlineAuthorizationText}</dd></div><div><dt>{isHsmm?"最新影子版本":"最新版本"}</dt><dd>{pick(latest,["version","id"],pick(active,["version","id"],"尚无当前协议版本"))}</dd></div><div><dt>训练数据覆盖</dt><dd>{pick(coverage,["effective_sessions"],"正在读取")} / {pick(coverage,["formal_walk_forward_sessions","minimum_sessions","required_sessions"],"正在读取")}</dd></div><div><dt>评价协议</dt><dd>{modelStatusLabel(evaluationStatus)}</dd></div><div><dt>{isHsmm?"HSMM 任务 OOS":"任务原生 OOS"}</dt><dd>{isHsmm?modelStatusLabel(hsmmValidation.status):taskOos.window_count?`${taskOos.passed_windows||0}/${taskOos.window_count} 窗通过 · 同任务基线${taskBaseline.outperformed?"占优":"未占优"}`:"尚未执行"}</dd></div><div><dt>环境覆盖</dt><dd>{isHsmm?"4 个父状态 · 12 个市场分项":environmentCoverage.profile_count?`${environmentCoverage.covered_parent_states||0}/4 大类 · ${environmentCoverage.covered_profiles||0}/12 分项`:"尚未形成完整环境覆盖"}</dd></div>{isHsmm?<div><dt>影子观察</dt><dd>{productionObservationText}</dd></div>:<><div><dt>候选未来观察</dt><dd>{candidateObservationText}</dd></div><div><dt>生产运行观察</dt><dd>{productionObservationText}</dd></div></>}<div><dt>当前硬门</dt><dd>{modelStatusLabel(gate)}</dd></div><div><dt>{isHsmm?"状态更新时间":"候选观察起点"}</dt><dd>{isHsmm?formatLogTime(health.updated_at,"状态尚未更新"):candidateObservation.candidate_active?String(candidateObservation.signal_start_trade_date||"等待下一个交易日"):"当前没有已入组候选"}</dd></div></dl><EvidenceDetails title="验证、观察、阻塞与回退证据"><ModelEvidencePanel isHsmm={isHsmm} row={{...row,latest_version:latest}} metrics={metrics} health={health} artifact={artifact} blockers={Array.isArray(blockers)?blockers:[]} gateAudit={pick(row,["current_gate_audit"],{}) as Json} hsmmValidation={hsmmValidation}/></EvidenceDetails></article>
    })}</div></Section>
    <div className="two-column"><Section title="市场状态"><dl className="fact-list"><div><dt>当前状态</dt><dd><Status>{marketState}</Status></dd></div><div><dt>置信度</dt><dd>{marketConfidence===null||marketConfidence===undefined?"正在读取市场置信度":fmtPct(marketConfidence,false)}</dd></div><div><dt>环境分项</dt><dd>{scalarLabel(marketProfile,"正在读取环境分项")}</dd></div><div><dt>历史记录</dt><dd>{rowsFrom(pageData.modelMarketHistory,["items","history"]).length} 条</dd></div><div><dt>异常模型</dt><dd>{degradedCount} 个</dd></div></dl></Section><Section title="生产模型权重">{productionUsage.length?<CompactTable rows={productionUsage} columns={[["模型","name"],["观察进度","observation"],["基础","base"],["环境","environment"],["健康","health"],["成熟度","maturity"],["模型层归一化占比","normalized"],["最终融合权重","effective"]]} fallback="当前权重合同未记录"/>:<Empty title="当前生产激活模型为 0" detail="五个业务模型均未通过完整上线门，模型权重已归还基础因子与确定性规则；当前没有在线模型权重。"/>}</Section></div>
    <EvidenceDetails title={`底层模型版本记录（${registryVersions.length}）`}><CompactTable rows={registryVersions} columns={[["注册键","model_key"],["版本","version","id"],["状态","status"],["登记时间","trained_at","created_at"]]} fallback="登记时未记录该字段"/></EvidenceDetails>
  </>;
}

export function DataAssets({data}:{data:ConsoleData}){
  const assets=rowsFrom(data.watermarks,["items","assets","datasets"]);const failed=rowsFrom(data.failedRequests,["items","requests","failed_requests"]);const logs=rowsFrom(data.runtimeLogs,["items","logs"]);const taskRows=firstArray(data.tasks,["tasks","items"]);const modelBackfill=taskRows.find(row=>String(row.task_type||"")==="plan_model_data_backfill");const modelChecklist=modelBackfill?rowsFrom(pick(modelBackfill,["checkpoint"],{}) as Json,["checklist"]):[];
  const assetStatus=(row:Json)=>String(pick(row,["status","readiness"],"unknown"));
  const assetIsReady=(row:Json)=>/ready|complete|healthy|passed|normal/i.test(assetStatus(row));
  const readyAssets=assets.filter(assetIsReady);
  const reviewAssets=assets.filter(row=>!assetIsReady(row));
  const assetDegrees=Math.round(readyAssets.length/Math.max(1,assets.length)*360);
  const dataTasks=taskRows.filter(row=>/data|backfill|daily|watermark|asset|补全|日更|数据/i.test(`${pick(row,["task_type","module","name","task_name"],"")}`)).slice(0,4);
  const dataWork=modelBackfill?[modelBackfill,...dataTasks.filter(row=>row!==modelBackfill)].slice(0,4):dataTasks;
  const assetText=(row:Json,keys:string[],fallback="未记录")=>{const value=pick(row,keys,fallback);return typeof value==="object"?JSON.stringify(value):String(value);};
  const assetCoverageText=(row:Json)=>{const coverage=Number(pick(row,["metadata.coverage","coverage"],NaN));if(Number.isFinite(coverage))return fmtPct(coverage,false);const available=pick(row,["available_count"],null),expected=pick(row,["expected_count"],null);const availableNumber=Number(available),expectedNumber=Number(expected);if(available!==null&&expected!==null&&Number.isFinite(expectedNumber)&&expectedNumber>0)return `${available}/${expected}`;if(available!==null&&Number.isFinite(availableNumber))return `${new Intl.NumberFormat("zh-CN").format(availableNumber)} 条`;return "覆盖未记录";};
  const displayedAssets=assets.map(row=>({...row,display_name:pick(row,["metadata.asset_snapshot.name","name","dataset","asset_key","key"],"未命名资产"),display_coverage:assetCoverageText(row),display_watermark:formatSystemTime(pick(row,["watermark","latest_trade_date","updated_at","checked_at","metadata.asset_snapshot.max_date"],""),"水位未记录")}));
  return <><PageHeader eyebrow="DATA PLATFORM" title="数据资产" summary="决策硬门、资产水位与日更状态。"/>
    <div className="metric-grid headline-metrics data-headline-metrics"><Metric label="已登记资产" value={assets.length} note="水位与覆盖记录" icon={Database}/><Metric label="需要复核" value={reviewAssets.length} note="失败、部分或等待状态" tone={reviewAssets.length?"watch":"good"} icon={TriangleAlert}/><Metric label="OOS 硬数据" value={`${pick(data.oos,["eligible_sessions"],"未记录")}/${pick(data.oos,["required_sessions"],1537)}`} note="历史滚动验证覆盖" icon={Clock3}/><Metric label="密封集" value={`${pick(data.sealed,["eligible_sessions"],"未记录")}/${pick(data.sealed,["required_sessions"],126)}`} note="一次性保留集状态" icon={ShieldCheck}/></div>
    <div className="data-dashboard-grid">
      <aside className="data-readiness-card"><header><div><span>资产健康</span><h2>数据水位</h2></div><Status tone={reviewAssets.length?"watch":"good"}>{reviewAssets.length?"需要复核":"稳定"}</Status></header><div className="data-ready-ring" style={{background:`conic-gradient(#67d697 0deg ${assetDegrees}deg, #454358 ${assetDegrees}deg 360deg)`}}><div><strong>{readyAssets.length}/{assets.length}</strong><span>状态正常</span></div></div><dl><div><dt>当前数据任务</dt><dd>{dataWork.length}</dd></div><div><dt>失败请求</dt><dd className={failed.length?"negative":"positive"}>{failed.length}</dd></div><div><dt>需复核资产</dt><dd>{reviewAssets.length}</dd></div></dl></aside>
      <section className="data-asset-board"><header><div><span>实时水位</span><h2>关键数据资产</h2><p>按已有水位、覆盖和更新时间读取；完整资产清单保留在下方。</p></div><Database size={20}/></header>{assets.length?<div className="data-asset-list">{displayedAssets.slice(0,7).map((row,index)=><article key={String(pick(row,["asset_key","key","dataset","name"],index))}><Status>{assetStatus(row)}</Status><div><strong>{assetText(row,["display_name"],"未命名资产")}</strong><small>{assetText(row,["display_watermark"],"水位未记录")}</small></div><span>{assetText(row,["display_coverage"],"覆盖未记录")}</span></article>)}</div>:<Empty title="暂无资产水位" detail="数据平台尚未返回资产记录。"/>}</section>
    </div>
    {dataWork.length>0&&<Section title="当前补全与日更" detail="真实任务、阶段、进度与时间证据。" className="data-work-section"><div className="data-work-list">{dataWork.map((row,index)=>{const raw=Number(pick(row,["progress","progress_pct"],0));const pct=Math.max(0,Math.min(100,raw<=1?raw*100:raw));return <article key={String(pick(row,["id","task_id","name"],index))}><Status>{pick(row,["status","state"],"等待")}</Status><div><strong>{pick(row,["name","task_name","module"],"数据任务")}</strong><p>{pick(row,["current_step","message","summary"],"暂无步骤摘要")}</p><span><i style={{width:`${pct}%`}}/></span></div><b>{Math.round(pct)}%</b><small>{formatSystemTime(pick(row,["updated_at","heartbeat_at","next_run_at"],""),"等待时间证据")}</small><EvidenceDetails title="任务证据"><pre>{JSON.stringify({phase:row.phase,checkpoint:row.checkpoint,attempt:row.attempt,max_attempts:row.max_attempts,heartbeat_at:row.heartbeat_at,next_run_at:row.next_run_at,error:row.error},null,2)}</pre></EvidenceDetails></article>})}</div></Section>}
    {modelBackfill&&<Section title="模型历史数据补全工单" detail="清单、游标与当前进度。"><CompactTable rows={modelChecklist} columns={[["清单项","label"],["状态","status"],["当前覆盖","covered_raw_sessions","processed_calendar_days"],["目标","target_raw_sessions","target_calendar_days"],["缺口","missing_count"],["游标","cursor","source_window_start"]]}/><EvidenceDetails title="重试与队列"><pre>{JSON.stringify({attempt:modelBackfill.attempt,max_attempts:modelBackfill.max_attempts,error:modelBackfill.error,intervention:pick(modelBackfill,["checkpoint.intervention"],{}),queue:pick(modelBackfill,["checkpoint.queue"],{})},null,2)}</pre></EvidenceDetails></Section>}
    <Section title="全部资产与水位" detail="完整记录用于审计与问题定位。"><CompactTable rows={displayedAssets} columns={[["资产","display_name"],["状态","status","readiness"],["覆盖","display_coverage"],["水位","display_watermark"]]}/></Section><div className="two-column"><Section title="日更与数据流"><EvidenceDetails title="日更、数据流与监控"><pre>{JSON.stringify({daily:data.dailyUpdateStatus,streams:data.dataStreams,observability:data.dataObservability},null,2)}</pre></EvidenceDetails></Section><Section title="补全计划与进度"><EvidenceDetails title="补全进度"><pre>{JSON.stringify({plan:data.backfillPlan,status:data.backfillStatus},null,2)}</pre></EvidenceDetails></Section></div><div className="two-column"><Section title="失败请求分类"><CompactTable rows={failed.slice(0,20)} columns={[["接口","api_name","source","key"],["类别","category","status"],["尝试","attempts","attempt_count"],["错误","error","message"]]}/></Section><Section title="LLM 数据运维"><EvidenceDetails title="治理与执行记录"><pre>{JSON.stringify(data.dataOperations||{},null,2)}</pre></EvidenceDetails></Section></div><Section title="运行日志" detail="最近 100 条结构化日志。"><CompactTable rows={logs.slice(0,30)} columns={[["时间","created_at","timestamp"],["级别","level"],["模块","logger","module"],["消息","message","summary"]]}/></Section></>;
}

export function EvidenceChain({data:inputData}:{data:ConsoleData}){
  const data=inputData;const decisions=rowsFrom(data.decisions,["items","decisions"]);const reviews=rowsFrom(data.reviews,["items","reviews"]);const ledger=firstArray(data.ledgerDetail,["items"]);const reports=rowsFrom(data.performanceReports,["items","reports"]);const proposals=rowsFrom(data.llmProposals,["items"]);const llmQuality=pick(data.llmProposals,["quality_report"],{}) as Json;const llmQualityMetrics=pick(llmQuality,["metrics"],{}) as Json;const backtests=rowsFrom(data.backtests,["items"]);const firstDecisionId=Number(decisions[0]?.id||0);const [selectedId,setSelectedId]=useState<number>(firstDecisionId);const [selectedDetail,setSelectedDetail]=useState<Json>(data.decisionDetail||{});const [verification,setVerification]=useState<Json>(data.evidenceVerification||{});const selectedSummary=decisions.find(row=>Number(row.id)===selectedId)||decisions[0]||{};const selected=Number(selectedDetail.id||0)===selectedId?selectedDetail:selectedSummary;const execution=ledger.find(row=>Number(pick(row,["execution_id","id"],0))===Number(selected.execution_id||0));const review=reviews.find(row=>Number(row.decision_snapshot_id||0)===Number(selected.id||0));
  useEffect(()=>{const id=Number(selectedId||0);if(!id){setSelectedDetail({});setVerification({});return}const controller=new AbortController();const detailPromise=id===firstDecisionId&&data.decisionDetail!==undefined?Promise.resolve(data.decisionDetail):readApi(`/api/research/decisions/${id}`,controller.signal);const verificationPromise=id===firstDecisionId&&data.evidenceVerification!==undefined?Promise.resolve(data.evidenceVerification):readApi(`/api/research/evidence/verify/${id}`,controller.signal);Promise.allSettled([detailPromise,verificationPromise]).then(([detail,result])=>{if(controller.signal.aborted)return;if(detail.status==="fulfilled")setSelectedDetail(detail.value);if(result.status==="fulfilled")setVerification(result.value)});return()=>controller.abort()},[selectedId,data.decisionDetail,data.evidenceVerification,firstDecisionId]);
  const marketState=pick(selected,["market_state.label","market_state.state","market_state"],"未记录");const researchScore=pick(selected,["research_state.score","research_state.research_score"],"未记录");const valuationStatus=pick(selected,["valuation.status","valuation.method"],"未记录");const contract=pick(selected,["target_weight_contract"],{}) as Json;const outcome=pick(review,["outcome"],{}) as Json;const attribution=pick(review,["attribution"],{}) as Json;
  return <><PageHeader eyebrow="AUDIT EVIDENCE" title="决策证据链" summary="选择一次冻结决策，追踪当时数据、版本、动作、成交、收益和复盘。"/><div className="metric-grid headline-metrics"><Metric label="冻结决策" value={decisions.length}/><Metric label="已关联执行" value={decisions.filter(row=>row.execution_id).length}/><Metric label="已完成复盘" value={new Set(reviews.map(row=>row.decision_snapshot_id)).size}/><Metric label="证据异常" value={pick(data.decisionAudit,["quality.future_data_violations"],0)+pick(data.decisionAudit,["quality.missing_evidence"],0)} tone={pick(data.decisionAudit,["quality.future_data_violations"],0)+pick(data.decisionAudit,["quality.missing_evidence"],0)>0?"danger":"good"}/></div><Section title="选择决策"><label className="detail-select"><span>冻结决策快照</span><select value={Number(selected.id||0)} onChange={event=>setSelectedId(Number(event.target.value))}>{decisions.map(row=><option key={row.id} value={row.id}>#{row.id} · {pick(row,["decision_time"],"未记录")} · {pick(row,["ts_code"],"组合")} · {pick(row,["decision_type"],"未记录")}</option>)}</select></label></Section>{decisions.length?<><Section title={`决策 #${selected.id} · ${pick(selected,["ts_code"],"组合")}`} detail={String(pick(selected,["reason"],"未记录决策原因"))}><div className="audit-summary"><div><span>动作</span><strong>{pick(selected,["decision_type"],"未记录")}</strong></div><div><span>决策时间</span><strong>{pick(selected,["decision_time"],"未记录")}</strong></div><div><span>数据截止</span><strong>{pick(selected,["data_as_of_time"],"未记录")}</strong></div><div><span>证据完整性</span><Status tone={verification.verified?"good":"danger"}>{pick(verification,["status"],"校验中")}</Status></div></div></Section><div className="audit-timeline"><article><div className="audit-step">1</div><section><header><span>DATA</span><h2>点时数据冻结</h2><Status tone={String(selected.data_as_of_time)<=String(selected.decision_time)?"good":"danger"}>{String(selected.data_as_of_time)<=String(selected.decision_time)?"无未来数据":"时间违规"}</Status></header><dl><div><dt>数据截止</dt><dd>{pick(selected,["data_as_of_time"],"未记录")}</dd></div><div><dt>市场状态</dt><dd>{scalarLabel(marketState)}</dd></div><div><dt>研究评分</dt><dd>{String(researchScore)}</dd></div><div><dt>估值状态</dt><dd>{scalarLabel(valuationStatus)}</dd></div></dl><EvidenceDetails title="冻结市场、研究与估值证据"><pre>{JSON.stringify({market_state:selected.market_state,research_state:selected.research_state,valuation:selected.valuation},null,2)}</pre></EvidenceDetails></section></article><article><div className="audit-step">2</div><section><header><span>VERSION</span><h2>模型与策略版本</h2><Status>{pick(selected,["evidence_schema_version"],"未登记")}</Status></header><dl><div><dt>模型版本</dt><dd>{pick(selected,["model_version"],"未记录")}</dd></div><div><dt>证据哈希</dt><dd className="hash-value">{pick(selected,["evidence_hash"],"未记录")}</dd></div><div><dt>权重动作</dt><dd>{pick(contract,["action","function_bucket"],"未记录")}</dd></div><div><dt>目标权重</dt><dd>{fmtPct(pick(contract,["target_weight"]),false)}</dd></div></dl><EvidenceDetails title="组合与目标权重合同"><pre>{JSON.stringify({portfolio_context:selected.portfolio_context,target_weight_contract:contract,verification},null,2)}</pre></EvidenceDetails></section></article><article><div className="audit-step">3</div><section><header><span>EXECUTION</span><h2>计划与模拟成交</h2><Status tone={selected.execution_id?"good":"watch"}>{selected.execution_id?"已关联执行":"无关联成交"}</Status></header><dl><div><dt>执行批次</dt><dd>{pick(selected,["execution_id"],"未记录")}</dd></div><div><dt>逐笔操作</dt><dd>{pick(selected,["operation_id"],"未记录")}</dd></div><div><dt>成交日</dt><dd>{pick(execution,["execute_date","executed_at"],"未记录")}</dd></div><div><dt>费用</dt><dd>{fmtMoney(pick(execution,["summary.total_fee","total_fee","fees"]))}</dd></div></dl><EvidenceDetails title="成交批次与账本证据"><pre>{JSON.stringify(execution||{status:"未关联执行批次"},null,2)}</pre></EvidenceDetails></section></article><article><div className="audit-step">4</div><section><header><span>OUTCOME</span><h2>后续结果与复盘</h2><Status tone={review?"good":"watch"}>{review?"已复盘":"等待未来结果"}</Status></header><dl><div><dt>复盘时间</dt><dd>{pick(review,["review_time","created_at"],"未记录")}</dd></div><div><dt>错误类型</dt><dd>{pick(review,["error_type"],"无")}</dd></div><div><dt>后续收益</dt><dd>{fmtPct(pick(outcome,["return","stock_return","directional_return"]))}</dd></div><div><dt>经验结论</dt><dd>{pick(review,["lessons"],"等待成熟样本")}</dd></div></dl><EvidenceDetails title="结果与归因"><pre>{JSON.stringify({outcome,attribution},null,2)}</pre></EvidenceDetails></section></article></div></>:<Empty title="尚无冻结决策" detail="正式决策产生后将在这里形成审计时间线。"/>}<div className="two-column"><Section title="周期绩效与回测"><CompactTable rows={[...reports,...backtests].slice(0,30)} columns={[["类型","period_type","report_type","name"],["状态","status"],["收益","return_pct","total_return","period_return"],["时间","created_at","generated_at"]]}/></Section><Section title="LLM 提案治理"><dl className="fact-list"><div><dt>预算状态</dt><dd><Status>{pick(data.llmBudget,["status"],"证据缺失")}</Status></dd></div><div><dt>已用 Token</dt><dd>{pick(data.llmBudget,["used_tokens","usage.total_tokens"],"未记录")}</dd></div><div><dt>提案数量</dt><dd>{proposals.length}</dd></div><div><dt>质量状态</dt><dd><Status tone={String(llmQuality.status)==="measured"?"good":"watch"}>{pick(llmQuality,["status"],"正在积累")}</Status></dd></div><div><dt>同周期语义重复</dt><dd>{llmQualityMetrics.semantic_duplicate_rate===null||llmQualityMetrics.semantic_duplicate_rate===undefined?"等待样本":`${(Number(llmQualityMetrics.semantic_duplicate_rate)*100).toFixed(1)}%`}</dd></div><div><dt>冻结证据精确验证</dt><dd>{llmQualityMetrics.exact_evidence_verification_rate===null||llmQualityMetrics.exact_evidence_verification_rate===undefined?"等待新提案":`${(Number(llmQualityMetrics.exact_evidence_verification_rate)*100).toFixed(1)}%`}</dd></div><div><dt>完成但无新增动作</dt><dd>{pick(llmQualityMetrics,["completed_route_noop_count"],"未记录")} / {pick(llmQualityMetrics,["completed_route_count"],"未记录")}</dd></div><div><dt>生产修改</dt><dd>禁止</dd></div></dl><CompactTable rows={proposals.slice(0,12)} columns={[["提案","title","hypothesis","proposal_type"],["类型","target_type","kind"],["状态","status","governance_status"],["时间","created_at"]]}/><EvidenceDetails title="提案质量、重复、证据和路由聚合"><pre>{JSON.stringify(llmQuality,null,2)}</pre></EvidenceDetails></Section></div><Section title="独立账本与学习治理"><div className="two-column"><EvidenceDetails title="现金、持仓和漂移审计"><pre>{JSON.stringify(data.ledgerAudit||{},null,2)}</pre></EvidenceDetails><EvidenceDetails title="周期学习合同"><pre>{JSON.stringify({learning:data.reviewLearning,daily:data.reportCompact},null,2)}</pre></EvidenceDetails></div></Section></>;
}

function CompactTable({rows,columns,fallback="未记录"}:{rows:Json[],columns:string[][];fallback?:string}){return rows.length?<div className="table-wrap"><table><thead><tr>{columns.map(c=><th key={c[0]}>{c[0]}</th>)}</tr></thead><tbody>{rows.map((row,i)=><tr key={i}>{columns.map(c=>{const value=pick(row,c.slice(1),fallback);return <td key={c[0]}>{typeof value==="object"?JSON.stringify(value):String(value)}</td>})}</tr>)}</tbody></table></div>:<Empty/>}

function ReadOnlyPager({label,offset,pageSize,total,hasMore,loading,onChange}:{label:string;offset:number;pageSize:number;total:number;hasMore:boolean;loading:boolean;onChange:(offset:number)=>void}){
  const currentPage=Math.floor(offset/pageSize)+1;
  const totalPages=Math.max(1,Math.ceil(total/pageSize));
  return <div className="read-only-pager"><span>{label} · 第 {currentPage} / {totalPages} 页 · 共 {total} 条</span><div><button className="icon-button" type="button" title="上一页" aria-label={`${label}上一页`} disabled={loading||offset===0} onClick={()=>onChange(Math.max(0,offset-pageSize))}><ChevronLeft size={16}/></button><button className="icon-button" type="button" title="下一页" aria-label={`${label}下一页`} disabled={loading||!hasMore} onClick={()=>onChange(offset+pageSize)}><ChevronRight size={16}/></button></div></div>;
}

export function AdminOverview({ data }: { data: ConsoleData }) {
  const health = data.health || {};
  const [liveReadiness, setLiveReadiness] = useState<Json | undefined>(data.tradingReady);
  const dependencies = pick(health, ["dependencies"], {}) as Json;
  const [liveTaskPayload, setLiveTaskPayload] = useState<Json | undefined>(data.adminTasksLive || data.tasks);
  useEffect(() => {
    setLiveReadiness(data.tradingReady);
    setLiveTaskPayload(data.adminTasksLive || data.tasks);
  }, [data.adminTasksLive, data.tasks, data.tradingReady]);
  useEffect(() => {
    let active = true;
    let inFlight = false;
    const controller = new AbortController();
    const refreshLiveTasks = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const payload = await readApi("/api/model-strategy/task-center?compact=true&limit=30&log_limit=20", controller.signal);
        if (active) setLiveTaskPayload(payload);
      } catch {
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(refreshLiveTasks, 5_000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    let active = true;
    let inFlight = false;
    const controller = new AbortController();
    const refreshReadiness = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const payload = await readApi("/api/trading-ready?detail=compact", controller.signal, 30_000);
        if (active) setLiveReadiness(payload);
      } catch {
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(refreshReadiness, 15_000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, []);
  const readiness = liveReadiness || {};
  const readinessStatus = String(pick(readiness, ["status"], "unknown"));
  const readinessLabels: Record<string, string> = {
    alive: "存活", ready: "就绪", waiting: "等待", blocked: "阻断", running: "运行中",
    passed: "通过", observing: "观察中", failed: "失败", not_generated: "未生成", unknown: "未知",
  };
  const readinessLabel = (value: unknown) => readinessLabels[String(value || "unknown")] || String(value || "未知");
  const readinessTone = (value: unknown) => {
    const state = String(value || "");
    return state === "ready" || state === "alive" || state === "passed" || state === "ok" || state === "healthy" || state === "normal" || state === "completed" ? "good" : state === "blocked" || state === "failed" ? "danger" : "watch";
  };
  const layerRows = [
    { key: "process", label: "进程存活", status: pick(readiness, ["layers.process.status"], "unknown"), detail: pick(readiness, ["layers.process.detail"], "等待进程证据") },
    { key: "infrastructure", label: "基础设施就绪", status: pick(readiness, ["layers.infrastructure.status"], pick(health, ["status"], "unknown")), detail: "双库、Redis、后台组件和心跳" },
    { key: "trading", label: "交易业务就绪", status: readinessStatus, detail: pick(readiness, ["summary"], "等待业务门聚合") },
  ];
  const readinessGates = firstArray(readiness, ["gates"]);
  const blockers = Array.isArray(readiness.hard_blockers) ? readiness.hard_blockers.map((item: unknown) => String(item)) : [];
  const tasks = firstArray(liveTaskPayload, ["tasks", "items"]);
  const running = tasks.filter((row) => /running|leased|active/.test(String(pick(row, ["status", "state"], ""))));
  const failed = tasks.filter((row) => /failed|error|blocked/.test(String(pick(row, ["status", "state"], ""))));
  const continuousTypes = new Set(["evolution_worker_runtime", "evolution_scheduler_runtime"]);
  const dailyTypes = new Set(["daily_market_update", "latest_decision_data_gate", "system_autonomy_cycle", "factor_daily_cycle", "strategy_shadow_cycle"]);
  const weeklyTypes = new Set(["strategy_evolution_cycle", "strategy_controller_oos"]);
  const classifyTask = (row: Json) => {
    const type = String(pick(row, ["task_type"], ""));
    const name = String(pick(row, ["name", "task_name"], ""));
    if (continuousTypes.has(type) || /Worker|Scheduler|常驻|持续领取|持续创建/.test(name)) return "continuous";
    if (weeklyTypes.has(type) || /每周|周度|正式策略演化|控制器 OOS/.test(name)) return "weekly";
    if (dailyTypes.has(type) || /每日|日更|日评价|决策数据门|影子观察/.test(name)) return "daily";
    return "scheduled";
  };
  const currentTask = (rows: Json[]) => {
    const activeRows = rows.filter((row) => /running|waiting|observing|degraded|blocked/.test(String(pick(row, ["status", "state"], ""))));
    return (activeRows.length ? activeRows : rows).slice(0, 8);
  };
  const continuousTasks = currentTask(tasks.filter((row) => classifyTask(row) === "continuous"));
  const dailyTasks = currentTask(tasks.filter((row) => classifyTask(row) === "daily"));
  const weeklyTasks = currentTask(tasks.filter((row) => classifyTask(row) === "weekly"));
  const scheduledTasks = currentTask(tasks.filter((row) => classifyTask(row) === "scheduled"));
  const watermarks = rowsFrom(data.watermarks, ["items", "assets", "datasets"]);
  const staleAssets = watermarks.filter((row) => /stale|failed|blocked|degraded/.test(String(pick(row, ["status", "readiness"], ""))));
  const ledgerStatus = String(pick(data.ledgerAudit, ["status"], pick(data.ledgerAudit, ["ok"], null) === true ? "ready" : "unknown"));
  const dailyPhase = String(pick(data.dailyUpdateStatus, ["phase"], "idle"));
  const dailyRunning = Boolean(pick(data.dailyUpdateStatus, ["running"], false));
  const dailyReadyDate = String(pick(data.dailyUpdateStatus, ["latest_decision_ready_trade_date"], ""));
  const dailyTargetDate = String(pick(data.dailyUpdateStatus, ["trade_date"], dailyReadyDate));
  const dailyStatus = dailyRunning ? "running" : dailyPhase === "failed" ? "failed" : dailyReadyDate ? "ready" : "waiting";
  const componentState = (value: unknown) => {
    const item = value && typeof value === "object" ? value as Json : {};
    if (pick(item, ["status"], null)) return String(item.status);
    if (pick(item, ["ok"], null) === true || pick(item, ["available"], null) === true) return "ready";
    if (pick(item, ["should_start"], null) === true || pick(item, ["running"], null) === true) return "running";
    if (pick(item, ["mode"], "") === "disabled") return "disabled";
    return "degraded";
  };
  const dependencyRows = [
    { key: "主业务库", status: componentState(dependencies.primary_database), detail: pick(dependencies.primary_database, ["database", "message"], "alis_sim") },
    { key: "数据平台库", status: componentState(dependencies.data_platform_database), detail: pick(dependencies.data_platform_database, ["database", "message"], "alis_data") },
    { key: "Redis", status: componentState(dependencies.redis), detail: pick(dependencies.redis, ["message", "reason"], pick(dependencies.redis, ["available"], false) ? "连接可用" : "连接不可用") },
    { key: "后台运行边界", status: pick(dependencies.background_runtime, ["should_start"], false) ? "owner" : "follower", detail: pick(dependencies.background_runtime, ["reason"], "未返回边界证据") },
    { key: "演化运行时", status: componentState(dependencies.evolution_runtime), detail: pick(dependencies.evolution_runtime, ["ok"], false) ? "Worker 与 Scheduler 健康" : "检查组件心跳" },
  ];
  const chain = [
    { name: "基础服务", status: pick(health, ["status"], "unknown"), detail: dependencyRows.length + " 个运行依赖" },
    { name: "日更链路", status: dailyStatus, detail: dailyRunning ? String(pick(data.dailyUpdateStatus, ["message"], "正在更新")) : dailyReadyDate ? "最近可决策 " + dailyReadyDate + (dailyTargetDate && dailyTargetDate !== dailyReadyDate ? " · 目标 " + dailyTargetDate : "") : "等待首次完整日更" },
    { name: "决策硬数据", status: staleAssets.length ? "degraded" : watermarks.length ? "ready" : "unknown", detail: (watermarks.length - staleAssets.length) + "/" + watermarks.length + " 项无异常标记" },
    { name: "自治任务", status: failed.length ? "failed" : running.length ? "running" : tasks.length ? "ready" : "unknown", detail: running.length + " 运行 · " + failed.length + " 异常" },
    { name: "模拟账本", status: ledgerStatus, detail: String(pick(data.ledgerAudit, ["reason", "summary", "checked_at"], "只读一致性审计")) },
    { name: "OOS 硬数据", status: pick(data.oos, ["status"], "unknown"), detail: pick(data.oos, ["eligible_sessions"], "未记录") + "/" + pick(data.oos, ["required_sessions"], 1537) + " 个交易日" },
    { name: "密封保留集", status: pick(data.sealed, ["status"], "unknown"), detail: pick(data.sealed, ["eligible_sessions"], "未记录") + "/" + pick(data.sealed, ["required_sessions"], 126) + " 个交易日" },
  ];
  return <><PageHeader eyebrow="ADMIN CONSOLE" title="系统管理后台" summary="先看系统是否活着、基础服务是否就绪，再看当前是否允许自动运行；页面保持只读。"/>
    <section className={`readiness-hero readiness-${readinessStatus}`}><div><p className="eyebrow">TRADING READINESS</p><h2>{readinessStatus === "ready" ? "当前可运行" : readinessStatus === "waiting" ? "健康，等待条件" : readinessStatus === "blocked" ? "自动运行已阻断" : "正在读取业务状态"}</h2><p>{pick(readiness, ["summary"], "业务就绪聚合尚未返回")}</p></div><div className="readiness-hero-meta"><Status tone={readinessTone(readinessStatus)}>{readinessLabel(readinessStatus)}</Status><small>检查于 {pick(readiness, ["checked_at"], "未记录")}</small></div></section>
    <div className="health-layer-grid">{layerRows.map((row) => <article key={row.key} className={`health-layer health-layer-${String(row.status)}`}><div><span>{row.label}</span><Status tone={readinessTone(row.status)}>{readinessLabel(row.status)}</Status></div><strong>{row.detail}</strong><small>{row.key === "trading" ? "业务硬门聚合" : row.key === "infrastructure" ? "运行依赖" : "服务进程"}</small></article>)}</div>
    {blockers.length > 0 && <section className="readiness-blockers"><Status tone="danger">阻断原因 {blockers.length}</Status><div>{blockers.slice(0, 4).map((item, index) => <p key={index}>{item}</p>)}</div></section>}
    <Section title="业务就绪门" detail="只有数据、账本、冻结计划和执行边界同时满足，才会进入 ready；waiting 不是故障。"><div className="readiness-gates">{readinessGates.length ? readinessGates.map((gate) => <article key={String(gate.key)} className={`readiness-gate readiness-gate-${String(gate.status)}`}><div><span>{String(gate.label || gate.key || "业务门")}</span><Status tone={readinessTone(gate.status)}>{readinessLabel(gate.status)}</Status></div><strong>{String(gate.summary || "状态证据已记录")}</strong><small>{gate.key === "execution_lease" ? "防重复执行" : gate.key === "pending_plan" ? "自然交易日" : "只读审计"}</small></article>) : <Empty title="业务就绪证据尚未返回" detail="管理页不会用推测值替代后端状态。"/>}</div></Section>
    <div className="metric-grid headline-metrics"><Metric label="基础健康" value={<Status tone={readinessTone(pick(health, ["status"], "unknown"))}>{readinessLabel(pick(health, ["status"], "unknown"))}</Status>} note={pick(health, ["ready"], false) ? "关键依赖已就绪" : "存在未就绪依赖"}/><Metric label="运行任务" value={running.length}/><Metric label="异常任务" value={failed.length} tone={failed.length ? "danger" : "good"}/><Metric label="异常数据资产" value={staleAssets.length} tone={staleAssets.length ? "watch" : "good"}/></div>
    <Section title="运行链路" detail="这是全链路观测，不替代上方的交易业务就绪结论。"><div className="health-chain">{chain.map((item) => <article key={item.name}><Status>{readinessLabel(item.status)}</Status><strong>{item.name}</strong><p>{item.detail}</p></article>)}</div></Section>
    <div className="two-column"><Section title="基础依赖" detail="来自现有 /api/health，不触发刷新或修复。"><div className="dependency-list">{dependencyRows.map((row) => <article key={row.key}><div><strong>{row.key}</strong><p>{String(row.detail)}</p></div><Status>{row.status}</Status></article>)}</div></Section><Section title="当前处置建议" detail="管理后台不提供绕过硬门的操作。"><dl className="fact-list"><div><dt>页面权限</dt><dd>只读</dd></div><div><dt>日更异常</dt><dd>{/failed|blocked|error/.test(dailyStatus) ? "进入数据与日更查看证据" : "无需人工处理"}</dd></div><div><dt>任务异常</dt><dd>{failed.length ? "进入任务与链路定位失败步骤" : "无需人工处理"}</dd></div><div><dt>账本异常</dt><dd>{/failed|blocked|error|drift/.test(ledgerStatus) ? "执行门应保持阻断" : "无漂移证据"}</dd></div></dl></Section></div>
    <Section title="持续运行" detail="Worker 与 Scheduler 每 5 秒刷新一次心跳和当前步骤。"><TaskCategory rows={continuousTasks}/></Section>
    <div className="two-column"><Section title="日更任务" detail="交易日收盘更新、决策数据门、自治循环、因子日评价和 Shadow 观察。"><TaskCategory rows={dailyTasks}/></Section><Section title="周更任务" detail="正式策略演化与策略控制器 OOS；按周调度或自然窗口前移运行。"><TaskCategory rows={weeklyTasks}/></Section></div>
    {scheduledTasks.length > 0 && <Section title="其他计划任务" detail="不属于常驻、日更或周更合同的补全、恢复和专项任务。"><TaskCategory rows={scheduledTasks}/></Section>}
  </>;
}

function TaskCategory({ rows, className = "" }: { rows: Json[]; className?: string }) {
  return rows.length ? <div className={`task-category ${className}`}>{rows.map((row, index) => {
    const raw = Number(pick(row, ["progress", "progress_pct"], 0));
    const progress = raw <= 1 ? raw * 100 : raw;
    return <article key={String(pick(row, ["id"], index))}><div><strong>{pick(row, ["name", "task_name", "module"], "计划任务")}</strong><p>{pick(row, ["current_step", "message", "phase"], "等待下一次调度")}</p></div><div className="task-category-state"><Status>{runtimeStatusLabel(pick(row, ["status", "state"], "waiting"))}</Status><small>{Math.round(progress)}% · {formatSystemTime(pick(row, ["heartbeat_at", "updated_at", "next_run_at"], ""), "等待时间证据")}</small></div></article>;
  })}</div> : <Empty title="当前没有此类任务" detail="调度器会在满足频率和数据门后自动创建。"/>;
}

function formatSystemTime(value: unknown, fallback = "未记录") {
  const text = String(value || "");
  if (!text) return fallback;
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text.replace("T", " ").replace(/([+-]\d\d:\d\d|Z)$/, "").split(".")[0];
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(parsed).replace(/\//g, "-");
}

function runtimeStatusLabel(value: unknown) {
  return ({
    running: "运行中", active: "运行中", completed: "已完成", failed: "失败",
    blocked: "阻断", degraded: "已降级", partial: "部分可用", observing: "观察中",
    idle: "空闲", paused: "已暂停", waiting: "等待", queued: "排队",
    waiting_schedule: "等待收盘调度", recovery_pending: "等待安全恢复",
    retry_wait: "等待重试", ready: "就绪", healthy: "健康", normal: "正常",
    alive: "存活", owner: "主实例", follower: "从实例", stopping: "正在停止",
    unknown: "未知",
  } as Record<string, string>)[String(value || "unknown")] || String(value || "未知");
}

export function SystemObservatory({ data }: { data: ConsoleData }) {
  const payload = data.adminTasksLive || data.tasks || {};
  const taskRows = firstArray(payload, ["tasks", "items"]);
  const businessTasks = firstArray(payload, ["active_business_tasks"]);
  const runtimeServices = firstArray(payload, ["runtime_services"]);
  const modules = firstArray(payload, ["modules"]);
  const legacyBusinessTasks = taskRows.filter((task) => pick(task, ["source"], "") !== "runtime" && pick(task, ["activity_scope"], "current") !== "history" && /running|waiting|recovery_pending|observing|blocked|degraded|partial|failed/.test(String(pick(task, ["status", "state"], "")).toLowerCase()));
  const visibleBusinessTasks = businessTasks.length ? businessTasks : legacyBusinessTasks;
  const legacyRuntime = taskRows.filter((task) => pick(task, ["source"], "") === "runtime");
  const visibleRuntime = runtimeServices.length ? runtimeServices : legacyRuntime;
  const summary = pick(payload, ["business_summary"], {
    active: visibleBusinessTasks.length,
    running: visibleBusinessTasks.filter((task) => pick(task, ["status"], "") === "running").length,
    blocked: visibleBusinessTasks.filter((task) => pick(task, ["status"], "") === "blocked").length,
    failed: visibleBusinessTasks.filter((task) => pick(task, ["status"], "") === "failed").length,
  }) as Json;
  const recentBusiness = taskRows.filter((task) => pick(task, ["source"], "") !== "runtime").slice(0, 6);
  const activeCount = Number(pick(summary, ["active"], visibleBusinessTasks.length));
  const runningCount = Number(pick(summary, ["running"], 0));
  const waitingCount = Number(pick(summary, ["waiting"], 0));
  const blockedCount = Number(pick(summary, ["blocked"], 0)) + Number(pick(summary, ["failed"], 0));
  const healthStatus = pick(data.health, ["status"], "unknown");
  const tradingStatus = pick(data.tradingReady, ["status"], "unknown");
  return <><PageHeader eyebrow="SYSTEM OBSERVATORY" title="系统运行" summary="业务推进、运行服务与近期记录。" />
    <div className="system-hero-grid"><article className={`system-overview-card system-business-card ${blockedCount?"is-blocked":""}`}><header><span className="system-card-icon"><Workflow size={19}/></span><Status tone={blockedCount?"danger":runningCount?"info":waitingCount?"watch":"good"}>{blockedCount?"存在阻断":runningCount?"正在推进":waitingCount?"等待中":"当前空闲"}</Status></header><span>当前业务</span><h2>{activeCount?`${activeCount} 个任务处于活动状态`:"当前没有业务任务"}</h2><p>{blockedCount?`${blockedCount} 个任务阻断或失败，详情保留在下方任务证据。`:runningCount?`${runningCount} 个任务正在实际执行。`:waitingCount?`${waitingCount} 个任务正在等待时间窗口、上游数据或调度条件。`:"等待下一次数据、决策、执行或研究调度。"}</p><footer><div><span>活动任务</span><strong>{activeCount}</strong></div><div><span>实际运行</span><strong>{runningCount}</strong></div><div><span>阻断 / 失败</span><strong className={blockedCount?"negative":""}>{blockedCount}</strong></div></footer></article><article className="system-overview-card system-foundation-card"><header><span className="system-card-icon"><ServerCog size={19}/></span><Status>{runtimeStatusLabel(healthStatus)}</Status></header><span>运行底座</span><h2>{visibleRuntime.length} 个服务已登记</h2><p>Worker 与 Scheduler 心跳统一转换为 UTC+8 展示。</p><dl><div><dt>交易业务</dt><dd><Status>{runtimeStatusLabel(tradingStatus)}</Status></dd></div><div><dt>检查时间</dt><dd>{formatSystemTime(pick(data.health,["checked_at","generated_at"]))}</dd></div></dl></article></div>
    <Section title="当前业务推进" className="system-business-section">{visibleBusinessTasks.length ? <TaskCategory rows={visibleBusinessTasks.slice(0, 12)} className="system-business-list"/> : <Empty title="当前没有业务任务" detail="等待下一次调度。" />}</Section>
    <Section title="运行底座" detail="常驻服务不计入正在执行的业务任务。" className="system-runtime-section">{visibleRuntime.length ? <TaskCategory rows={visibleRuntime} className="system-runtime-grid"/> : <Empty title="暂无运行服务记录" />}</Section>
    <Section title="关键组件">{modules.length ? <div className="health-chain system-component-grid">{modules.slice(0, 8).map((module, index) => <article key={String(pick(module, ["key", "name"], index))}><Status>{runtimeStatusLabel(pick(module, ["status"], "unknown"))}</Status><strong>{pick(module, ["name", "key"], "组件")}</strong><p>{pick(module, ["current_step", "next_step", "message"], "等待下一步")}</p></article>)}</div> : <Empty title="暂无组件状态" />}</Section>
    <Section title="最近业务记录">{recentBusiness.length ? <TaskCategory rows={recentBusiness} className="system-recent-list"/> : <Empty title="暂无近期业务记录" />}</Section>
  </>;
}

export function SystemVersions({ data }: { data: ConsoleData }) {
  const status = data.systemVersionStatus || {};
  const active = pick(status, ["active_release"], {}) as Json;
  const releases = firstArray(data.systemReleases, ["items"]);
  const contracts = Object.entries(pick(data.systemContracts, ["items"], {}) as Json);
  const revisions = pick(status, ["database_revisions"], {}) as Json;
  const expected = pick(status, ["expected_database_revisions"], {}) as Json;
  const unlinked = pick(status, ["legacy_unlinked_records"], {}) as Json;
  const governance = pick(status, ["governance"], {}) as Json;
  const components = firstArray(active, ["components"]);
  const healthy = Boolean(pick(status, ["ok"], false));
  return <><PageHeader eyebrow="SYSTEM RELEASE CENTER" title="版本与发布" summary="统一查看代码、数据库、策略、模型、因子和数据合同的冻结发布。页面只读，发布与回滚必须通过后端确定性校验。" />
    <div className="system-hero-grid"><article className={`system-overview-card ${healthy?"":"is-blocked"}`}><header><span className="system-card-icon"><GitBranch size={19}/></span><Status tone={healthy?"good":"danger"}>{healthy?"发布一致":"阻断新计划"}</Status></header><span>当前系统发布</span><h2>{pick(active,["release_key"],"尚未激活")}</h2><p>{healthy?"运行组件与冻结清单一致。":`原因：${pick(status,["blocked_reason"],"未建立版本证据")}`}</p><footer><div><span>发布 ID</span><strong>{pick(active,["id"],"—")}</strong></div><div><span>Git</span><strong>{String(pick(active,["git_commit"],"—")).slice(0,10)}</strong></div><div><span>组件</span><strong>{components.length}</strong></div></footer></article><article className="system-overview-card system-foundation-card"><header><span className="system-card-icon"><Database size={19}/></span><Status tone={revisions.sim===expected.sim&&revisions.data===expected.data?"good":"danger"}>双库迁移</Status></header><span>数据库修订</span><h2>{revisions.sim || "未登记"}</h2><p>数据平台：{revisions.data || "未登记"}</p><dl><div><dt>运行库目标</dt><dd>{expected.sim || "—"}</dd></div><div><dt>数据库目标</dt><dd>{expected.data || "—"}</dd></div></dl></article></div>
    {pick(status,["drift"],false) && <Section title="版本漂移" detail="漂移不会被静默接受，新正式计划保持阻断。"><div className="dependency-list">{(pick(status,["drift_components"],[]) as string[]).map(key=><article key={key}><div><strong>{key}</strong><p>冻结值与当前观测值不同</p></div><Status tone="danger">漂移</Status></article>)}</div></Section>}
    <Section title="当前发布组件" detail="版本清单不可变；原生模型、策略和因子注册表仍是各自权威来源。">{components.length?<div className="dependency-list">{components.map((row,index)=><article key={String(pick(row,["component_key"],index))}><div><strong>{pick(row,["component_key"],"组件")}</strong><p>组件版本记录 #{pick(row,["component_version_id"],"—")}</p></div><Status>{pick(row,["required"],false)?"必需":"可选"}</Status></article>)}</div>:<Empty title="尚无激活发布组件" />}</Section>
    <div className="two-column"><Section title="发布历史" detail="同一清单幂等复用，不重复制造版本。">{releases.length?<div className="task-category">{releases.map(row=><article key={String(row.id)}><div><strong>{row.release_key}</strong><p>{String(row.manifest_hash||"").slice(0,16)} · {formatSystemTime(row.created_at)}</p></div><Status tone={row.status==="active"?"good":row.status==="blocked"?"danger":"neutral"}>{row.status}</Status></article>)}</div>:<Empty title="暂无发布历史" />}</Section><Section title="关键数据合同" detail="关键合同参加发布兼容性硬门。"><div className="dependency-list">{contracts.map(([key,value])=><article key={key}><div><strong>{key}</strong><p>{String((value as Json).version||"")}</p></div><Status>{(value as Json).critical?"关键":"观察"}</Status></article>)}</div></Section></div>
    <div className="two-column"><Section title="自治发布治理" detail="每五分钟发现一次变化；只有确定性硬门全部通过才自动激活。"><dl className="fact-list"><div><dt>自动激活</dt><dd>{pick(governance,["auto_activate_enabled"],false)?"已启用":"已关闭"}</dd></div><div><dt>运行代码</dt><dd>{String(pick(governance,["runtime_git.commit"],"—")).slice(0,10)}</dd></div><div><dt>仓库代码</dt><dd>{String(pick(governance,["repository_git.commit"],"—")).slice(0,10)}</dd></div><div><dt>需要重启</dt><dd>{pick(governance,["restart_required"],false)?"是，禁止激活":"否"}</dd></div><div><dt>最近治理事件</dt><dd>{pick(governance,["latest_event.event_type"],"尚无")}</dd></div></dl></Section><Section title="历史谱系完整性" detail="旧记录不会伪造 Git 或数据库版本；缺少发布引用的历史记录显式保留为未知。"><dl className="fact-list"><div><dt>调仓计划未关联</dt><dd>{unlinked.daily_rebalance_plans ?? "—"}</dd></div><div><dt>决策快照未关联</dt><dd>{unlinked.decision_snapshots ?? "—"}</dd></div><div><dt>模拟成交未关联</dt><dd>{unlinked.executions ?? "—"}</dd></div></dl></Section></div>
  </>;
}

export function Operations({ data }: { data: ConsoleData }) {
  const owner=unwrapOwner(data.owner); const tasks=firstArray(data.tasks,["tasks","items"]); const logs=firstArray(data.tasks,["logs"]); const incidents=ownerIncidents(owner);const apiRows=apiCatalogRows(data.apiCatalog);const readApis=apiRows.filter(row=>row.method==="GET");const coveredApis=readApis.filter(row=>row.covered==="前端已读取");const quantTasks=rowsFrom(data.quantTasks,["items","tasks"]);
  const taskStatus=(row:Json)=>String(pick(row,["status","state"],"unknown")).toLowerCase();
  // Historical task evidence stays inspectable below, but must not make the live
  // operations summary look like there is a current operational incident.
  const currentTasks=tasks.filter(row=>pick(row,["activity_scope"],"current")!=="history");
  const runningTasks=currentTasks.filter(row=>/^(running|active|in_progress|executing)$/.test(taskStatus(row)));
  const queuedTasks=currentTasks.filter(row=>/^(queued|waiting|waiting_schedule|recovery_pending|pending|retry_wait|scheduled)$/.test(taskStatus(row)));
  const problemTasks=currentTasks.filter(row=>/^(failed|blocked|error|cancelled)$/.test(taskStatus(row)));
  const completedTasks=currentTasks.filter(row=>/^(completed|passed|succeeded|done)$/.test(taskStatus(row)));
  const classifiedCount=runningTasks.length+queuedTasks.length+problemTasks.length+completedTasks.length;
  const recordedTasks=Math.max(0,currentTasks.length-classifiedCount);
  const liveTasks=[...runningTasks,...queuedTasks].slice(0,5);
  const recentTasks=tasks.filter(row=>!liveTasks.includes(row)).slice(0,8);
  const distribution=[
    {key:"running",label:"运行中",count:runningTasks.length,color:"#5dd4e7",tone:"info" as const},
    {key:"queued",label:"等待 / 队列",count:queuedTasks.length,color:"#f4c35b",tone:"watch" as const},
    {key:"completed",label:"完成",count:completedTasks.length,color:"#64d38b",tone:"good" as const},
    {key:"problem",label:"阻断 / 失败",count:problemTasks.length,color:"#ff7085",tone:"danger" as const},
    {key:"recorded",label:"其他记录",count:recordedTasks,color:"#9d91ff",tone:"neutral" as const},
  ];
  let distributionOffset=0;
  const distributionGradient=currentTasks.length?`conic-gradient(${distribution.filter(item=>item.count>0).map(item=>{const start=distributionOffset;distributionOffset+=item.count/currentTasks.length*100;return `${item.color} ${start}% ${distributionOffset}%`;}).join(",")})`:"conic-gradient(#38394a 0 100%)";
  const pageSize=20; const apiPageSize=25; const [operationsOffset,setOperationsOffset]=useState(0); const [executionsOffset,setExecutionsOffset]=useState(0); const [apiOffset,setApiOffset]=useState(0); const [operationsPage,setOperationsPage]=useState<Json|null>(null); const [executionsPage,setExecutionsPage]=useState<Json|null>(null); const [pageLoading,setPageLoading]=useState<"operations"|"executions"|null>(null); const [pageError,setPageError]=useState(""); const operationsRequest=useRef<AbortController|null>(null); const executionsRequest=useRef<AbortController|null>(null);
  const operationsPayload=operationsPage||data.operationsPage; const executionsPayload=executionsPage||data.executionsPage; const operations=rowsFrom(operationsPayload,["items"]); const executions=rowsFrom(executionsPayload,["items"]); const operationsTotal=number(operationsPayload,["total"],operations.length); const executionsTotal=number(executionsPayload,["total"],executions.length); const operationsHasMore=Boolean(pick(operationsPayload,["has_more"],operationsOffset+operations.length<operationsTotal)); const executionsHasMore=Boolean(pick(executionsPayload,["has_more"],executionsOffset+executions.length<executionsTotal));
  const visibleApiRows=apiRows.slice(apiOffset,apiOffset+apiPageSize);
  useEffect(()=>()=>{operationsRequest.current?.abort();executionsRequest.current?.abort()},[]);
  async function changePage(kind:"operations"|"executions",offset:number){
    const bounded=Math.max(0,offset); const requestRef=kind==="operations"?operationsRequest:executionsRequest; const setter=kind==="operations"?setOperationsPage:setExecutionsPage; const setOffset=kind==="operations"?setOperationsOffset:setExecutionsOffset;
    requestRef.current?.abort();
    if(bounded===0){setter(null);setOffset(0);setPageError("");return}
    const controller=new AbortController(); requestRef.current=controller; setPageLoading(kind); setPageError("");
    try{const value=await readApi(`/api/${kind}/page?limit=${pageSize}&offset=${bounded}`,controller.signal);if(!controller.signal.aborted){setter(value);setOffset(bounded)}}catch(error:any){if(String(error?.name)!=="AbortError")setPageError(`${kind==="operations"?"操作":"执行"}分页读取失败：${String(error?.message||error)}`)}finally{if(requestRef.current===controller){requestRef.current=null;setPageLoading(null)}}
  }
  const grouped=[{key:"self_healing",title:"系统正在自行处理",tone:"info"},{key:"waiting_condition",title:"等待自然条件或上游发布",tone:"watch"},{key:"internal_fault",title:"内部故障",tone:"danger"},{key:"owner_required",title:"需要所有者授权",tone:"danger"}] as const;
  return <><a className="back-link" href="/overview">← 返回总览</a><PageHeader eyebrow="SYSTEM OPERATIONS" title="任务与链路" summary="当前自治进程、任务状态和可回放的运行证据。" />
    <div className="metric-grid headline-metrics ops-headline-metrics"><Metric label="运行中" value={runningTasks.length} note="正在占用运行资源的任务" tone="info" icon={Radio}/><Metric label="等待 / 队列" value={queuedTasks.length} note="等待上游、时间窗口或重试" tone="watch" icon={Clock3}/><Metric label="本轮完成" value={completedTasks.length} note="当前任务中心的已完成记录" tone="good" icon={ShieldCheck}/><Metric label="阻断 / 失败" value={problemTasks.length} note="需要在下方保留证据" tone={problemTasks.length?"danger":"neutral"} icon={TriangleAlert}/></div>
    <div className="ops-dashboard-grid">
      <section className="ops-live-board">
        <header className="ops-live-head"><div><span>即时执行</span><h2>{liveTasks.length?`${liveTasks.length} 项任务正在推进`:"当前没有正在推进的任务"}</h2><p>{liveTasks.length?"只展示运行中与等待调度的真实任务；历史完成记录收拢在下方。":"当前没有占用 Worker 的任务，等待下一次数据、决策或研究调度。"}</p></div><div className="ops-live-count"><Workflow size={20}/><strong>{liveTasks.length}</strong><small>活动任务</small></div></header>
        {liveTasks.length?<div className="ops-live-list">{liveTasks.map((row,i)=>{const raw=Number(pick(row,["progress","progress_pct"],0));const pct=Math.max(0,Math.min(100,raw<=1?raw*100:raw));return <article key={String(pick(row,["id","task_id","name"],i))}><div className="ops-task-status"><Status>{pick(row,["status","state"],"等待")}</Status><small>{formatSystemTime(pick(row,["heartbeat_at","updated_at","next_run_at"],""),"等待时间证据")}</small></div><div className="ops-task-copy"><strong>{pick(row,["name","task_name","module"],"自治任务")}</strong><p>{pick(row,["current_step","message","summary"],"暂无步骤摘要")}</p><div className="ops-progress"><i style={{width:`${pct}%`}} /></div></div><b>{Math.round(pct)}%</b><EvidenceDetails title="任务证据"><pre>{JSON.stringify({phase:row.phase,checkpoint:row.checkpoint,attempt:row.attempt,max_attempts:row.max_attempts,lease:row.lease,heartbeat_at:row.heartbeat_at,next_step:row.next_step,next_run_at:row.next_run_at,error:row.error},null,2)}</pre></EvidenceDetails></article>})}</div>:<Empty title="暂无活动任务" detail="任务中心没有返回运行中或等待中的任务。" />}
      </section>
      <aside className="ops-distribution-card">
        <header><div><span>任务分布</span><strong>实时状态结构</strong></div><ListTree size={19}/></header>
        <div className="ops-status-ring" style={{background:distributionGradient}}><div><strong>{currentTasks.length}</strong><span>当前任务</span></div></div>
        <div className="ops-status-legend">{distribution.filter(item=>item.count>0).map(item=><div key={item.key}><i style={{background:item.color}}/><span>{item.label}</span><strong>{item.count}</strong></div>)}</div>
        <footer><span>量化任务</span><strong>{quantTasks.length}</strong><span>只读接口覆盖</span><strong>{`${coveredApis.length}/${readApis.length}`}</strong></footer>
      </aside>
    </div>
    <Section title="最近任务" detail="完成、阻断与其他历史记录保留为审计证据。" className="ops-recent-section">{recentTasks.length?<div className="ops-recent-list">{recentTasks.map((row,i)=>{const raw=Number(pick(row,["progress","progress_pct"],0));const pct=Math.max(0,Math.min(100,raw<=1?raw*100:raw));return <article key={String(pick(row,["id","task_id","name"],i))}><Status>{pick(row,["status","state"],"未知")}</Status><div><strong>{pick(row,["name","task_name","module"],"自治任务")}</strong><p>{pick(row,["current_step","message","summary"],"暂无步骤摘要")}</p></div><small>{formatSystemTime(pick(row,["updated_at","heartbeat_at","finished_at"],""),"未记录时间")}</small><b>{Math.round(pct)}%</b><EvidenceDetails title="检查点与重试"><pre>{JSON.stringify({phase:row.phase,checkpoint:row.checkpoint,attempt:row.attempt,max_attempts:row.max_attempts,lease:row.lease,heartbeat_at:row.heartbeat_at,next_step:row.next_step,next_run_at:row.next_run_at,error:row.error},null,2)}</pre></EvidenceDetails></article>})}</div>:<Empty title="暂无近期任务证据" detail="任务中心接口未返回最近任务。" />}</Section>
    <Section title="异常责任分组" detail="未来样本不足、冷却期和窗口未前移不显示为红色故障。"><div className="responsibility-grid">{grouped.map(group=>{const rows=incidents.filter(x=>{const kind=String(pick(x,["resolution_class","disposition","category","kind"],"waiting_condition"));return group.key==="self_healing"?kind==="self_managed":kind===group.key});return <div key={group.key}><header><Status tone={group.tone}>{rows.length}</Status><strong>{group.title}</strong></header>{rows.length?rows.map((row,i)=><article key={i}><b>{pick(row,["title","summary","module","name"],"运行事项")}</b><p>{pick(row,["reason","detail","message","next_review"],"等待复核")}</p></article>):<small>当前没有此类事项</small>}</div>})}</div></Section>
    <Section title="结构化日志" detail="最近任务中心日志，不把历史失败误报为当前故障。"><CompactTable rows={logs.slice(0,40)} columns={[["时间","created_at","timestamp"],["级别","level"],["模块","logger","module"],["消息","message"]]}/></Section>
    <div className="two-column"><Section title="量化任务与最近运行"><CompactTable rows={quantTasks} columns={[["任务","id","task_id","name"],["类型","task_type","kind"],["状态","status","state"],["进度","progress","progress_pct"],["更新时间","updated_at","finished_at"]]}/><CompactTable rows={rowsFrom(data.quantRecent,["items"])} columns={[["信号日","signal_date"],["执行日","execute_date"],["卖出","sold_count"],["买入","bought_count"],["摘要","message"]]}/><EvidenceDetails title="运行状态与调度"><pre>{JSON.stringify({status:data.quantStatus,schedule:data.evolutionSchedule,evolutionTasks:data.evolutionTasks},null,2)}</pre></EvidenceDetails></Section><Section title="执行批次与逐笔操作"><CompactTable rows={executions} columns={[["执行","id"],["策略","strategy_id"],["执行时间","executed_at"],["预算","budget"],["订单","orders"]]}/><ReadOnlyPager label="执行批次" offset={executionsOffset} pageSize={pageSize} total={executionsTotal} hasMore={executionsHasMore} loading={pageLoading==="executions"} onChange={offset=>changePage("executions",offset)}/><CompactTable rows={operations} columns={[["执行","execution_id"],["股票","symbol"],["动作","side"],["数量","quantity"],["佣金","commission"],["价格","price"]]}/><ReadOnlyPager label="逐笔操作" offset={operationsOffset} pageSize={pageSize} total={operationsTotal} hasMore={operationsHasMore} loading={pageLoading==="operations"} onChange={offset=>changePage("operations",offset)}/>{pageError&&<div className="error-strip">{pageError}</div>}</Section></div>
    <Section title="后端接口地图" detail="实时读取 OpenAPI；GET 接口逐步映射为页面或聚合证据，写接口仅标记后台所有权。"><div className="metric-grid"><Metric label="全部接口" value={apiRows.length}/><Metric label="GET" value={readApis.length}/><Metric label="前端直接读取" value={coveredApis.length}/><Metric label="后台写接口" value={apiRows.filter(row=>row.method!=="GET").length}/></div><CompactTable rows={visibleApiRows} columns={[["方法","method"],["路径","path"],["用途","summary"],["前端处置","covered"]]}/>{apiRows.length>0&&<ReadOnlyPager label="接口地图" offset={apiOffset} pageSize={apiPageSize} total={apiRows.length} hasMore={apiOffset+apiPageSize<apiRows.length} loading={false} onChange={setApiOffset}/>}</Section>
    <EvidenceDetails title="查看原始运行合同"><pre>{JSON.stringify({tasks:data.tasks,oos:data.oos,sealed:data.sealed},null,2)}</pre></EvidenceDetails>
  </>;
}
