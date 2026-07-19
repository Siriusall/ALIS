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
  { id: "stockPool", title: "股票池", description: "先看候选范围。", badge: "入口" },
  { id: "thesis", title: "投资逻辑", description: "确认逻辑是否还成立。", badge: "逻辑" },
  { id: "portfolio", title: "组合管理", description: "查看当前持仓与权益。", badge: "持仓" },
  { id: "positionRisk", title: "持仓风险", description: "先处理风险动作。", badge: "风险" },
  { id: "executionLedger", title: "模拟执行账本", description: "核对建议是否执行。", badge: "执行" },
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
          <span>PORTFOLIO HUB</span>
          <h1>组合与执行</h1>
          <p>这里把研究结论变成组合动作。</p>
        </div>
      </div>

      {loadError && <div className="alert">{loadError}</div>}

      <div className="workbench-hero">
        <div>
          <span>当前主线</span>
          <strong>
            {breaches.length > 0
              ? `先处理 ${breaches.length} 个风险突破，再看新增机会`
              : stateRecommendations.length > 0
                ? `有 ${stateRecommendations.length} 个持仓建议做状态迁移`
                : coreCount > 0
                  ? `当前有 ${coreCount} 个核心候选，可继续核查是否进入执行`
                  : "先看股票池和持仓风险，再决定是否执行"}
          </strong>
        </div>
        <button type="button" className="button primary" onClick={() => onNavigate?.(breaches.length > 0 ? "positionRisk" : "portfolio")}>
          {breaches.length > 0 ? "去处理持仓风险" : "打开组合管理"}
        </button>
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
          <h3>当前待处理动作</h3>
          <div className="review-queue">
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("stockPool")}>
              <div>
                <strong>先看股票池</strong>
                <span>{watchCount} 个观察标的，{coreCount} 个核心候选</span>
              </div>
              <em>准入</em>
            </button>
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("positionRisk")}>
              <div>
                <strong>处理持仓风险</strong>
                <span>{breaches.length} 个风险突破，{stateRecommendations.length} 个状态迁移建议</span>
              </div>
              <em>风险</em>
            </button>
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("executionLedger")}>
              <div>
                <strong>核对执行账本</strong>
                <span>{recentOps.length} 条最近流水，确认建议是否被正确执行</span>
              </div>
              <em>执行</em>
            </button>
            <button type="button" className="review-queue-item" onClick={() => onNavigate?.("thesis")}>
              <div>
                <strong>复核投资逻辑</strong>
                <span>把状态迁移和减仓动作回连到逻辑是否失效</span>
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
    </section>
  );
}
