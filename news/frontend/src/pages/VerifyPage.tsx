import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchVerifyStatus,
  startVerify,
  stopVerify,
  fetchVerifyReport,
  fetchSampleDetail,
  fetchVerifyLogs,
} from "../api/verify";
import type {
  VerifyStatusResponse,
  VerifyReportResponse,
  SampleDetailItem,
  VerifyLogItem,
} from "../types";

const FIELD_LABELS: Record<string, string> = {
  revenue_yoy: "营收同比",
  deduct_profit_yoy: "扣非净利同比",
  cagr_3y: "3年CAGR",
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
  profit_volatility_5y: "5年业绩波动",
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

export default function VerifyPage() {
  const [status, setStatus] = useState<VerifyStatusResponse | null>(null);
  const [report, setReport] = useState<VerifyReportResponse | null>(null);
  const [detail, setDetail] = useState<SampleDetailItem[]>([]);
  const [detailTotal, setDetailTotal] = useState(0);
  const [detailPage, setDetailPage] = useState(1);
  const [logs, setLogs] = useState<VerifyLogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [detailField, setDetailField] = useState("");
  const [detailStatus, setDetailStatus] = useState("");
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const running = Boolean(status?.running);
  const latest = status?.latest_run;
  const progress =
    latest && latest.total > 0
      ? Math.round((latest.processed / latest.total) * 100)
      : 0;

  const refresh = useCallback(async () => {
    try {
      const [s, r, logData] = await Promise.all([
        fetchVerifyStatus(),
        fetchVerifyReport(),
        fetchVerifyLogs(200),
      ]);
      setStatus(s);
      setReport(r);
      setLogs(logData.items);
    } catch {
      setError("无法获取校验状态");
    }
  }, []);

  const loadDetail = useCallback(
    async (page = detailPage) => {
      if (!report?.run_id) return;
      try {
        const data = await fetchSampleDetail({
          runId: report.run_id,
          field: detailField || undefined,
          status: detailStatus || undefined,
          page,
          size: 30,
        });
        setDetail(data.items);
        setDetailTotal(data.total);
      } catch {
        // ignore
      }
    },
    [report?.run_id, detailField, detailStatus, detailPage]
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (running) {
      timerRef.current = setInterval(refresh, 3000);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [running, refresh]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail, detailPage, detailField, detailStatus]);

  async function handleStart() {
    setLoading(true);
    setError("");
    setMessage("");
    try {
      const resp = await startVerify();
      setMessage(resp.message);
      await refresh();
    } catch (err: unknown) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { error?: string } } }).response?.data
              ?.error
          : undefined;
      setError(msg || "启动校验失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleStop() {
    setLoading(true);
    setError("");
    setMessage("");
    try {
      await stopVerify();
      setMessage("已发送停止信号");
      await refresh();
    } catch {
      setError("停止失败");
    } finally {
      setLoading(false);
    }
  }

  const sortedFields = useMemo(
    () => [...(report?.fields || [])].sort((a, b) => a.accuracy - b.accuracy),
    [report?.fields]
  );

  const hasData = latest && (latest.total > 0 || latest.status === "completed");

  return (
    <section className="dashboard-section" data-section="verify">
      <h2 className="dashboard-section-title">数据校验</h2>
      <div className="dashboard-section-inner quant-analyze-page">
        <div className="card">
          <h3>校验控制台</h3>
          <p className="text-muted">
            随机抽样股票，联网比对真实值，自动补全低准确率字段。
          </p>
          <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="quant-analyze-btn"
              disabled={loading || running}
              onClick={handleStart}
            >
              {running ? "校验中..." : "开始校验"}
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

        {running && latest && (
          <div className="card">
            <h3>校验进度</h3>
            <div
              style={{
                width: "100%",
                height: 20,
                background: "var(--color-bg-subtle)",
                borderRadius: 10,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  width: `${progress}%`,
                  height: "100%",
                  background: "var(--color-accent)",
                  transition: "width 0.5s",
                  borderRadius: 10,
                }}
              />
            </div>
            <p className="text-muted" style={{ marginTop: 8 }}>
              {latest.processed}/{latest.total} ({progress}%) —{" "}
              {latest.message || "处理中..."}
            </p>
          </div>
        )}

        {report && hasData && (
          <>
            <div className="metrics-row">
              <div className="card metric-card">
                <div className="sub">总准确率</div>
                <div className="metric">
                  {report.fields.length > 0
                    ? Math.round(
                        report.fields.reduce((s, f) => s + f.accuracy, 0) /
                          report.fields.length
                      )
                    : 0}
                  %
                </div>
              </div>
              <div className="card metric-card">
                <div className="sub">抽样数</div>
                <div className="metric">{report.total_samples}</div>
              </div>
              <div className="card metric-card">
                <div className="sub">字段数</div>
                <div className="metric">{report.fields.length}</div>
              </div>
            </div>

            <div className="card">
              <h3>字段准确率</h3>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>字段</th>
                      <th>准确率</th>
                      <th>正确/总数</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedFields.map((f) => (
                      <tr key={f.field}>
                        <td>{FIELD_LABELS[f.field] || f.field}</td>
                        <td>
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                            }}
                          >
                            <div
                              style={{
                                width: 120,
                                height: 10,
                                background: "var(--color-bg-subtle)",
                                borderRadius: 5,
                                overflow: "hidden",
                              }}
                            >
                              <div
                                style={{
                                  width: `${f.accuracy}%`,
                                  height: "100%",
                                  background:
                                    f.accuracy >= 80
                                      ? "var(--color-success)"
                                      : f.accuracy >= 50
                                      ? "var(--color-warning)"
                                      : "var(--color-danger)",
                                  borderRadius: 5,
                                }}
                              />
                            </div>
                            <span
                              style={{
                                fontWeight: 600,
                                color:
                                  f.accuracy >= 80
                                    ? "var(--color-success)"
                                    : f.accuracy >= 50
                                    ? "var(--color-warning)"
                                    : "var(--color-danger)",
                              }}
                            >
                              {f.accuracy}%
                            </span>
                          </div>
                        </td>
                        <td>
                          {f.correct}/{f.total}
                        </td>
                        <td>
                          <button
                            type="button"
                            className="quant-analyze-secondary-btn"
                            style={{ fontSize: 12, padding: "4px 8px" }}
                            onClick={() => {
                              setDetailField(f.field);
                              setDetailPage(1);
                            }}
                          >
                            查看明细
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {sortedFields.length === 0 && (
                  <div className="empty">暂无数据，请先点击开始校验。</div>
                )}
              </div>
            </div>

            <div className="card">
              <div className="quant-analyze-section-head">
                <h3>抽样明细</h3>
                <div className="quant-analyze-toolbar">
                  <select
                    value={detailField}
                    onChange={(e) => {
                      setDetailField(e.target.value);
                      setDetailPage(1);
                    }}
                    style={{ padding: "4px 8px", fontSize: 13 }}
                  >
                    <option value="">全部字段</option>
                    {sortedFields.map((f) => (
                      <option key={f.field} value={f.field}>
                        {FIELD_LABELS[f.field] || f.field}
                      </option>
                    ))}
                  </select>
                  <select
                    value={detailStatus}
                    onChange={(e) => {
                      setDetailStatus(e.target.value);
                      setDetailPage(1);
                    }}
                    style={{ padding: "4px 8px", fontSize: 13 }}
                  >
                    <option value="">全部状态</option>
                    <option value="correct">正确</option>
                    <option value="wrong">错误</option>
                    <option value="corrected">已补全</option>
                  </select>
                </div>
              </div>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>代码</th>
                      <th>名称</th>
                      <th>字段</th>
                      <th>DB值</th>
                      <th>真实值</th>
                      <th>偏差</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.map((item, i) => (
                      <tr key={`${item.code}-${item.field}-${i}`}>
                        <td>{item.code}</td>
                        <td>{item.name || "-"}</td>
                        <td>{FIELD_LABELS[item.field] || item.field}</td>
                        <td>{item.db_value ?? "-"}</td>
                        <td>{item.real_value ?? "-"}</td>
                        <td>
                          {item.deviation != null
                            ? `${Math.round(item.deviation)}%`
                            : "-"}
                        </td>
                        <td>
                          <span
                            style={{
                              color:
                                item.status === "correct"
                                  ? "var(--color-success)"
                                  : item.status === "corrected"
                                  ? "var(--color-accent)"
                                  : item.status === "wrong"
                                  ? "var(--color-danger)"
                                  : "var(--color-text-muted)",
                            }}
                          >
                            {item.status === "correct"
                              ? "✅"
                              : item.status === "corrected"
                              ? "🔄"
                              : item.status === "wrong"
                              ? "❌"
                              : "⏳"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {detail.length === 0 && (
                  <div className="empty">暂无明细数据。</div>
                )}
              </div>
              <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className="quant-analyze-secondary-btn"
                  disabled={detailPage <= 1}
                  onClick={() => setDetailPage((p) => p - 1)}
                >
                  上一页
                </button>
                <span className="text-muted">
                  第 {detailPage} 页 / 共 {Math.ceil(detailTotal / 30)} 页
                </span>
                <button
                  type="button"
                  className="quant-analyze-secondary-btn"
                  disabled={detailPage * 30 >= detailTotal}
                  onClick={() => setDetailPage((p) => p + 1)}
                >
                  下一页
                </button>
              </div>
            </div>
          </>
        )}

        <div className="card">
          <h3>校验日志</h3>
          <div className="logs-scroll">
            <ul className="logs">
              {logs.map((row, i) => (
                <li key={`${row.timestamp}-${i}`}>
                  <span className="level">{row.level}</span>
                  <span>{new Date(row.timestamp).toLocaleTimeString()}</span>
                  <span>{row.message}</span>
                </li>
              ))}
            </ul>
            {logs.length === 0 && (
              <div className="empty">暂无日志。</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
