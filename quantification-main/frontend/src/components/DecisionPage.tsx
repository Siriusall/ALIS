import { useEffect, useMemo, useRef, useState } from "react";
import type { SectionId } from "./Layout";
import { fetchDecisionSummary, type DecisionSummary } from "../api/decision";
import type { QuantAnalyzeResponse } from "../types";

const ANALYZE_RESULT_KEY = "quant_analyze_result_v1";

const TREND_TEXT: Record<string, string> = {
  uptrend: "趋势偏强",
  sideways: "震荡",
  downtrend: "趋势偏弱",
  high_risk: "高风险",
};

const RISK_TEXT: Record<string, string> = {
  normal: "正常",
  elevated: "偏高",
  high: "高",
};

const SENTIMENT_TEXT: Record<string, string> = {
  risk_on: "偏进攻",
  neutral: "中性",
  risk_off: "偏防守",
};

const OBSERVATION_TEXT: Record<string, string> = {
  breadth: "上涨宽度",
  average_return: "全市场平均收益",
  volume_ratio: "成交额强度",
  limit_up_ratio: "涨停占比",
  limit_down_ratio: "跌停占比",
  crowding: "成交拥挤度",
};

function loadStoredAnalyzeResult(): QuantAnalyzeResponse | null {
  try {
    const raw = sessionStorage.getItem(ANALYZE_RESULT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as QuantAnalyzeResponse;
    return parsed?.ok ? parsed : null;
  } catch {
    return null;
  }
}

function fmtMoney(value: number | null | undefined): string {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  return String(value).slice(0, 19).replace("T", " ");
}

function fmtPct(value: unknown, digits = 1): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function issueText(issue: { level?: string; code?: string; message?: string }): string {
  const prefix = issue.level === "error" ? "错误" : issue.level === "warning" ? "警告" : issue.level || "提示";
  return `${prefix} / ${issue.code}：${issue.message}`;
}

interface DecisionPageProps {
  onNavigate?: (section: SectionId) => void;
}

export default function DecisionPage({ onNavigate }: DecisionPageProps) {
  const [summary, setSummary] = useState<DecisionSummary | null>(null);
  const [analyze, setAnalyze] = useState<QuantAnalyzeResponse | null>(() => loadStoredAnalyzeResult());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const refreshRunning = useRef(false);

  async function refresh(showLoading = true) {
    if (refreshRunning.current) return;
    refreshRunning.current = true;
    if (showLoading) {
      setLoading(true);
      setError("");
    }
    try {
      const data = await fetchDecisionSummary(30);
      setSummary(data);
      setAnalyze(loadStoredAnalyzeResult());
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      if (showLoading) {
        setError(typeof msg === "string" ? msg : "决策展板加载失败");
      }
    } finally {
      refreshRunning.current = false;
      if (showLoading) setLoading(false);
    }
  }

  useEffect(() => {
    let active = true;
    (async () => {
      await refresh();
      if (!active) return;
    })();
    const timer = setInterval(() => {
      void refresh(false);
    }, 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  const health = summary?.data_health ?? null;
  const ledger = summary?.ledger ?? null;
  const gate = summary?.gate_decision;
  const modelStatus = summary?.model_status ?? null;
  const activeModel = modelStatus?.active_version ?? null;
  const market = summary?.market_overview ?? null;
  const marketState = market?.market_state ?? null;
  const sentiment = market?.sentiment ?? null;
  const positionGate = market?.position_gate ?? null;
  const validation = summary?.latest_model_validation ?? null;
  const recentExec = summary?.recent_execution ?? null;

  const dataErrorCount = health?.issues?.filter((x) => x.level === "error").length ?? 0;
  const warningCount = health?.issues?.filter((x) => x.level === "warning").length ?? 0;
  const recentHealthDays = health?.market_snapshots?.recent_days ?? [];
  const latestFactorCoverage =
    recentHealthDays.length > 0 ? recentHealthDays[recentHealthDays.length - 1]?.core_factor_coverage ?? {} : {};

  const buySuggestions = analyze?.rebalance_actions?.buy?.length ?? 0;
  const sellSuggestions = analyze?.rebalance_actions?.sell?.length ?? 0;
  const holdSuggestions = analyze?.rebalance_actions?.hold?.length ?? 0;
  const pendingAnalyze = Boolean(analyze?.ok && (buySuggestions > 0 || sellSuggestions > 0));

  const modelReadyText = useMemo(() => {
    if (!activeModel) return "待训练";
    if (activeModel.is_active) return "已通过验证";
    return String(activeModel.status || "候选");
  }, [activeModel]);

  return (
    <section id="decision" className="dashboard-section" data-section="decision">
      <h2 className="dashboard-section-title">市场总览</h2>
      <div className="dashboard-section-inner decision-page">
        <div className={`decision-banner ${gate?.tone || "hold"}`}>
          <div>
            <div className="decision-banner-label">今日调仓判断</div>
            <div className="decision-banner-title">{gate?.title || "加载中"}</div>
            <div className="decision-banner-detail">{gate?.detail || "正在读取数据、模型与账户状态。"}</div>
          </div>
          <button type="button" onClick={() => void refresh()} disabled={loading}>
            {loading ? "检查中" : "刷新"}
          </button>
        </div>
        {error && <div className="alert">{error}</div>}

        <div className="market-overview-strip">
          <article className={`market-overview-state ${market?.ok ? "ready" : "degraded"}`}>
            <span>市场状态</span>
            <strong>{marketState?.label || "观测不足"}</strong>
            <small>
              {marketState
                ? `置信度 ${(marketState.confidence * 100).toFixed(0)}% · ${marketState.model}`
                : market?.reason || "等待市场数据"}
            </small>
          </article>
          <article>
            <span>市场情绪</span>
            <strong>{sentiment ? sentiment.score.toFixed(1) : "—"}</strong>
            <small>{sentiment ? SENTIMENT_TEXT[sentiment.state] || sentiment.state : "无合格观测"}</small>
          </article>
          <article>
            <span>组合风险预算</span>
            <strong>{marketState ? `${Math.round(marketState.risk_budget_multiplier * 100)}%` : "收缩"}</strong>
            <small>{positionGate?.reason || "环境门控只降低预算，不改变企业质量"}</small>
          </article>
          <article>
            <span>当前回撤</span>
            <strong className={Number(ledger?.drawdown || 0) <= -0.08 ? "decision-cell-stop" : ""}>
              {fmtPct(ledger?.drawdown)}
            </strong>
            <small>高水位 {fmtMoney(ledger?.high_water_mark)}</small>
          </article>
          <article>
            <span>主题主线</span>
            <strong>{market?.themes.items?.[0]?.theme || "未确认"}</strong>
            <small>{market?.themes.reason || "暂无通过确认门的主题"}</small>
          </article>
        </div>

        <div className="decision-grid market-overview-context">
          <div className="card decision-panel">
            <div className="quant-analyze-section-head">
              <h3>市场环境证据</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("sentiment")}>
                查看情绪详情
              </button>
            </div>
            {market?.ok ? (
              <>
                <div className="market-observation-grid">
                  {Object.entries(market.observations ?? {})
                    .filter(([key]) => key in OBSERVATION_TEXT)
                    .map(([key, value]) => (
                      <div key={key}>
                        <span>{OBSERVATION_TEXT[key]}</span>
                        <strong>{key.includes("ratio") || key === "breadth" || key === "average_return" || key === "crowding" ? fmtPct(value) : Number(value).toFixed(2)}</strong>
                      </div>
                    ))}
                </div>
                <p className="decision-message">{marketState?.reason}</p>
                {(market.limitations ?? []).map((item) => <div className="decision-issue warning" key={item}>{item}</div>)}
              </>
            ) : (
              <div className="decision-issue warning">{market?.reason || "市场数据不足，当前不生成环境判断。"}</div>
            )}
          </div>
          <div className="card decision-panel">
            <div className="quant-analyze-section-head">
              <h3>主题与组合解释</h3>
              <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("themes")}>
                查看主题中心
              </button>
            </div>
            {market?.themes.items?.length ? market.themes.items.slice(0, 5).map((theme) => (
              <div className="market-theme-row" key={theme.theme}><strong>{theme.theme}</strong><span>{theme.score.toFixed(1)} · {theme.state}</span></div>
            )) : <div className="market-empty-state"><strong>没有已确认主题</strong><p>{market?.themes.reason || "主题数据尚未接入"}</p></div>}
            <div className="decision-two-col market-account-compact">
              <span>总权益</span><strong>{fmtMoney(ledger?.total_equity)}</strong>
              <span>现金占比</span><strong>{fmtPct(ledger?.cash_pct)}</strong>
              <span>持仓数量</span><strong>{ledger?.position_count ?? 0}</strong>
              <span>最大单票</span><strong>{ledger?.max_position_symbol ? `${ledger.max_position_symbol} ${fmtPct(ledger.max_position_pct)}` : "—"}</strong>
            </div>
          </div>
        </div>

        <div className="decision-grid decision-metrics">
          <div className="card metric-card">
            <div className="sub">数据状态</div>
            <div className="metric">{health?.ok ? "可信" : "需修复"}</div>
            <div className="decision-note">错误 {dataErrorCount} / 警告 {warningCount}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">总权益 / 现金</div>
            <div className="metric">{fmtMoney(ledger?.total_equity)}</div>
            <div className="decision-note">现金 {fmtMoney(ledger?.cash_balance)}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">模型状态</div>
            <div className="metric">{activeModel?.version || "未生成"}</div>
            <div className="decision-note">{modelReadyText}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">最近分析</div>
            <div className="metric">{analyze?.signal_date || "暂无"}</div>
            <div className="decision-note">
              买 {buySuggestions} / 卖 {sellSuggestions}
              {analyze?.same_day_fallback ? " · 同日口径" : ""}
            </div>
          </div>
        </div>

        <div className="decision-grid">
          <div className="card decision-panel">
            <h3>数据是否可信</h3>
            <div className="decision-two-col">
              <span>交易日覆盖</span>
              <strong>{health?.trade_dates?.count ?? 0} 天，最近 {health?.trade_dates?.last || "—"}</strong>
              <span>PostgreSQL 行情快照</span>
              <strong>{health?.market_snapshots?.count ?? 0} 天，最近 {health?.market_snapshots?.last || "—"}</strong>
              <span>快照覆盖</span>
              <strong>
                PostgreSQL {health?.snapshots?.postgresql_snapshot?.count ?? 0} 天
              </strong>
              <span>daily_basic</span>
              <strong>
                PostgreSQL {health?.daily_basic?.postgresql?.count ?? 0} 天
              </strong>
              <span>最近因子覆盖</span>
              <strong>
                close {Math.round(Number(latestFactorCoverage.close || 0) * 100)}% / volume{" "}
                {Math.round(Number(latestFactorCoverage.volume || 0) * 100)}% / pb{" "}
                {Math.round(Number(latestFactorCoverage.pb || 0) * 100)}%
              </strong>
            </div>
            <div className="decision-issue-list">
              {(health?.issues ?? []).slice(0, 5).map((issue) => (
                <div key={`${issue.code}-${issue.message}`} className={`decision-issue ${issue.level}`}>
                  {issueText(issue)}
                </div>
              ))}
              {health && health.issues.length === 0 && <div className="empty">暂无数据健康问题</div>}
            </div>
          </div>

          <div className="card decision-panel">
            <h3>账户与风险状态</h3>
            <div className="decision-two-col">
              <span>账户总权益</span>
              <strong>{fmtMoney(ledger?.total_equity)}</strong>
              <span>持仓市值</span>
              <strong>{fmtMoney(ledger?.position_market_value)}</strong>
              <span>最近估值日</span>
              <strong>{ledger?.valuation_date || "—"}</strong>
              <span>持仓数量</span>
              <strong>{ledger?.position_count ?? 0}</strong>
              <span>现金占比</span>
              <strong>{fmtPct(ledger?.cash_pct)}</strong>
              <span>相对初始资金</span>
              <strong className={Number(ledger?.equity_delta || 0) < 0 ? "decision-cell-stop" : "decision-cell-go"}>
                {fmtMoney(ledger?.equity_delta)}
              </strong>
              <span>最大单票占比</span>
              <strong>
                {ledger?.max_position_symbol
                  ? `${ledger.max_position_symbol} ${fmtPct(ledger.max_position_pct)}`
                  : "—"}
              </strong>
            </div>
            <div className="decision-issue-list">
              {(ledger?.account_risks ?? []).map((risk) => (
                <div key={risk.code} className={`decision-issue ${risk.level}`}>
                  {risk.message}
                </div>
              ))}
              {(ledger?.account_risks ?? []).length === 0 && (
                <div className="empty">账户风险指标正常</div>
              )}
            </div>
          </div>
        </div>

        <div className="card decision-panel">
          <div className="quant-analyze-section-head">
            <h3>模型就绪状态</h3>
            <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("strategy")}>
              前往模型策略
            </button>
          </div>
          <div className="decision-two-col">
            <span>当前版本</span>
            <strong>{activeModel?.version || "尚未生成"}</strong>
            <span>模型类型</span>
            <strong>{String(activeModel?.config?.model_type || "机器学习 ranking")}</strong>
            <span>验证状态</span>
            <strong>{modelReadyText}</strong>
            <span>最近训练</span>
            <strong>
              {modelStatus?.latest_train_run
                ? `${modelStatus.latest_train_run.status} / ${fmtDate(modelStatus.latest_train_run.updated_at)}`
                : "—"}
            </strong>
          </div>
          {validation && (
            <div className="metrics-row" style={{ marginTop: 12 }}>
              <div className="card metric-card">
                <div className="sub">验证总收益</div>
                <div className="metric">{fmtPct(validation.metrics.total_return)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">最大回撤</div>
                <div className="metric">{fmtPct(validation.metrics.max_drawdown)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">夏普</div>
                <div className="metric">{Number(validation.metrics.sharpe || 0).toFixed(2)}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">验证区间</div>
                <div className="metric" style={{ fontSize: 14 }}>
                  {validation.start_date} ~ {validation.end_date}
                </div>
              </div>
            </div>
          )}
          {!activeModel && (
            <div className="decision-message">还没有可用模型，请先在模型策略页完成自动训练与验证。</div>
          )}
        </div>

        <div className="card decision-panel">
          <div className="quant-analyze-section-head">
            <h3>最新分析摘要</h3>
            <button type="button" className="quant-analyze-secondary-btn" onClick={() => onNavigate?.("validation")}>
              前往量化分析
            </button>
          </div>
          {!analyze ? (
            <div className="empty">暂无分析结果。请前往量化分析页生成模型调仓建议。</div>
          ) : (
            <>
              <div className="decision-two-col">
                <span>分析日</span>
                <strong>{analyze.signal_date || "—"}</strong>
                <span>执行日</span>
                <strong>{analyze.execute_date || "—"}</strong>
                <span>趋势</span>
                <strong>
                  {TREND_TEXT[String(analyze.trend_analysis?.trend || "")] ||
                    analyze.trend_analysis?.trend ||
                    "—"}
                </strong>
                <span>风险档位</span>
                <strong>
                  {RISK_TEXT[String(analyze.trend_analysis?.risk_level || "")] ||
                    analyze.trend_analysis?.risk_level ||
                    "—"}
                </strong>
                <span>目标组合</span>
                <strong>
                  买 {buySuggestions} / 卖 {sellSuggestions} / 持有 {holdSuggestions}
                </strong>
                <span>执行说明</span>
                <strong>{analyze.execution_note || (analyze.same_day_fallback ? "同日执行口径" : "T+1 口径")}</strong>
              </div>
              <div className="decision-message">{analyze.summary || "—"}</div>
              {(analyze.selections?.length ?? 0) > 0 && (
                <div className="table-scroll decision-trade-table" style={{ marginTop: 12 }}>
                  <table>
                    <thead>
                      <tr>
                        <th>股票</th>
                        <th>得分</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(analyze.selections ?? []).slice(0, 5).map((row) => (
                        <tr key={row.symbol}>
                          <td>
                            {row.symbol}
                            {row.name ? ` ${row.name}` : ""}
                          </td>
                          <td>{row.score == null ? "—" : Number(row.score).toFixed(4)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {pendingAnalyze && (
                <div className="decision-message" style={{ marginTop: 8 }}>
                  有待确认调仓建议，请在量化分析页预览后人工确认执行。
                </div>
              )}
            </>
          )}
        </div>

        <div className="card decision-panel">
          <h3>最近确认执行</h3>
          {recentExec ? (
            <>
              <div className="decision-two-col">
                <span>执行时间</span>
                <strong>{fmtDate(recentExec.executed_at)}</strong>
                <span>买入/卖出</span>
                <strong>
                  买 {recentExec.buy_count} / 卖 {recentExec.sell_count}
                </strong>
                <span>买入成本</span>
                <strong>{fmtMoney(recentExec.total_buy_cost)}</strong>
                <span>预算 / TopN</span>
                <strong>
                  {fmtMoney(recentExec.budget)} / {recentExec.top_n}
                </strong>
              </div>
              <div className="table-scroll decision-trade-table" style={{ marginTop: 12 }}>
                <table>
                  <thead>
                    <tr>
                      <th>股票</th>
                      <th>方向</th>
                      <th>数量</th>
                      <th>成交价</th>
                      <th>时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentExec.operations.map((op, index) => (
                      <tr key={`${op.symbol}-${op.side}-${index}`}>
                        <td>
                          {op.symbol}
                          {op.name ? ` ${op.name}` : ""}
                        </td>
                        <td>{String(op.side).toUpperCase() === "SELL" ? "卖出" : "买入"}</td>
                        <td>{op.quantity}</td>
                        <td>{Number(op.price || 0).toFixed(3)}</td>
                        <td>{fmtDate(op.at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <div className="empty">暂无确认执行记录。在量化分析页预览并确认执行后，这里会显示最近流水。</div>
          )}
        </div>
      </div>
    </section>
  );
}
