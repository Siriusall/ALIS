import { useEffect, useState } from "react";
import { fetchFactorLibrary, type FactorLibraryResponse } from "../api/factors";
import LoadingState from "./LoadingState";

const statusLabel: Record<string, string> = {
  active: "生产",
  candidate: "候选",
  degraded: "降级观察",
  blocked_leakage: "泄漏永久阻断",
};

export default function FactorLabPage() {
  const [data, setData] = useState<FactorLibraryResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchFactorLibrary()
      .then(setData)
      .catch((reason) => setError(reason instanceof Error ? reason.message : "因子数据读取失败"));
  }, []);

  if (!data && !error) {
    return <LoadingState variant="page" title="正在打开因子实验室" detail="读取生产因子版本、物化与评价状态" rows={6} />;
  }
  if (!data) return <section className="factor-lab-page"><div className="alert">{error}</div></section>;

  const registry = data.versioned_registry;
  return (
    <section className="factor-lab-page">
      <header className="strategy-lab-head">
        <div>
          <span>FACTOR LAB</span>
          <h1>因子实验室</h1>
          <p>同一注册版本同时供选股、模型训练、回测、策略重放和每日决策使用。</p>
        </div>
        <div className="strategy-lab-verdict pass">
          <small>统一合同</small>
          <strong>{registry.schema_version}</strong>
          <span>点时计算 · 自动评价 · 泄漏永久阻断</span>
        </div>
      </header>

      <div className="strategy-lab-metrics">
        <article><span>生产因子</span><strong>{registry.active_count}</strong><small>已冻结版本</small></article>
        <article><span>候选因子</span><strong>{registry.candidate_count}</strong><small>未过硬门不进入生产</small></article>
        <article><span>物化日期</span><strong>{registry.latest_materialized_date || "等待首轮"}</strong><small>每日 T 收盘截面</small></article>
        <article><span>因子值</span><strong>{registry.value_count.toLocaleString("zh-CN")}</strong><small>可追溯到输入哈希</small></article>
        <article><span>评价记录</span><strong>{registry.evaluation_count}</strong><small>IC / Rank IC / 成本后增量</small></article>
        <article><span>消费链</span><strong>{Object.keys(registry.consumer_contract).length}</strong><small>统一读取方</small></article>
      </div>

      <article className="strategy-lab-card factor-lab-contract">
        <header><div><span>自动治理</span><h2>候选如何成为生产因子</h2></div><small>无需人工挑因子</small></header>
        <div>
          <span>每日点时物化</span><i>→</i><span>覆盖与泄漏检查</span><i>→</i>
          <span>IC / Rank IC</span><i>→</i><span>分组成本后收益</span><i>→</i>
          <span>20 期稳定性门</span><i>→</i><span>晋升或降级</span>
        </div>
        <p>任何未来数据泄漏都会把对应版本标记为永久阻断；模型训练指标好但策略级成本后收益差，也不能晋升。</p>
      </article>

      <article className="strategy-lab-card">
        <header><div><span>版本注册表</span><h2>当前因子定义与状态</h2></div><small>{registry.definitions.length} 个登记版本</small></header>
        <div className="factor-lab-table">
          <table>
            <thead><tr><th>因子</th><th>状态</th><th>周期</th><th>方向</th><th>公式</th><th>输入</th><th>点时/泄漏</th></tr></thead>
            <tbody>
              {registry.definitions.map((item) => (
                <tr key={item.id}>
                  <td><strong>{item.name_cn}</strong><small>{item.factor_key} · {item.version}</small></td>
                  <td><b className={`factor-status ${item.status}`}>{statusLabel[item.status] || item.status}</b></td>
                  <td>{item.window_sessions} 日</td>
                  <td>{item.direction >= 0 ? "正向" : "反向"}</td>
                  <td>{item.formula.expression || "—"}</td>
                  <td>{item.inputs.join("、")}</td>
                  <td><span>{item.available_time_rule}</span><small>{item.leakage_status === "passed" ? "无泄漏" : item.leakage_status}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </article>
    </section>
  );
}
