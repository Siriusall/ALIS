import { useEffect, useMemo, useState } from "react";
import { fetchAccount, type AccountInfo } from "../api/account";
import { fetchPositions } from "../api/positions";
import { fetchPortfolioCandles, fetchProfitLoop, type PortfolioCandles, type ProfitLoopOverview } from "../api/research";
import type { Position } from "../types";
import LoadingState from "./LoadingState";
import { formatGmt8Time } from "../utils/time";
import { fetchOwnerDailySummary, type OwnerDailySummaryResponse } from "../api/modelStrategy";

type Focus = "returns" | "rebalance" | "models";

type OwnerNotice = { level: "block" | "degraded" | "observe"; title: string; detail: string };

const MODULE_LABELS: Record<string, string> = {
  periodic_performance_review: "收益复盘",
  official_policy_documents: "政策资料",
  research_data_assets: "研究数据",
  autonomous_self_healing: "自动修复",
  simulation_ledger_audit: "账户账本",
  hard_data_gate: "长期数据",
  decision_data_gate: "决策数据",
  sealed_holdout_data_gate: "密封验证集",
};

function ownerNotice(module: string, message: string): OwnerNotice {
  const label = MODULE_LABELS[module] || "系统模块";
  const normalized = message.toLowerCase();
  if (module === "sealed_holdout_data_gate") {
    return { level: "observe", title: "密封验证集补全中", detail: "未补齐前不会运行保留集验证，无需人工操作" };
  }
  if (module === "decision_data_gate") {
    return { level: "degraded", title: "今日决策数据未就绪", detail: "正式计划与策略实验已自动暂停，数据刷新后复核" };
  }
  if (module === "autonomous_self_healing" && (normalized.includes("timeout") || normalized.includes("timed_out"))) {
    return { level: "degraded", title: "自动修复本轮超时", detail: "本轮已停止等待，下个周期继续检查且不会重复并发" };
  }
  if (module === "simulation_ledger_audit" || normalized.includes("failed") || normalized.includes("blocked")) {
    return { level: "block", title: `${label}阻断`, detail: "系统已停止相关自动动作，需要检查后再恢复" };
  }
  if (normalized.includes("degraded")) {
    return { level: "degraded", title: `${label}已降级`, detail: "保护措施已生效，核心安全门仍保持" };
  }
  if (normalized.includes("attention")) {
    return { level: "observe", title: `${label}待观察`, detail: "已形成改进信号，不要求立即操作" };
  }
  if (normalized.includes("partial")) {
    return { level: "observe", title: `${label}部分可用`, detail: "不影响模拟交易主链，系统将继续补充" };
  }
  return { level: "observe", title: `${label}待观察`, detail: message || "系统将继续自动检查" };
}

function governanceStatusText(status: string | null | undefined) {
  return {
    audited_no_actionable_hard_gap: "已审计，无需修复",
    ready: "就绪",
    blocked: "已阻断",
    degraded: "降级运行",
  }[String(status || "")] || "待检查";
}

function money(value: number | null | undefined) {
  return Number.isFinite(Number(value)) ? `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}` : "—";
}

function pct(value: number | null | undefined, alreadyPercent = false) {
  if (!Number.isFinite(Number(value))) return "—";
  return `${(Number(value) * (alreadyPercent ? 1 : 100)).toFixed(2)}%`;
}

export default function InvestorDashboardPage({ focus }: { focus: Focus }) {
  const [loop, setLoop] = useState<ProfitLoopOverview | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [candles, setCandles] = useState<PortfolioCandles | null>(null);
  const [ownerSummary, setOwnerSummary] = useState<OwnerDailySummaryResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    Promise.all([fetchProfitLoop(), fetchAccount(), fetchPositions(), fetchPortfolioCandles(90), fetchOwnerDailySummary()])
      .then(([nextLoop, nextAccount, nextPositions, nextCandles, nextOwnerSummary]) => {
        if (!active) return;
        setLoop(nextLoop); setAccount(nextAccount); setPositions(nextPositions.positions ?? []); setCandles(nextCandles); setOwnerSummary(nextOwnerSummary);
      })
      .catch(() => active && setError("投资数据暂时无法读取，请稍后重试"));
    return () => { active = false; };
  }, []);

  const contributors = useMemo(() => [...positions].sort((a, b) => Math.abs(Number(b.profit_loss || 0)) - Math.abs(Number(a.profit_loss || 0))).slice(0, 5), [positions]);
  if (error) return <div className="investor-empty">{error}</div>;
  if (!loop || !account) return <LoadingState variant="page" title="正在汇总投资账户" detail="读取收益、调仓与模型贡献" rows={3} />;

  const titles = { returns: ["我的收益", "先看账户发生了什么"], rebalance: ["调仓方案", "今天要不要动，以及为什么"], models: ["模型贡献", "哪些模型正在产生有效证据"] } as const;
  const proposal = loop.portfolio_optimization.rebalance_proposal ?? [];
  const actions = proposal.filter((item) => ["buy", "sell", "reduce"].includes(item.action));
  const orderedProposal = [...proposal].sort((a, b) => {
    const order = { sell: 0, buy: 1, reduce: 2, observe: 3, hold: 4 };
    return order[a.action] - order[b.action] || Number(b.estimated_trade_amount || 0) - Number(a.estimated_trade_amount || 0);
  });
  const comparison = loop.portfolio_optimization.portfolio_comparison ?? (() => {
    const currentExpected = proposal.reduce((sum, item) => sum + Number(item.expected_return_contribution_before || 0), 0);
    const targetExpected = proposal.reduce((sum, item) => sum + Number(item.expected_return_contribution_after || 0), 0);
    const estimatedCost = proposal.reduce((sum, item) => sum + Number(item.estimated_one_way_cost || 0), 0);
    const currentWeight = proposal.reduce((sum, item) => sum + Number(item.current_weight || 0), 0);
    const targetWeight = proposal.reduce((sum, item) => sum + Number(item.target_weight || 0), 0);
    return {
      current: { expected_20d_return: currentExpected, cash_weight: Math.max(0, 1 - currentWeight), max_position_weight: Math.max(0, ...proposal.map((item) => Number(item.current_weight || 0))) },
      target: { expected_20d_return: targetExpected, cash_weight: Math.max(0, 1 - targetWeight), max_position_weight: Math.max(0, ...proposal.map((item) => Number(item.target_weight || 0))) },
      expected_improvement: targetExpected - currentExpected,
      estimated_one_way_cost: estimatedCost,
      net_expected_improvement: targetExpected - currentExpected - estimatedCost,
      turnover: proposal.reduce((sum, item) => sum + Math.abs(Number(item.weight_change || 0)), 0),
      method: "依据当前方案权重与预期收益即时计算；后端服务重启后使用正式组合对比契约。",
    };
  })();
  const checks = loop.portfolio_optimization.execution_checks ?? [];
  const isBlocked = checks.some((item) => item.status === "block");
  const planAuthorized = loop.rebalance_plan.execution_authorized ?? !isBlocked;
  const tradeAmount = actions.reduce((sum, item) => sum + Number(item.estimated_trade_amount ?? Math.abs(Number(item.weight_change || 0)) * Number(account.assets || 0)), 0);
  const actionSummary = [
    ["新建/加仓", proposal.filter((item) => item.action === "buy").length],
    ["清仓", proposal.filter((item) => item.action === "sell").length],
    ["减仓", proposal.filter((item) => item.action === "reduce").length],
  ].filter(([, count]) => Number(count) > 0).map(([label, count]) => `${label} ${count}`).join(" · ");
  const modelItems = loop.model_return_contribution?.items ?? [];
  const autonomy = ownerSummary?.summary?.questions;
  const anomalies = autonomy?.anomalies ?? [];
  const supervision = autonomy?.incident_supervision;
  const supervisedCases = (supervision?.cases ?? [])
    .filter((item) => item.module !== "autonomous_self_healing")
    .map((item) => ({
      ...item,
      resolution_class: item.resolution_class || (["decision_data_gate", "periodic_performance_review", "official_policy_documents", "llm_data_operations"].includes(item.module) ? "waiting_condition" : "internal_fault"),
    }));
  const internalFaultCount = supervisedCases.filter((item) => item.resolution_class === "internal_fault").length;
  const waitingConditionCount = supervisedCases.filter((item) => item.resolution_class === "waiting_condition").length;
  const ownerCases = supervisedCases.filter((item) => item.owner_action_required || item.owner === "owner");
  const notices = (supervision
    ? ownerCases.map((item) => ({ module: item.module, message: item.summary || item.reason }))
    : anomalies).map((item) => ownerNotice(item.module, item.message));
  const blockingNotices = notices.filter((item) => item.level === "block");
  const degradedNotices = notices.filter((item) => item.level === "degraded");
  const observingNotices = notices.filter((item) => item.level === "observe");
  const executionGate = autonomy?.system_changes.champion_execution_gate;
  const isResearchOnly = executionGate?.status === "quarantined" || executionGate?.mode === "research_only_sell_reduce";
  const executableActions = actions.filter((item) => !isResearchOnly || ["sell", "reduce"].includes(item.action));
  const ownerProposalCount = planAuthorized ? executableActions.length : 0;
  const systemState = blockingNotices.length
    ? { tone: "blocked", title: `${blockingNotices.length} 项阻断`, detail: "自动动作已暂停，需下钻检查" }
    : degradedNotices.length
      ? { tone: "degraded", title: "保护运行中", detail: `${degradedNotices.length} 项降级已由系统接管` }
      : supervision?.status === "self_managed"
        ? { tone: "healthy", title: "系统正在自行处理", detail: `内部故障 ${internalFaultCount} 项 · 等待条件 ${waitingConditionCount} 项，无需你操作` }
        : { tone: "healthy", title: "运行正常", detail: observingNotices.length ? `${observingNotices.length} 项观察，不需要立即处理` : "没有需要处理的异常" };
  const benchmarkComparison = candles?.latest_comparison;
  const selectedMethod = loop.portfolio_optimization.optimizer.selected_method
    ?? loop.portfolio_optimization.optimizer.method_competition?.selected_method
    ?? "等待比较";
  const selectedMethodLabel: Record<string, string> = {
    hold_current: "保持当前持仓",
    forced_risk_exit: "强制风险退出",
    empirical_cvar: "经验 CVaR",
    equal_weight: "等权",
    hrp: "HRP",
  };

  return (
    <main className="investor-page">
      <header className="investor-heading"><div><span>{titles[focus][1]}</span><h2>{titles[focus][0]}</h2></div><time>更新于 {loop.generated_at?.slice(0, 16).replace("T", " ")}</time></header>

      {focus === "returns" && <>
        <section className={`owner-autonomy-strip ${ownerSummary?.available ? systemState.tone : "waiting"}`}>
          <div><span>系统状态</span><strong>{ownerSummary?.available ? systemState.title : "等待今日闭环"}</strong><small>{ownerSummary?.available ? systemState.detail : ownerSummary?.message || "每日自动完成数据、模型、策略、组合与复盘"}{ownerSummary?.finished_at ? ` · 最近完整周期 ${formatGmt8Time(ownerSummary.finished_at)}` : ""}{loop.portfolio_optimization.decision_data_gate && !loop.portfolio_optimization.decision_data_gate.decision_ready ? ` · 实时决策门阻断 ${loop.portfolio_optimization.decision_data_gate.target_trade_date}` : ""}</small></div>
          <div className={!planAuthorized || isResearchOnly ? "execution-quarantined" : ""}><span>执行权限</span><strong>{!planAuthorized ? "禁止执行" : isResearchOnly ? "仅减仓 / 卖出" : "正常模拟执行"}</strong><small>{!planAuthorized ? `最新决策数据未就绪，今日 0 项可执行动作；系统刷新后自动复核` : isResearchOnly ? `今日 ${ownerProposalCount} 项可执行动作；Champion 正式 OOS 未通过，禁止新开仓和加仓` : `今日 ${ownerProposalCount} 项可执行动作`}</small></div>
          <div><span>系统变化</span><strong>{(autonomy?.system_changes.model_auto_promoted ?? 0) + (autonomy?.system_changes.model_training_started ?? 0) + (autonomy?.system_changes.strategy_candidates_created ?? 0) + (autonomy?.system_changes.corporate_actions_applied ?? 0) + (autonomy?.system_changes.model_degraded_count ?? 0)} 项</strong><small>模型 {autonomy?.system_changes.model_degraded_count ? `降级 ${autonomy.system_changes.model_degraded_count}` : "健康"} · 模型晋升 {autonomy?.system_changes.model_auto_promoted ?? 0} · 策略候选 {autonomy?.system_changes.strategy_candidates_created ?? 0} · 复盘候选 {autonomy?.system_changes.strategy_learning_candidate_count ?? 0} · 样本重评 {autonomy?.system_changes.strategy_candidates_re_evaluated ?? 0} · 复盘信号 {autonomy?.system_changes.performance_learning_signal_count ?? 0} · 数据治理 {governanceStatusText(autonomy?.system_changes.data_quality_governance_status)} · 分红送股 {autonomy?.system_changes.corporate_actions_applied ?? 0} · 账本 {autonomy?.system_changes.ledger_status === "passed" ? "一致" : autonomy?.system_changes.ledger_status === "failed" ? `漂移 ${autonomy?.system_changes.ledger_position_drift_count ?? 0} 项` : "待审计"} · 组合 {(autonomy?.system_changes.portfolio_method || "待选择").toUpperCase()} · 成本 {autonomy?.system_changes.execution_cost_version || "待校准"}</small></div>
          {notices.length > 0 && <div className="owner-notice-list">{notices.slice(0, 4).map((item, index) => <article className={`notice-${item.level}`} key={`${item.title}-${index}`}><b>{item.level === "block" ? "阻断" : item.level === "degraded" ? "降级" : "观察"}</b><div><strong>{item.title}</strong><small>{item.detail}</small></div></article>)}</div>}
        </section>
        <section className="investor-hero">
          <article><span>总资产</span><strong>{money(account.assets)}</strong><small>现金 {money(account.balance)} · 持仓 {money(account.position_value)}</small></article>
          <article><span>今日收益</span><strong className={loop.attribution.latest_daily_return_pct >= 0 ? "gain" : "loss"}>{pct(loop.attribution.latest_daily_return_pct, true)}</strong><small>账户日收益率</small></article>
          <article><span>本月收益</span><strong className={loop.attribution.latest_monthly_return_pct >= 0 ? "gain" : "loss"}>{pct(loop.attribution.latest_monthly_return_pct, true)}</strong><small>已实现与持仓共同变化</small></article>
          <article><span>同期沪深300</span><strong className={(benchmarkComparison?.benchmark_return_pct ?? 0) >= 0 ? "gain" : "loss"}>{benchmarkComparison?.benchmark_return_pct == null ? "—" : pct(benchmarkComparison.benchmark_return_pct, true)}</strong><small>账户累计超额 {benchmarkComparison?.excess_return_pct == null ? "—" : pct(benchmarkComparison.excess_return_pct, true)}</small></article>
        </section>
        <PortfolioCandleChart data={candles?.series ?? []} />
        <section className="investor-panel"><header><div><span>持仓影响</span><h3>谁在影响我的收益</h3></div><em>{positions.length} 个持仓</em></header><div className="holding-contributors">{contributors.map((item) => <article key={item.symbol}><div><strong>{item.name || item.symbol}</strong><span>{item.symbol}</span></div><b className={Number(item.profit_loss) >= 0 ? "gain" : "loss"}>{money(item.profit_loss)}</b><small>{pct(Number(item.profit_loss_pct || 0), true)}</small></article>)}</div></section>
      </>}

      {focus === "rebalance" && <>
        <section className={`rebalance-decision ${isBlocked ? "blocked" : actions.length ? "active" : "quiet"}`}>
          <div><span>今日结论</span><strong>{isBlocked ? "禁止执行" : actions.length ? "建议调仓" : "今日保持不动"}</strong><small>{isBlocked ? "系统已安全阻断；数据门恢复后会自动重算，无需人工进入模块" : actionSummary || "目标权重变化不足以覆盖成本"}</small></div>
          <dl><div><dt>涉及资产</dt><dd>{actions.length} 项</dd></div><div><dt>预计交易额</dt><dd>{money(tradeAmount)}</dd></div><div><dt>净预期改善</dt><dd className={comparison.net_expected_improvement >= 0 ? "gain" : "loss"}>{pct(comparison.net_expected_improvement)}</dd></div></dl>
        </section>
        <section className={`plan-identity ${loop.rebalance_plan.is_executed ? "executed" : planAuthorized ? "planned" : "blocked"}`}><div><span>方案版本</span><strong>{loop.rebalance_plan.plan_version}</strong></div><div><span>数据截止</span><strong>{loop.rebalance_plan.data_as_of_date}</strong></div><div><span>计划执行时间</span><strong>{planAuthorized && loop.rebalance_plan.planned_execute_at ? formatGmt8Time(loop.rebalance_plan.planned_execute_at) : "未授权执行"}</strong></div><div><span>实际执行时间</span><strong>{loop.rebalance_plan.executed_at ? formatGmt8Time(loop.rebalance_plan.executed_at) : "尚未执行"}</strong></div><div><span>状态</span><strong>{loop.rebalance_plan.is_executed ? "已执行" : planAuthorized ? "等待执行" : loop.rebalance_plan.status === "blocked_by_decision_data" ? "数据门阻断" : loop.rebalance_plan.status === "blocked_by_risk" ? "风险门阻断" : loop.rebalance_plan.status === "blocked_by_ledger" ? "账本阻断" : loop.rebalance_plan.status === "blocked_by_optimizer" ? "优化器阻断" : "执行检查阻断"}</strong></div><small>{planAuthorized ? `${loop.rebalance_plan.execution_policy}；打开页面不会重复调仓。` : loop.rebalance_plan.blocked_reason || "当前方案不可执行，系统将按真实阻断原因自动复核。"}</small></section>
        <section className="investor-panel rebalance-comparison"><header><div><span>不调仓基线 → 调仓后</span><h3>{planAuthorized ? "组合会发生什么变化" : "不可执行方案预览"}</h3></div><em>自动选择 {selectedMethodLabel[selectedMethod] || selectedMethod}</em></header><div className="comparison-grid"><div><span>预期 20 日</span><strong>{pct(comparison.current.expected_20d_return)} → {pct(comparison.target.expected_20d_return)}</strong></div><div><span>现金仓位</span><strong>{pct(comparison.current.cash_weight)} → {pct(comparison.target.cash_weight)}</strong></div><div><span>最大单仓</span><strong>{pct(comparison.current.max_position_weight)} → {pct(comparison.target.max_position_weight)}</strong></div><div><span>换手率</span><strong>{pct(comparison.turnover)}</strong></div><div><span>预计成本</span><strong>{pct(comparison.estimated_one_way_cost)}</strong></div><div><span>扣成本改善</span><strong className={comparison.net_expected_improvement >= 0 ? "gain" : "loss"}>{pct(comparison.net_expected_improvement)}</strong></div></div><small>{comparison.method}；执行成本版本 {loop.portfolio_optimization.execution_cost_contract?.version || "builtin"}（样本 {loop.portfolio_optimization.execution_cost_contract?.sample_count ?? 0}）</small></section>
        <section className="execution-checks">{checks.map((item) => <article key={item.key} className={`check-${item.status}`}><b>{item.status === "pass" ? "通过" : item.status === "warn" ? "提醒" : "阻断"}</b><div><strong>{item.label}</strong><small>{item.detail}</small></div></article>)}</section>
        <section className="candidate-pipeline"><div><span>观察池</span><strong>{loop.portfolio_optimization.candidate_universe.watch_count} 只</strong></div><div><span>候选池</span><strong>{loop.portfolio_optimization.candidate_universe.candidate_count} 只</strong></div><div><span>核心池</span><strong>{loop.portfolio_optimization.candidate_universe.core_count} 只</strong></div><p>{loop.portfolio_optimization.candidate_universe.admission_policy}</p></section>
        <section className="investor-panel"><header><div><span>{planAuthorized ? "按执行优先级排序" : "仅供系统复核"}</span><h3>{planAuthorized ? "逐项调仓清单" : "不可执行预览清单"}</h3></div><em>{planAuthorized ? actionSummary || "无交易动作" : "不会生成订单"}</em></header><div className="rebalance-list">{orderedProposal.map((item) => { const actionLabel = item.action === "buy" ? (item.current_weight <= .002 ? "新建仓" : "加仓") : item.action === "sell" ? "清仓" : item.action === "reduce" ? "减仓" : item.action === "observe" ? "观察" : "持有"; return <article key={item.symbol}><b className={`action-${item.action}`}>{planAuthorized ? actionLabel : "预览"}</b><div className="rebalance-security"><strong>{item.name || "名称待补"}</strong><i>{item.symbol}{item.candidate_source === "core_pool" ? " · 核心池" : ""}</i></div><span>{pct(item.current_weight)} → {pct(item.target_weight)} <i>{item.weight_change >= 0 ? "+" : ""}{pct(item.weight_change)}</i></span><div className="trade-amount"><small>{money(item.current_amount)} → {money(item.target_amount)}</small><strong>{planAuthorized ? item.action === "hold" || item.action === "observe" ? "不交易" : `${item.weight_change > 0 ? "买入" : "卖出"} ${money(item.estimated_trade_amount)}` : "禁止下单"}</strong></div><div className="rebalance-detail"><small>{item.reason}</small><em>预期20日 {pct(item.expected_20d_return)} · 风险贡献 {pct(item.cvar_contribution_after)} · 成本 {pct(item.estimated_one_way_cost)}</em>{item.candidate_lineage && <i className="candidate-lineage">{item.candidate_lineage.path.join(" → ")}{item.candidate_lineage.source_reason ? ` · ${item.candidate_lineage.source_reason}` : ""}</i>}</div></article>; })}</div></section>
      </>}

      {focus === "models" && <section className="investor-panel model-contribution-panel"><header><div><span>生产模型</span><h3>收益贡献与有效性</h3></div><em>{modelItems.length} 个模型</em></header><div className="model-contribution-list">{modelItems.map((item) => { const evidence = item.preferred_20d_evidence; return <article key={item.model_key}><div><strong>{item.name}</strong><span>{item.model_version || "等待版本"}</span></div><b>{item.portfolio_return_contribution_pct == null ? "观察中" : pct(item.portfolio_return_contribution_pct, true)}</b><dl><div><dt>20日样本</dt><dd>{evidence?.samples ?? 0}</dd></div><div><dt>平均超额</dt><dd>{evidence ? pct(evidence.avg_excess_return_pct, true) : "—"}</dd></div><div><dt>命中率</dt><dd>{evidence ? pct(evidence.hit_rate) : "—"}</dd></div></dl><small>{item.portfolio_return_contribution_pct == null ? "影子组合样本积累中" : "已形成可验证账户贡献"}</small></article>; })}</div></section>}
    </main>
  );
}

function PortfolioCandleChart({ data }: { data: PortfolioCandles["series"] }) {
  const visible = data.slice(-60);
  if (!visible.length) return <section className="investor-panel portfolio-kline"><header><div><span>整仓走势</span><h3>组合 K 线</h3></div></header><div className="investor-empty">组合历史行情不足</div></section>;
  const width = 1000, height = 280, pad = 24;
  const benchmarkValues = visible.map(x => x.benchmark_close).filter((value): value is number => value != null);
  const min = Math.min(...visible.map(x => x.low), ...benchmarkValues), max = Math.max(...visible.map(x => x.high), ...benchmarkValues);
  const range = max - min || 1, step = (width - pad * 2) / visible.length;
  const y = (value: number) => pad + (max - value) / range * (height - pad * 2);
  const latest = visible[visible.length - 1];
  const benchmarkPoints = visible.map((item,index) => item.benchmark_close == null ? null : `${pad+index*step+step/2},${y(item.benchmark_close)}`).filter(Boolean).join(" ");
  return <section className="investor-panel portfolio-kline"><header><div><span>自首笔模拟成交起 · {visible.length} 个交易日</span><h3>整仓 K 线</h3></div><em className={latest.change_pct >= 0 ? "gain" : "loss"}>{latest.close.toFixed(2)} · {latest.change_pct >= 0 ? "+" : ""}{latest.change_pct.toFixed(2)}%</em></header><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="整仓组合与沪深300对比图">{[0,.25,.5,.75,1].map(v => <line key={v} x1={pad} x2={width-pad} y1={pad+v*(height-pad*2)} y2={pad+v*(height-pad*2)} className="kline-grid" />)}{visible.map((item,index) => {const x=pad+index*step+step/2, up=item.close>=item.open; return <g key={item.date}><line x1={x} x2={x} y1={y(item.high)} y2={y(item.low)} className={up?"candle-up":"candle-down"}/><rect x={x-Math.max(1.5,step*.28)} width={Math.max(3,step*.56)} y={Math.min(y(item.open),y(item.close))} height={Math.max(1.5,Math.abs(y(item.open)-y(item.close)))} className={up?"candle-up":"candle-down"}/></g>})}{benchmarkPoints && <polyline points={benchmarkPoints} className="benchmark-line" />}</svg><footer><span>{visible[0].date}</span><small>模拟账本与现金重放 · 首日 100　<span className="benchmark-legend">— 沪深300</span></small><span>{latest.date}</span></footer></section>;
}
