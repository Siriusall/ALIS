import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchDailyUpdateStatus, fetchDataCenterStatus, fetchDataObservability, fetchReadiness,
  fetchSimpleDataHealth, fetchDataAssets, fetchColdArchiveStatus, startColdArchive, startDailyUpdate, syncFinancialBatch, syncMarket, startWebFallbackCrawl,
  fetchBackfillPlan, startBackfill, fetchBackfillStatus, fetchRuntimeLogsAfter,
  type ConsumerReadiness, type DailyUpdateStatus, type DataCenterStatus, type DataObservability,
  type ReadinessOverview, type SimpleDataHealth, type DataAssetsResponse, type ColdArchiveStatus,
  type BackfillPlan, type BackfillStatus, type BackfillStep,
} from "../api/dataCenter";
import { elapsedMinutesFromServerTime, formatGmt8Date, formatGmt8Time } from "../utils/time";
import LoadingState from "./LoadingState";

export type SectionKey = "overview" | "readiness" | "assets" | "archive" | "repair" | "quality" | "sources" | "logs";

const DOMAINS = [
  { key: "financial", name: "财务/估值/现金流", weight: 40, icon: "财", consumers: ["financial_research", "quant_analysis"] },
  { key: "industry", name: "行业/板块景气", weight: 25, icon: "行", consumers: ["quant_analysis"] },
  { key: "trend", name: "趋势/技术状态", weight: 15, icon: "趋", consumers: ["decision", "backtest"] },
  { key: "events", name: "公告/新闻/研报", weight: 15, icon: "事", consumers: ["financial_research", "position_risk"] },
  { key: "macro", name: "大盘/外围/宏观", weight: 5, icon: "宏", consumers: ["decision"] },
];

export default function TechnicalConsole({ section }: { section: SectionKey }) {
  const [health, setHealth] = useState<SimpleDataHealth | null>(null);
  const [readiness, setReadiness] = useState<ReadinessOverview | null>(null);
  const [observability, setObservability] = useState<DataObservability | null>(null);
  const [status, setStatus] = useState<DataCenterStatus | null>(null);
  const [assets, setAssets] = useState<DataAssetsResponse | null>(null);
  const [archive, setArchive] = useState<ColdArchiveStatus | null>(null);
  const [update, setUpdate] = useState<DailyUpdateStatus | null>(null);
  const [selectedConsumer, setSelectedConsumer] = useState("backtest");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const refreshRunning = useRef(false);

  async function refresh(quiet = false) {
    if (refreshRunning.current) return;
    refreshRunning.current = true;
    if (!quiet) setLoading(true);
    try {
      const results = await Promise.allSettled([
        fetchSimpleDataHealth().then(setHealth),
        fetchReadiness().then(setReadiness),
        fetchDataObservability(100).then(setObservability),
        fetchDataCenterStatus(20).then(setStatus),
        fetchDailyUpdateStatus().then(setUpdate),
        fetchDataAssets().then(setAssets),
        fetchColdArchiveStatus().then(setArchive),
      ]);
      const failed = results.find((x) => x.status === "rejected");
      if (failed?.status === "rejected") setMessage(failed.reason instanceof Error ? failed.reason.message : String(failed.reason));
      else setMessage("");
    }
    finally { refreshRunning.current = false; setLoading(false); }
  }
  useEffect(() => { refresh(); }, []);
  useEffect(() => {
    const intervalMs = update?.running ? 2000 : 5000;
    const timer = window.setInterval(() => refresh(true), intervalMs);
    return () => window.clearInterval(timer);
  }, [section, update?.running]);

  async function latestUpdate() {
    setMessage("");
    try { setUpdate(await startDailyUpdate()); } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }

  const selected = readiness?.consumers.find((item) => item.key === selectedConsumer);
  const context = { health, readiness, observability, status, update, assets, archive, selected, selectedConsumer, setSelectedConsumer, refresh, latestUpdate, message, loading };
  if (loading && !health && !readiness && !observability && !status) {
    return <LoadingState variant="page" title="正在读取数据中台" detail="正在汇总数据健康、完整度、任务和存储状态" rows={5} />;
  }
  return <>
    {message && <div className="notice-bar"><span>!</span>{message}<button onClick={() => setMessage("")}>×</button></div>}
    {section === "overview" && <Overview {...context} />}
    {section === "readiness" && <Readiness {...context} />}
    {section === "assets" && <AssetsDetailed {...context} />}
    {section === "archive" && <Archive {...context} />}
    {section === "repair" && <Repair {...context} />}
    {section === "quality" && <Quality {...context} />}
    {section === "sources" && <Sources {...context} />}
    {section === "logs" && <Logs {...context} />}
  </>;
}

type Ctx = ReturnType<typeof makeContext>;
function makeContext() { return {} as any; }

function PageHead({ title, description, children }: { eyebrow: string; title: string; description: string; children?: React.ReactNode }) {
  return <div className="page-head"><div><h1>{title}</h1><p>{description}</p></div><div className="page-actions">{children}</div></div>;
}

function Overview(c: Ctx) {
  const readinessKnown = Boolean(c.readiness);
  const blocked = c.readiness?.consumers.filter((x: ConsumerReadiness) => x.status === "blocked").length ?? 0;
  const historicalGaps = (c.assets?.items ?? []).filter((x:any) => x.status === "partial" || x.status === "missing").length;
  const weighted = domainScores(c.readiness);
  return <div className="console-page">
    <PageHead eyebrow="DATA PLATFORM OVERVIEW" title="数据中台总览" description="沪深A股 · 滚动两年 · 财务8季度 + 3年度" />
    <div className="hero-status">
      <div className={`hero-orb ${!readinessKnown ? "unknown" : blocked ? "danger" : "success"}`}>{!readinessKnown ? "…" : blocked ? blocked : "✓"}</div>
      <div><span>长线分析数据状态</span><h2>{!readinessKnown ? "正在读取功能数据" : blocked ? `${blocked} 个功能被数据阻断` : historicalGaps ? `当前分析可用，${historicalGaps} 项历史数据未完成` : "当前分析数据可用"}</h2><p>最新行情 {dateText(c.health?.latest_trade_date)} · 沪深股票 {c.health ? `${num(c.health.stock_count)} 只` : "读取中"}</p></div>
      <div className="hero-score"><strong>{weighted.total.toFixed(1)}%</strong><span>加权数据就绪度</span></div>
    </div>
    {c.update?.running && <Progress update={c.update} />}
    <div className="domain-grid">{weighted.items.map((d: any) => <div className="domain-card" key={d.key}><div className="domain-icon">{d.icon}</div><div><span>{d.name}</span><strong>{d.score.toFixed(1)}%</strong><em>分析权重 {d.weight}%</em></div><Status status={d.status}/></div>)}</div>
    <div className="grid two"><Panel title="需要优先处理" sub="阻断和降级问题">
      <div className="issue-list">{(c.readiness?.consumers ?? []).filter((x: ConsumerReadiness) => x.status !== "ready").slice(0, 6).map((x: ConsumerReadiness) => <div key={x.key}><Status status={x.status}/><div><strong>{x.name}</strong><span>{x.requirements.filter(r=>r.status!=="ready").map(r=>r.label).join("、")}</span></div></div>)}</div>
    </Panel><Panel title="最近任务" sub="采集与检查流水线"><RunList runs={c.status?.recent_runs.slice(0, 7) ?? []}/></Panel></div>
  </div>;
}

function Readiness(c: Ctx) {
  const assetIssues=(c.assets?.items??[]).filter((x:any)=>x.status==="partial"||x.status==="missing");
  return <div className="console-page"><PageHead eyebrow="FUNCTION READINESS" title="长线分析自检" description="按功能的数据契约判断是否能够安全运行" />
    <div className="readiness-tabs">{(c.readiness?.consumers ?? []).map((x: ConsumerReadiness)=><button className={c.selectedConsumer===x.key?"active":""} onClick={()=>c.setSelectedConsumer(x.key)} key={x.key}><span>{x.name}</span><Status status={x.status}/><em>{x.blocked_count} 阻断 · {x.degraded_count} 降级</em></button>)}</div>
    {c.selected && <Panel title={`${c.selected.name} · 必需数据`} sub={c.selected.description} right={<Status status={c.selected.status}/>}>
      <div className="contract-list">{c.selected.requirements.map((r:any,i:number)=><div className="contract-item" key={r.key}><span className="branch">{i===c.selected.requirements.length-1?"└":"├"}</span><div className="contract-name"><strong>{r.label}</strong><span>{r.evidence}</span></div><Bar value={r.coverage}/><strong>{pct(r.coverage)}</strong><Status status={r.status}/><em>阈值 ≥ {pct(r.threshold)}</em></div>)}</div>
    </Panel>}
    <Panel title="全资产缺口" sub="业务合同之外也纳入检查；这些问题会同步进入补全计划"><Table heads={["数据集","数据域","状态","覆盖率","审计窗口","来源"]} rows={assetIssues.map((x:any)=>[x.name,x.domain,<AssetStatus status={x.status}/>,x.coverage==null?"—":pct(x.coverage),assetRange(x.window_start,x.window_end),x.source])}/></Panel>
  </div>;
}

function Assets(c: Ctx) {
  const cov=c.status?.coverage;
  const assets=[
    ["股票基础与上市状态","基础资料",cov?.stock_master_rows,"只","沪深A股"], ["交易日历","市场数据",cov?.trade_calendar_rows,"日","日常调度"],
    ["日线行情与估值","市场数据",cov?.raw_record_rows,"条","滚动两年"], ["每日股票快照","标准数据",cov?.stock_daily_snapshot_rows,"条","最新切片"],
    ["公告原文","事件数据",cov?.announcement_raw_rows,"篇","巨潮资讯"], ["结构化公告事件","事件数据",cov?.announcement_event_rows,"条","规则识别"],
    ["行业景气指标","行业数据",cov?.industry_metric_values,"条","待扩充"], ["网页/F10备用因子","备用数据",cov?.web_factor_rows,"条","低优先级"],
  ];
  return <div className="console-page"><PageHead eyebrow="DATA CATALOG" title="数据资产" description="查看系统拥有的数据集、规模、范围和服务对象" />
    <div className="asset-summary"><Metric label="数据资产" value={String(assets.length)} hint="第一版目录"/><Metric label="量化数据域" value="7" hint="个股、行业、事件"/><Metric label="宏观数据域" value="1" hint="待接入序列"/><Metric label="最新交易日" value={dateText(c.health?.latest_trade_date)} hint="日频数据"/></div>
    <div className="asset-grid">{assets.map((a:any)=><div className="asset-card" key={a[0]}><div><span>{a[1]}</span><Status status={Number(a[2])>0?"ready":"blocked"}/></div><h3>{a[0]}</h3><strong>{num(a[2])}<em>{a[3]}</em></strong><p>{a[4]}</p></div>)}</div>
  </div>;
}

function AssetsDetailed(c: Ctx) {
  const [domain,setDomain]=useState("全部");
  const items=(c.assets?.items??[]).filter((x:any)=>domain==="全部"||x.domain===domain);
  const domains:string[]=["全部",...Array.from(new Set<string>((c.assets?.items??[]).map((x:any)=>String(x.domain))))];
  return <div className="console-page"><PageHead eyebrow="DATA CATALOG" title="数据资产" description="第一版所需数据、目标规模、实际存量和完整度">
    <button className="button ghost" onClick={()=>c.refresh()}>刷新资产统计</button>
  </PageHead>
    <div className="asset-summary"><Metric label="所需数据集" value={String(c.assets?.summary.datasets??0)} hint="长线投资第一版"/><Metric label="平均完整度" value={pct(c.assets?.summary.average_coverage)} hint="仅计有应有量的资产"/><Metric label="完整/可用" value={String((c.assets?.summary.complete??0)+(c.assets?.summary.available??0))} hint="达到目标或事件数据已有"/><Metric label="完全缺失" value={String(c.assets?.summary.missing??0)} hint="需要新增来源"/></div>
    <div className="scope-strip"><div><span>市场范围</span><strong>{c.assets?.scope.market}</strong></div><div><span>股票数量</span><strong>{num(c.assets?.scope.stocks)}</strong></div><div><span>审计窗口</span><strong>{dateText(c.assets?.scope.start_date)} 至 {dateText(c.assets?.scope.end_date)}</strong></div><div><span>应有交易日</span><strong>{num(c.assets?.scope.open_days)} 日</strong></div><div><span>财务范围</span><strong>{c.assets?.scope.financial}</strong></div></div>
    <Panel title="数据集目录" sub="日频按交易日覆盖度；财务按股票覆盖度；事件型只计是否有数据">
      <div className="asset-filters">{domains.map(d=><button className={domain===d?"active":""} onClick={()=>setDomain(d)} key={d}>{d}</button>)}</div>
      <div className="table-wrap asset-table"><table><thead><tr><th>数据集</th><th>服务模块</th><th>审计周期</th><th>目标范围</th><th>实际/应有</th><th>完整度</th><th>股票/日期</th><th>库内区间</th><th>频率</th><th>主来源</th><th>状态</th></tr></thead>
      <tbody>{items.map((x:any)=><tr key={x.key}><td><strong>{x.name}</strong><small>{x.domain}</small></td><td>{x.consumer}</td><td className="wrap-cell"><b>{x.window_depth||"—"}</b><small>{assetRange(x.window_start,x.window_end)}</small></td><td className="wrap-cell">{x.target_scope}</td><td><b>{num(x.coverage_basis??x.actual_records)}</b>{x.coverage_expected==null?"（事件）":` / ${num(x.coverage_expected)}`}</td><td>{x.coverage===null?"—":<><Bar value={x.coverage}/><strong>{pct(x.coverage)}</strong></>}</td><td>{x.symbols?`${num(x.symbols)}只`:"—"} / {x.dates?`${num(x.dates)}日`:"—"}</td><td>{assetRange(x.min_date,x.max_date)}</td><td>{frequencyCN(x.frequency)}</td><td>{x.source}</td><td><AssetStatus status={x.status}/></td></tr>)}</tbody></table></div>
    </Panel>
  </div>;
}

function Archive(c: Ctx) {
  const items = c.archive?.items ?? [];
  const [apis, setApis] = useState("index_daily");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [archiving, setArchiving] = useState(false);
  const [archiveMessage, setArchiveMessage] = useState("");
  async function archive() {
    const apiNames = apis.split(",").map((item) => item.trim()).filter(Boolean);
    if (!apiNames.length || !startDate || !endDate) { setArchiveMessage("请填写接口名、开始日期和结束日期。"); return; }
    setArchiving(true); setArchiveMessage("");
    try {
      const result = await startColdArchive({ apiNames, startDate, endDate });
      setArchiveMessage(`归档完成：${num(result.row_count)} 行，已写入 ${result.file_path}`);
      await c.refresh();
    } catch (error) { setArchiveMessage(error instanceof Error ? error.message : String(error)); }
    finally { setArchiving(false); }
  }
  return <div className="console-page"><PageHead eyebrow="LOCAL COLD STORAGE" title="本地归档与恢复" description="Parquet 历史归档是在线 PostgreSQL 的只读副本；不会自动删除业务数据。">
    <button className="button ghost" onClick={()=>c.refresh()}>刷新状态</button>
  </PageHead>
    <div className="asset-summary"><Metric label="存储位置" value={c.archive?.storage === "local_filesystem" ? "本机" : "—"} hint={c.archive?.root || "读取中"}/><Metric label="归档文件" value={String(items.length)} hint="manifest 已登记"/><Metric label="校验通过" value={String(items.filter((x:any)=>x.file_exists).length)} hint="文件存在"/><Metric label="可用磁盘" value={c.archive ? `${(c.archive.disk.free_bytes / 1024 / 1024 / 1024).toFixed(1)} GB` : "—"} hint={c.archive ? `可用 ${(c.archive.disk.free_pct * 100).toFixed(1)}%；完整备份会做空间预检` : "读取中"}/></div>
    <Panel title="归档清单" sub="每份文件均带日期范围、数据接口、行数和 SHA-256 校验值">
      <div className="table-wrap asset-table"><table><thead><tr><th>创建时间</th><th>数据范围</th><th>接口</th><th>行数</th><th>文件大小</th><th>文件校验</th></tr></thead><tbody>{items.map((item:any)=><tr key={item.archive_id}><td>{formatGmt8Time(item.created_at)}</td><td>{dateText(item.date_start)} 至 {dateText(item.date_end)}</td><td>{item.api_names.join("、")}</td><td>{num(item.row_count)}</td><td>{item.file_exists?`${(Number(item.file_bytes||0)/1024).toFixed(1)} KB`:"文件缺失"}</td><td>{item.file_exists?"已验证":"缺失"}</td></tr>)}</tbody></table></div>
      {!items.length&&<div className="empty">暂无归档。归档只在明确指定接口和日期范围后执行，避免意外占满本机磁盘。</div>}
    </Panel>
    <Panel title="创建受限归档" sub="必须指定接口和日期范围；单次默认最多 25 万行，超出时系统拒绝执行而不是占满磁盘。">
      <div className="form-stack"><label>TuShare 接口（逗号分隔）<input value={apis} onChange={(event)=>setApis(event.target.value)} placeholder="例如 index_daily" /></label><label>开始日期（YYYYMMDD）<input value={startDate} onChange={(event)=>setStartDate(event.target.value)} placeholder="例如 20160715" /></label><label>结束日期（YYYYMMDD）<input value={endDate} onChange={(event)=>setEndDate(event.target.value)} placeholder="例如 20160731" /></label><button type="button" className="button primary" onClick={()=>void archive()} disabled={archiving}>{archiving ? "归档中…" : "创建本地 Parquet 归档"}</button>{archiveMessage&&<div className="decision-issue warning">{archiveMessage}</div>}</div>
    </Panel>
    <Panel title="本地备份与恢复" sub="完整数据库备份受磁盘空间保护；恢复前会验证 SHA-256。">
      <div className="decision-issue warning"><strong>当前策略：不自动启用 WAL。</strong><br/>完整备份请使用 <code>scripts/backup_local_postgres.sh</code>；恢复使用 <code>scripts/restore_local_postgres.sh &lt;备份目录&gt;</code>。空间不足时备份会在写入前停止。</div>
    </Panel>
  </div>;
}

function Repair(c: Ctx) {
  const [tradeDate,setTradeDate]=useState(c.health?.latest_trade_date??"");
  const [period,setPeriod]=useState("");
  const [codes,setCodes]=useState("000001.SZ,000002.SZ");
  const [msg,setMsg]=useState("");
  const [plan,setPlan]=useState<BackfillPlan|null>(null);
  const [backfill,setBackfill]=useState<BackfillStatus|null>(null);
  const [planning,setPlanning]=useState(false);
  const backfillPollRunning=useRef(false);

  async function run(label:string, fn:()=>Promise<any>){
    setMsg(`${label}执行中…`);
    try{await fn();setMsg(`${label}已提交`);c.refresh(true);}
    catch(e){setMsg(`${label}失败：${e instanceof Error?e.message:String(e)}`)}
  }

  async function generatePlan(){
    setPlanning(true); setMsg("正在根据审计窗口生成历史补全计划…");
    try{
      const next=await fetchBackfillPlan();
      setPlan(next);
      setMsg(`计划已生成：${next.summary.steps_needed} 个待执行步骤，预计约 ${next.summary.estimated_hours} 小时`);
    }catch(e){setMsg(`生成计划失败：${e instanceof Error?e.message:String(e)}`)}
    finally{setPlanning(false)}
  }

  async function finishBackfill(status: BackfillStatus){
    setBackfill(status);
    setMsg(`${status.message||"历史补全已结束"}；正在刷新审计与计划…`);
    try{
      const next=await fetchBackfillPlan();
      setPlan(next);
      setMsg(`${status.message||"历史补全已结束"}；计划已刷新，剩余 ${next.summary.steps_needed} 步`);
      void c.refresh(true);
    }catch(e){
      setMsg(`${status.message||"历史补全已结束"}；刷新计划失败：${e instanceof Error?e.message:String(e)}`);
      void c.refresh(true);
    }
  }

  async function runPlan(){
    setMsg("正在启动历史补全…");
    try{
      const result=await startBackfill(0);
      setBackfill(result);
      if(result.plan) setPlan(result.plan);
      const needed=result.plan?.summary?.steps_needed ?? result.plan_summary?.steps_needed;
      setMsg(result.message||(needed!=null?`历史补全已启动，将执行 ${needed} 个可自动执行步骤`:"历史补全已启动，将执行可自动补全计划"));
      if(!result.running) await finishBackfill(result);
    }catch(e){setMsg(`启动补全失败：${e instanceof Error?e.message:String(e)}`)}
  }

  async function continueBackfill(){
    setMsg("正在继续历史补全，会从剩余缺口重新生成计划…");
    await runPlan();
  }

  useEffect(()=>{
    let cancelled=false;
    fetchBackfillPlan().then((next)=>{if(!cancelled)setPlan(next)}).catch(()=>{});
    fetchBackfillStatus().then((status)=>{
      if(cancelled) return;
      setBackfill(status);
      if(status.phase==="completed"||status.phase==="completed_with_gaps"||status.phase==="failed") setMsg(status.message||"");
    }).catch(()=>{});
    return ()=>{cancelled=true};
  },[]);

  useEffect(()=>{
    let cancelled=false;
    const timer=window.setInterval(async()=>{
      if(backfillPollRunning.current) return;
      backfillPollRunning.current=true;
      try{
        const status=await fetchBackfillStatus();
        if(cancelled) return;
        setBackfill(status);
        if(status.running){
          if(msg.includes("历史补全失败") || msg.includes("自动续跑")) setMsg("");
          void c.refresh(true);
        }else if(status.auto_resume){
          setMsg(status.message || `自动续跑中：${status.auto_resume_delay_seconds ?? "稍后"} 秒后继续`);
        }else if(status.phase==="completed" || status.phase==="completed_with_gaps" || status.phase==="failed" || status.phase==="interrupted"){
          window.clearInterval(timer);
          await finishBackfill(status);
        }
      }catch{/* ignore poll errors */}finally{backfillPollRunning.current=false}
    },2000);
    return ()=>{cancelled=true; window.clearInterval(timer);};
  },[msg]);

  // 计划是常驻状态：页面打开立即加载，运行期间独立于任务状态轮询，
  // 避免只更新顶部进度而让“历史补全计划”停留在旧快照。
  useEffect(()=>{
    let cancelled=false;
    const refreshPlan=async()=>{
      try{
        const next=await fetchBackfillPlan();
        if(!cancelled) setPlan(next);
      }catch{/* 保留上一次计划，避免短暂限流导致页面消失 */}
    };
    void refreshPlan();
    const timer=window.setInterval(refreshPlan, backfill?.running ? 5000 : 15000);
    return ()=>{cancelled=true; window.clearInterval(timer);};
  },[backfill?.running]);

  const showProgress=!!backfill && (backfill.running || backfill.phase==="completed" || backfill.phase==="completed_with_gaps" || backfill.phase==="failed" || backfill.phase==="interrupted");
  const hasExecutableSteps=(plan?.summary.steps_needed??0)>0;

  return <div className="console-page"><PageHead eyebrow="INGESTION & REPAIR" title="数据补全" description="日常增量更新与历史缺口修复分开执行" />
    {msg&&<div className="inline-message">{msg}</div>}
    {c.update?.running&&<Progress update={c.update}/>}
    {showProgress&&<div className={`task-progress ${(backfill?.phase==="failed"||backfill?.phase==="interrupted")&&!backfill?.auto_resume?"task-failed":""}`}><div><strong>{backfill?.message||"历史补全"}</strong><span>总进度 {backfill?.progress??0}%</span></div><div className="bar"><i style={{width:`${backfill?.progress??0}%`}}/></div>{backfill?.activity&&<div className={`live-activity activity-${backfill.activity.event}`}><i/><span>实时</span><strong>{backfill.activity.text}</strong><time>{timeText(backfill.activity.at)}</time></div>}{backfill?.auto_resume&&<div className="inline-message">自动续跑中：{backfill.auto_resume_delay_seconds ?? "稍后"} 秒后继续，第 {backfill.auto_resume_attempt ?? 1}{backfill.auto_resume_max_attempts?`/${backfill.auto_resume_max_attempts}`:" 次（不限次数）"}。无需人工点击。</div>}{(backfill?.phase==="failed"||backfill?.phase==="interrupted")&&!backfill?.auto_resume&&<FailureBox status={backfill} onResume={continueBackfill} disabled={!!backfill?.running||planning}/>}</div>}
    <div className="grid two">
      <Panel title="更新最新数据" sub="适合每日使用，只补最新交易日"><ul className="step-list"><li>确认最近交易日</li><li>更新行情、估值和涨跌停</li><li>更新最近公告</li><li>执行质量检查</li></ul><button className="button primary wide" onClick={c.latestUpdate}>开始日常更新</button></Panel>
      <Panel title="历史缺口概览" sub="按审计窗口识别缺口，并生成可执行计划">
        <div className="gap-list">{(c.readiness?.consumers??[]).map((x:ConsumerReadiness)=><div key={x.key}><span>{x.name}</span><strong>{x.requirements.filter(r=>r.status!=="ready").length} 项缺口</strong><Status status={x.status}/></div>)}</div>
        <div style={{display:"grid",gap:8,marginTop:12}}>
          <button className="button primary wide" disabled={planning} onClick={generatePlan}>{planning?"生成中…":backfill?.running?"刷新缺口统计（运行中）":"生成历史补全计划"}</button>
          <button className="button ghost wide" disabled={!plan||!hasExecutableSteps||!!backfill?.running||planning} onClick={runPlan}>{hasExecutableSteps?"执行可自动步骤":"无可执行步骤"}</button>
        </div>
      </Panel>
    </div>
    {plan&&<Panel title="历史补全计划" sub={`生成于 ${plan.generated_at} · 待执行 ${plan.summary.steps_needed} 步 · 预计 ${plan.summary.estimated_hours} 小时 · 下一步：${plan.summary.next_action}`} right={<button className="button ghost" disabled={planning||!!backfill?.running} onClick={generatePlan}>刷新计划</button>}>
      {backfill?.running&&<RunningBackfillDetail status={backfill}/>}
      <div className="asset-summary" style={{marginBottom:12}}>
        <Metric label="资产缺口" value={String(plan.summary.asset_gaps)} hint="partial/missing"/>
        <Metric label="待执行步骤" value={String(plan.summary.steps_needed)} hint={`阻塞 ${plan.summary.steps_blocked}`}/>
        <Metric label={backfill?.running?"动态剩余":"预计耗时"} value={backfill?.running?dynamicEta(backfill):`${plan.summary.estimated_hours}h`} hint={backfill?.running?`已运行 ${elapsedText(backfill.started_at)} · 初始估计 ${plan.summary.estimated_hours}h`:`${plan.summary.estimated_minutes} 分钟`}/>
        <Metric label="日历开市日" value={String((plan.scope as any).open_days_in_calendar??0)} hint={`日线已有 ${(plan.scope as any).daily_dates_have??0} 日`}/>
      </div>
      <div className="table-wrap"><table><thead><tr><th>步骤</th><th>优先级</th><th>内容</th><th>原因</th><th>预估</th><th>状态</th></tr></thead>
        <tbody>{plan.steps.map((s)=><tr key={s.id}><td>{s.id}</td><td>P{s.priority}</td><td><strong>{s.title}</strong><small>{s.action}</small></td><td className="wrap-cell">{planReason(s)}</td><td>{s.estimated_minutes} 分钟 / {s.estimated_calls} 次</td><td><AssetStatus status={s.status==="needed"?"partial":s.status==="done"?"complete":s.status==="blocked"?"missing":s.status}/></td></tr>)}</tbody>
      </table></div>
    </Panel>}
    <Panel title="定向补数工具" sub="技术排障与小范围修复"><div className="repair-form"><label>交易日<input value={tradeDate} onChange={e=>setTradeDate(e.target.value)} placeholder="YYYYMMDD"/></label><button className="button ghost" onClick={()=>run("行情补数",()=>syncMarket(tradeDate))}>补指定行情</button><label>报告期<input value={period} onChange={e=>setPeriod(e.target.value)} placeholder="20260331"/></label><label>股票代码<input value={codes} onChange={e=>setCodes(e.target.value)}/></label><button className="button ghost" onClick={()=>run("财务补数",()=>syncFinancialBatch({period,codes,limit:20}))}>补财务数据</button><button className="button ghost" onClick={()=>run("网页备用源",()=>startWebFallbackCrawl(codes,20))}>运行备用源</button></div></Panel>
  </div>;
}

function RunningBackfillDetail({status}:{status:BackfillStatus}) {
  const step=status.current_step;
  const gap=(step?.gap ?? {}) as Record<string, unknown>;
  const checked=Number(gap.checked_symbols ?? gap.have_symbols ?? gap.checked_dates ?? gap.have ?? 0);
  const need=Number(gap.need_symbols ?? gap.need_dates ?? gap.need ?? 0);
  const batch=Number(gap.batch ?? 0);
  const stepPct=need>0 ? checked/need : 0;
  const executed=Array.isArray(status.executed) ? status.executed.length : 0;
  return <div className="running-detail">
    <div className="running-head">
      <div><span>全量补全总进度</span><strong>{status.message || step?.title || "历史补全运行中"}</strong></div>
      <b>{status.progress ?? 0}%</b>
    </div>
    <div className="bar"><i style={{width:`${status.progress??0}%`}}/></div>
    <div className="running-grid">
      <Metric label="当前步骤" value={step?.id || "—"} hint={step?.action || status.phase || "运行中"}/>
      <Metric label="本项已检查" value={need>0?`${num(checked)} / ${num(need)}`:num(checked)} hint={batch?`每批 ${num(batch)} 只/日`:"按缺口续跑"}/>
      <Metric label="本项覆盖" value={need>0?pct(stepPct):"—"} hint={step?.title || "当前任务"}/>
      <Metric label="动态剩余" value={dynamicEta(status)} hint={`已运行 ${elapsedText(status.started_at)}`}/>
      <Metric label="已执行步骤" value={String(executed)} hint={`run ${status.run_id ?? "—"}`}/>
    </div>
    {status.activity&&<div className={`live-activity activity-${status.activity.event}`}><i/><span>实时</span><strong>{status.activity.text}</strong><time>{timeText(status.activity.at)}</time></div>}
  </div>
}

function FailureBox({status,onResume,disabled}:{status:BackfillStatus;onResume:()=>void;disabled?:boolean}) {
  const detail=status.failure_detail;
  const failedItems=detail?.summary?.failed_items ?? [];
  const stepTitle=detail?.step?.title || status.current_step?.title || status.phase || "未知步骤";
  const error=detail?.summary?.error || status.error || status.message || "未知错误";
  return <div className="failure-box">
    <div className="failure-main">
      <div><span>失败步骤</span><strong>{stepTitle}</strong></div>
      <div><span>失败原因</span><strong>{error}</strong></div>
      {status.auto_resume&&<div><span>自动续跑</span><strong>{status.auto_resume_delay_seconds ?? "稍后"} 秒后继续 · 第 {status.auto_resume_attempt ?? 1}{status.auto_resume_max_attempts?`/${status.auto_resume_max_attempts}`:" 次（不限次数）"} · {timeText(status.auto_resume_at)}</strong></div>}
      <div><span>处理方式</span><strong>{detail?.resume_hint || "已写入的数据会保留；继续补全会重新生成缺口计划。"}</strong></div>
    </div>
    {failedItems.length>0&&<div className="failed-items">{failedItems.slice(0,5).map((x,i)=><div key={`${x.ts_code}-${x.api}-${i}`}><code>{x.ts_code || "—"}</code><span>{x.api || "接口"}</span><strong>{x.error || "failed"}</strong></div>)}</div>}
    <button className="button primary" disabled={disabled} onClick={onResume}>{status.auto_resume ? "立即继续补全" : "继续补全剩余缺口"}</button>
  </div>
}

function Quality(c: Ctx) {
  const q=c.observability?.quality;
  const audit=c.observability?.quality_audit;
  return <div className="console-page"><PageHead eyebrow="DATA QUALITY" title="质量监控" description="最新交易日日切片、字段缺失与规则检查" />
    <div className="quality-hero"><Metric label="质量规则通过率" value={pct(q?.quality_pass_rate)} hint={q?.definition??""}/><Metric label="平均质量分" value={pct(q?.average_quality_score)} hint={`${num(q?.quality_pass_rows)} / ${num(q?.sample_rows)} 行通过`}/><Metric label="可信审计状态" value={auditStatusText(audit?.status)} hint={`错误 ${audit?.summary.errors??0} · 告警 ${audit?.summary.warnings??0}`}/><Metric label="原始记录" value={num(audit?.summary.raw_records)} hint="Tushare raw"/></div>
    <Panel title="数据库可信审计" sub="这些问题不一定阻断运行，但会影响数据可信度，不能被总览 ready 掩盖">
      <div className="table-wrap"><table><thead><tr><th>审计项</th><th>状态</th><th>数量</th><th>说明</th><th>样本</th></tr></thead>
        <tbody>{(audit?.issues??[]).map((x:any)=><tr key={x.code}><td><strong>{x.title}</strong><em>{x.code}</em></td><td><Status status={x.status==="error"?"blocked":x.status==="warning"?"degraded":"ready"}/></td><td>{num(x.count)}</td><td className="wrap-cell">{x.message}</td><td className="wrap-cell">{auditSample(x.sample)}</td></tr>)}</tbody></table></div>
    </Panel>
    <Panel title="最新交易日日切片完整度" sub={`日期 ${dateText(c.observability?.latest_trade_date)}`}><div className="field-list">{(c.observability?.missingness??[]).map((x:any)=><div key={x.field}><div><strong>{x.label}</strong><span>{x.category}</span></div><Bar value={x.coverage_rate}/><strong>{pct(x.coverage_rate)}</strong><span>缺失 {num(x.missing)} / {num(x.total)}</span></div>)}</div></Panel>
    <Panel title="质量检查明细" sub="覆盖、完整性和异常"><Table heads={["检查项","范围","结果","严重度","影响数量","说明","时间"]} rows={(q?.checks??[]).map((x:any)=>[checkName(x.check_name),x.scope,x.status,x.severity,num(x.affected_count),x.message,timeText(x.checked_at)])}/></Panel>
  </div>;
}

function Sources(c: Ctx) {
  return <div className="console-page"><PageHead eyebrow="SOURCES & APIS" title="数据源与接口" description="接口探测、返回规模、权限与错误监控" />
    <div className="source-summary"><Metric label="全部接口" value={String(c.observability?.interface_summary.total??0)} hint="Tushare目录"/><Metric label="正常" value={String(c.observability?.interface_summary.healthy??0)} hint="最近探测返回有效数据"/><Metric label="异常" value={String(c.observability?.interface_summary.failed??0)} hint={`空结果/待确认 ${c.observability?.interface_summary.warning??0}`}/><Metric label="Redis" value={c.observability?.redis?.available?"在线":"降级"} hint={c.observability?.redis?.available?"任务状态与统计缓存":"已回退本地状态"}/></div>
    <Panel title="来源运行情况" sub="按来源汇总"><div className="source-cards">{(c.observability?.source_stats??[]).map((x:any)=><div key={x.source_code}><div className="source-logo">{sourceName(x.source_code).slice(0,1)}</div><strong>{sourceName(x.source_code)}</strong><span>成功 {x.successful_runs} · 失败 {x.failed_runs}</span><em>{timeText(x.last_run_at)}</em></div>)}</div></Panel>
    <Panel title="接口健康明细" sub="空结果按业务不可用处理，必须更换有效参数复测或启用替代源"><Table heads={["接口","类别","状态","返回行数","频率","最近探测","错误"]} rows={(c.observability?.interfaces??[]).map((x:any)=>[`${x.display_name} · ${x.api_name}`,x.category,x.last_probe_status==="empty"?<span className="status-pill status-degraded">空结果·待处理</span>:<Status status={x.health==="healthy"?"ready":x.health==="failed"?"blocked":"degraded"}/>,num(x.last_probe_rows),x.recommended_frequency,timeText(x.last_probe_at),x.last_probe_error||""])}/></Panel>
  </div>;
}

function Logs(c: Ctx) {
  const [kind,setKind]=useState<"tasks"|"backend">("backend");
  const [runtime,setRuntime]=useState(c.observability?.runtime_logs??[]);
  const lastIdRef=useRef(0);
  useEffect(()=>{
    const initial=c.observability?.runtime_logs??[];
    setRuntime(initial);
    lastIdRef.current=Math.max(0,...initial.map((x:any)=>Number(x.id)||0));
  },[c.observability?.runtime_logs]);
  useEffect(()=>{
    if(kind!=="backend") return;
    let cancelled=false;
    const poll=async()=>{
      try{
        const data=await fetchRuntimeLogsAfter(lastIdRef.current,100,lastIdRef.current > 0 ? 20 : 0);
        if(cancelled) return;
        if(data.items.length>0){
          lastIdRef.current=Math.max(lastIdRef.current,...data.items.map((x:any)=>Number(x.id)||0));
          setRuntime((prev:any[])=>{
            const seen=new Set(prev.map((x:any)=>Number(x.id)));
            const merged=[...data.items.filter((x:any)=>!seen.has(Number(x.id))).reverse(),...prev];
            return merged.slice(0,200);
          });
        }
      }catch{/* keep quiet; page-level refresh still handles auth/errors */}
      if(!cancelled) void poll();
    };
    void poll();
    return ()=>{cancelled=true};
  },[kind]);
  return <div className="console-page"><PageHead eyebrow="OPERATIONS LOG" title="运行日志" description="任务审计与后端实时请求、重试、异常日志"><button className="button ghost" onClick={()=>c.refresh()}>刷新日志</button></PageHead>
  <div className="log-stats"><Metric label="最近任务总数" value={String(c.observability?.logs.length??0)} hint="最近加载的任务审计记录（最多100条）"/><Metric label="已完成任务" value={String(c.observability?.logs.filter((x:any)=>x.status==="completed").length??0)} hint="已完成"/><Metric label="失败任务" value={String(c.observability?.logs.filter((x:any)=>x.status==="failed").length??0)} hint="需要排查"/><Metric label="当前运行任务" value={String(c.observability?.logs.filter((x:any)=>x.status==="running").length??0)} hint="后台执行中"/></div>
  <div className="log-tabs"><button className={kind==="backend"?"active":""} onClick={()=>setKind("backend")}>后端实时日志 <span>{runtime.length}</span></button><button className={kind==="tasks"?"active":""} onClick={()=>setKind("tasks")}>任务审计 <span>{c.observability?.logs.length??0}</span></button></div>
  {kind==="tasks"?<Panel title="任务审计" sub="采集任务、补数任务和质量检查"><Table heads={["ID","任务","来源","状态","读取","写入","开始","结束","错误"]} rows={(c.observability?.logs??[]).map((x:any)=>[x.id,jobName(x.job_name),sourceName(x.source_code),<Status status={x.status==="completed"?"ready":x.status==="failed"?"blocked":"degraded"}/>,num(x.rows_read),num(x.rows_written),timeText(x.started_at),timeText(x.finished_at),x.error||""])}/></Panel>:<Panel title="后端实时日志" sub="有新日志时逐条追加；包含 Tushare 请求、重试和异常"><Table heads={["时间","级别","模块","消息","上下文"]} rows={runtime.map((x:any)=>[timeText(x.created_at),<span className={`log-level level-${String(x.level).toLowerCase()}`}>{x.level}</span>,x.logger,x.message,compactJson(x.context)])}/></Panel>}
  </div>; }

function Panel({title,sub,right,children}:{title:string;sub?:string;right?:React.ReactNode;children:React.ReactNode}){return <section className="tech-panel"><header><div><h2>{title}</h2>{sub&&<p>{sub}</p>}</div>{right}</header>{children}</section>}
function Metric({label,value,hint}:{label:string;value:string;hint:string}){return <div className="metric"><span>{label}</span><strong>{value}</strong><small>{hint}</small></div>}
function Status({status}:{status:string}){const t:any={ready:"可用",healthy:"正常",degraded:"降级",warning:"待确认",blocked:"阻断",failed:"异常",running:"运行中",unknown:"检查中"};return <span className={`status-pill status-${status}`}>{t[status]??status}</span>}
function AssetStatus({status}:{status:string}){const map:any={complete:["ready","完整"],available:["ready","已有数据"],partial:["degraded","部分数据"],missing:["blocked","缺失"],needed:["degraded","待执行"],done:["ready","已完成"],blocked:["blocked","阻塞"]};const v=map[status]??["warning",status];return <span className={`status-pill status-${v[0]}`}>{v[1]}</span>}
function Bar({value}:{value:number}){return <div className="bar"><i style={{width:`${Math.min(100,value*100)}%`}}/></div>}
function Progress({update}:{update:DailyUpdateStatus}){return <div className="task-progress"><div><strong>{update.message}</strong><span>{update.progress??0}%</span></div><div className="bar"><i style={{width:`${update.progress??0}%`}}/></div></div>}
function planReason(s:BackfillStep){
  if(s.action==="sync_financial_batch") {
    const period=String(s.params?.period??"");
    const extra=Boolean(s.params?.include_yearly);
    if(s.params?.completion_api==="core") return `按报告期批量拉取四项核心财务数据${period?`（${period}）`:""}；批量失败的接口再逐股补漏${extra?"，年度附加数据另行处理":""}`;
    return `按报告期补全审计意见${period?`（${period}）`:""}；审计意见逐股断点获取，主营构成由独立 VIP 批次获取`;
  }
  return s.why;
}
function RunList({runs}:{runs:any[]}){return <div className="run-list">{runs.map(x=><div key={x.id}><i className={`dot ${x.status}`}/><div><strong>{jobName(x.job_name)}</strong><span>{timeText(x.started_at)}</span></div><Status status={x.status==="completed"?"ready":x.status==="failed"?"blocked":"running"}/></div>)}</div>}
function Table({heads,rows}:{heads:string[];rows:React.ReactNode[][]}){return <div className="table-wrap"><table><thead><tr>{heads.map(h=><th key={h}>{h}</th>)}</tr></thead><tbody>{rows.map((r,i)=><tr key={i}>{r.map((v,j)=><td key={j}>{v}</td>)}</tr>)}</tbody></table></div>}
function domainScores(r:ReadinessOverview|null){const items=DOMAINS.map(d=>{const cs=(r?.consumers??[]).filter(x=>d.consumers.includes(x.key));const vals=cs.map(x=>x.requirements.reduce((a,b)=>a+b.coverage,0)/Math.max(1,x.requirements.length));const score=(vals.reduce((a,b)=>a+b,0)/Math.max(1,vals.length))*100;const status=!r||cs.length===0?"unknown":cs.some(x=>x.status==="blocked")?"blocked":cs.some(x=>x.status==="degraded")?"degraded":"ready";return {...d,score,status}});return {items,total:items.reduce((a,b)=>a+b.score*b.weight/100,0)}}
const num=(v?:number)=>new Intl.NumberFormat("zh-CN").format(v??0); const pct=(v?:number)=>`${((v??0)*100).toFixed(2)}%`;
const dateText=(v?:string|null)=>formatGmt8Date(v); const timeText=(v?:string|null)=>formatGmt8Time(v);
const elapsedMinutes=(v?:string|null)=>elapsedMinutesFromServerTime(v);
const elapsedText=(v?:string|null)=>formatMinutes(elapsedMinutes(v));
const dynamicEta=(s:BackfillStatus)=>{const p=Math.max(1,Math.min(99,Number(s.progress??0)));const elapsed=elapsedMinutes(s.started_at);if(elapsed<=0)return "计算中";return formatMinutes(elapsed*(100-p)/p)};
const formatMinutes=(m:number)=>m>=60?`${(m/60).toFixed(1)}h`:`${Math.max(1,Math.round(m))}分钟`;
const sourceName=(v:string)=>({tushare:"Tushare",cninfo:"巨潮公告",web_fallback:"网页备用源",system:"系统任务",manual:"手工导入"} as any)[v]??v;
const jobName=(v:string)=>v==="data_platform.daily_update"?"一键更新":v.includes("daily_basic")?"日度估值":v.includes("daily")?"日线行情":v.includes("announcement")?"公司公告":v.includes("quality")?"质量检查":v.includes("trade_cal")?"交易日历":v;
const checkName=(v:string)=>({stock_master_coverage:"股票主数据覆盖",snapshot_coverage:"行情快照覆盖",snapshot_low_quality:"低质量记录"} as any)[v]??v;
const frequencyCN=(v:string)=>({daily:"每日",weekly:"每周",monthly:"每月",quarterly:"每季度",event:"事件发生时","daily/monthly":"日频/月频"} as any)[v]??v;
const assetRange=(a?:string|null,b?:string|null)=>a||b?`${dateText(a)} ～ ${dateText(b)}`:"暂无数据";
const compactJson=(v:unknown)=>{try{const s=JSON.stringify(v);return s.length>180?`${s.slice(0,180)}…`:s}catch{return String(v)}};
const auditStatusText=(v?:string)=>v==="ok"?"可信":v==="warning"?"有告警":v==="error"?"有错误":"读取中";
const auditSample=(v?:Array<Record<string,unknown>>)=>v?.length?v.slice(0,3).map(compactJson).join("；"):"—";
