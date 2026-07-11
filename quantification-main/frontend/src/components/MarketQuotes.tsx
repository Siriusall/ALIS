import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";
import type { Candidate } from "../types";
import { chartTooltipStyle } from "../chartTheme";

interface MarketQuotesProps {
  candidates: Candidate[];
  selectedSymbol: string;
  onSelectSymbol: (symbol: string) => void;
}

function formatNumber(value: number) {
  return Intl.NumberFormat("zh-CN").format(value);
}

export default function MarketQuotes({
  candidates,
  selectedSymbol,
  onSelectSymbol,
}: MarketQuotesProps) {
  const list = Array.isArray(candidates) ? candidates : [];
  const chartData = list.slice(0, 10).map((c) => ({
    name: c.symbol,
    涨跌幅: c.change_pct,
  }));

  return (
    <section id="market" className="dashboard-section" data-section="market">
      <h2 className="dashboard-section-title">市场行情</h2>
      <div className="dashboard-section-inner">
        <div className="chart-wrap chart-fixed-height">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={chartData}
              margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
            >
              <XAxis dataKey="name" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} width={36} />
              <Tooltip
                formatter={(value: number | undefined) => [
                  value != null ? `${value.toFixed(2)}%` : "",
                  "涨跌幅",
                ]}
                  contentStyle={chartTooltipStyle}
              />
              <Bar dataKey="涨跌幅" radius={[4, 4, 0, 0]}>
                {chartData.map((d, i) => (
                  <Cell
                    key={i}
                    fill={d.涨跌幅 >= 0 ? "#22c55e" : "#ef4444"}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>代码</th>
                <th>名称</th>
                <th>价格</th>
                <th>涨跌幅</th>
                <th>成交量</th>
                <th>信心值</th>
              </tr>
            </thead>
            <tbody>
              {list.map((item, idx) => (
                <tr
                  key={`${item.symbol}-${idx}`}
                  className={item.symbol === selectedSymbol ? "active" : ""}
                  onClick={() => onSelectSymbol(item.symbol)}
                >
                  <td>{item.symbol}</td>
                  <td>{item.name}</td>
                  <td>{item.price.toFixed(2)}</td>
                  <td>{item.change_pct.toFixed(2)}%</td>
                  <td>{formatNumber(item.volume)}</td>
                  <td>{item.confidence.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
