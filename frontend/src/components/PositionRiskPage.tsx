import { useEffect, useState } from "react";
import { confirmPositionState, fetchPortfolioRisk, type PortfolioRiskOverview } from "../api/research";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";
import SecurityLabel from "./SecurityLabel";

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

function stateText(value: string) {
  const map: Record<string, string> = { uninitialized: "未建立", warning: "观察", reduce: "减仓", core: "核心", exit: "退出" };
  return map[value] || value || "—";
}

function reasonText(value: string) {
  const map: Record<string, string> = {
    investment_thesis_invalidated: "投资逻辑已失效",
    single_name_risk_limit_breached: "单票风险超过上限",
    thesis_evidence_missing_or_below_core_threshold: "投资逻辑证据不足或置信度未达核心标准",
  };
  return map[value] || value || "—";
}

export default function PositionRiskPage({ onNavigate }: { onNavigate?: (section: SectionId) => void }) {
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

  if (loading && !data) {
    return <LoadingState variant="page" title="正在读取持仓风险" detail="正在计算风险突破、状态迁移和压力测试" rows={5} />;
  }

  return (
    <section className="product-module-page">
      <div className="product-module-head">
        <div>
          <span>POSITION RISK</span>
          <h1>系统风险处置方案</h1>
          <p>风险突破由系统识别并转成收缩、迁移和阻断建议；你只看最终方案是否允许落账。</p>
        </div>
        <button className="button ghost" onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="alert">{error}</div>}

      {data && (
        <>
          <div className="decision-issue warning">
            风险突破不是交给你逐条处理的待办；系统已经根据突破生成下方的状态迁移、风险收缩和组合约束建议。
          </div>

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

          <div className="decision-command-bar" aria-label="系统风险处置顺序">
            <div className="decision-command-intro">
              <span>RISK ORDER</span>
              <strong>系统先完成风险处置，再开放新增机会</strong>
            </div>
            <button type="button" className={breachCount > 0 ? "urgent" : ""}>
              <span>01 · 系统识别突破</span>
              <strong>{breachCount}</strong>
            </button>
            <button type="button" className={stateRecommendations.length > 0 ? "active" : ""}>
              <span>02 · 系统迁移建议</span>
              <strong>{stateRecommendations.length}</strong>
            </button>
            <button type="button" className={deleveraging.length > 0 ? "urgent" : ""}>
              <span>03 · 系统收缩建议</span>
              <strong>{deleveraging.length}</strong>
            </button>
            <button type="button">
              <span>04 · 压力测试</span>
              <strong>{data.stress_tests.length}</strong>
            </button>
          </div>

          <div className="card decision-panel">
            <h3>五功能仓目标合同</h3>
            <div className="product-module-grid">
              {Object.entries(data.five_bucket_summary?.buckets ?? {}).map(([key, item]) => (
                <article className="product-module-card" key={key}>
                  <h2>{item.name}</h2>
                  <strong>{pct(item.actual)}</strong>
                  <p>计划 {pct(item.target)} · 区间 {pct(item.range?.[0])}–{pct(item.range?.[1])} · {item.count} 个标的</p>
                </article>
              ))}
            </div>
            <div className="decision-two-col">
              <span>稳定集团</span><strong>{pct(data.five_bucket_summary?.risk_groups?.stable)}</strong>
              <span>进攻集团</span><strong>{pct(data.five_bucket_summary?.risk_groups?.offensive)}</strong>
            </div>
          </div>

          <div className="decision-grid">
            <div className="card decision-panel">
              <h3>系统处置队列</h3>
              <div className="review-queue">
                <div className="review-queue-item">
                  <div>
                    <strong>系统识别风险突破</strong>
                    <span>{breachCount} 个突破已进入自动风险收缩计算</span>
                  </div>
                  <em>识别</em>
                </div>
                <div className="review-queue-item">
                  <div>
                    <strong>系统生成状态迁移</strong>
                    <span>{stateRecommendations.length} 个持仓建议变更状态，确认后才写入</span>
                  </div>
                  <em>迁移</em>
                </div>
                <div className="review-queue-item">
                  <div>
                    <strong>系统生成风险收缩</strong>
                    <span>{deleveraging.length} 条减仓、降权或预算收缩建议</span>
                  </div>
                  <em>收缩</em>
                </div>
              </div>
            </div>

            <div className="card decision-panel">
              <h3>系统识别到的风险事实</h3>
              <div className="decision-issue-list">
                {data.breaches.slice(0, 5).map((item, index) => (
                  <div className={`decision-issue ${item.severity}`} key={`${item.code}-${index}`}>
                    {item.symbol ? <><SecurityLabel code={item.symbol} name={data.exposures.find((value) => value.symbol === item.symbol)?.name} /> · </> : item.industry ? `${item.industry} · ` : ""}
                    {item.message}
                  </div>
                ))}
                {data.limitations.slice(0, 3).map((item) => <div className="decision-issue warning" key={item}>{item}</div>)}
                {!data.breaches.length && !data.limitations.length && <div className="empty">当前没有额外风险提示。</div>}
              </div>
            </div>
          </div>

          <div className="card decision-panel">
            <h3>系统状态迁移建议</h3>
            {stateRecommendations.length ? (
              stateRecommendations.map((item) => (
                <div className="decision-issue warning" key={item.symbol}>
                  <strong><SecurityLabel code={item.symbol} name={data.exposures.find((value) => value.symbol === item.symbol)?.name} /></strong>：{stateText(item.current_state)} → {stateText(item.recommended_state)} · {reasonText(item.reason)}
                  <br />
                  <em>
                    逻辑 {item.evidence.thesis_status} / 置信度 {item.evidence.thesis_confidence.toFixed(0)}% / 风险 {item.evidence.risk_breaches.join("、") || "—"}；系统建议，确认后写入。
                  </em>
                  {item.current_state !== "uninitialized" && (
                    <button className="button ghost" onClick={() => void confirm(item)} disabled={confirming === item.symbol}>
                      {confirming === item.symbol ? "确认中…" : "确认系统建议"}
                    </button>
                  )}
                  {item.current_state === "uninitialized" && (
                    <button className="button ghost" onClick={() => onNavigate?.("thesis")}>查看逻辑中控</button>
                  )}
                </div>
              ))
            ) : (
              <div className="empty">系统当前没有生成状态迁移建议</div>
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
            <h3>逐标的目标权重合同</h3>
            {(data.target_weight_contracts ?? []).length ? (
              (data.target_weight_contracts ?? []).map((item) => (
                <div className={`decision-issue ${item.data_status === "ready" ? "warning" : "error"}`} key={item.symbol}>
                  <strong><SecurityLabel code={item.symbol} name={data.exposures.find((value) => value.symbol === item.symbol)?.name} /></strong>
                  ：{item.function_bucket_name} · 目标 {pct(item.target_weight)} · 当前 {pct(item.current_weight)} · {item.return_source}
                  <br />
                  <em>周期 {item.holding_period} / 生命周期 {item.narrative_lifecycle} / 资金 {item.funding_confirmation} / 数据 {item.data_status}</em>
                  <br />
                  <em>退出：{item.exit_conditions.join("；")}</em>
                </div>
              ))
            ) : (
              <div className="empty">当前没有可落账的目标权重合同。</div>
            )}
          </div>

          <div className="card decision-panel">
            <h3>系统风险收缩建议</h3>
            {deleveraging.length ? (
              deleveraging.map((item, index) => (
                <div className="decision-issue warning" key={`${item.action}-${index}`}>
                  {item.action}
                  {item.symbol ? <> · <SecurityLabel code={item.symbol} name={data.exposures.find((value) => value.symbol === item.symbol)?.name} /></> : item.industry ? ` · ${item.industry}` : ""}
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
            <h3>逐仓暴露与优化器参考权重</h3>
            <p className="muted">参考权重用于风险诊断，不代表已获授权的调仓动作；实际执行以“今日调仓建议”为准。</p>
            <div className="table-scroll">
              <table>
                <thead><tr><th>标的</th><th>行业</th><th>当前</th><th>优化器参考</th><th>参考差</th><th>解释</th></tr></thead>
                <tbody>
                  {data.weight_explanations.map((item) => {
                    const exposure = data.exposures.find((value) => value.symbol === item.symbol);
                    return (
                      <tr key={item.symbol}>
                        <td><SecurityLabel code={item.symbol} name={exposure?.name} /></td>
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
