import { useEffect, useMemo, useState } from "react";
import {
  fetchDailyUpdateStatus,
  fetchDataAssets,
  fetchDataCenterStatus,
  fetchReadiness,
  fetchSimpleDataHealth,
  type DataAssetsResponse,
  type DataCenterStatus,
  type DailyUpdateStatus,
  type ReadinessOverview,
  type SimpleDataHealth,
} from "../api/dataCenter";
import DataCenterTechnicalConsole, { type SectionKey } from "./DataCenterTechnicalConsole";
import { DATA_CENTER_SECTION_EVENT, DATA_CENTER_SECTION_STORAGE_KEY } from "./dataCenterSections";
import "./DataCenterTechnicalConsole.css";

export default function DataCenterPage() {
  const [section, setSection] = useState<SectionKey>(() => {
    const saved = window.localStorage.getItem(DATA_CENTER_SECTION_STORAGE_KEY) as SectionKey | null;
    return saved || "overview";
  });
  const [health, setHealth] = useState<SimpleDataHealth | null>(null);
  const [readiness, setReadiness] = useState<ReadinessOverview | null>(null);
  const [status, setStatus] = useState<DataCenterStatus | null>(null);
  const [assets, setAssets] = useState<DataAssetsResponse | null>(null);
  const [update, setUpdate] = useState<DailyUpdateStatus | null>(null);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [loadFailures, setLoadFailures] = useState(0);

  useEffect(() => {
    const onChange = (event: Event) => {
      const next = (event as CustomEvent<SectionKey>).detail;
      if (next) setSection(next);
    };
    window.addEventListener(DATA_CENTER_SECTION_EVENT, onChange);
    return () => window.removeEventListener(DATA_CENTER_SECTION_EVENT, onChange);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let loading = false;
    async function load() {
      if (loading) return;
      loading = true;
      setLoadFailures(0);
      const failed = () => !cancelled && setLoadFailures((count) => count + 1);
      const tasks = [
        fetchSimpleDataHealth()
          .then((value) => { if (!cancelled) setHealth(value); })
          .catch(failed)
          .finally(() => { if (!cancelled) setLoadedOnce(true); }),
        fetchReadiness().then((value) => { if (!cancelled) setReadiness(value); }).catch(failed),
        fetchDataCenterStatus(8).then((value) => { if (!cancelled) setStatus(value); }).catch(failed),
        fetchDataAssets().then((value) => { if (!cancelled) setAssets(value); }).catch(failed),
        fetchDailyUpdateStatus().then((value) => { if (!cancelled) setUpdate(value); }).catch(failed),
      ];
      void Promise.allSettled(tasks).finally(() => { loading = false; });
    }
    void load();
    const timer = window.setInterval(load, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  function changeSection(next: SectionKey) {
    setSection(next);
    window.localStorage.setItem(DATA_CENTER_SECTION_STORAGE_KEY, next);
    window.dispatchEvent(new CustomEvent(DATA_CENTER_SECTION_EVENT, { detail: next }));
  }

  const blockedConsumers = useMemo(
    () => readiness?.consumers.filter((item) => item.status !== "ready").length ?? 0,
    [readiness],
  );
  const partialAssets = useMemo(
    () => assets?.items.filter((item) => item.status === "partial" || item.status === "missing").length ?? 0,
    [assets],
  );
  const recentFailedRuns = health?.recent_failed_runs ?? 0;
  const healthUnavailable = loadedOnce && !health;
  const decisionBlocked = healthUnavailable || Boolean(health && health.level !== "healthy");
  const updateStatus = update?.running
    ? "更新中"
    : update?.phase === "cooldown"
      ? "冷却复核"
      : update?.phase === "failed"
        ? "自动恢复中"
        : "等待周期";

  return (
    <div className="embedded-data-platform">
      <div className="product-module-head">
        <div>
          <h1>数据中台</h1>
          <p>先看能不能支撑决策，再处理缺口。</p>
        </div>
      </div>

      {section === "overview" && (
        <section className="product-module-page workbench-page">
          <div className="workbench-hero">
            <div>
              <span>当前状态</span>
              <strong>
                {healthUnavailable
                  ? "实时数据门读取失败，当前状态未知"
                  : !health
                    ? "正在读取实时决策数据门"
                    : decisionBlocked
                  ? health?.summary || "最新交易日决策数据未就绪"
                  : blockedConsumers > 0
                  ? `${blockedConsumers} 个消费模块被数据阻断`
                  : partialAssets > 0
                    ? `核心分析可运行，仍有 ${partialAssets} 项历史缺口`
                    : "当前可支撑前台决策与研究"}
              </strong>
            </div>
            <button type="button" className="button primary" onClick={() => changeSection("repair")}>
              查看自治修复
            </button>
          </div>

          <div className="product-module-grid">
            <article className="product-module-card">
              <h2>最新交易日</h2>
              <strong>{health?.target_trade_date || health?.latest_trade_date || "—"}</strong>
              <p>{healthUnavailable ? "数据门接口不可用，禁止据此判断可执行" : decisionBlocked ? `最近决策可用 ${health?.latest_decision_ready_trade_date || "无"}` : health ? `${health.stock_count.toLocaleString("zh-CN")} 只股票` : "读取中"}</p>
            </article>
            <article className="product-module-card">
              <h2>消费就绪</h2>
              <strong>{readiness ? `${readiness.consumers.filter((item) => item.status === "ready").length}/${readiness.consumers.length}` : "—"}</strong>
              <p>{!readiness ? loadedOnce ? "就绪合同读取失败" : "读取中" : decisionBlocked ? "正式决策链路已安全阻断" : blockedConsumers ? `${blockedConsumers} 个模块未就绪` : "前台主模块可用"}</p>
            </article>
            <article className="product-module-card">
              <h2>数据缺口</h2>
              <strong>{assets ? partialAssets : "—"}</strong>
              <p>{!assets ? loadedOnce ? "数据资产目录读取失败" : "读取中" : partialAssets ? "仍有待补数据" : "当前覆盖正常"}</p>
            </article>
            <article className="product-module-card">
              <h2>运行状态</h2>
              <strong>{updateStatus}</strong>
              <p>{!health ? loadedOnce ? `实时健康读取失败${loadFailures ? `（${loadFailures} 个接口异常）` : ""}` : "读取中" : recentFailedRuns ? `近 24 小时 ${recentFailedRuns} 次失败，系统正在分类复核` : "近 24 小时无失败"}</p>
            </article>
          </div>

          <div className="workbench-grid">
            <article className="workbench-card">
              <div className="workbench-card-top">
                <span>优先处理</span>
                <h2>长线分析自检</h2>
              </div>
              <p>先看哪些模块现在不能信。</p>
              <button type="button" className="button ghost" onClick={() => changeSection("readiness")}>打开自检</button>
            </article>
            <article className="workbench-card">
              <div className="workbench-card-top">
                <span>补数据</span>
                <h2>数据补全</h2>
              </div>
              <p>这里就是修缺口。</p>
              <button type="button" className="button ghost" onClick={() => changeSection("repair")}>打开补全</button>
            </article>
            <article className="workbench-card">
              <div className="workbench-card-top">
                <span>查来源</span>
                <h2>数据资产与质量</h2>
              </div>
              <p>看哪些完整，哪些还缺。</p>
              <button type="button" className="button ghost" onClick={() => changeSection("assets")}>查看资产</button>
            </article>
          </div>

          <div className="card decision-panel">
            <h3>最近数据流水</h3>
            <div className="decision-issue-list">
              {(status?.recent_runs ?? []).slice(0, 6).map((run) => (
                <div key={run.id} className={`decision-issue ${run.status === "failed" ? "error" : "warning"}`}>
                  {run.job_name} · {run.status} · 读取 {run.rows_read} / 写入 {run.rows_written}
                </div>
              ))}
              {!status?.recent_runs?.length && <div className="empty">暂无最近数据流水。</div>}
            </div>
          </div>
        </section>
      )}

      {section !== "overview" && <DataCenterTechnicalConsole section={section} />}
    </div>
  );
}
