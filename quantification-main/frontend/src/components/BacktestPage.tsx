import { useEffect, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchStrategies } from "../api/strategies";
import {
  deleteBacktest,
  fetchBacktest,
  fetchBacktestEquity,
  fetchBacktestJob,
  fetchBacktestList,
  fetchBacktestTrades,
  runBacktestAsync,
  type BacktestEquityResponse,
  type BacktestJobResponse,
  type BacktestListItem,
  type BacktestRunResponse,
  type BacktestTradesResponse,
} from "../api/backtest";
import type { Strategy } from "../types";
import { chartGridStroke, chartLineBenchmark, chartLinePrimary, chartTooltipStyle } from "../chartTheme";

const JOB_POLL_INTERVAL_MS = 400;

function formatLocalDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function getDefaultBacktestDateRange(): { start: string; end: string } {
  const end = new Date();
  end.setDate(end.getDate() - 1);
  return { start: "2020-01-01", end: formatLocalDate(end) };
}

function asPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(2)}%`;
}

export default function BacktestPage() {
  type ExchangeGroup = "ALL" | "HS" | "CYB" | "BJ";
  const defaultRange = useMemo(() => getDefaultBacktestDateRange(), []);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [history, setHistory] = useState<BacktestListItem[]>([]);
  const [selectedBacktestId, setSelectedBacktestId] = useState<number | null>(null);

  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [startDate, setStartDate] = useState(defaultRange.start);
  const [endDate, setEndDate] = useState(defaultRange.end);
  const [initialCash, setInitialCash] = useState(1_000_000);
  const [freq, setFreq] = useState("daily");
  const [benchmarkSymbol, setBenchmarkSymbol] = useState("000300");
  const [exchangeGroup, setExchangeGroup] = useState<ExchangeGroup>("HS");

  const [summary, setSummary] = useState<BacktestRunResponse | null>(null);
  const [equity, setEquity] = useState<BacktestEquityResponse | null>(null);
  const [trades, setTrades] = useState<BacktestTradesResponse | null>(null);
  const [jobProgress, setJobProgress] = useState<BacktestJobResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState("");
  const pollTimerRef = useRef<number | null>(null);

  useEffect(() => {
    fetchStrategies()
      .then((rows) => {
        setStrategies(rows);
        if (rows.length > 0 && strategyId == null) setStrategyId(rows[0].id);
      })
      .catch(() => setError("策略列表加载失败"));

    fetchBacktestList()
      .then((rows) => setHistory(Array.isArray(rows) ? rows : []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current != null) {
        window.clearTimeout(pollTimerRef.current);
      }
    };
  }, []);

  async function loadBacktestDetail(backtestId: number) {
    setLoadingDetail(true);
    setError("");
    try {
      const s = await fetchBacktest(backtestId);
      setSummary(s);

      const [equityResult, tradesResult] = await Promise.allSettled([
        fetchBacktestEquity(backtestId),
        fetchBacktestTrades(backtestId),
      ]);

      if (equityResult.status === "fulfilled") {
        setEquity(equityResult.value);
      } else {
        setEquity(null);
      }

      if (tradesResult.status === "fulfilled") {
        setTrades(tradesResult.value);
      } else {
        setTrades({ backtest_id: backtestId, trades: [] });
      }

      if (equityResult.status !== "fulfilled" || tradesResult.status !== "fulfilled") {
        setError("回测详情部分加载失败，已展示可用结果");
      }
      setSelectedBacktestId(backtestId);
    } catch {
      setError("回测详情加载失败");
    } finally {
      setLoadingDetail(false);
    }
  }

  async function handleRun() {
    if (strategyId == null) return;
    setRunning(true);
    setError("");
    setJobProgress(null);
    setEquity(null);
    setTrades(null);
    setSummary(null);
    try {
      const res = await runBacktestAsync({
        strategy_id: strategyId,
        start_date: startDate,
        end_date: endDate,
        initial_cash: Number(initialCash) || 1_000_000,
        rebalance_freq: freq,
        benchmark_symbol: benchmarkSymbol || "000300",
        exchange_group: exchangeGroup,
      });
      await pollJobUntilDone(res.job_id);
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg || "回测执行失败");
      setRunning(false);
    }
  }

  async function handleDeleteHistory(backtestId: number) {
    const ok = window.confirm(`确认删除回测 #${backtestId} 吗？`);
    if (!ok) return;
    try {
      await deleteBacktest(backtestId);
      setHistory((prev) => prev.filter((x) => x.id !== backtestId));
      if (selectedBacktestId === backtestId) {
        setSelectedBacktestId(null);
        setSummary(null);
        setEquity(null);
        setTrades(null);
      }
    } catch {
      setError("删除历史回测失败");
    }
  }

  async function pollJobUntilDone(jobId: string) {
    const loop = async () => {
      try {
        const job = await fetchBacktestJob(jobId);
        setJobProgress(job);
        if (job.daily_records.length > 0) {
          setEquity({
            backtest_id: job.backtest_id ?? 0,
            equity_series: job.daily_records.map((r) => ({ date: r.date, total_value: r.total_value })),
            benchmark_series: job.daily_records.map((r) => ({ date: r.date, total_value: r.benchmark_value })),
          });
        }
        if (job.status === "completed" && job.backtest_id) {
          await loadBacktestDetail(job.backtest_id);
          const rows = await fetchBacktestList();
          setHistory(Array.isArray(rows) ? rows : []);
          setRunning(false);
          return;
        }
        if (job.status === "failed") {
          setError(job.error || "回测任务失败");
          setRunning(false);
          return;
        }
        pollTimerRef.current = window.setTimeout(loop, JOB_POLL_INTERVAL_MS);
      } catch {
        setError("回测进度查询失败");
        setRunning(false);
      }
    };
    await loop();
  }

  const progressText = useMemo(() => {
    if (!running || !jobProgress) return "";
    const p = jobProgress.processed_days || 0;
    const t = jobProgress.total_days || 0;
    const phaseTextMap: Record<string, string> = {
      queued: "排队中",
      loading_snapshot: "加载行情快照",
      loading_daily_basic: "加载估值数据",
      calculating_factors: "计算因子中",
      ranking: "筛选与打分中",
      day_completed: "当日计算完成",
      completed: "回测完成",
      failed: "回测失败",
    };
    const phase = (jobProgress.phase || "").trim();
    const phaseText = phaseTextMap[phase] || "运行中";
    if (phase === "calculating_factors") {
      const up = jobProgress.universe_processed || 0;
      const ut = jobProgress.universe_total || 0;
      return `已处理 ${p}/${t || "?"} 交易日，${phaseText} ${up}/${ut || "?"}${jobProgress.last_date ? `（${jobProgress.last_date}）` : ""}`;
    }
    if (jobProgress.last_date) {
      return `已处理 ${p}/${t || "?"} 交易日，${phaseText}（${jobProgress.last_date}）`;
    }
    return `回测任务${phaseText}（已处理 ${p}/${t || "?"}）`;
  }, [running, jobProgress]);

  const chartData = useMemo(() => {
    const eq = equity?.equity_series ?? [];
    const bm = equity?.benchmark_series ?? [];
    if (eq.length === 0) return [];
    const bmMap = new Map(bm.map((x) => [x.date, x.total_value]));
    return eq.map((x) => ({
      date: x.date,
      strategy: x.total_value,
      benchmark: bmMap.get(x.date) ?? null,
    }));
  }, [equity]);

  const dailyRecords = jobProgress?.daily_records ?? [];

  return (
    <section id="backtest" className="dashboard-section" data-section="backtest">
      <h2 className="dashboard-section-title">回测</h2>
      <div className="dashboard-section-inner" style={{ overflowY: "auto" }}>
        <div className="card">
          <h3>新建回测</h3>
          <div style={{ display: "flex", gap: "12px", flexWrap: "wrap", alignItems: "center" }}>
            <label>
              策略：
              <select
                value={strategyId ?? ""}
                onChange={(e) => setStrategyId(e.target.value ? Number(e.target.value) : null)}
                style={{ marginLeft: 6 }}
              >
                {strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              开始：
              <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} style={{ marginLeft: 6 }} />
            </label>
            <label>
              结束：
              <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} style={{ marginLeft: 6 }} />
            </label>
            <label>
              初始资金：
              <input
                type="number"
                min={1000}
                value={initialCash}
                onChange={(e) => setInitialCash(Number(e.target.value) || 1_000_000)}
                style={{ marginLeft: 6, width: 120 }}
              />
            </label>
            <label>
              周期：
              <select value={freq} onChange={(e) => setFreq(e.target.value)} style={{ marginLeft: 6 }}>
                <option value="daily">daily</option>
                <option value="weekly">weekly</option>
                <option value="monthly">monthly</option>
              </select>
            </label>
            <label>
              基准：
              <input
                type="text"
                value={benchmarkSymbol}
                onChange={(e) => setBenchmarkSymbol(e.target.value.trim())}
                style={{ marginLeft: 6, width: 100 }}
                placeholder="000300"
              />
            </label>
            <label>
              交易所：
              <select
                value={exchangeGroup}
                onChange={(e) => setExchangeGroup((e.target.value as ExchangeGroup) || "HS")}
                style={{ marginLeft: 6 }}
              >
                <option value="ALL">全市场</option>
                <option value="HS">沪深（不含创业板）</option>
                <option value="CYB">创业板</option>
                <option value="BJ">北交所</option>
              </select>
            </label>
            <button type="button" onClick={handleRun} disabled={running || strategyId == null}>
              {running ? "回测中…" : "开始回测"}
            </button>
          </div>
          {progressText && <div className="text-progress" style={{ marginTop: 10 }}>{progressText}</div>}
        </div>

        {error && <div className="alert">{error}</div>}

        {summary && (
          <>
          <div className="grid metrics" style={{ marginTop: 8 }}>
            <div className="card metric-card"><div className="sub">总收益</div><div className="metric">{asPct(summary.metrics.total_return)}</div></div>
            <div className="card metric-card"><div className="sub">年化收益</div><div className="metric">{asPct(summary.metrics.annual_return)}</div></div>
            <div className="card metric-card"><div className="sub">最大回撤</div><div className="metric">{asPct(summary.metrics.max_drawdown)}</div></div>
            <div className="card metric-card"><div className="sub">夏普</div><div className="metric">{summary.metrics.sharpe.toFixed(3)}</div></div>
            <div className="card metric-card"><div className="sub">胜率</div><div className="metric">{asPct(summary.metrics.win_rate)}</div></div>
            <div className="card metric-card"><div className="sub">换手率</div><div className="metric">{asPct(summary.metrics.turnover_rate)}</div></div>
            <div className="card metric-card"><div className="sub">基准收益</div><div className="metric">{asPct(summary.metrics.benchmark_return)}</div></div>
            <div className="card metric-card"><div className="sub">超额收益</div><div className="metric">{asPct(summary.metrics.excess_return)}</div></div>
          </div>

          {(summary.warnings?.length > 0 || summary.data_quality || summary.trade_constraints) && (
            <div className={`card backtest-credibility ${summary.data_quality?.used_fallback || (summary.trade_constraints?.blocked_count ?? 0) > 0 ? "backtest-credibility-warn" : ""}`} style={{ marginTop: 8 }}>
              <h3>回测可信度与交易口径</h3>
              {summary.warnings?.length > 0 && (
                <ul className="backtest-warning-list">
                  {summary.warnings.map((w, i) => (
                    <li key={`${i}-${w.slice(0, 24)}`}>{w}</li>
                  ))}
                </ul>
              )}
              <div className="backtest-credibility-grid">
                <div>
                  <div className="sub">数据质量 fallback</div>
                  <strong className={summary.data_quality?.used_fallback ? "decision-cell-stop" : "decision-cell-go"}>
                    {summary.data_quality?.used_fallback ? "是（需核对）" : "否"}
                  </strong>
                  {(summary.data_quality?.fallback_events?.length ?? 0) > 0 && (
                    <div className="backtest-mini-table table-scroll">
                      <table>
                        <thead>
                          <tr><th>类型</th><th>目标日</th><th>回退日</th></tr>
                        </thead>
                        <tbody>
                          {(summary.data_quality?.fallback_events ?? []).slice(0, 8).map((ev, idx) => (
                            <tr key={`fb-${idx}`}>
                              <td>{ev.type}</td>
                              <td>{ev.target_date || "—"}</td>
                              <td>{ev.fallback_date || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
                <div>
                  <div className="sub">交易约束拦截</div>
                  <strong>{summary.trade_constraints?.blocked_count ?? 0} 次</strong>
                  <div className="decision-note">
                    口径：100股整数倍、滑点、佣金/印花税/过户费、ST禁买、涨跌停/停牌拦截
                  </div>
                  {summary.trade_constraints?.policy && (
                    <pre className="backtest-policy-pre">{JSON.stringify(summary.trade_constraints.policy, null, 2)}</pre>
                  )}
                  {(summary.trade_constraints?.blocked_events?.length ?? 0) > 0 && (
                    <div className="backtest-mini-table table-scroll">
                      <table>
                        <thead>
                          <tr><th>日期</th><th>代码</th><th>方向</th><th>原因</th></tr>
                        </thead>
                        <tbody>
                          {(summary.trade_constraints?.blocked_events ?? []).slice(0, 10).map((ev, idx) => (
                            <tr key={`blk-${idx}`}>
                              <td>{ev.date}</td>
                              <td>{ev.symbol}</td>
                              <td>{ev.side}</td>
                              <td>{ev.message || ev.code}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
          </>
        )}

        <div className="card" style={{ marginTop: 8 }}>
          <h3>净值曲线（策略 vs 基准）</h3>
          {loadingDetail ? (
            <div className="empty">加载中…</div>
          ) : chartData.length === 0 ? (
            <div className="empty">暂无回测曲线，请先执行一次回测</div>
          ) : (
            <div className="chart-wrap" style={{ height: 280 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chartGridStroke} />
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} width={48} domain={["dataMin", "dataMax"]} />
                  <Tooltip
                    formatter={(value: number | undefined) => [value != null ? value.toFixed(2) : "", "净值"]}
                    contentStyle={chartTooltipStyle}
                  />
                  <Legend />
                  <Line
                    type="monotone"
                    dataKey="strategy"
                    name="策略净值"
                    stroke={chartLinePrimary}
                    dot={false}
                    strokeWidth={2}
                    isAnimationActive={false}
                    animationDuration={0}
                  />
                  <Line
                    type="monotone"
                    dataKey="benchmark"
                    name="基准净值"
                    stroke={chartLineBenchmark}
                    dot={false}
                    strokeWidth={2}
                    isAnimationActive={false}
                    animationDuration={0}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 12, marginTop: 8 }}>
          <div className="card" style={{ minHeight: 280 }}>
            <h3>历史回测</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>策略</th>
                    <th>区间</th>
                    <th style={{ width: 84, whiteSpace: "nowrap" }}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((h) => (
                    <tr
                      key={h.id}
                      role="button"
                      tabIndex={0}
                      className={selectedBacktestId === h.id ? "row-selected" : undefined}
                      style={{ cursor: "pointer" }}
                      onClick={() => loadBacktestDetail(h.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") loadBacktestDetail(h.id);
                      }}
                    >
                      <td>#{h.id}</td>
                      <td>{h.strategy_name}</td>
                      <td>{h.start_date} ~ {h.end_date}</td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <button
                          type="button"
                          style={{ minWidth: 56, whiteSpace: "nowrap" }}
                          onClick={(e) => {
                            e.stopPropagation();
                            void handleDeleteHistory(h.id);
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {history.length === 0 && <div className="empty">暂无历史回测</div>}
            </div>
          </div>

          <div className="card" style={{ minHeight: 280 }}>
            <h3>交易明细</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>代码</th>
                    <th>方向</th>
                    <th>价格</th>
                    <th>数量</th>
                    <th>金额</th>
                    <th>费用</th>
                    <th>盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {(trades?.trades ?? []).map((t, i) => (
                    <tr key={`${t.date}-${t.symbol}-${t.side}-${i}`}>
                      <td>{t.date}</td>
                      <td>{t.symbol}</td>
                      <td>{t.side}</td>
                      <td>{Number(t.price).toFixed(2)}</td>
                      <td>{t.quantity}</td>
                      <td>{Number(t.gross_amount).toFixed(2)}</td>
                      <td>{Number(t.fees).toFixed(2)}</td>
                      <td>{typeof t.pnl === "number" ? t.pnl.toFixed(2) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(trades?.trades ?? []).length === 0 && <div className="empty">暂无交易记录</div>}
            </div>
          </div>
        </div>

        <div className="card" style={{ marginTop: 8 }}>
          <h3>逐日记录</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>日期</th>
                  <th>策略净值</th>
                  <th>基准净值</th>
                  <th>累计交易数</th>
                </tr>
              </thead>
              <tbody>
                {dailyRecords.map((r) => (
                  <tr key={r.date}>
                    <td>{r.date}</td>
                    <td>{r.total_value.toFixed(2)}</td>
                    <td>{r.benchmark_value.toFixed(2)}</td>
                    <td>{r.trades_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {dailyRecords.length === 0 && <div className="empty">暂无逐日记录，运行回测后会实时追加</div>}
          </div>
        </div>
      </div>
    </section>
  );
}

