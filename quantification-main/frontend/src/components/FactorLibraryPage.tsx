import { useEffect, useMemo, useState } from "react";
import { fetchFactorLibrary, type FactorItem, type FactorLibraryResponse } from "../api/factors";

function boolTag(ok: boolean, yes = "是", no = "—") {
  return ok ? yes : no;
}

export default function FactorLibraryPage() {
  const [data, setData] = useState<FactorLibraryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [category, setCategory] = useState("全部");

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      setData(await fetchFactorLibrary());
    } catch {
      setError("因子库加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const categories = useMemo(() => {
    if (!data) return ["全部"];
    return ["全部", ...Object.keys(data.categories).sort()];
  }, [data]);

  const rows: FactorItem[] = useMemo(() => {
    if (!data) return [];
    if (category === "全部") return data.factors;
    return data.factors.filter((f) => f.category === category);
  }, [category, data]);

  return (
    <section id="factorLibrary" className="dashboard-section" data-section="factorLibrary">
      <h2 className="dashboard-section-title">因子库</h2>
      <div className="dashboard-section-inner decision-page">
        <div className="card">
          <div className="quant-analyze-section-head">
            <div>
              <h3>已注册因子</h3>
              <p className="text-muted" style={{ margin: "6px 0 0", fontSize: 13 }}>
                汇总因子引擎、回测引擎、模型训练与 all-in-one 缓存中的因子定义，便于核对口径与覆盖范围。
              </p>
            </div>
            <button type="button" className="quant-analyze-secondary-btn" onClick={refresh} disabled={loading}>
              {loading ? "刷新中…" : "刷新"}
            </button>
          </div>
          {error && <div className="alert">{error}</div>}
          <div className="metrics-row" style={{ marginTop: 12 }}>
            <div className="card metric-card">
              <div className="sub">因子总数</div>
              <div className="metric">{data?.total ?? "—"}</div>
            </div>
            <div className="card metric-card">
              <div className="sub">模型训练特征</div>
              <div className="metric">{data?.model_training_features.length ?? "—"}</div>
            </div>
            <div className="card metric-card">
              <div className="sub">分类数</div>
              <div className="metric">{data ? Object.keys(data.categories).length : "—"}</div>
            </div>
          </div>
        </div>

        <div className="card decision-panel">
          <div className="quant-analyze-toolbar" style={{ marginBottom: 12 }}>
            {categories.map((cat) => (
              <button
                key={cat}
                type="button"
                className={category === cat ? "quant-analyze-confirm-btn" : "quant-analyze-secondary-btn"}
                onClick={() => setCategory(cat)}
              >
                {cat}
                {cat !== "全部" && data?.categories[cat] != null ? ` (${data.categories[cat]})` : ""}
              </button>
            ))}
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>因子键</th>
                  <th>名称</th>
                  <th>分类</th>
                  <th>说明</th>
                  <th>计算口径</th>
                  <th>因子引擎</th>
                  <th>回测</th>
                  <th>模型训练</th>
                  <th>缓存覆盖</th>
                  <th>需历史K线</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((f) => (
                  <tr key={f.key}>
                    <td><code>{f.key}</code></td>
                    <td>{f.name_cn}</td>
                    <td>{f.category}</td>
                    <td style={{ minWidth: 220 }}>{f.description || "—"}</td>
                    <td style={{ fontSize: 12 }}><code>{f.formula_hint || "—"}</code></td>
                    <td>{boolTag(f.in_factor_engine)}</td>
                    <td>{boolTag(f.in_backtest)}</td>
                    <td>{boolTag(f.in_model_training)}</td>
                    <td>{boolTag(f.in_cache_coverage)}</td>
                    <td>{boolTag(f.needs_history)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && !loading && <div className="empty">暂无因子数据</div>}
          </div>
        </div>
      </div>
    </section>
  );
}
