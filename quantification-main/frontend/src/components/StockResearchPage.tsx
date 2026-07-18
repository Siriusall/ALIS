import { useEffect, useMemo, useState } from "react";
import {
  fetchExpectation,
  fetchResearchEvents,
  fetchStockResearch,
  fetchTheses,
  fetchValuation,
  type ExpectationState,
  type InvestmentThesis,
  type ResearchEvent,
  type StockResearchResult,
  type ValuationState,
} from "../api/research";
import { useStockContext } from "../context/StockContext";
import type { SectionId } from "./Layout";

const DIMENSION_NAMES: Record<string, string> = {
  quality: "企业质量", growth: "成长", cashflow: "现金流", governance: "治理",
  industry: "行业景气", expectation: "预期变化", valuation: "估值",
};

const EVENT_NAMES: Record<string, string> = {
  regulatory_risk: "监管风险", shareholder_reduction: "股东减持", capital_operation: "资本运作",
  earnings: "业绩", pledge: "质押", repurchase: "回购", major_contract: "重大合同",
};

interface WorkspaceData {
  research: StockResearchResult;
  expectation: ExpectationState | null;
  valuation: ValuationState | null;
  events: ResearchEvent[];
  theses: InvestmentThesis[];
  partialErrors: string[];
}

interface StockResearchPageProps {
  onNavigate?: (section: SectionId) => void;
}

function pct(value: number | undefined | null, digits = 0) {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function money(value: number | undefined | null) {
  return value == null ? "—" : new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(value);
}

export default function StockResearchPage({ onNavigate }: StockResearchPageProps) {
  const { activeStock, setActiveStock } = useStockContext();
  const [code, setCode] = useState(activeStock);
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function run(input = code) {
    const normalized = input.trim().toUpperCase();
    if (!normalized) return setError("请输入股票代码，例如 000001.SZ");
    setCode(normalized);
    setActiveStock(normalized);
    setLoading(true);
    setError("");
    try {
      const research = await fetchStockResearch(normalized);
      if (!research.ok) {
        setData(null);
        setError(research.reason === "stock_not_found" ? "股票主数据中不存在该代码" : research.reason ?? "研究失败");
        return;
      }
      const settled = await Promise.allSettled([
        fetchExpectation(normalized), fetchValuation(normalized), fetchResearchEvents(normalized), fetchTheses(),
      ]);
      const failures = settled.flatMap((result, index) => result.status === "rejected"
        ? [`${["预期", "估值", "公告", "投资逻辑"][index]}数据读取失败`]
        : []);
      setData({
        research,
        expectation: settled[0].status === "fulfilled" ? settled[0].value as ExpectationState : null,
        valuation: settled[1].status === "fulfilled" ? settled[1].value as ValuationState : null,
        events: settled[2].status === "fulfilled" ? settled[2].value as ResearchEvent[] : [],
        theses: settled[3].status === "fulfilled"
          ? (settled[3].value as InvestmentThesis[]).filter((item) => item.ts_code === normalized)
          : [],
        partialErrors: failures,
      });
    } catch (reason) {
      setData(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (activeStock) void run(activeStock);
    // Load the selected workspace once when entering this route.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const decision = useMemo(() => {
    if (!data) return null;
    const risk = data.research.risk;
    const valuation = data.valuation;
    const thesis = data.theses[0];
    if (risk && !risk.eligible) return { tone: "stop", title: "禁止进入候选池", detail: risk.hard_reasons[0] || "风险硬门控未通过" };
    if (!valuation?.ok || !valuation.scenarios) return { tone: "hold", title: "继续研究，暂不进入核心候选", detail: "估值或安全边际证据不足" };
    if (!thesis) return { tone: "hold", title: "等待投资逻辑", detail: "尚未建立可证伪的投资逻辑卡片" };
    if (["warning", "invalidated"].includes(thesis.derived_status)) return { tone: "stop", title: "逻辑预警", detail: "必要条件异常，进入复核或退出流程" };
    const margin = valuation.current_price ? valuation.scenarios.base / valuation.current_price - 1 : 0;
    if (margin <= 0) return { tone: "hold", title: "质量与逻辑可研究，但价格不合适", detail: "基准情景尚无正安全边际" };
    return { tone: "go", title: "具备候选研究条件", detail: "风险、逻辑与基础安全边际已形成，仍需通过组合约束" };
  }, [data]);

  return <section className="stock-workspace">
    <div className="product-module-head stock-workspace-head">
      <div><span>COMPANY RESEARCH WORKSPACE</span><h1>单股研究工作台</h1><p>把一只股票该看的证据放在同一个地方看完。</p></div>
      {data && <div className={`workspace-decision workspace-decision-${decision?.tone ?? "hold"}`}><span>当前研究结论</span><strong>{decision?.title}</strong><em>{decision?.detail}</em></div>}
    </div>
    <div className="stock-research-search workspace-search">
      <input aria-label="研究股票代码" value={code} onChange={(event) => setCode(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void run()} placeholder="输入 Tushare 代码，如 000001.SZ"/>
      <button onClick={() => void run()} disabled={loading}>{loading ? "正在汇总证据…" : "刷新完整研究"}</button>
    </div>
    {error && <div className="alert">{error}</div>}
    {!data && !error && <div className="research-empty"><strong>{loading ? "正在建立研究工作区" : "选择一只沪深股票"}</strong><span>输入代码后开始研究。</span></div>}
    {data && <>
      {data.partialErrors.map((item) => <div className="decision-issue warning" key={item}>{item}；其他研究证据仍可使用。</div>)}
      <div className="decision-command-bar" aria-label="单股研究下一步">
        <div className="decision-command-intro">
          <span>NEXT ACTION</span>
          <strong>{data.research.ts_code} 的下一步</strong>
        </div>
        <button type="button" className={decision?.tone === "go" ? "active" : ""} onClick={() => onNavigate?.("stockPool")}>
          <span>01 · 股票池</span>
          <strong>{decision?.tone === "go" ? "可申请" : "先补证据"}</strong>
        </button>
        <button type="button" onClick={() => onNavigate?.("thesis")}>
          <span>02 · 投资逻辑</span>
          <strong>{data.theses.length ? "已建立" : "待建立"}</strong>
        </button>
        <button type="button" onClick={() => onNavigate?.("valuation")}>
          <span>03 · 估值中心</span>
          <strong>{data.valuation?.ok ? "有情景" : "缺证据"}</strong>
        </button>
        <button type="button" onClick={() => onNavigate?.("portfolio")}>
          <span>04 · 组合管理</span>
          <strong>约束检查</strong>
        </button>
      </div>
      <nav className="workspace-anchor-nav" aria-label="研究工作台目录">
        <a href="#workspace-profile">企业</a><a href="#workspace-dimensions">研究</a><a href="#workspace-events">事件</a><a href="#workspace-valuation">估值</a><a href="#workspace-thesis">逻辑</a>
      </nav>
      <section id="workspace-profile" className="workspace-section">
        <div className="research-profile">
          <div><span>{data.research.ts_code}</span><h2>{data.research.profile?.name}</h2><p>{data.research.classification?.industry} · {data.research.classification?.business_model} · {data.research.classification?.lifecycle}</p></div>
          <div><span>研究状态</span><strong>{data.research.research_state?.classification}</strong><em>置信度 {pct(data.research.research_state?.confidence, 1)}</em></div>
          <div><span>风险门控</span><strong>{data.research.risk?.eligible ? data.research.risk.risk_level : "禁止准入"}</strong><em>单股上限 {pct(data.research.risk?.max_position)}</em></div>
          <div><span>证据时点</span><strong>{data.research.profile?.trade_date || "—"}</strong><em>{data.research.evidence?.length ?? 0} 组证据</em></div>
        </div>
        <div className="workspace-risk-grid">
          <article><h3>硬否决</h3>{data.research.risk?.hard_reasons.length ? data.research.risk.hard_reasons.map(item => <div className="decision-issue error" key={item}>{item}</div>) : <p className="workspace-clear">当前未触发硬否决</p>}</article>
          <article><h3>强预警</h3>{data.research.risk?.warnings.length ? data.research.risk.warnings.map(item => <div className="decision-issue warning" key={item}>{item}</div>) : <p className="workspace-clear">当前未触发强预警</p>}</article>
        </div>
      </section>
      <section id="workspace-dimensions" className="workspace-section"><header><div><span>P1 · RESEARCH STATE</span><h2>多维研究状态</h2></div></header>
        <div className="research-dimensions">{Object.entries(data.research.dimensions ?? {}).map(([key, item]) => <article key={key} className={item.status === "ready" ? "ready" : "insufficient"}><span>{DIMENSION_NAMES[key] ?? key}</span><strong>{item.score.toFixed(1)}</strong><div><i style={{width: `${item.coverage * 100}%`}}/></div><em>{item.status === "ready" ? `覆盖 ${pct(item.coverage)}` : `缺 ${item.missing.length} 项`}</em></article>)}</div>
      </section>
      <section id="workspace-events" className="workspace-section"><header><div><span>P1/P2 · CHANGE</span><h2>事件与预期变化</h2></div></header>
        <div className="workspace-two-col"><article className="workspace-card"><h3>近期公告事件</h3>{data.events.slice(0, 5).map(item => <div className={`workspace-event event-${item.impact_direction}`} key={item.id}><div><strong>{EVENT_NAMES[item.event_type] ?? item.event_type}</strong><span>{item.event_date} · 影响 {item.impact.decayed_score.toFixed(1)}</span></div><p>{item.title}</p><em>{item.follow_up.next_action}</em></div>)}{!data.events.length && <div className="empty">没有匹配事件</div>}</article>
          <article className="workspace-card"><h3>预期状态</h3>{data.expectation?.ok ? <><div className="workspace-kpi"><span>维度得分</span><strong>{data.expectation.dimension?.score.toFixed(1)}</strong><em>覆盖 {pct(data.expectation.dimension?.coverage)}</em></div><div className="workspace-facts"><span>最新财务期</span><strong>{data.expectation.latest_period || "—"}</strong><span>预测基线</span><strong>{data.expectation.baseline?.target_period || "未生成"}</strong><span>业绩预告</span><strong>{data.expectation.external_forecast?.status || "未同步"}</strong></div></> : <div className="empty">预期证据不足</div>}</article></div>
      </section>
      <section id="workspace-valuation" className="workspace-section"><header><div><span>P3 · SAFETY MARGIN</span><h2>估值与安全边际</h2></div></header>
        {data.valuation?.ok && data.valuation.scenarios ? <div className="valuation-hero"><div><span>当前价格</span><strong>{data.valuation.current_price?.toFixed(2)}</strong><em>{data.valuation.trade_date}</em></div><div className="bear"><span>悲观价值</span><strong>{data.valuation.scenarios.bear.toFixed(2)}</strong><em>{pct(data.valuation.current_price ? data.valuation.scenarios.bear / data.valuation.current_price - 1 : null, 1)}</em></div><div className="base"><span>基准价值</span><strong>{data.valuation.scenarios.base.toFixed(2)}</strong><em>{pct(data.valuation.current_price ? data.valuation.scenarios.base / data.valuation.current_price - 1 : null, 1)}</em></div><div className="bull"><span>乐观价值</span><strong>{data.valuation.scenarios.bull.toFixed(2)}</strong><em>{pct(data.valuation.current_price ? data.valuation.scenarios.bull / data.valuation.current_price - 1 : null, 1)}</em></div></div> : <div className="research-empty"><strong>暂不输出目标价值</strong><span>先补估值证据。</span></div>}
      </section>
      <section id="workspace-thesis" className="workspace-section"><header><div><span>P3 · INVESTMENT THESIS</span><h2>投资逻辑与必要条件</h2></div></header>
        {data.theses.map(item => <article className="workspace-thesis" key={item.id}><div><span>{item.thesis_type} · {item.derived_status}</span><h3>{item.title}</h3><p>{item.core_thesis || "未填写核心逻辑"}</p></div><div className="workspace-kpi"><span>成立置信度</span><strong>{item.derived_confidence.toFixed(1)}</strong><em>下次复核 {item.next_review_at?.slice(0, 10) || "未设置"}</em></div><ul>{item.hypotheses.map(h => <li key={h.id}><strong>{h.necessary ? "必要条件" : "普通条件"}</strong><span>{h.statement}</span><em>{h.metric} · {h.status}</em></li>)}</ul></article>)}
        {!data.theses.length && <div className="research-empty"><strong>尚未建立投资逻辑</strong><span>先去补逻辑卡片。</span></div>}
      </section>
      <section className="workspace-section"><header><div><span>EVIDENCE & LIMITATIONS</span><h2>证据来源与限制</h2></div></header><div className="workspace-two-col"><article className="workspace-card">{data.research.evidence?.map(item => <div className="module-output" key={`${item.api}-${item.period}`}><i/>{item.api} · {item.period} · {item.available_time?.slice(0, 16)}</div>)}</article><article className="workspace-card">{data.research.limitations?.map(item => <div className="module-missing" key={item}>{item}</div>)}{data.valuation?.limitations?.map(item => <div className="module-missing" key={`valuation-${item}`}>{item}</div>)}{!data.research.limitations?.length && !data.valuation?.limitations?.length && <div className="workspace-clear">当前没有明显限制项</div>}</article></div></section>
    </>}
  </section>;
}
