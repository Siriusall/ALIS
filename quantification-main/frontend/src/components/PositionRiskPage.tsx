import { useEffect, useState } from "react";
import { confirmPositionState, fetchPortfolioRisk, type PortfolioRiskOverview } from "../api/research";

const pct = (value: number | undefined | null) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const money = (value: number | undefined | null) => value == null ? "—" : value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });

function statusText(value: string) {
  const map: Record<string, string> = {
    ok: "正常",
    warning: "预警",
    breach: "突破",
    critical: "严重",
  };
  return map[value] || value || "—";
}

export default function PositionRiskPage() {
  const [data, setData] = useState<PortfolioRiskOverview | null>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState("");
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      setData(await fetchPortfolioRisk());
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }

  async function confirm(item: PortfolioRiskOverview["position_state_recommendations"][number]) {
    setConfirming(item.symbol);
    setError("");
    try {
      await confirmPositionState({
        ts_code: item.symbol,
        to_state: item.recommended_state,
        reason: `risk_review_confirmed:${item.reason}`,
        evidence: [{ source: "portfolio_risk_overview", ...item.evidence }],
        confirmed: true,
      });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setConfirming("");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const breachCount = data?.breaches.length ?? 0;
  const stateRecommendations = data?.position_state_recommendations ?? [];
  const deleveraging = data?.deleveraging_actions ?? [];

  return (
    <section className="product-module-page">
      <div className="product-module-head">
        <div>
          <span>POSITION RISK</span>
          <h1>持仓风险与状态迁移</h1>
          <p>组合尾部风险、投资逻辑状态、集中度和压力测试使用同一数据时点；状态迁移建议必须留痕确认后才写入。</p>
        </div>
        <button className="button ghost" onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="alert">{error}</div>}

      {data && (
        <>
          <div className="product-module-grid">
            <article className="product-module-card">
              <h2>组合权益</h2>
              <strong>{money(data.account.equity)}</strong>
              <p>截至 {data.as_of || "—"}</p>
            </article>
            <article className="product-module-card">
              <h2>高水位回撤</h2>
              <strong>{pct(data.account.drawdown)}</strong>
              <p>目标不超过 -10%</p>
            </article>
            <article className="product-module-card">
              <h2>95% CVaR</h2>
              <strong>{data.tail_risk.observations ? pct(data.tail_risk.cvar) : "—"}</strong>
              <p>{data.tail_risk.observations} 个观测</p>
            </article>
            <article className="product-module-card">
              <h2>风险门</h2>
              <strong>{statusText(data.status)}</strong>
              <p>{breachCount} 项提示</p>
            </article>
          </div>

          <div className="decision-command-bar" aria-label="风险处理顺序">
            <div className="decision-command-intro">
              <span>RISK ORDER</span>
              <strong>风险处理优先于新增机会</strong>
            </div>
            <button type="button" className={breachCount > 0 ? "urgent" : ""}>
              <span>01 · 风险突破</span>
              <strong>{breachCount}</strong>
            </button>
            <button type="button" className={stateRecommendations.length > 0 ? "active" : ""}>
              <span>02 · 状态迁移</span>
              <strong>{stateRecommendations.length}</strong>
            </button>
            <button type="button" className={deleveraging.length > 0 ? "urgent" : ""}>
              <span>03 · 风险收缩</span>
              <strong>{deleveraging.length}</strong>
            </button>
            <button type="button">
              <span>04 · 压力测试</span>
              <strong>{data.stress_tests.length}</strong>
            </button>
          </div>

          <div className="decision-grid">
            <div className="card decision-panel">
              <h3>当前优先处理</h3>
              <div className="review-queue">
                <div className="review-queue-item">
                  <div>
                    <strong>先处理风险突破</strong>
                    <span>{breachCount} 个突破，优先于新增机会</span>
                  </div>
                  <em>突破</em>
                </div>
                <div className="review-queue-item">
                  <div>
                    <strong>确认状态迁移</strong>
                    <span>{stateRecommendations.length} 个持仓建议变更状态</span>
                  </div>
                  <em>迁移</em>
                </div>
                <div className="review-queue-item">
                  <div>
                    <strong>执行风险收缩</strong>
                    <span>{deleveraging.length} 条减仓、降权或预算收缩建议</span>
                  </div>
                  <em>收缩</em>
                </div>
              </div>
            </div>

            <div className="card decision-panel">
              <h3>当前风险摘要</h3>
              <div className="decision-issue-list">
                {data.breaches.slice(0, 5).map((item, index) => (
                  <div className={`decision-issue ${item.severity}`} key={`${item.code}-${index}`}>
                    {item.symbol ? `${item.symbol} · ` : item.industry ? `${item.industry} · ` : ""}
                    {item.message}
                  </div>
                ))}
                {data.limitations.slice(0, 3).map((item) => <div className="decision-issue warning" key={item}>{item}</div>)}
                {!data.breaches.length && !data.limitations.length && <div className="empty">当前没有额外风险提示。</div>}
              </div>
            </div>
          </div>

          <div className="card decision-panel">
            <h3>投资逻辑驱动的状态迁移建议</h3>
            {stateRecommendations.length ? (
              stateRecommendations.map((item) => (
                <div className="decision-issue warning" key={item.symbol}>
                  <strong>{item.symbol}</strong>：{item.current_state} → {item.recommended_state} · {item.reason}
                  <br />
                  <em>
                    逻辑 {item.evidence.thesis_status} / 置信度 {item.evidence.thesis_confidence.toFixed(0)}% / 风险 {item.evidence.risk_breaches.join("、") || "—"}；需留痕确认。
                  </em>
                  {item.current_state !== "uninitialized" && (
                    <button className="button ghost" onClick={() => void confirm(item)} disabled={confirming === item.symbol}>
                      {confirming === item.symbol ? "确认中…" : "确认迁移"}
                    </button>
                  )}
                </div>
              ))
            ) : (
              <div className="empty">当前持仓没有由逻辑或单票风险触发的状态迁移建议</div>
            )}
          </div>

          <div className="decision-grid">
            <div className="card decision-panel">
              <h3>压力测试</h3>
              {data.stress_tests.length ? (
                data.stress_tests.map((item) => (
                  <div className={`decision-issue ${item.severity}`} key={item.code}>
                    <strong>{item.name}</strong>：{pct(item.loss_pct)}（{money(item.pnl)}）· {item.assumption}
                  </div>
                ))
              ) : (
                <div className="empty">当前无权益持仓，尚无压力测试结果</div>
              )}
            </div>

            <div className="card decision-panel">
              <h3>行业集中度约束</h3>
              {data.industry_exposures.length ? (
                data.industry_exposures.map((item) => (
                  <div className={`decision-issue ${item.status === "breach" ? "error" : "warning"}`} key={item.industry}>
                    <strong>{item.industry}</strong>：{pct(item.weight)} / 上限 {pct(item.limit)}
                  </div>
                ))
              ) : (
                <div className="empty">当前无权益持仓</div>
              )}
            </div>
          </div>

          <div className="card decision-panel">
            <h3>风险收缩建议</h3>
            {deleveraging.length ? (
              deleveraging.map((item, index) => (
                <div className="decision-issue warning" key={`${item.action}-${index}`}>
                  {item.action}
                  {item.symbol ? ` · ${item.symbol}` : item.industry ? ` · ${item.industry}` : ""}
                  {item.target_weight != null ? `，目标权重 ${pct(item.target_weight)}` : ""}
                  {item.risk_budget_multiplier != null ? `，风险预算 ${pct(item.risk_budget_multiplier)}` : ""}
                  {" · "}
                  {item.reason}
                </div>
              ))
            ) : (
              <div className="empty">当前没有触发减仓建议</div>
            )}
          </div>

          <div className="card decision-panel">
            <h3>逐仓暴露、目标权重与解释</h3>
            <div className="table-scroll">
              <table>
                <thead><tr><th>标的</th><th>行业</th><th>当前</th><th>目标</th><th>调仓差</th><th>解释</th></tr></thead>
                <tbody>
                  {data.weight_explanations.map((item) => {
                    const exposure = data.exposures.find((value) => value.symbol === item.symbol);
                    return (
                      <tr key={item.symbol}>
                        <td>{item.symbol} {exposure?.name}</td>
                        <td>{exposure?.industry || "—"}</td>
                        <td>{pct(item.current_weight)}</td>
                        <td>{pct(item.target_weight)}</td>
                        <td>{pct(item.delta)}</td>
                        <td>{item.reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {!data.exposures.length && <div className="empty">当前没有持仓</div>}
          </div>
        </>
      )}
    </section>
  );
}
