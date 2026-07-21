import { useEffect, useMemo, useState } from "react";
import { fetchOperations, fetchPositions } from "../api/positions";
import { fetchPortfolioRisk, fetchStockPools, type PortfolioRiskOverview, type StockPoolMember } from "../api/research";
import type { Operation, Position } from "../types";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";
import SecurityLabel from "./SecurityLabel";

const MODULES: Array<{
  id: SectionId;
  title: string;
  description: string;
  badge: string;
}> = [
  { id: "stockPool", title: "候选池明细", description: "查看模型输出的候选范围。", badge: "候选" },
  { id: "thesis", title: "逻辑合同", description: "查看目标权重背后的证据。", badge: "证据" },
  { id: "portfolio", title: "当前持仓", description: "查看权益、现金和持仓。", badge: "持仓" },
  { id: "positionRisk", title: "风险方案", description: "查看系统处置建议。", badge: "风险" },
  { id: "executionLedger", title: "模拟账本", description: "查看建议是否落账。", badge: "执行" },
];

interface PortfolioWorkbenchPageProps {
  onNavigate?: (section: SectionId) => void;
}

function money(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function pct(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

export default function PortfolioWorkbenchPage({ onNavigate }: PortfolioWorkbenchPageProps) {
  const [poolItems, setPoolItems] = useState<StockPoolMember[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [risk, setRisk] = useState<PortfolioRiskOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const results = await Promise.allSettled([
        fetchStockPools(),
        fetchPositions(),
        fetchOperations(),
        fetchPortfolioRisk(),
      ]);
      if (cancelled) return;
      if (results[0].status === "fulfilled") setPoolItems(results[0].value);
      if (results[1].status === "fulfilled") setPositions(results[1].value.positions ?? []);
      if (results[2].status === "fulfilled") setOperations(results[2].value);
      if (results[3].status === "fulfilled") setRisk(results[3].value);
      const failures = results.filter((result) => result.status === "rejected");
      setLoadError(failures.length ? `${failures.length} 项组合数据暂时无法读取，页面展示其余已就绪数据。` : "");
      setLoading(false);
    }
    void load();
    const timer = window.setInterval(load, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const watchCount = poolItems.filter((item) => item.pool_type === "watch").length;
  const coreCount = poolItems.filter((item) => item.pool_type === "core").length;
  const holdingCount = poolItems.filter((item) => item.pool_type === "holding").length;
  const stateRecommendations = risk?.position_state_recommendations ?? [];
  const deleveraging = risk?.deleveraging_actions ?? [];
  const breaches = risk?.breaches ?? [];
  const recentOps = operations.slice(0, 5);
  const bucketEntries = Object.entries(risk?.five_bucket_summary?.buckets ?? {});
  const contracts = risk?.target_weight_contracts ?? [];
  const allocationTotal = bucketEntries.reduce((sum, [, item]) => sum + Number(item.actual || 0), 0) || 1;
  const totalMarketValue = positions.reduce((sum, item) => sum + (item.current_value ?? item.price * item.quantity), 0);
  const largestPosition = useMemo(() => {
    if (!positions.length || totalMarketValue <= 0) return null;
    return positions
      .map((item) => ({
        ...item,
        value: item.current_value ?? item.price * item.quantity,
      }))
      .sort((a, b) => b.value - a.value)[0];
  }, [positions, totalMarketValue]);

  if (loading) {
    return <LoadingState variant="page" title="正在读取组合与执行" detail="正在汇总股票池、持仓、风险和执行流水" rows={5} />;
  }

  return (
    <section className="product-module-page workbench-page">
      <div className="product-module-head">
        <div>
          <span>ALLOCATION</span>
          <h1>组合配置</h1>
          <p>只看系统给出的资金分布、风险方案和模拟落账状态。</p>
        </div>
      </div>

      {loadError && <div className="alert">{loadError}</div>}

      <div className="workbench-hero">
        <div>
          <span>当前主线</span>
          <strong>
            {breaches.length > 0
              ? `系统已识别 ${breaches.length} 个风险突破，先看自动处置方案`
              : stateRecommendations.length > 0
                ? `系统生成了 ${stateRecommendations.length} 个持仓状态迁移建议`
                : coreCount > 0
                  ? `当前有 ${coreCount} 个核心候选，可继续看系统执行建议`
                  : "系统会先汇总股票池和持仓风险，再给出执行建议"}
          </strong>
        </div>
        <button type="button" className="button primary" onClick={() => onNavigate?.(breaches.length > 0 ? "positionRisk" : "portfolio")}>
          {breaches.length > 0 ? "查看系统风险方案" : "打开组合管理"}
        </button>
      </div>

      <section className="portfolio-allocation-board">
        <header>
          <div><span>FIVE BUCKET ALLOCATION</span><h2>资金现在放在哪里</h2><p>横条表示当前资金占比，右侧直接标出与目标的偏差。</p></div>
          <button type="button" className="button ghost" onClick={() => onNavigate?.("positionRisk")}>完整风险分析</button>
        </header>
        <div className="portfolio-allocation-layout">
          <div className="portfolio-allocation-main">
            <div className="portfolio-stacked-bar" aria-label="五功能仓资金分布">
              {bucketEntries.map(([key, item]) => <i key={key} className={`bucket-${key}`} style={{ width: `${Math.max(0, Number(item.actual || 0) / allocationTotal * 100)}%` }} title={`${item.name} ${pct(item.actual)}`} />)}
            </div>
            <div className="portfolio-bucket-list">
              {bucketEntries.map(([key, item]) => {
                const delta = Number(item.actual || 0) - Number(item.target || 0);
                const outside = Number(item.actual || 0) < Number(item.range?.[0] || 0) || Number(item.actual || 0) > Number(item.range?.[1] || 1);
                return <article key={key}>
                  <i className={`bucket-${key}`} />
                  <div><strong>{item.name}</strong><span>合理区间 {pct(item.range?.[0])}–{pct(item.range?.[1])}</span></div>
                  <b>{pct(item.actual)}</b>
                  <em className={outside ? "outside" : "inside"}>{delta > 0 ? "+" : ""}{pct(delta)}</em>
                </article>;
              })}
              {!bucketEntries.length && <div className="empty">当前还没有可展示的五仓配置。</div>}
            </div>
          </div>
          <aside className="portfolio-allocation-aside">
            <span>组合当前取向</span>
            <strong>{Number(risk?.five_bucket_summary?.risk_groups?.stable || 0) >= .7 ? "明显偏防守" : "攻守相对均衡"}</strong>
            <p>稳定类 {pct(risk?.five_bucket_summary?.risk_groups?.stable)}，进攻类 {pct(risk?.five_bucket_summary?.risk_groups?.offensive)}。</p>
            <div><span>风险突破</span><b>{breaches.length}</b></div>
            <div><span>待确认合同</span><b>{contracts.filter((item) => item.data_status !== "ready").length}</b></div>
            <button type="button" className="button primary" onClick={() => onNavigate?.("positionRisk")}>查看系统处置方案</button>
          </aside>
        </div>
      </section>

      <section className="portfolio-operator-board">
        <button type="button" onClick={() => onNavigate?.("stockPool")}>
          <span>候选来源</span><strong>{watchCount}/{coreCount}/{holdingCount}</strong><p>观察 / 核心 / 持仓</p>
        </button>
        <button type="button" onClick={() => onNavigate?.("positionRisk")} className={breaches.length ? "urgent" : ""}>
          <span>系统风险方案</span><strong>{breaches.length}</strong><p>{deleveraging.length} 条收缩建议，{stateRecommendations.length} 条迁移建议</p>
        </button>
        <button type="button" onClick={() => onNavigate?.("executionLedger")}>
          <span>模拟落账</span><strong>{recentOps.length}</strong><p>查看最近执行流水和建议是否落账</p>
        </button>
        <button type="button" onClick={() => onNavigate?.("thesis")}>
          <span>逻辑合同</span><strong>{contracts.length}</strong><p>目标权重的收益来源、周期、退出条件</p>
        </button>
      </section>

      <details className="module-detail-drawer">
        <summary><span>展开组合合同、快照、流水和详情入口</span><em>目标权重合同、组合风险、执行账本、候选池明细</em></summary>

      <div className="card decision-panel">
        <div className="governance-section-head">
          <div><h3>目标权重合同</h3><p>每笔目标仓位都必须说明收益来源、持有周期、资金确认和退出条件。</p></div>
          <strong>{contracts.length}</strong>
        </div>
        <div className="decision-issue-list">
          {contracts.slice(0, 6).map((item) => (
            <div className={`decision-issue ${item.data_status === "ready" ? "warning" : "error"}`} key={item.symbol}>
              <strong><SecurityLabel code={item.symbol} name={positions.find((position) => position.symbol === item.symbol)?.name} /></strong>
              ：{item.function_bucket_name} · 当前 {pct(item.current_weight)} → 目标 {pct(item.target_weight)}
              <br />
              <em>{item.return_source} · 持有 {item.holding_period} · 资金 {item.funding_confirmation} · 数据 {item.data_status}</em>
              <br />
              <em>退出：{item.exit_conditions.join("；") || "尚未记录"}</em>
            </div>
          ))}
          {!contracts.length && <div className="empty">当前没有可落账的目标权重合同。</div>}
        </div>
      </div>

      <div className="product-module-grid">
        <article className="product-module-card">
          <h2>股票池</h2>
          <strong>{watchCount}/{coreCount}/{holdingCount}</strong>
          <p>观察 / 核心 / 持仓</p>
        </article>
        <article className="product-module-card">
          <h2>组合市值</h2>
          <strong>{money(totalMarketValue)}</strong>
          <p>{positions.length} 个持仓</p>
        </article>
        <article className="product-module-card">
          <h2>风险突破</h2>
          <strong>{breaches.length}</strong>
          <p>{deleveraging.length} 条风险收缩建议</p>
        </article>
        <article className="product-module-card">
          <h2>最近操作</h2>
          <strong>{recentOps.length}</strong>
          <p>最近执行流水条数</p>
        </article>
      </div>

      <div className="decision-grid">
        <div className="card decision-panel">
          <h3>系统下一步建议</h3>
          <div className="review-queue">
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("stockPool")}>
              <div>
                <strong>系统候选池</strong>
                <span>{watchCount} 个观察标的，{coreCount} 个核心候选</span>
              </div>
              <em>准入</em>
            </button>
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("positionRisk")}>
              <div>
                <strong>查看风险处置方案</strong>
                <span>系统已把 {breaches.length} 个突破转成 {deleveraging.length} 条收缩建议、{stateRecommendations.length} 条迁移建议</span>
              </div>
              <em>风险</em>
            </button>
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("executionLedger")}>
              <div>
                <strong>系统执行账本</strong>
                <span>{recentOps.length} 条最近流水，用来验证系统建议是否落账</span>
              </div>
              <em>执行</em>
            </button>
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("thesis")}>
              <div>
                <strong>系统逻辑复核</strong>
                <span>状态迁移和减仓动作会回连到逻辑是否失效</span>
              </div>
              <em>逻辑</em>
            </button>
          </div>
        </div>

        <div className="card decision-panel">
          <h3>组合快照</h3>
          <div className="decision-two-col">
            <span>组合权益</span>
            <strong>{money(risk?.account.equity)}</strong>
            <span>高水位回撤</span>
            <strong>{pct(risk?.account.drawdown)}</strong>
            <span>风险门</span>
            <strong>{risk?.status || "—"}</strong>
            <span>最大单票</span>
            <strong>{largestPosition ? <><SecurityLabel code={largestPosition.symbol} name={largestPosition.name} /> {pct(largestPosition.value / totalMarketValue)}</> : "—"}</strong>
            <span>95% CVaR</span>
            <strong>{pct(risk?.tail_risk.cvar)}</strong>
            <span>最新风险时点</span>
            <strong>{risk?.as_of || "—"}</strong>
          </div>
        </div>
      </div>

      <div className="decision-grid">
        <div className="card decision-panel">
          <h3>风险与状态迁移摘要</h3>
          <div className="decision-issue-list">
            {breaches.slice(0, 5).map((item) => (
              <div key={`${item.code}-${item.message}`} className="decision-issue error">
                {item.message}
              </div>
            ))}
            {stateRecommendations.slice(0, 4).map((item) => (
              <div key={`${item.symbol}-${item.recommended_state}`} className="decision-issue warning">
                <SecurityLabel code={item.symbol} name={positions.find((position) => position.symbol === item.symbol)?.name} />：{item.current_state} → {item.recommended_state} · {item.reason}
              </div>
            ))}
            {!breaches.length && !stateRecommendations.length && (
              <div className="empty">当前没有突出的单票或组合级风险动作。</div>
            )}
          </div>
        </div>

        <div className="card decision-panel">
          <h3>最近执行流水</h3>
          <div className="decision-issue-list">
            {recentOps.map((item, index) => (
              <div key={`${item.symbol}-${index}`} className="decision-issue warning">
                <SecurityLabel code={item.symbol} name={item.name} /> · {String(item.side || "操作")} · 数量 {item.quantity ?? "—"} · 价格 {item.price ?? "—"}
              </div>
            ))}
            {!recentOps.length && <div className="empty">暂无最近执行流水。</div>}
          </div>
        </div>
      </div>

      <div className="workbench-grid">
        {MODULES.map((item) => (
          <button key={item.id} type="button" className="workbench-card workbench-card-link" onClick={() => onNavigate?.(item.id)}>
            <div className="workbench-card-top">
              <span>{item.badge}</span>
              <h2>{item.title}</h2>
            </div>
            <p>{item.description}</p>
            <span className="workbench-card-enter">进入工作台 <b>→</b></span>
          </button>
        ))}
      </div>
      </details>
    </section>
  );
}
