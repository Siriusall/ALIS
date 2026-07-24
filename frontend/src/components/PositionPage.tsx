import { useEffect, useMemo, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import type { Position, Operation } from "../types";
import { formatGmt8Time, toGmt8ISOString } from "../utils/time";
import type { SectionId } from "./Layout";
import LoadingState from "./LoadingState";
import {
  addPosition as apiAddPosition,
  deletePosition,
  fetchPositions,
  fetchOperations,
  fetchStockNames,
  sellPosition as apiSellPosition,
  updatePosition as apiUpdatePosition,
} from "../api/positions";
import { fetchPositionsEquityCurve } from "../api/strategies";
import { chartGridStroke, chartLinePrimary, chartTooltipStyle } from "../chartTheme";

type TabId = "positions" | "operations" | "chart";

interface PositionPageProps {
  onNavigate?: (section: SectionId) => void;
}

function formatNumber(value: number) {
  return Intl.NumberFormat("zh-CN").format(value);
}

function formatAmount(value: number) {
  return Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function formatTime(iso: string | null | undefined) {
  return formatGmt8Time(iso);
}

function loadPositions(
  setPositions: (p: Position[]) => void,
  setDataSourceTime?: (t: string | null) => void
) {
  fetchPositions()
    .then((res) => {
      setPositions(res.positions ?? []);
      if (setDataSourceTime) setDataSourceTime(res.data_source_time ?? null);
    })
    .catch(() => {
      setPositions([]);
      if (setDataSourceTime) setDataSourceTime(null);
    });
}

export default function PositionPage({ onNavigate }: PositionPageProps) {
  const [tab, setTab] = useState<TabId>("positions");
  const [positions, setPositions] = useState<Position[]>([]);
  const [operations, setOperations] = useState<Operation[]>([]);
  const [curveData, setCurveData] = useState<{ date: string; total_value: number; by_symbol: Record<string, number> }[]>([]);
  const [curveLoading, setCurveLoading] = useState(false);
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);

  const [addSymbol, setAddSymbol] = useState("");
  const [addQuantity, setAddQuantity] = useState("");
  const [addPrice, setAddPrice] = useState("");
  const [adding, setAdding] = useState(false);
  const [positionError, setPositionError] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editQuantity, setEditQuantity] = useState("");
  const [editPrice, setEditPrice] = useState("");
  const [savingId, setSavingId] = useState<number | null>(null);
  const [dataSourceTime, setDataSourceTime] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState(() => new Date());
  const [sellingId, setSellingId] = useState<number | null>(null);
  /** 仅对接口未返回名称的代码补请求一次，避免旧数据/手动添加的持仓没有名称 */
  const [fetchedNames, setFetchedNames] = useState<Record<string, string>>({});

  useEffect(() => {
    loadPositions(setPositions, setDataSourceTime);
  }, []);

  useEffect(() => {
    const t = setInterval(() => setCurrentTime(new Date()), 60_000);
    return () => clearInterval(t);
  }, []);

  const symbolsInCurve =
    curveData.length > 0 && curveData[0].by_symbol
      ? Object.keys(curveData[0].by_symbol)
      : [];

  /** 优先用接口返回的名称（选股时已写入），缺的再补请求 */
  const symbolNames = useMemo(() => {
    const m: Record<string, string> = {};
    positions.forEach((p) => {
      if (p.name) m[p.symbol] = p.name;
    });
    operations.forEach((o) => {
      if (o.name) m[o.symbol] = o.name;
    });
    return m;
  }, [positions, operations]);

  const symbolsMissingName = useMemo(() => {
    const have = new Set(Object.keys(symbolNames).filter((s) => symbolNames[s]));
    const out: string[] = [];
    positions.forEach((p) => {
      if (p.symbol && !have.has(p.symbol)) out.push(p.symbol);
    });
    operations.forEach((o) => {
      if (o.symbol && !have.has(o.symbol)) out.push(o.symbol);
    });
    symbolsInCurve.forEach((s) => {
      if (s && !have.has(s)) out.push(s);
    });
    return [...new Set(out)];
  }, [positions, operations, symbolsInCurve, symbolNames]);

  useEffect(() => {
    if (symbolsMissingName.length === 0) return;
    fetchStockNames(symbolsMissingName)
      .then((res) => setFetchedNames((prev) => ({ ...prev, ...res })))
      .catch(() => {});
  }, [symbolsMissingName.join(",")]);

  const displayNames = useMemo(
    () => ({ ...symbolNames, ...fetchedNames }),
    [symbolNames, fetchedNames]
  );

  useEffect(() => {
    fetchOperations().then(setOperations).catch(() => setOperations([]));
  }, []);

  useEffect(() => {
    if (tab !== "chart") {
      setCurveData([]);
      return;
    }
    setCurveLoading(true);
    fetchPositionsEquityCurve()
      .then((res) => setCurveData(res.series ?? []))
      .catch(() => setCurveData([]))
      .finally(() => setCurveLoading(false));
  }, [tab]);

  const chartData = curveData.map((d) => {
    const row: Record<string, string | number> = {
      date: d.date,
      total_value: d.total_value,
    };
    if (selectedSymbol && d.by_symbol[selectedSymbol] != null) {
      row[selectedSymbol] = d.by_symbol[selectedSymbol];
    }
    return row;
  });

  const totalReturnPct =
    curveData.length >= 2 && curveData[0].total_value
      ? ((curveData[curveData.length - 1].total_value - curveData[0].total_value) /
          curveData[0].total_value) *
        100
      : null;

  const singleSymbolReturnPct =
    selectedSymbol &&
    curveData.length >= 2 &&
    curveData[0].by_symbol[selectedSymbol] != null &&
    curveData[0].by_symbol[selectedSymbol] !== 0
      ? ((curveData[curveData.length - 1].by_symbol[selectedSymbol] -
          curveData[0].by_symbol[selectedSymbol]) /
          curveData[0].by_symbol[selectedSymbol]) *
        100
      : null;

  const totalCost = useMemo(
    () => positions.reduce((sum, p) => sum + p.price * p.quantity, 0),
    [positions]
  );
  const totalCurrentValue = useMemo(
    () => positions.reduce((sum, p) => sum + (p.current_value ?? p.price * p.quantity), 0),
    [positions]
  );
  const totalProfitLoss = totalCurrentValue - totalCost;
  const totalProfitLossPct = totalCost > 0 ? (totalProfitLoss / totalCost) * 100 : null;
  const largestPosition = useMemo(() => {
    if (positions.length === 0 || totalCurrentValue <= 0) return null;
    return positions
      .map((p) => ({ ...p, weight: (p.current_value ?? p.price * p.quantity) / totalCurrentValue }))
      .sort((a, b) => b.weight - a.weight)[0];
  }, [positions, totalCurrentValue]);

  return (
    <section id="position" className="dashboard-section" data-section="position">
      <h2 className="dashboard-section-title">组合管理</h2>
      <div className="dashboard-section-inner decision-page">
        <div className="product-module-head">
          <div>
            <h1>持仓、模拟执行和风险约束</h1>
            <p>这里看持仓、执行和风险。</p>
          </div>
          <button type="button" className="button ghost" onClick={() => loadPositions(setPositions, setDataSourceTime)}>
            刷新组合
          </button>
        </div>
        <div className="text-muted" style={{ marginBottom: "0.75rem", fontSize: "14px" }}>
          <span>当前时间：{formatTime(toGmt8ISOString(currentTime))}</span>
          <span style={{ marginLeft: "1rem" }}>行情/数据源时间：{formatTime(dataSourceTime ?? undefined)}</span>
        </div>
        <div className="product-module-grid">
          <article className="product-module-card">
            <h2>持仓数量</h2>
            <strong>{positions.length}</strong>
            <p>当前权益标的</p>
          </article>
          <article className="product-module-card">
            <h2>组合市值</h2>
            <strong>{formatAmount(totalCurrentValue)}</strong>
            <p>按当前价格估算</p>
          </article>
          <article className="product-module-card">
            <h2>估算盈亏</h2>
            <strong style={{ color: totalProfitLoss >= 0 ? "#16a34a" : "#dc2626" }}>
              {totalCost > 0 ? `${totalProfitLoss >= 0 ? "+" : ""}${formatAmount(totalProfitLoss)}` : "—"}
            </strong>
            <p>{totalProfitLossPct == null ? "暂无成本基准" : `${totalProfitLossPct >= 0 ? "+" : ""}${totalProfitLossPct.toFixed(2)}%`}</p>
          </article>
          <article className="product-module-card">
            <h2>最大单票</h2>
            <strong>{largestPosition ? `${largestPosition.symbol} ${(largestPosition.weight * 100).toFixed(1)}%` : "—"}</strong>
            <p>看集中度</p>
          </article>
        </div>
        <div className="decision-command-bar" aria-label="组合执行下一步">
          <div className="decision-command-intro">
            <span>EXECUTION LOOP</span>
            <strong>组合动作必须留下证据</strong>
          </div>
          <button type="button" onClick={() => onNavigate?.("executionLedger")}>
            <span>01 · 执行账本</span>
            <strong>逐笔核对</strong>
          </button>
          <button type="button" className={largestPosition && largestPosition.weight >= 0.25 ? "urgent" : ""} onClick={() => onNavigate?.("positionRisk")}>
            <span>02 · 持仓风险</span>
            <strong>{largestPosition ? `${(largestPosition.weight * 100).toFixed(1)}%` : "—"}</strong>
          </button>
          <button type="button" onClick={() => onNavigate?.("validation")}>
            <span>03 · 再平衡预览</span>
            <strong>模拟前置</strong>
          </button>
          <button type="button" onClick={() => onNavigate?.("review")}>
            <span>04 · 复盘归因</span>
            <strong>闭环</strong>
          </button>
        </div>
        <div className="strategy-tabs" style={{ marginBottom: "1rem" }}>
          <button
            type="button"
            className={tab === "positions" ? "active" : ""}
            onClick={() => setTab("positions")}
          >
            当前组合
          </button>
          <button
            type="button"
            className={tab === "operations" ? "active" : ""}
            onClick={() => setTab("operations")}
          >
            执行记录
          </button>
          <button
            type="button"
            className={tab === "chart" ? "active" : ""}
            onClick={() => setTab("chart")}
          >
            组合曲线
          </button>
        </div>

        {tab === "positions" && (
          <>
            <div className="card" style={{ marginBottom: "1rem" }}>
              <h3>模拟账户校准</h3>
              <div className="decision-message" style={{ marginBottom: 12 }}>
                这是低频校准动作，只用于修正本地模拟账户。正式买卖仍应通过“今日决策 → 模拟执行 → 执行账本”留下证据链。
              </div>
              {positionError && <div className="alert">{positionError}</div>}
              <div className="position-calibration-form">
                <input
                  type="text"
                  placeholder="代码"
                  value={addSymbol}
                  onChange={(e) => setAddSymbol(e.target.value.trim().toUpperCase())}
                />
                <input
                  type="number"
                  placeholder="数量"
                  min={100}
                  step={100}
                  value={addQuantity}
                  onChange={(e) => setAddQuantity(e.target.value)}
                />
                <input
                  type="number"
                  placeholder="成本价"
                  min={0}
                  step={0.01}
                  value={addPrice}
                  onChange={(e) => setAddPrice(e.target.value)}
                />
                <button
                  type="button"
                  disabled={adding || !addSymbol || !addQuantity || !addPrice}
                  onClick={async () => {
                    setPositionError("");
                    setAdding(true);
                    try {
                      await apiAddPosition(addSymbol, Number(addQuantity), Number(addPrice));
                      setAddSymbol("");
                      setAddQuantity("");
                      setAddPrice("");
                      loadPositions(setPositions, setDataSourceTime);
                    } catch (e: unknown) {
                      setPositionError((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "模拟账户校准失败");
                    } finally {
                      setAdding(false);
                    }
                  }}
                  className="button primary"
                >
                  {adding ? "校准中…" : "登记校准"}
                </button>
              </div>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>成本价</th>
                    <th>数量</th>
                    <th>市值（成本）</th>
                    <th>当前价</th>
                    <th>当前市值</th>
                    <th>盈亏</th>
                    <th>盈亏%</th>
                    <th>策略下一步</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr
                      key={p.id}
                      role="button"
                      tabIndex={0}
                      title="点击查看该股票收益曲线"
                      style={{ cursor: "pointer" }}
                      onClick={(e) => {
                        const row = e.currentTarget;
                        const lastCell = row.cells[row.cells.length - 1];
                        if (lastCell?.contains(e.target as Node)) return;
                        setTab("chart");
                        setSelectedSymbol(p.symbol);
                      }}
                      onKeyDown={(e) => {
                        if (e.key !== "Enter" && e.key !== " ") return;
                        const row = e.currentTarget;
                        const lastCell = row.cells[row.cells.length - 1];
                        if (lastCell?.contains(document.activeElement)) return;
                        setTab("chart");
                        setSelectedSymbol(p.symbol);
                      }}
                    >
                      <td>{displayNames[p.symbol] ? `${displayNames[p.symbol]} ${p.symbol}` : p.symbol}</td>
                      {editingId === p.id ? (
                        <>
                          <td>
                            <input
                              type="number"
                              min={0}
                              step={0.01}
                              value={editPrice}
                              onChange={(e) => setEditPrice(e.target.value)}
                              style={{ width: "72px", padding: "4px" }}
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              min={0}
                              step={100}
                              value={editQuantity}
                              onChange={(e) => setEditQuantity(e.target.value)}
                              style={{ width: "80px", padding: "4px" }}
                            />
                          </td>
                          <td>{formatNumber(Number(editPrice || 0) * Number(editQuantity || 0))}</td>
                          <td>—</td>
                          <td>—</td>
                          <td>—</td>
                          <td>—</td>
                          <td>{p.strategy_next ?? "—"}</td>
                          <td>
                            <button
                              type="button"
                              disabled={savingId === p.id}
                              onClick={async () => {
                                setSavingId(p.id);
                                try {
                                  await apiUpdatePosition(p.id, {
                                    quantity: Number(editQuantity) || 0,
                                    price: Number(editPrice) || 0,
                                  });
                                  setEditingId(null);
                                  loadPositions(setPositions, setDataSourceTime);
                                } catch {
                                  setEditingId(null);
                                  loadPositions(setPositions, setDataSourceTime);
                                } finally {
                                  setSavingId(null);
                                }
                              }}
                              style={{ marginRight: "6px", padding: "4px 8px", fontSize: "12px", cursor: "pointer" }}
                            >
                              保存
                            </button>
                            <button type="button" onClick={() => setEditingId(null)} style={{ padding: "4px 8px", fontSize: "12px", cursor: "pointer" }}>
                              取消
                            </button>
                          </td>
                        </>
                      ) : (
                        <>
                          <td>{p.price.toFixed(2)}</td>
                          <td>{formatNumber(p.quantity)}</td>
                          <td>{formatNumber(p.price * p.quantity)}</td>
                          <td>{p.current_price != null ? p.current_price.toFixed(2) : "—"}</td>
                          <td>{p.current_value != null ? formatNumber(p.current_value) : "—"}</td>
                          <td>
                            {p.profit_loss != null ? (
                              <span style={{ color: p.profit_loss >= 0 ? "#22c55e" : "#f87171" }}>
                                {p.profit_loss >= 0 ? "+" : ""}{formatNumber(p.profit_loss)}
                              </span>
                            ) : "—"}
                          </td>
                          <td>
                            {p.profit_loss_pct != null ? (
                              <span style={{ color: p.profit_loss_pct >= 0 ? "#22c55e" : "#f87171" }}>
                                {p.profit_loss_pct >= 0 ? "+" : ""}{p.profit_loss_pct.toFixed(2)}%
                              </span>
                            ) : "—"}
                          </td>
                          <td style={{ fontSize: "12px", maxWidth: "180px" }}>{p.strategy_next ?? "—"}</td>
                          <td>
                            <button
                              type="button"
                              onClick={() => {
                                setEditingId(p.id);
                                setEditQuantity(String(p.quantity));
                                setEditPrice(String(p.price));
                              }}
                              style={{ marginRight: "6px", padding: "4px 8px", fontSize: "12px", cursor: "pointer" }}
                            >
                              校准
                            </button>
                            <button
                              type="button"
                              disabled={sellingId === p.id}
                              onClick={async () => {
                                const msg = `确定以当前价${p.current_price != null ? ` ${p.current_price.toFixed(2)} 元` : ""}卖出 ${displayNames[p.symbol] ? `${displayNames[p.symbol]} ${p.symbol}` : p.symbol} 的全部持仓（${formatNumber(p.quantity)} 股）？`;
                                if (!confirm(msg)) return;
                                setSellingId(p.id);
                                setPositionError("");
                                try {
                                  await apiSellPosition(p.id, {});
                                  loadPositions(setPositions, setDataSourceTime);
                                  fetchOperations().then(setOperations).catch(() => {});
                                } catch {
                                  setPositionError("模拟卖出失败");
                                } finally {
                                  setSellingId(null);
                                }
                              }}
                              style={{ marginRight: "6px", padding: "4px 8px", fontSize: "12px", cursor: "pointer", background: "#16a34a", color: "#ffffff", border: "none", borderRadius: "4px" }}
                            >
                              {sellingId === p.id ? "模拟卖出中…" : "模拟卖出"}
                            </button>
                            <button
                              type="button"
                              onClick={async () => {
                                if (confirm(`确定删除 ${displayNames[p.symbol] ? `${displayNames[p.symbol]} ${p.symbol}` : p.symbol} 的持仓？`)) {
                                  try {
                                    await deletePosition(p.id);
                                    loadPositions(setPositions, setDataSourceTime);
                                  } catch {
                                    setPositionError("删除失败");
                                  }
                                }
                              }}
                              style={{ padding: "4px 8px", fontSize: "12px", cursor: "pointer", color: "#f87171" }}
                            >
                              删除
                            </button>
                          </td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
              {positions.length === 0 && (
                <div className="empty">暂无持仓。请先从今日决策/量化验证生成模拟执行，必要时再用校准入口补录初始模拟账户。</div>
              )}
            </div>
          </>
        )}

        {tab === "operations" && (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>代码</th>
                  <th>方向</th>
                  <th>价格</th>
                  <th>数量</th>
                </tr>
              </thead>
              <tbody>
                {operations.map((o) => (
                  <tr key={o.id}>
                    <td>{o.at.slice(0, 19).replace("T", " ")}</td>
                    <td>{displayNames[o.symbol] ? `${displayNames[o.symbol]} ${o.symbol}` : o.symbol}</td>
                    <td>{o.side}</td>
                    <td>{o.price.toFixed(2)}</td>
                    <td>{formatNumber(o.quantity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {operations.length === 0 && (
              <div className="empty">暂无执行记录。新的模拟执行会进入“模拟执行账本”，这里保留历史操作流水。</div>
            )}
          </div>
        )}

        {tab === "chart" && (
          <>
            {selectedSymbol && (
              <div style={{ marginBottom: "0.75rem", fontSize: "15px" }}>
                <a
                  href={`https://stockpage.10jqka.com.cn/${selectedSymbol.replace(/\.(SZ|SH)$/i, "").slice(0, 6)}/`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: "#2563eb", textDecoration: "underline" }}
                >
                  {displayNames[selectedSymbol] ? `${displayNames[selectedSymbol]} ${selectedSymbol}` : selectedSymbol}
                </a>
                <span className="text-muted" style={{ marginLeft: "0.5rem", fontSize: "13px" }}>在同花顺查看</span>
              </div>
            )}
            <div style={{ display: "flex", gap: "1rem", marginBottom: "1rem", flexWrap: "wrap" }}>
              {symbolsInCurve.length > 0 && (
                <label>
                  单股折线：
                  <select
                    value={selectedSymbol ?? ""}
                    onChange={(e) => setSelectedSymbol(e.target.value || null)}
                  >
                    <option value="">仅总持仓</option>
                    {symbolsInCurve.map((sym) => (
                      <option key={sym} value={sym}>
                        {displayNames[sym] ? `${displayNames[sym]} ${sym}` : sym}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            {selectedSymbol
              ? singleSymbolReturnPct != null && (
                  <div style={{ marginBottom: "0.5rem", fontSize: 13 }}>
                    单股涨跌幅：
                    <span
                      style={{
                        color: singleSymbolReturnPct >= 0 ? "#22c55e" : "#f87171",
                      }}
                    >
                      {singleSymbolReturnPct >= 0 ? "+" : ""}
                      {singleSymbolReturnPct.toFixed(2)}%
                    </span>
                  </div>
                )
              : totalReturnPct != null && (
                  <div style={{ marginBottom: "0.5rem", fontSize: 13 }}>
                    总持仓涨跌幅：
                    <span
                      style={{ color: totalReturnPct >= 0 ? "#22c55e" : "#f87171" }}
                    >
                      {totalReturnPct >= 0 ? "+" : ""}
                      {totalReturnPct.toFixed(2)}%
                    </span>
                  </div>
                )}
            {curveLoading ? (
              <LoadingState title="正在读取组合曲线" detail="正在计算持仓收益与基准走势" />
            ) : chartData.length === 0 ? (
              <div className="empty">暂无持仓或曲线数据，请先形成模拟持仓。</div>
            ) : (
              <div className="chart-wrap chart-fixed-height">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={chartData}
                    margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke={chartGridStroke} />
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                    <YAxis
                      tick={{ fontSize: 11 }}
                      width={48}
                      domain={["dataMin", "dataMax"]}
                    />
                    <Tooltip
                      formatter={() => ""}
                      labelFormatter={() => ""}
                      content={({ active, payload, label }) => {
                        if (!active || !payload || payload.length === 0) return null;
                        const raw = Number(payload[0]?.value ?? 0);
                        const marketValue = Number.isFinite(raw) ? raw : 0;
                        let currentPriceText = "—";
                        let changePctText = "—";
                        let changePctValue: number | null = null;

                        if (selectedSymbol) {
                          const pos = positions.find((p) => p.symbol === selectedSymbol);
                          const qty = pos?.quantity ?? 0;
                          const costPrice = pos?.price ?? 0;
                          if (qty > 0) {
                            const currentPrice = marketValue / qty;
                            currentPriceText = currentPrice.toFixed(2);
                            if (costPrice > 0) {
                              changePctValue = ((currentPrice - costPrice) / costPrice) * 100;
                              changePctText = `${changePctValue >= 0 ? "+" : ""}${changePctValue.toFixed(2)}%`;
                            }
                          }
                        } else if (totalCost > 0) {
                          changePctValue = ((marketValue - totalCost) / totalCost) * 100;
                          changePctText = `${changePctValue >= 0 ? "+" : ""}${changePctValue.toFixed(2)}%`;
                        }

                        return (
                          <div
                            style={{
                              background: "#ffffff",
                              border: "1px solid #e2e8f0",
                              padding: "8px 10px",
                              borderRadius: 6,
                              fontSize: 12,
                              color: "#111827",
                              boxShadow: "0 4px 12px rgba(15, 23, 42, 0.08)",
                            }}
                          >
                            <div style={{ color: "#64748b", marginBottom: 4 }}>{String(label ?? "")}</div>
                            <div>市值：{formatAmount(marketValue)}</div>
                            <div>现价：{currentPriceText}</div>
                            <div>
                              涨跌幅：
                              <span style={{ color: changePctValue == null ? "#64748b" : changePctValue >= 0 ? "#16a34a" : "#dc2626" }}>
                                {changePctText}
                              </span>
                            </div>
                          </div>
                        );
                      }}
                              contentStyle={chartTooltipStyle}
                    />
                    <Legend />
                    {selectedSymbol ? (
                      <Line
                        type="monotone"
                        dataKey={selectedSymbol}
                        name={
                          displayNames[selectedSymbol]
                            ? `${displayNames[selectedSymbol]} ${selectedSymbol}`
                            : selectedSymbol
                        }
                        stroke="#34d399"
                        dot={false}
                        strokeWidth={2}
                      />
                    ) : (
                      <Line
                        type="monotone"
                        dataKey="total_value"
                        name="总持仓市值"
                        stroke={chartLinePrimary}
                        dot={false}
                        strokeWidth={2}
                      />
                    )}
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
