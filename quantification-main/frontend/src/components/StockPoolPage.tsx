import { useEffect, useMemo, useState } from "react";
import {
  fetchPoolEligibility,
  fetchStockPools,
  removeStockPoolMember,
  saveStockPoolMember,
  stageCandidateWatchPool,
  type PoolEligibility,
  type StockPoolMember,
} from "../api/research";
import { useStockContext } from "../context/StockContext";

const TYPES = [
  { key: "watch", label: "研究观察池", rule: "机会发现后进入，允许证据尚不完整。" },
  { key: "core", label: "核心候选池", rule: "必须通过风险、研究、估值和投资逻辑门控。" },
  { key: "holding", label: "实际持仓池", rule: "由模拟成交和持仓状态机同步，不建议手工维护。" },
] as const;

type PoolType = (typeof TYPES)[number]["key"];

export default function StockPoolPage() {
  const { activeStock, setActiveStock } = useStockContext();
  const [items, setItems] = useState<StockPoolMember[]>([]);
  const [code, setCode] = useState(activeStock);
  const [type, setType] = useState<PoolType>("watch");
  const [reason, setReason] = useState("");
  const [eligibility, setEligibility] = useState<PoolEligibility | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [checking, setChecking] = useState(false);
  const [staging, setStaging] = useState(false);

  async function load() {
    try {
      setItems(await fetchStockPools());
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const grouped = useMemo(
    () => Object.fromEntries(TYPES.map((group) => [group.key, items.filter((item) => item.pool_type === group.key)])),
    [items],
  ) as Record<PoolType, StockPoolMember[]>;

  const normalizedCode = code.trim().toUpperCase();

  async function check() {
    if (!normalizedCode) {
      setError("请输入股票代码");
      return;
    }
    setChecking(true);
    setError("");
    setNotice("");
    setActiveStock(normalizedCode);
    setCode(normalizedCode);
    try {
      setEligibility(await fetchPoolEligibility(normalizedCode));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setEligibility(null);
    } finally {
      setChecking(false);
    }
  }

  async function save() {
    if (!normalizedCode || !reason.trim()) {
      setError("股票代码和准入理由不能为空");
      return;
    }
    if (type === "core" && (!eligibility || eligibility.ts_code !== normalizedCode)) {
      setError("升级核心候选前必须先检查当前证据门控");
      return;
    }
    if (type === "core" && !eligibility?.eligible_for_core) {
      setError("核心候选门控未通过，不能绕过证据直接升级");
      return;
    }
    if (type === "holding") {
      setError("实际持仓池由模拟成交同步，请从组合执行流程建仓");
      return;
    }

    try {
      await saveStockPoolMember(normalizedCode, type, reason.trim());
      setNotice(type === "core" ? "已通过全部门控并进入核心候选池" : "已加入研究观察池");
      setReason("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function stage() {
    setStaging(true);
    setError("");
    try {
      const result = await stageCandidateWatchPool();
      setNotice(`已将 ${result.staged} 个候选放入观察池，尚未升级为核心候选`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStaging(false);
    }
  }

  async function remove(id: number) {
    try {
      await removeStockPoolMember(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const selectedType = TYPES.find((item) => item.key === type);

  return (
    <section className="pool-workspace">
      <div className="product-module-head">
        <div>
          <h1>股票池与准入门控</h1>
          <p>先入观察池，过门再升核心候选。</p>
        </div>
        <button className="button ghost" disabled={staging} onClick={() => void stage()}>
          {staging ? "候选入池中…" : "候选只进入观察池"}
        </button>
      </div>

      {error && <div className="alert">{error}</div>}
      {notice && <div className="decision-issue success">{notice}</div>}

      <div className="product-module-grid">
        {TYPES.map((group) => (
          <article className="product-module-card" key={group.key}>
            <h2>{group.label}</h2>
            <strong>{grouped[group.key]?.length ?? 0}</strong>
            <p>{group.key === "watch" ? "观察" : group.key === "core" ? "核心" : "持仓"}</p>
          </article>
        ))}
      </div>

      <div className="decision-grid">
        <div className="card decision-panel">
          <h3>当前待处理动作</h3>
          <div className="review-queue">
            <div className="review-queue-item">
              <div>
                <strong>先检查当前标的能否升级</strong>
                <span>{normalizedCode || "输入股票代码"} · 当前动作 {selectedType?.label}</span>
              </div>
              <em>{eligibility?.eligible_for_core ? "可升级" : "待检查"}</em>
            </div>
            <div className="review-queue-item">
              <div>
                <strong>优先清理观察池</strong>
                <span>{grouped.watch?.length ?? 0} 个观察标的待处理</span>
              </div>
              <em>观察池</em>
            </div>
            <div className="review-queue-item">
              <div>
                <strong>核心候选进入执行前复核</strong>
                <span>{grouped.core?.length ?? 0} 个核心候选待复核</span>
              </div>
              <em>核心池</em>
            </div>
          </div>
        </div>

        <div className="card decision-panel">
          <h3>当前准入结果</h3>
          {eligibility ? (
            <div className="decision-two-col">
              <span>标的</span>
              <strong>{eligibility.ts_code}</strong>
              <span>核心候选资格</span>
              <strong>{eligibility.eligible_for_core ? "允许升级" : "禁止升级"}</strong>
              <span>投资逻辑数量</span>
              <strong>{eligibility.thesis_count}</strong>
              <span>未通过门数</span>
              <strong>{eligibility.gates.filter((gate) => !gate.passed).length}</strong>
            </div>
          ) : (
            <div className="empty">先检查一只标的，首页再告诉你现在能不能升级。</div>
          )}
        </div>
      </div>

      <div className="pool-gate-console">
        <div className="pool-gate-form">
          <h2>检查并登记标的</h2>
          <p className="text-muted">当前动作：{selectedType?.label}</p>
          <div>
            <input
              aria-label="股票池标的"
              placeholder="000001.SZ"
              value={code}
              onChange={(event) => {
                setCode(event.target.value.toUpperCase());
                setEligibility(null);
              }}
            />
            <select value={type} onChange={(event) => setType(event.target.value as PoolType)}>
              {TYPES.map((group) => (
                <option key={group.key} value={group.key}>{group.label}</option>
              ))}
            </select>
            <button className="button ghost" onClick={() => void check()} disabled={checking}>
              {checking ? "检查中…" : "检查证据门控"}
            </button>
          </div>
          <textarea
            placeholder="记录本次准入或降级理由"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <button
            className="pool-primary-action"
            onClick={() => void save()}
            disabled={type === "core" && !eligibility?.eligible_for_core}
          >
            {type === "core" ? "通过门控后升级核心候选" : type === "holding" ? "由模拟成交同步" : "加入观察池"}
          </button>
        </div>

        <div className="pool-gate-result">
          <header>
            <div>
              <span>CORE POOL GATE</span>
              <h2>核心候选准入</h2>
            </div>
            {eligibility && (
              <strong className={eligibility.eligible_for_core ? "passed" : "blocked"}>
                {eligibility.eligible_for_core ? "允许升级" : "禁止升级"}
              </strong>
            )}
          </header>
          {eligibility ? (
            <div className="pool-gates">
              {eligibility.gates.map((gate) => (
                <article key={gate.key} className={gate.passed ? "passed" : "blocked"}>
                  <i>{gate.passed ? "✓" : "!"}</i>
                  <div>
                    <strong>{gate.label}</strong>
                    <em>{gate.detail}</em>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="research-empty">
              <strong>尚未检查当前标的</strong>
              <span>先跑一次门控。</span>
            </div>
          )}
        </div>
      </div>

      <div className="pool-columns">
        {TYPES.map((group) => (
          <article className="pool-column" key={group.key}>
            <header>
              <div>
                <span>{group.key.toUpperCase()}</span>
                <h2>{group.label}</h2>
              </div>
              <strong>{grouped[group.key]?.length ?? 0}</strong>
            </header>
            <div className="pool-member-list">
              {(grouped[group.key] ?? []).map((item) => (
                <div className="pool-member" key={item.id}>
                  <div>
                    <strong>{item.ts_code}</strong>
                    <em>{item.reason}</em>
                    <time>{item.updated_at?.slice(0, 16)}</time>
                  </div>
                  <button className="button ghost" onClick={() => void remove(item.id)}>移除</button>
                </div>
              ))}
              {!(grouped[group.key] ?? []).length && <div className="empty">暂无标的</div>}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
