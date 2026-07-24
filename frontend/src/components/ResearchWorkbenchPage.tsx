import { useEffect, useMemo, useState } from "react";
import {
  fetchIndustryAllocation,
  fetchResearchEvents,
  fetchStockPools,
  fetchTheses,
  type IndustryAllocation,
  type InvestmentThesis,
  type ResearchEvent,
  type StockPoolMember,
} from "../api/research";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";

interface ResearchWorkbenchPageProps {
  onNavigate?: (section: SectionId) => void;
}

interface ResearchHubState {
  pools: StockPoolMember[];
  allocation: IndustryAllocation | null;
  events: ResearchEvent[];
  theses: InvestmentThesis[];
}

const STATE_NAME: Record<string, string> = {
  watch: "观察中",
  valid: "已成立",
  warning: "预警",
  invalidated: "已证伪",
  draft: "生成中",
};

function pct(value: number | undefined | null, digits = 0) {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

export default function ResearchWorkbenchPage({ onNavigate }: ResearchWorkbenchPageProps) {
  const [data, setData] = useState<ResearchHubState | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const settled = await Promise.allSettled([
        fetchStockPools(),
        fetchIndustryAllocation(),
        fetchResearchEvents(),
        fetchTheses(),
      ]);
      const failures = settled.filter((item) => item.status === "rejected");
      setData({
        pools: settled[0].status === "fulfilled" ? settled[0].value : [],
        allocation: settled[1].status === "fulfilled" ? settled[1].value : null,
        events: settled[2].status === "fulfilled" ? settled[2].value : [],
        theses: settled[3].status === "fulfilled" ? settled[3].value : [],
      });
      setError(failures.length ? `有 ${failures.length} 个研究模块暂时不可用，其余结论仍可查看。` : "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const validTheses = useMemo(() => data?.theses.filter((item) => item.derived_status === "valid") ?? [], [data]);
  const warningTheses = useMemo(() => data?.theses.filter((item) => ["warning", "invalidated"].includes(item.derived_status)) ?? [], [data]);
  const urgentEvents = useMemo(() => data?.events.filter((item) => item.follow_up.status === "urgent") ?? [], [data]);
  const budgetItems = useMemo(() => data?.allocation?.items.filter((item) => item.target_weight > 0).slice(0, 6) ?? [], [data]);
  const watchPool = useMemo(() => data?.pools.filter((item) => item.pool_type === "watch").slice(0, 8) ?? [], [data]);

  return (
    <section className="product-module-page workbench-page research-hub-page">
      <div className="product-module-head">
        <div>
          <span>EVIDENCE DETAIL</span>
          <h1>证据库</h1>
          <p>这里不是日常入口，只用于查看系统判断背后的候选、行业预算、投资逻辑和反证细节。</p>
        </div>
        <div className="thesis-toolbar">
          <button type="button" className="button ghost" onClick={() => void load()} disabled={loading}>
            {loading ? "刷新中…" : "刷新系统判断"}
          </button>
          <button type="button" className="button primary" onClick={() => onNavigate?.("thesis")}>进入投资逻辑中控</button>
        </div>
      </div>

      {error && <div className="decision-issue warning">{error}</div>}
      {loading && !data && <LoadingState title="正在读取系统研究结论" detail="汇总股票池、行业预算、事件和投资逻辑" rows={4} />}

      {data && (
        <>
          <div className="decision-command-bar" aria-label="系统研究总览">
            <div className="decision-command-intro">
              <span>RESEARCH STATUS</span>
              <strong>系统当前研究状态</strong>
            </div>
            <button type="button" className={validTheses.length > 0 ? "active" : ""} onClick={() => onNavigate?.("thesis")}>
              <span>01 · 成立逻辑</span>
              <strong>{validTheses.length}</strong>
              <em>可进入组合约束</em>
            </button>
            <button type="button" className={warningTheses.length > 0 ? "urgent" : ""} onClick={() => onNavigate?.("thesis")}>
              <span>02 · 预警/证伪</span>
              <strong>{warningTheses.length}</strong>
              <em>等待复核处理</em>
            </button>
            <button type="button" onClick={() => onNavigate?.("industryResearch")}>
              <span>03 · 行业预算</span>
              <strong>{pct(data.allocation?.allocated_equity_budget, 1)}</strong>
              <em>{data.allocation?.market_state || "未同步"}</em>
            </button>
            <button type="button" className={urgentEvents.length > 0 ? "urgent" : ""} onClick={() => onNavigate?.("events")}>
              <span>04 · 高影响事件</span>
              <strong>{urgentEvents.length}</strong>
              <em>需重新计算影响</em>
            </button>
          </div>

          <div className="research-system-layout">
            <section className="research-system-panel">
              <header>
                <span>SYSTEM PICKS</span>
                <h2>系统建议关注</h2>
              </header>
              <div className="research-pick-list">
                {watchPool.map((item) => (
                  <button key={item.id} type="button" onClick={() => onNavigate?.("stockResearch")}>
                    <strong>{item.ts_code}</strong>
                    <span>{item.reason || "系统观察池候选"}</span>
                    <em>{item.status}</em>
                  </button>
                ))}
                {!watchPool.length && <div className="research-empty"><strong>暂无观察池候选</strong><span>系统会在候选筛选后自动补充。</span></div>}
              </div>
            </section>

            <section className="research-system-panel">
              <header>
                <span>INDUSTRY BUDGET</span>
                <h2>系统行业资金方向</h2>
              </header>
              <div className="research-budget-list">
                {budgetItems.map((item) => (
                  <article key={item.industry}>
                    <div>
                      <strong>{item.industry}</strong>
                      <span>{item.narrative_lifecycle} · 景气 {item.raw_score.toFixed(1)}</span>
                    </div>
                    <em>{pct(item.target_weight, 1)}</em>
                  </article>
                ))}
                {!budgetItems.length && <div className="research-empty"><strong>当前行业预算未打开</strong><span>股票预算会转入防御资产。</span></div>}
              </div>
            </section>
          </div>

          <div className="research-system-layout">
            <section className="research-system-panel">
              <header>
                <span>THESIS JUDGEMENT</span>
                <h2>系统投资逻辑</h2>
              </header>
              <div className="research-thesis-list">
                {data.theses.slice(0, 8).map((item) => (
                  <button key={item.id} type="button" onClick={() => onNavigate?.("thesis")}>
                    <div>
                      <strong>{item.ts_code} · {item.title}</strong>
                      <span>{item.core_thesis || "系统尚未生成完整解释"}</span>
                    </div>
                    <em>{STATE_NAME[item.derived_status] ?? item.derived_status} · {item.derived_confidence.toFixed(0)}</em>
                  </button>
                ))}
                {!data.theses.length && <div className="research-empty"><strong>暂无系统逻辑卡</strong><span>进入投资逻辑中控生成。</span></div>}
              </div>
            </section>

            <section className="research-system-panel">
              <header>
                <span>COUNTER EVIDENCE</span>
                <h2>反证与观察点</h2>
              </header>
              <div className="research-event-feed">
                {urgentEvents.slice(0, 6).map((item) => (
                  <article key={item.id} className={`event-${item.impact_direction}`}>
                    <strong>{item.ts_code} · {item.title}</strong>
                    <span>{item.follow_up.next_action}</span>
                    <em>影响 {item.impact.decayed_score.toFixed(1)}</em>
                  </article>
                ))}
                {!urgentEvents.length && <div className="research-empty"><strong>暂无紧急反证</strong><span>系统仍会持续跟踪事件和财务事实。</span></div>}
              </div>
            </section>
          </div>
        </>
      )}
    </section>
  );
}
