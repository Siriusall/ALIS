import { useEffect, useMemo, useState } from "react";
import { fetchGovernanceOverview, type GovernanceOverview } from "../api/research";

const LABEL: Record<string, string> = {
  data_health: "数据健康",
  data_readiness: "消费就绪度",
  model: "生产模型",
  task_failures: "任务异常",
};

function statusText(value: string) {
  const map: Record<string, string> = {
    ready: "就绪",
    partial: "部分就绪",
    blocked: "阻塞",
    missing: "缺失",
    building: "建设中",
  };
  return map[value] || value || "—";
}

export default function GovernancePage() {
  const [data, setData] = useState<GovernanceOverview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function load() {
    setLoading(true);
    try {
      setData(await fetchGovernanceOverview());
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const blockedCount = useMemo(
    () => data?.gates.filter((item) => item.status !== "ready").length ?? 0,
    [data],
  );
  const failedTasks = data?.tasks.failed_or_paused ?? [];
  const consumers = data?.data.readiness.consumers ?? [];
  const urgentGates = data?.gates.filter((item) => item.status !== "ready") ?? [];

  return (
    <section className="product-module-page">
      <div className="product-module-head">
        <div>
          <h1>运行治理与上线门控</h1>
          <p>这里只看阻断项和上线门。</p>
        </div>
        <button className="button ghost" onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="alert">{error}</div>}

      {data && (
        <>
          <div className="workbench-hero">
            <div>
              <span>治理状态</span>
              <strong>
                {blockedCount > 0
                  ? `${blockedCount} 个门控未通过，暂不应把系统当成完全可运行`
                  : "当前没有治理级阻断项"}
              </strong>
            </div>
            <button className="button primary" onClick={() => void load()}>
              刷新治理快照
            </button>
          </div>

          <div className="product-module-grid">
            <article className="product-module-card">
              <h2>治理状态</h2>
              <strong>{blockedCount > 0 ? "需处理" : "可运行"}</strong>
              <p>{blockedCount} 个非就绪门控</p>
            </article>
            <article className="product-module-card">
              <h2>生产模型</h2>
              <strong>{data.model.active_version?.version || "未设置"}</strong>
              <p>{data.model.version_count} 个版本</p>
            </article>
            <article className="product-module-card">
              <h2>异常任务</h2>
              <strong>{failedTasks.length}</strong>
              <p>失败或暂停</p>
            </article>
            <article className="product-module-card">
              <h2>消费者就绪</h2>
              <strong>{consumers.filter((item) => item.status === "ready").length}/{consumers.length}</strong>
              <p>前台可安全使用</p>
            </article>
          </div>

          <div className="decision-grid">
            <div className="card decision-panel">
              <h3>优先处理项</h3>
              <div className="decision-issue-list">
                {urgentGates.map((item) => (
                  <div key={item.key} className="decision-issue warning">
                    {LABEL[item.key] ?? item.key} · {statusText(item.status)} · {item.detail}
                  </div>
                ))}
                {failedTasks.slice(0, 5).map((item, index) => (
                  <div key={`task-${index}`} className="decision-issue error">
                    任务异常 · {String((item as Record<string, unknown>).job_name || (item as Record<string, unknown>).name || "未命名任务")}
                  </div>
                ))}
                {!urgentGates.length && !failedTasks.length && <div className="empty">当前没有需要立即处理的治理项。</div>}
              </div>
            </div>

            <div className="card decision-panel">
              <h3>待处理动作</h3>
              <div className="review-queue">
                <div className="review-queue-item">
                  <div>
                    <strong>先核对数据健康</strong>
                    <span>先排除缺失、未来函数和覆盖问题</span>
                  </div>
                  <em>{statusText(data.gates.find((item) => item.key === "data_health")?.status || "")}</em>
                </div>
                <div className="review-queue-item">
                  <div>
                    <strong>检查生产模型</strong>
                    <span>确认 active 版本、artifact 和训练状态一致</span>
                  </div>
                  <em>{statusText(data.gates.find((item) => item.key === "model")?.status || "")}</em>
                </div>
                <div className="review-queue-item">
                  <div>
                    <strong>查看任务异常</strong>
                    <span>确认失败任务不会让前台误用旧数据</span>
                  </div>
                  <em>{failedTasks.length} 项</em>
                </div>
                <div className="review-queue-item">
                  <div>
                    <strong>核对消费者就绪</strong>
                    <span>确认前台模块不是“能打开但不能信”</span>
                  </div>
                  <em>{consumers.filter((item) => item.status === "ready").length}/{consumers.length}</em>
                </div>
              </div>
            </div>
          </div>

          <div className="decision-grid">
            <div className="card decision-panel">
              <h3>模块完成状态</h3>
              <div className="decision-two-col">
                <span>基础可用</span><strong>{data.capabilities.ready || 0}</strong>
                <span>部分就绪</span><strong>{data.capabilities.partial || 0}</strong>
                <span>建设中</span><strong>{data.capabilities.building || 0}</strong>
                <span>缺失</span><strong>{data.capabilities.missing || 0}</strong>
              </div>
            </div>

            <div className="card decision-panel">
              <h3>模型与任务</h3>
              <div className="decision-two-col">
                <span>生产版本</span><strong>{data.model.active_version?.version || "未设置"}</strong>
                <span>模型版本数</span><strong>{data.model.version_count}</strong>
                <span>最近训练</span><strong>{data.model.latest_train_run?.status || "—"}</strong>
                <span>失败/暂停任务</span><strong>{failedTasks.length}</strong>
              </div>
            </div>
          </div>

          <div className="card decision-panel">
            <h3>消费者就绪度</h3>
            {consumers.map((item) => (
              <div className="market-theme-row" key={item.key}>
                <strong>{item.name}</strong>
                <span>{statusText(item.status)}</span>
              </div>
            ))}
            {!consumers.length && <div className="empty">暂无消费者就绪度数据</div>}
          </div>
        </>
      )}
    </section>
  );
}
