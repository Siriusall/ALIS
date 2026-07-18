import { useEffect, useMemo, useState } from "react";
import { diffDecisionEvidence, fetchDecisionAudit, verifyDecisionEvidence, type DecisionAuditOverview, type DecisionEvidenceDiff, type DecisionEvidenceVerification } from "../api/research";
import { formatGmt8Time } from "../utils/time";

function text(value: unknown) {
  if (value == null || value === "") return "—";
  return typeof value === "string" || typeof value === "number" ? String(value) : JSON.stringify(value);
}

export default function DecisionAuditPage() {
  const [data, setData] = useState<DecisionAuditOverview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [verification, setVerification] = useState<Record<number, DecisionEvidenceVerification>>({});
  const [compareIds, setCompareIds] = useState<[number | null, number | null]>([null, null]);
  const [diff, setDiff] = useState<DecisionEvidenceDiff | null>(null);

  async function load() {
    setLoading(true);
    try {
      setData(await fetchDecisionAudit());
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

  const evidenceIssues = (data?.quality.future_data_violations ?? 0) + (data?.quality.missing_evidence ?? 0);
  const auditStatus = useMemo(() => {
    if (!data) return "加载中";
    if (evidenceIssues > 0) return "证据异常";
    if (data.counts.awaiting_review > 0) return "待复盘";
    return "闭环正常";
  }, [data, evidenceIssues]);

  async function verify(id: number) {
    try {
      setVerification((value) => ({ ...value, [id]: { ok: false, id, verified: false, status: "checking", schema_version: "" } }));
      const result = await verifyDecisionEvidence(id);
      setVerification((value) => ({ ...value, [id]: result }));
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }

  async function compare() {
    if (!compareIds[0] || !compareIds[1]) return;
    try { setDiff(await diffDecisionEvidence(compareIds[0], compareIds[1])); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }

  return (
    <section className="product-module-page">
      <div className="product-module-head">
        <div>
          <span>DECISION AUDIT</span>
          <h1>决策快照与证据审计</h1>
          <p>这里只检查三件事：有没有未来函数，有没有缺证据，有没有漏复盘。</p>
        </div>
        <button className="button ghost" onClick={() => void load()} disabled={loading}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="alert">{error}</div>}

      {data && (
        <>
          <div className="product-module-grid">
            <article className="product-module-card">
              <h2>审计状态</h2>
              <strong>{auditStatus}</strong>
              <p>当前快照是否已进入可复盘状态</p>
            </article>
            <article className="product-module-card">
              <h2>决策快照</h2>
              <strong>{data.counts.decisions}</strong>
              <p>已冻结的正式判断数量</p>
            </article>
            <article className="product-module-card">
              <h2>复盘记录</h2>
              <strong>{data.counts.reviews}</strong>
              <p>已完成的复盘条数</p>
            </article>
            <article className="product-module-card">
              <h2>证据异常</h2>
              <strong>{evidenceIssues}</strong>
              <p>未来函数与缺证据总数</p>
            </article>
          </div>

          <div className="decision-command-bar" aria-label="决策审计门控">
            <div className="decision-command-intro">
              <span>AUDIT GATES</span>
              <strong>不允许没有证据的结论进复盘</strong>
            </div>
            <button type="button" className={data.quality.future_data_violations > 0 ? "urgent" : ""}>
              <span>01 · 未来函数</span>
              <strong>{data.quality.future_data_violations}</strong>
            </button>
            <button type="button" className={data.quality.missing_evidence > 0 ? "urgent" : ""}>
              <span>02 · 缺少证据</span>
              <strong>{data.quality.missing_evidence}</strong>
            </button>
            <button type="button" className={data.counts.awaiting_review > 0 ? "active" : ""}>
              <span>03 · 待复盘</span>
              <strong>{data.counts.awaiting_review}</strong>
            </button>
            <button type="button">
              <span>04 · 已闭环</span>
              <strong>{data.counts.reviews}</strong>
            </button>
          </div>

          <div className="card decision-panel">
            <h3>冻结决策记录</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr><th>时间</th><th>标的</th><th>动作</th><th>数据截至</th><th>模型</th><th>证据</th><th>理由</th></tr>
                </thead>
                <tbody>
                  {data.decisions.map((raw, index) => {
                    const item = raw as Record<string, unknown>;
                    const id = Number(item.id);
                    const result = verification[id];
                    return (
                      <tr key={String(item.id ?? index)}>
                        <td>{formatGmt8Time(String(item.decision_time ?? ""))}</td>
                        <td>{text(item.ts_code)}</td>
                        <td>{text(item.decision_type)}</td>
                        <td>{formatGmt8Time(String(item.data_as_of_time ?? ""))}</td>
                        <td>{text(item.model_version)}</td>
                        <td><button type="button" className="button ghost" onClick={() => void verify(id)} disabled={!id || result?.status === "checking"}>{result?.status === "verified" ? "已验证" : result?.status === "legacy_schema_unverifiable" ? "旧版" : result?.status === "integrity_mismatch" ? "异常" : "校验"}</button></td>
                        <td>{text(item.reason)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {!data.decisions.length && <div className="empty">尚无正式决策快照</div>}
          </div>
          {data.decisions.length >= 2 && <div className="card decision-panel"><h3>冻结快照差异比较</h3><p className="sub">只比较已冻结内容，不读取当前实时数据。</p><div className="stock-research-search"><select aria-label="左侧决策快照" value={compareIds[0] ?? ""} onChange={(event) => setCompareIds((ids) => [event.target.value ? Number(event.target.value) : null, ids[1]])}><option value="">选择较早快照</option>{data.decisions.map((raw) => { const item = raw as Record<string, unknown>; return <option key={`left-${String(item.id)}`} value={String(item.id)}>{String(item.id)} · {String(item.ts_code)} · {String(item.decision_time).slice(0, 10)}</option>; })}</select><select aria-label="右侧决策快照" value={compareIds[1] ?? ""} onChange={(event) => setCompareIds((ids) => [ids[0], event.target.value ? Number(event.target.value) : null])}><option value="">选择较晚快照</option>{data.decisions.map((raw) => { const item = raw as Record<string, unknown>; return <option key={`right-${String(item.id)}`} value={String(item.id)}>{String(item.id)} · {String(item.ts_code)} · {String(item.decision_time).slice(0, 10)}</option>; })}</select><button type="button" onClick={() => void compare()} disabled={!compareIds[0] || !compareIds[1]}>比较冻结差异</button></div>{diff && <div className="decision-issue-list">{diff.changed ? diff.changes.slice(0, 20).map((change) => <div className="decision-issue warning" key={change.path}><strong>{change.path}</strong>：{text(change.left)} → {text(change.right)}</div>) : <div className="workspace-clear">两个快照的冻结字段一致。</div>}</div>}</div>}

          <div className="card decision-panel">
            <h3>复盘记录抽样</h3>
            <div className="table-scroll">
              <table>
                <thead><tr><th>复盘时间</th><th>决策</th><th>错误类型</th><th>改进结论</th></tr></thead>
                <tbody>
                  {data.reviews.map((raw, index) => {
                    const item = raw as Record<string, unknown>;
                    return (
                      <tr key={String(item.id ?? index)}>
                        <td>{formatGmt8Time(String(item.review_time ?? item.created_at ?? ""))}</td>
                        <td>{text(item.decision_snapshot_id)}</td>
                        <td>{text(item.error_type)}</td>
                        <td>{text(item.lessons)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {!data.reviews.length && <div className="empty">尚无复盘记录。</div>}
          </div>
        </>
      )}
    </section>
  );
}
