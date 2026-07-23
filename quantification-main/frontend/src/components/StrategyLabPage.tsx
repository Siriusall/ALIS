import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  fetchEvolutionTasks,
  fetchStrategyLab,
  type EvolutionSchedule,
  type EvolutionTask,
  type StrategyLabResponse,
} from "../api/modelStrategy";
import LoadingState from "./LoadingState";

const gateLabels: Record<string, string> = {
  minimum_required_windows: "正式窗口数量",
  all_four_market_states_covered: "四类市场状态覆盖",
  point_in_time_all_passed: "点时数据检查",
  same_champion_all_windows: "统一 Champion 参照",
  complete_test_data_all_windows: "测试数据完整",
  majority_windows_outperform: "多数窗口跑赢基准",
  aggregate_cost_after_excess_positive: "成本后累计超额为正",
  not_worse_than_v1_return: "收益不差于 V1",
  not_worse_than_v1_drawdown: "回撤不差于 V1",
  not_worse_than_v1_excess: "超额不差于 V1",
  minimum_future_sessions: "未来影子交易日",
  shadow_compounded_delta_positive: "影子累计相对收益为正",
  majority_shadow_sessions_outperform: "多数影子交易日占优",
};

const lifecycleLabels: Record<string, string> = {
  pending_oos: "等待快速诊断",
  quick_diagnostic_passed: "快速诊断通过",
  quick_diagnostic_failed: "快速诊断淘汰",
  formal_oos_passed: "正式 OOS 通过",
  formal_oos_failed: "正式 OOS 未通过",
  formal_oos_rejected: "正式 OOS 淘汰",
  shadow_observation: "未来影子观察",
  shadow_rejected: "影子期淘汰",
  promoted_cooling: "已晋升·冷却观察",
  promoted_stable: "已晋升·稳定",
  rolled_back: "已自动回退",
};

const attributionLabels: Record<string, string> = {
  market_exposure: "市场敞口",
  transaction_costs: "交易成本",
  execution_constraints: "执行约束",
  selection_or_allocation_residual: "选股与配置残差",
  unknown: "证据不足",
};

const confidenceLabels: Record<string, string> = {
  high: "高置信",
  medium: "中置信",
  low: "低置信",
};

function percent(value: number | null | undefined, digits = 2) {
  return value == null || !Number.isFinite(value) ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function money(value: number | null | undefined) {
  return value == null || !Number.isFinite(value)
    ? "—"
    : new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", maximumFractionDigits: 2 }).format(value);
}

export default function StrategyLabPage() {
  const [data, setData] = useState<StrategyLabResponse | null>(null);
  const [tasks, setTasks] = useState<EvolutionTask[]>([]);
  const [schedule, setSchedule] = useState<EvolutionSchedule | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const refresh = () => Promise.all([fetchStrategyLab(), fetchEvolutionTasks(10)])
      .then(([lab, taskResult]) => {
        if (!active) return;
        setData(lab);
        setTasks(taskResult.items);
        setSchedule(taskResult.schedule);
        const hasActive = taskResult.items.some((item) => ["queued", "running", "retry_wait"].includes(item.status));
        if (hasActive) timer = window.setTimeout(refresh, 5000);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "策略数据读取失败"))
      .finally(() => setLoading(false));
    refresh();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  if (loading) {
    return <LoadingState variant="page" title="正在打开策略实验室" detail="正在读取 Champion 与 Challenger 的正式验证数据" rows={5} />;
  }
  if (error || !data) {
    return <section className="strategy-lab-page"><div className="alert">{error || "暂无策略数据"}</div></section>;
  }

  const challenger = data.challengers.find((item) => item.formal_oos.valid_windows >= 5) ?? data.challengers[0];
  if (!challenger) {
    return <section className="strategy-lab-page"><div className="strategy-lab-empty">尚未登记 Challenger。</div></section>;
  }
  const oos = challenger.formal_oos;
  const attribution = oos.failure_attribution ?? {
    status: "pending",
    primary_cause: "unknown",
    confidence: "low",
    evidence: {},
    limitations: [],
  };
  const attributionEvidence = attribution.evidence;
  const chartData = oos.windows.map((item) => ({ ...item, label: `窗口 ${item.window}`, percent: (item.return ?? 0) * 100 }));
  const latestTask = tasks[0];
  const comparisons = [challenger, ...data.baselines].map((item) => ({
    name: item.role === "challenger" ? "Challenger V1" : item.name,
    role: item.role,
    return: item.formal_oos.aggregate_cost_after_return,
    benchmark: item.formal_oos.aggregate_benchmark_return,
    excess: item.formal_oos.aggregate_excess_return,
    drawdown: item.formal_oos.worst_window_drawdown,
    wins: `${item.formal_oos.outperform_windows}/${item.formal_oos.valid_windows}`,
  }));

  return (
    <section className="strategy-lab-page">
      <header className="strategy-lab-head">
        <div>
          <span>STRATEGY LAB</span>
          <h1>策略实验室</h1>
          <p>查看候选策略如何改变买卖纪律，以及它在正式样本外窗口中的真实表现。</p>
        </div>
        <div className={`strategy-lab-verdict ${challenger.production_eligible ? "pass" : "fail"}`}>
          <small>当前结论</small>
          <strong>{challenger.production_eligible ? "具备生产资格" : "暂不替换 Champion"}</strong>
          <span>确定性硬门自动晋升 · 异常自动回退</span>
        </div>
      </header>

      <div className="strategy-lab-lineage">
        <article>
          <span>当前正式策略 · Champion</span>
          <strong>{data.champion.name}</strong>
          <small>策略 #{data.champion.strategy_id} · 唯一激活参照</small>
        </article>
        <i aria-hidden="true">→</i>
        <article className="challenger">
          <span>候选策略 · Challenger</span>
          <strong>{challenger.name}</strong>
          <small>策略 #{challenger.strategy_id} · 正式 OOS 已完成</small>
        </article>
      </div>

      <article className="strategy-lab-task">
        <div>
          <span>后台自进化</span>
          <strong>{latestTask ? latestTask.message : "等待每周自动验证周期"}</strong>
          <small>
            {latestTask
              ? `任务 #${latestTask.id} · ${latestTask.status} · 第 ${latestTask.attempt}/${latestTask.max_attempts} 次尝试`
              : schedule?.next_formal_cycle_at
                ? `下次正式周期 ${new Date(schedule.next_formal_cycle_at).toLocaleString("zh-CN")}`
                : "任务持久化后可在后端重启时从窗口检查点恢复"}
          </small>
        </div>
        <div>
          <b>{latestTask ? percent(latestTask.progress, 0) : "空闲"}</b>
          <span><i style={{ width: `${Math.max(0, Math.min(100, (latestTask?.progress ?? 0) * 100))}%` }} /></span>
          <small>
            {latestTask?.estimated_remaining_seconds
              ? `预计剩余 ${Math.max(1, Math.ceil(latestTask.estimated_remaining_seconds / 60))} 分钟`
              : latestTask?.status === "completed"
                ? schedule?.next_formal_cycle_at
                  ? `下次 ${new Date(schedule.next_formal_cycle_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`
                  : "最近周期已完成"
                : "等待下一次计划"}
          </small>
        </div>
      </article>

      <article className="strategy-lab-card strategy-lab-evolution">
        <header>
          <div><span>自治候选流水线</span><h2>生成 → 快速诊断 → 正式 OOS → 影子期 → 晋升/回退</h2></div>
          <small>只影响研究与模拟交易</small>
        </header>
        <div className="strategy-lab-evolution-list">
          {data.challengers.map((item) => (
            <div key={item.model_version_id}>
              <div>
                <strong>{item.name}</strong>
                <small>策略 #{item.strategy_id} · 版本 #{item.model_version_id}</small>
              </div>
              <b className={item.status.includes("failed") || item.status.includes("rejected") || item.status === "rolled_back" ? "negative" : ""}>
                {lifecycleLabels[item.status] || item.status}
              </b>
              <span>
                {item.status === "shadow_observation" || item.status === "promoted_cooling"
                  ? `影子 ${item.shadow.session_count}/${item.shadow.minimum_sessions} 日`
                  : item.formal_oos.valid_windows
                    ? `正式窗口 ${item.formal_oos.valid_windows}/5`
                    : item.blocking_reasons.length
                      ? `阻塞 ${item.blocking_reasons.length} 项`
                      : "等待后台硬门"}
              </span>
            </div>
          ))}
        </div>
        {data.promotion_events.length > 0 && (
          <div className="strategy-lab-events">
            <strong>最近版本事件</strong>
            {data.promotion_events.slice(0, 3).map((event) => (
              <small key={event.id}>
                {event.event_type === "promoted" ? "自动晋升" : "自动回退"} ·
                Champion #{event.from_champion_version_id ?? "—"} → #{event.to_champion_version_id ?? "—"}
              </small>
            ))}
          </div>
        )}
      </article>

      <div className="strategy-lab-metrics">
        <article><span>成本后累计收益</span><strong className={(oos.aggregate_cost_after_return ?? 0) >= 0 ? "positive" : "negative"}>{percent(oos.aggregate_cost_after_return)}</strong><small>5 个正式窗口复合</small></article>
        <article><span>沪深300同期</span><strong>{percent(oos.aggregate_benchmark_return)}</strong><small>点时基准</small></article>
        <article><span>累计超额</span><strong className={(oos.aggregate_excess_return ?? 0) >= 0 ? "positive" : "negative"}>{percent(oos.aggregate_excess_return)}</strong><small>策略收益减基准</small></article>
        <article><span>最差窗口回撤</span><strong className="negative">{percent(oos.worst_window_drawdown)}</strong><small>风险控制结果</small></article>
        <article><span>跑赢窗口</span><strong>{oos.outperform_windows}/{oos.valid_windows}</strong><small>{percent(oos.outperform_ratio, 0)}</small></article>
        <article><span>总交易费用</span><strong>{money(oos.total_fees)}</strong><small>正式 OOS 成本</small></article>
      </div>

      <div className="strategy-lab-grid">
        <article className="strategy-lab-card strategy-lab-comparison">
          <header><div><span>横向基线</span><h2>排序与组合方式对比</h2></div><small>同一最近 1,537 日 · 固定五窗口</small></header>
          <div className="strategy-lab-comparison-table">
            <table>
              <thead><tr><th>策略</th><th>成本后收益</th><th>沪深300</th><th>累计超额</th><th>最差回撤</th><th>跑赢窗口</th></tr></thead>
              <tbody>
                {comparisons.map((item) => (
                  <tr key={`${item.role}-${item.name}`}>
                    <td><strong>{item.name}</strong><small>{item.role === "baseline" ? "诊断基线" : "候选策略"}</small></td>
                    <td className={(item.return ?? 0) >= 0 ? "positive" : "negative"}>{percent(item.return)}</td>
                    <td>{percent(item.benchmark)}</td>
                    <td className={(item.excess ?? 0) >= 0 ? "positive" : "negative"}>{percent(item.excess)}</td>
                    <td>{percent(item.drawdown)}</td>
                    <td>{item.wins}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>横向比较只呈现同协议的真实结果；下一候选由正式 OOS 的持久化失败归因决定，不根据基准差距猜测原因。</p>
        </article>

        <article className="strategy-lab-card strategy-lab-chart">
          <header><div><span>正式 OOS</span><h2>各窗口成本后收益</h2></div><small>绿色为正收益，红色为负收益</small></header>
          <div className="strategy-lab-chart-body">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 16, right: 16, bottom: 4, left: 2 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e9eef4" />
                <XAxis dataKey="label" tickLine={false} axisLine={false} />
                <YAxis tickFormatter={(value) => `${value}%`} tickLine={false} axisLine={false} width={50} />
                <Tooltip formatter={(value) => [`${Number(value).toFixed(2)}%`, "成本后收益"]} />
                <ReferenceLine y={0} stroke="#98a2b3" />
                <Bar dataKey="percent" radius={[5, 5, 0, 0]}>
                  {chartData.map((entry) => <Cell key={entry.window} fill={entry.percent >= 0 ? "#159a70" : "#d84b5b"} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </article>

        <article className="strategy-lab-card">
          <header><div><span>买卖纪律</span><h2>V1 做了什么改变</h2></div></header>
          <dl className="strategy-lab-policy">
            <div><dt>调仓频率</dt><dd>{challenger.policy.rebalance_frequency === "weekly" ? "每周" : challenger.policy.rebalance_frequency}</dd></div>
            <div><dt>单票上限</dt><dd>{percent(challenger.policy.max_single_weight, 0)}</dd></div>
            <div><dt>单次换手上限</dt><dd>{percent(challenger.policy.max_rebalance_turnover, 0)}</dd></div>
            <div><dt>忽略微小调仓</dt><dd>低于 {percent(challenger.policy.min_weight_change, 0)}</dd></div>
            <div><dt>禁止单日追高</dt><dd>涨幅高于 {percent(challenger.policy.max_entry_1d_return, 0)}</dd></div>
            <div><dt>禁止五日追高</dt><dd>涨幅高于 {percent(challenger.policy.max_entry_5d_return, 0)}</dd></div>
          </dl>
        </article>

        <article className="strategy-lab-card strategy-lab-gates">
          <header><div><span>验收门</span><h2>通过与阻塞项</h2></div></header>
          <div>
            {challenger.passed_gates.map((gate) => <p className="pass" key={gate}><i /> <span>{gateLabels[gate] || gate}</span><b>通过</b></p>)}
            {challenger.blocking_reasons.map((gate) => <p className="fail" key={gate}><i /> <span>{gateLabels[gate] || gate}</span><b>未通过</b></p>)}
          </div>
        </article>

        <article className="strategy-lab-card strategy-lab-attribution">
          <header><div><span>失败归因</span><h2>正式 OOS 的可复算证据</h2></div><small>{confidenceLabels[attribution.confidence] || "待验证"}</small></header>
          {attribution.status === "pending" ? (
            <p>等待完成正式 OOS 后生成归因。</p>
          ) : (
            <>
              <strong>{attributionLabels[attribution.primary_cause] || attribution.primary_cause}</strong>
              <dl>
                <div><dt>现金拖累</dt><dd>{percent(typeof attributionEvidence.estimated_cash_drag_return === "number" ? attributionEvidence.estimated_cash_drag_return : null)}</dd></div>
                <div><dt>费用负担</dt><dd>{percent(typeof attributionEvidence.fee_burden_return === "number" ? attributionEvidence.fee_burden_return : null)}</dd></div>
                <div><dt>被拦截订单</dt><dd>{Number(attributionEvidence.blocked_count ?? 0)}</dd></div>
                <div><dt>未解释缺口</dt><dd>{percent(typeof attributionEvidence.residual_failure_gap_return === "number" ? attributionEvidence.residual_failure_gap_return : null)}</dd></div>
              </dl>
              {attribution.limitations.slice(0, 1).map((item) => <p key={item}>{item}</p>)}
            </>
          )}
        </article>

        <article className="strategy-lab-card strategy-lab-conclusion">
          <header><div><span>怎么看</span><h2>这组数据说明什么</h2></div></header>
          <p>当前 Challenger 的结论来自固定协议、成本后 OOS 和窗口级失败归因；缺少充分证据时系统保持候选隔离。</p>
          <strong>系统已自动生成并淘汰弱 V2 候选；只有正式 OOS 与至少 20 个未来影子交易日同时通过，才会替换唯一 Champion。</strong>
        </article>
      </div>
    </section>
  );
}
