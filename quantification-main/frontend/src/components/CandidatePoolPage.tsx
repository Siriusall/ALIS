import { useEffect, useMemo, useState } from "react";
import { fetchPoolEligibility, fetchStockPools, type PoolEligibility, type StockPoolMember } from "../api/research";
import SecurityLabel from "./SecurityLabel";

function sourceText(reason: string) {
  if (reason.includes("auto_promote_from_watch")) return "量化预筛与研究评分";
  if (reason.includes("candidate_stage_v2")) return "观察池按研究分晋级";
  if (reason.includes("transition_from_watch")) return "观察池晋级";
  return "系统候选流程";
}

export default function CandidatePoolPage() {
  const [all, setAll] = useState<StockPoolMember[]>([]);
  const [selected, setSelected] = useState("");
  const [eligibility, setEligibility] = useState<PoolEligibility | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchStockPools().then((items) => {
      setAll(items);
      const first = items.find((item) => item.pool_type === "candidate")?.ts_code || "";
      setSelected(first);
    }).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason))).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setChecking(true);
    fetchPoolEligibility(selected).then(setEligibility).catch((reason) => setError(reason instanceof Error ? reason.message : String(reason))).finally(() => setChecking(false));
  }, [selected]);

  const candidates = useMemo(() => all.filter((item) => item.pool_type === "candidate"), [all]);
  const counts = useMemo(() => ({
    watch: all.filter((item) => item.pool_type === "watch").length,
    candidate: candidates.length,
    core: all.filter((item) => item.pool_type === "core").length,
  }), [all, candidates]);

  return (
    <section className="candidate-page">
      <header className="candidate-page-head">
        <div><span>04 · CANDIDATE POOL</span><h1>候选池</h1><p>这里回答两件事：有哪些股票，以及它们为什么会到这里。</p></div>
        <strong>{loading ? "—" : candidates.length}<small>当前候选</small></strong>
      </header>

      {error && <div className="alert">{error}</div>}

      <div className="candidate-flow" aria-label="候选产生流程">
        <article><em>01</em><strong>全市场</strong><span>沪深股票快照</span></article>
        <i>→</i>
        <article><em>02</em><strong>量化预筛</strong><span>基础过滤与因子评分</span></article>
        <i>→</i>
        <article><em>03</em><strong>观察池</strong><span>{counts.watch} 只等待分层</span></article>
        <i>→</i>
        <article className="active"><em>04</em><strong>候选池</strong><span>{counts.candidate} 只补研究证据</span></article>
        <i>→</i>
        <article><em>05</em><strong>核心池</strong><span>{counts.core} 只可参与新买入</span></article>
      </div>

      <div className="candidate-policy">
        <strong>进入候选池</strong><span>通过基础交易过滤，并按研究评分从观察池晋级。</span>
        <strong>进入核心池</strong><span>还需同时通过主数据、风险、财务完整性、多维研究、估值和投资逻辑六道门。</span>
        <strong>进入调仓方案</strong><span>核心池只是可选范围，仍需行业预算、市场状态和组合风险共同允许。</span>
      </div>

      <div className="candidate-main-grid">
        <div className="candidate-table-card">
          <header><h2>当前候选名单</h2><span>点击查看晋升阻塞项</span></header>
          <div className="candidate-table">
            <div className="candidate-table-row candidate-table-header"><span>标的</span><span>候选来源</span><span>入池时间</span><span>下一步</span></div>
            {candidates.map((item) => (
              <button key={item.id} className={`candidate-table-row${selected === item.ts_code ? " selected" : ""}`} onClick={() => setSelected(item.ts_code)}>
                <strong><SecurityLabel code={item.ts_code} /></strong>
                <span>{sourceText(item.reason)}</span>
                <time>{item.entered_at?.slice(0, 10) || "—"}</time>
                <em>检查核心门控</em>
              </button>
            ))}
            {!loading && !candidates.length && <div className="empty">当前没有候选标的。</div>}
          </div>
        </div>

        <aside className="candidate-gate-card">
          <header><div><span>核心池晋升检查</span><h2>{selected ? <SecurityLabel code={selected} /> : "未选择"}</h2></div>{eligibility && <b className={eligibility.eligible_for_core ? "passed" : "blocked"}>{eligibility.eligible_for_core ? "可晋升" : "暂不晋升"}</b>}</header>
          {checking ? <div className="empty">正在核对研究证据…</div> : eligibility ? (
            <div className="candidate-gates">
              {eligibility.gates.map((gate) => <div key={gate.key} className={gate.passed ? "passed" : "blocked"}><i>{gate.passed ? "✓" : "!"}</i><p><strong>{gate.label}</strong><span>{gate.detail}</span></p></div>)}
            </div>
          ) : <div className="empty">从左侧选择一只候选股。</div>}
        </aside>
      </div>
    </section>
  );
}
