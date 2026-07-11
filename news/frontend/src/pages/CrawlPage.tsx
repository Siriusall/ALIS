import { useEffect, useMemo, useState } from "react";
import { fetchCrawlCoverage, fetchCrawlLogs, fetchCrawlStatus, stopCrawl, triggerCrawl } from "../api/crawl";
import type { CrawlCoverageResponse, CrawlLogsResponse, CrawlStatusResponse } from "../types";

const FIELD_LABELS: Record<string, string> = {
  revenue_yoy: "营收同比",
  deduct_profit_yoy: "扣非净利同比",
  cagr_3y: "近3年CAGR",
  gross_margin: "毛利率",
  net_margin: "净利率",
  roe: "ROE",
  roic: "ROIC",
  ocf_to_profit: "经营现金流/净利润",
  free_cashflow: "自由现金流",
  debt_ratio: "资产负债率",
  interest_debt: "有息负债",
  cash_coverage: "货币资金覆盖",
  dividend_yield: "股息率",
  payout_ratio: "分红率",
  buyback_amount: "回购金额",
  profit_volatility_5y: "过去5年业绩波动",
  cyclicality: "周期性",
  pe: "PE",
  pb: "PB",
  ps: "PS",
  valuation_percentile: "估值分位",
  fund_holding: "公募持仓",
  northbound: "北向",
  etf_holding: "ETF",
  research_count: "调研次数",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "等待中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
};

export default function CrawlPage() {
  const [status, setStatus] = useState<CrawlStatusResponse | null>(null);
  const [coverage, setCoverage] = useState<CrawlCoverageResponse | null>(null);
  const [logs, setLogs] = useState<CrawlLogsResponse | null>(null);
  const [showAllLogs, setShowAllLogs] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const latest = status?.latest;
  const running = Boolean(status?.processRunning ?? status?.running);
  const progress = latest && latest.total > 0
    ? Math.round(((latest.success + latest.failed) / latest.total) * 100)
    : 0;
  const coverageRows = useMemo(() => (
    Object.entries(coverage?.fields || {}).map(([field, value]) => ({
      field,
      label: FIELD_LABELS[field] || field,
      nonNull: value.nonNull,
      coverage: value.coverage,
    }))
  ), [coverage]);
  const orderedLogs = useMemo(() => [...(logs?.items || [])].reverse(), [logs]);
  const visibleLogs = showAllLogs ? orderedLogs : orderedLogs.slice(0, 30);

  async function refresh() {
    try {
      const data = await fetchCrawlStatus();
      setStatus(data);
      const runId = data.latest?.id;
      const [coverageRows, logRows] = await Promise.all([
        fetchCrawlCoverage(runId),
        fetchCrawlLogs(500),
      ]);
      setCoverage(coverageRows);
      setLogs(logRows);
    } catch {
      setError("无法获取爬取状态");
    }
  }

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, []);

  async function handleTrigger() {
    setLoading(true);
    setError("");
    setMessage("");
    try {
      const resp = await triggerCrawl();
      const runHint = resp.runId ? `（任务 ${resp.runId}）` : "";
      setMessage(`${resp.message}${runHint}`);
      await refresh();
    } catch (err: unknown) {
      const msg = err && typeof err === "object" && "response" in err
        ? (err as { response?: { data?: { error?: string } } }).response?.data?.error
        : undefined;
      setError(msg || "启动爬取失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleStop() {
    setLoading(true);
    setError("");
    setMessage("");
    try {
      await stopCrawl();
      setMessage("已发送停止信号");
      await refresh();
    } catch (err: unknown) {
      const msg = err && typeof err === "object" && "response" in err
        ? (err as { response?: { data?: { error?: string } } }).response?.data?.error
        : undefined;
      setError(msg || "停止爬取失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="dashboard-section" data-section="crawl">
      <h2 className="dashboard-section-title">数据爬取</h2>
      <div className="dashboard-section-inner quant-analyze-page">
        <div className="card">
          <h3>网页爬虫控制台</h3>
          <p className="text-muted">
            默认爬取沪深全市场。点击开始会自动继续最近未完成任务，并根据数据库里已保存的股票因子跳过已完成股票。
          </p>
          <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="quant-analyze-btn"
              disabled={loading || running}
              onClick={handleTrigger}
            >
              开始/继续爬取
            </button>
            <button
              type="button"
              className="quant-analyze-secondary-btn"
              disabled={loading || !running}
              onClick={handleStop}
            >
              停止
            </button>
          </div>
          {message && <div className="quant-analyze-buy-success">{message}</div>}
          {error && <div className="alert">{error}</div>}
        </div>

        <div className="metrics-row">
          <div className="card metric-card">
            <div className="sub">状态</div>
            <div className="metric" style={{ fontSize: 18 }}>
              {running ? "运行中" : latest?.status ? STATUS_LABELS[latest.status] || latest.status : "未开始"}
            </div>
          </div>
          <div className="card metric-card">
            <div className="sub">任务 ID</div>
            <div className="metric" style={{ fontSize: 18 }}>{latest?.id ?? "-"}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">总数</div>
            <div className="metric" style={{ fontSize: 18 }}>{latest?.total ?? "-"}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">成功 / 失败</div>
            <div className="metric" style={{ fontSize: 18 }}>{latest ? `${latest.success} / ${latest.failed}` : "-"}</div>
          </div>
          <div className="card metric-card">
            <div className="sub">进度</div>
            <div className="metric" style={{ fontSize: 18 }}>{latest ? `${progress}%` : "-"}</div>
          </div>
        </div>

        <div className="card">
          <div className="quant-analyze-section-head">
            <h3>字段覆盖率</h3>
            <span className="text-muted">样本数：{coverage?.total ?? 0}</span>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>字段</th>
                  <th>非空数</th>
                  <th>覆盖率</th>
                </tr>
              </thead>
              <tbody>
                {coverageRows.map((row) => (
                  <tr key={row.field}>
                    <td>{row.label}</td>
                    <td>{row.nonNull} / {coverage?.total || 0}</td>
                    <td>{Math.round(row.coverage * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!coverageRows.length && <div className="empty">暂无覆盖率数据，爬到因子后这里会显示每列覆盖情况。</div>}
          </div>
        </div>

        <div className="card">
          <div className="quant-analyze-section-head">
            <h3>爬取日志</h3>
            <div className="quant-analyze-toolbar">
              {(logs?.items.length || 0) > 30 && (
                <button type="button" className="quant-analyze-secondary-btn" onClick={() => setShowAllLogs((value) => !value)}>
                  {showAllLogs ? "收起日志" : "展开全部"}
                </button>
              )}
              <button type="button" className="quant-analyze-secondary-btn" onClick={refresh}>
                刷新
              </button>
            </div>
          </div>
          <div className="logs-scroll">
            <ul className="logs">
              {visibleLogs.map((row, index) => (
                <li key={`${row.timestamp}-${index}`}>
                  <span className="level">{row.level}</span>
                  <span>{new Date(row.timestamp).toLocaleTimeString()}</span>
                  <span>{row.message}</span>
                </li>
              ))}
            </ul>
            {(logs?.items.length || 0) > 30 && !showAllLogs && (
              <div className="empty empty-hint">日志已折叠，仅显示前 30 条，共 {logs?.items.length} 条。</div>
            )}
            {!logs?.items.length && <div className="empty">暂无运行日志。启动爬取后会显示进程输出。</div>}
          </div>
        </div>
      </div>
    </section>
  );
}
