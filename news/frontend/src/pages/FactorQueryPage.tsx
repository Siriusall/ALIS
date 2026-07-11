import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchSectors, fetchStocks } from "../api/stocks";
import type { Sector, StockFactor } from "../types";

type FactorFormatter = (value: StockFactor[keyof StockFactor]) => string;
type FactorItem = [label: string, key: keyof StockFactor, formatter: FactorFormatter];
type FactorGroup = { title: string; items: FactorItem[] };

function num(value: StockFactor[keyof StockFactor]): number | null {
  return typeof value === "number" ? value : null;
}

function fmt(value: number | null | undefined, digits = 2, suffix = ""): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `${value.toFixed(digits)}${suffix}`;
}

function fmtPct(value: number | null | undefined): string {
  return fmt(value, 2, "%");
}

function fmtMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (Math.abs(value) >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return value.toFixed(0);
}

const asPct: FactorFormatter = (value) => fmtPct(num(value));
const asNum: FactorFormatter = (value) => fmt(num(value));
const asMoney: FactorFormatter = (value) => fmtMoney(num(value));

const GROUPS: FactorGroup[] = [
  {
    title: "成长能力",
    items: [
      ["营收同比", "revenue_yoy", asPct],
      ["扣非净利同比", "deduct_profit_yoy", asPct],
      ["3年CAGR", "cagr_3y", (v) => fmtPct(typeof v === "number" ? v * 100 : null)],
    ],
  },
  {
    title: "盈利质量",
    items: [
      ["毛利率", "gross_margin", asPct],
      ["净利率", "net_margin", asPct],
      ["ROE", "roe", asPct],
      ["ROIC", "roic", asPct],
    ],
  },
  {
    title: "现金流质量",
    items: [
      ["经营现金流/净利润", "ocf_to_profit", asNum],
      ["自由现金流", "free_cashflow", asMoney],
    ],
  },
  {
    title: "偿债安全",
    items: [
      ["资产负债率", "debt_ratio", asPct],
      ["有息负债", "interest_debt", asMoney],
      ["货币资金覆盖", "cash_coverage", asNum],
    ],
  },
  {
    title: "股东回报",
    items: [
      ["股息率", "dividend_yield", asPct],
      ["分红率", "payout_ratio", asPct],
      ["回购金额", "buyback_amount", asMoney],
    ],
  },
  {
    title: "稳定性",
    items: [
      ["5年业绩波动", "profit_volatility_5y", asNum],
      ["周期性", "cyclicality", (v) => String(v || "-")],
    ],
  },
  {
    title: "估值",
    items: [
      ["PE", "pe", asNum],
      ["PB", "pb", asNum],
      ["PS", "ps", asNum],
      ["股息率", "dividend_yield", asPct],
      ["估值分位", "valuation_percentile", (v) => fmtPct(typeof v === "number" ? v * 100 : null)],
    ],
  },
  {
    title: "机构认可",
    items: [
      ["公募持仓", "fund_holding", asPct],
      ["北向", "northbound", asPct],
      ["ETF", "etf_holding", asPct],
      ["调研次数", "research_count", (v) => (v == null ? "-" : String(v))],
    ],
  },
];

const ALL_FACTOR_ITEMS = GROUPS.flatMap((group) => group.items);

export default function FactorQueryPage() {
  const [sectors, setSectors] = useState<Sector[]>([]);
  const [items, setItems] = useState<StockFactor[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [size] = useState(50);
  const [sector, setSector] = useState("全部");
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<StockFactor | null>(null);

  const sectorTabs = useMemo(() => ["全部", ...sectors.map((s) => s.name)], [sectors]);

  const load = useCallback(async (nextPage = page, silent = false) => {
    if (!silent) {
      setLoading(true);
    }
    setError("");
    try {
      const [sectorRows, stockRows] = await Promise.all([
        fetchSectors(),
        fetchStocks({
          page: nextPage,
          size,
          q: keyword || undefined,
          sector: sector === "全部" ? undefined : sector,
        }),
      ]);
      setSectors(sectorRows);
      setItems(stockRows.items);
      setTotal(stockRows.total);
    } catch {
      setError("数据加载失败，请先在数据爬取页执行爬取。");
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [keyword, page, sector, size]);

  useEffect(() => {
    load(page);
  }, [load, page, sector]);

  useEffect(() => {
    const timer = setInterval(() => {
      load(page, true);
    }, 3000);
    return () => clearInterval(timer);
  }, [load, page]);

  function search() {
    setPage(1);
    load(1);
  }

  return (
    <section className="dashboard-section" data-section="factors">
      <h2 className="dashboard-section-title">因子查询</h2>
      <div className="dashboard-section-inner decision-page">
        <div className="card">
          <div className="quant-analyze-toolbar">
            <input
              placeholder="搜索代码或名称"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") search();
              }}
              style={{ padding: "8px 12px", minWidth: 220 }}
            />
            <button type="button" className="quant-analyze-secondary-btn" onClick={search} disabled={loading}>
              搜索
            </button>
            <span className="text-muted">共 {total} 条</span>
          </div>
          <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
            {sectorTabs.slice(0, 12).map((name) => (
              <button
                key={name}
                type="button"
                className={sector === name ? "quant-analyze-confirm-btn" : "quant-analyze-secondary-btn"}
                onClick={() => {
                  setSector(name);
                  setPage(1);
                }}
              >
                {name}
              </button>
            ))}
          </div>
          {error && <div className="alert" style={{ marginTop: 12 }}>{error}</div>}
        </div>

        <div className="card table-card">
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th rowSpan={2}>代码</th>
                  <th rowSpan={2}>名称</th>
                  <th rowSpan={2}>板块</th>
                  {GROUPS.map((group) => (
                    <th key={group.title} colSpan={group.items.length}>{group.title}</th>
                  ))}
                </tr>
                <tr>
                  {ALL_FACTOR_ITEMS.map(([label, key]) => (
                    <th key={`${key}-${label}`}>{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr
                    key={row.code}
                    className={selected?.code === row.code ? "row-selected" : ""}
                    onClick={() => setSelected(row)}
                    style={{ cursor: "pointer" }}
                  >
                    <td>{row.code}</td>
                    <td>{row.name}</td>
                    <td>{row.sector || "-"}</td>
                    {ALL_FACTOR_ITEMS.map(([, key, formatter]) => (
                      <td key={`${row.code}-${key}`}>{formatter(row[key])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {items.length === 0 && !loading && <div className="empty">暂无数据，请先在数据爬取页执行爬取。</div>}
          </div>
          <div className="quant-analyze-toolbar" style={{ marginTop: 12 }}>
            <button type="button" className="quant-analyze-secondary-btn" disabled={page <= 1 || loading} onClick={() => setPage((p) => p - 1)}>
              上一页
            </button>
            <span className="text-muted">第 {page} 页</span>
            <button
              type="button"
              className="quant-analyze-secondary-btn"
              disabled={page * size >= total || loading}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </button>
          </div>
        </div>

        {selected && (
          <div className="card">
            <h3>{selected.name} ({selected.code}) 详情</h3>
            <div className="decision-two-col">
              <span>市场</span><strong>{selected.market || "-"}</strong>
              <span>板块</span><strong>{selected.sector || "-"}</strong>
              <span>爬取时间</span><strong>{selected.crawled_at || "-"}</strong>
              <span>成长</span><strong>营收 {fmtPct(selected.revenue_yoy)} / 扣非 {fmtPct(selected.deduct_profit_yoy)}</strong>
              <span>盈利</span><strong>ROE {fmtPct(selected.roe)} / ROIC {fmtPct(selected.roic)}</strong>
              <span>估值</span><strong>PE {fmt(selected.pe)} / PB {fmt(selected.pb)} / PS {fmt(selected.ps)}</strong>
              <span>机构</span><strong>公募 {fmtPct(selected.fund_holding)} / 北向 {fmtPct(selected.northbound)} / ETF {fmtPct(selected.etf_holding)}</strong>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
