import { useEffect, useMemo, useState } from "react";
import {
  fetchDataCenterEstimate,
  fetchDataCenterStatus,
  seedDataCenter,
  syncAnnouncements,
  syncFinancialBatch,
  syncMarket,
  type DataCenterEstimate,
  type DataCenterStatus,
  type SyncResponse,
  type TushareCatalogItem,
} from "../api/dataCenter";

const DEFAULT_TRADE_DATE = "20260709";
const DEFAULT_PERIOD = "20260331";

export default function DataCenterPage() {
  const [status, setStatus] = useState<DataCenterStatus | null>(null);
  const [estimate, setEstimate] = useState<DataCenterEstimate | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState("");
  const [message, setMessage] = useState("");
  const [tradeDate, setTradeDate] = useState(DEFAULT_TRADE_DATE);
  const [period, setPeriod] = useState(DEFAULT_PERIOD);
  const [codes, setCodes] = useState("000001.SZ,000002.SZ");
  const [batchLimit, setBatchLimit] = useState(2);
  const [annStart, setAnnStart] = useState("20260709");
  const [annEnd, setAnnEnd] = useState("20260710");

  async function refresh(options: { keepMessage?: boolean } = {}) {
    setLoading(true);
    try {
      const [nextStatus, nextEstimate] = await Promise.all([
        fetchDataCenterStatus(20),
        fetchDataCenterEstimate(5),
      ]);
      setStatus(nextStatus);
      setEstimate(nextEstimate);
      if (!options.keepMessage) setMessage("");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "数据中台状态读取失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const groupedCatalog = useMemo(() => groupCatalog(status?.catalog ?? []), [status]);
  const coverage = status?.coverage;

  async function runAction(label: string, action: () => Promise<SyncResponse>) {
    setActionLoading(label);
    setMessage("");
    try {
      const result = await action();
      setMessage(`${label} 完成：${summarizeResult(result)}`);
      await refresh({ keepMessage: true });
    } catch (err) {
      setMessage(`${label} 失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setActionLoading("");
    }
  }

  return (
    <div className="data-center-page">
      <section className="dc-toolbar">
        <div>
          <h2>数据中台</h2>
          <p>结构化数据源、采集任务、覆盖状态和增量同步控制台。</p>
        </div>
        <div className="dc-toolbar-actions">
          <button type="button" onClick={() => refresh()} disabled={loading || !!actionLoading}>
            刷新
          </button>
          <button
            type="button"
            onClick={() => runAction("初始化目录", seedDataCenter)}
            disabled={loading || !!actionLoading}
          >
            初始化目录元数据
          </button>
        </div>
      </section>

      {message && <div className="dc-message">{message}</div>}

      <section className="dc-kpi-grid">
        <Kpi title="股票主数据" value={coverage?.stock_master_rows ?? 0} hint="dc_stock_master" />
        <Kpi title="Raw 记录" value={coverage?.raw_record_rows ?? 0} hint="Tushare 原始层" />
        <Kpi title="每日快照" value={coverage?.stock_daily_snapshot_rows ?? 0} hint="stock_daily_snapshot" />
        <Kpi title="公告原文" value={coverage?.announcement_raw_rows ?? 0} hint="CNINFO Raw" />
        <Kpi title="行业指标" value={coverage?.industry_metric_values ?? 0} hint={`${coverage?.industry_metric_definitions ?? 0} 个定义`} />
        <Kpi title="质量检查" value={coverage?.data_quality_check_rows ?? 0} hint="data_quality_checks" />
      </section>

      <section className="dc-main-grid">
        <div className="dc-panel dc-sync-panel">
          <div className="dc-panel-title">
            <h3>同步控制台</h3>
            <span>{actionLoading ? `运行中：${actionLoading}` : "就绪"}</span>
          </div>
          <div className="dc-form-grid">
            <label>
              <span>交易日</span>
              <input value={tradeDate} onChange={(e) => setTradeDate(e.target.value)} />
            </label>
            <button
              type="button"
              onClick={() => runAction("行情快照同步", () => syncMarket(tradeDate))}
              disabled={!!actionLoading}
            >
              同步行情快照
            </button>
            <label>
              <span>报告期</span>
              <input value={period} onChange={(e) => setPeriod(e.target.value)} />
            </label>
            <label>
              <span>股票代码</span>
              <input value={codes} onChange={(e) => setCodes(e.target.value)} />
            </label>
            <label>
              <span>批量上限</span>
              <input
                type="number"
                min={1}
                max={500}
                value={batchLimit}
                onChange={(e) => setBatchLimit(Number(e.target.value) || 1)}
              />
            </label>
            <button
              type="button"
              onClick={() =>
                runAction("财务批同步", () =>
                  syncFinancialBatch({ period, codes, limit: batchLimit })
                )
              }
              disabled={!!actionLoading}
            >
              同步财务批次
            </button>
            <label>
              <span>公告开始</span>
              <input value={annStart} onChange={(e) => setAnnStart(e.target.value)} />
            </label>
            <label>
              <span>公告结束</span>
              <input value={annEnd} onChange={(e) => setAnnEnd(e.target.value)} />
            </label>
            <button
              type="button"
              onClick={() =>
                runAction("公告增量同步", () =>
                  syncAnnouncements({ startDate: annStart, endDate: annEnd, pageSize: 10, maxPages: 1 })
                )
              }
              disabled={!!actionLoading}
            >
              同步公告
            </button>
          </div>
        </div>

        <div className="dc-panel">
          <div className="dc-panel-title">
            <h3>耗时估算</h3>
            <span>5 年窗口</span>
          </div>
          <div className="dc-estimate">
            <EstimateLine label="核心池首次" value={String(estimate?.initial_full_load?.estimated_time_hours_core_300_conservative ?? "-")} unit="小时" />
            <EstimateLine label="全市场财务" value={String(estimate?.initial_full_load?.estimated_financial_time_hours_full_market_conservative ?? "-")} unit="小时" />
            <EstimateLine label="每日增量" value={String(estimate?.daily_incremental?.estimated_time_minutes ?? "-")} unit="分钟" />
          </div>
          <div className="dc-note">{String(estimate?.initial_full_load?.practical_window ?? "")}</div>
        </div>
      </section>

      <section className="dc-panel">
        <div className="dc-panel-title">
          <h3>数据源</h3>
          <span>{status?.sources.length ?? 0} 个源</span>
        </div>
        <div className="dc-source-list">
          {(status?.sources ?? []).map((source) => (
            <div key={source.code} className="dc-source-item">
              <div>
                <strong>{source.name}</strong>
                <span>{source.code} / {source.source_type}</span>
              </div>
              <Badge status={source.status} />
            </div>
          ))}
        </div>
      </section>

      <section className="dc-panel">
        <div className="dc-panel-title">
          <h3>接口覆盖矩阵</h3>
          <span>{status?.catalog.length ?? 0} 个接口</span>
        </div>
        <div className="dc-catalog-grid">
          {Object.entries(groupedCatalog).map(([category, items]) => (
            <div key={category} className="dc-catalog-group">
              <div className="dc-catalog-category">{category}</div>
              {items.map((item) => (
                <div key={item.api_name} className="dc-catalog-row">
                  <div>
                    <strong>{item.display_name}</strong>
                    <span>{item.api_name} · {item.recommended_frequency}</span>
                  </div>
                  <div className="dc-catalog-status">
                    <Badge status={item.last_probe_status} />
                    <span>{formatNumber(item.last_probe_rows)}</span>
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </section>

      <section className="dc-panel">
        <div className="dc-panel-title">
          <h3>最近任务</h3>
          <span>{status?.recent_runs.length ?? 0} 条</span>
        </div>
        <div className="dc-table-wrap">
          <table>
            <thead>
              <tr>
                <th>任务</th>
                <th>状态</th>
                <th>读取</th>
                <th>写入</th>
                <th>开始</th>
                <th>错误</th>
              </tr>
            </thead>
            <tbody>
              {(status?.recent_runs ?? []).map((run) => (
                <tr key={run.id}>
                  <td>{run.job_name}</td>
                  <td><Badge status={run.status} /></td>
                  <td>{formatNumber(run.rows_read)}</td>
                  <td>{formatNumber(run.rows_written)}</td>
                  <td>{formatTime(run.started_at)}</td>
                  <td className="dc-error-cell">{run.error || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Kpi({ title, value, hint }: { title: string; value: number; hint: string }) {
  return (
    <div className="dc-kpi">
      <span>{title}</span>
      <strong>{formatNumber(value)}</strong>
      <em>{hint}</em>
    </div>
  );
}

function Badge({ status }: { status: string }) {
  const normalized = status === "completed" || status === "active" ? "ok" : status;
  return <span className={`dc-badge dc-badge-${normalized}`}>{status}</span>;
}

function EstimateLine({ label, value, unit }: { label: string; value: string; unit: string }) {
  return (
    <div className="dc-estimate-line">
      <span>{label}</span>
      <strong>{value}</strong>
      <em>{unit}</em>
    </div>
  );
}

function groupCatalog(items: TushareCatalogItem[]) {
  return items.reduce<Record<string, TushareCatalogItem[]>>((acc, item) => {
    if (!acc[item.category]) acc[item.category] = [];
    acc[item.category].push(item);
    return acc;
  }, {});
}

function summarizeResult(result: SyncResponse) {
  if (typeof result.catalog_count === "number") return `接口目录 ${formatNumber(result.catalog_count)} 项`;
  if (typeof result.updated === "number") return `更新 ${formatNumber(result.updated)} 项`;
  const rows = result.rows_written ?? result.rows_read ?? result.definitions ?? result.events_created;
  if (typeof rows === "number") return `写入 ${formatNumber(rows)} 行`;
  return result.ok ? "已提交" : "返回异常";
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}

function formatTime(value?: string | null) {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 19);
}
