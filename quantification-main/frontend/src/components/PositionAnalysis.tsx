import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer, Legend } from "recharts";
import { chartTooltipStyle, pieColors } from "../chartTheme";
import type { ExecutionResult } from "../types";

type PositionAnalysisProps = {
  execution?: ExecutionResult | null;
};

function formatNumber(value: number) {
  return Intl.NumberFormat("zh-CN").format(value);
}

const PIE_COLORS = pieColors;

export default function PositionAnalysis({ execution }: PositionAnalysisProps) {
  const orders = execution?.orders ?? [];
  const pieData = orders.map((o) => ({
    name: o.symbol,
    value: o.price * o.quantity,
  }));

  return (
    <section id="position" className="dashboard-section" data-section="position">
      <h2 className="dashboard-section-title">持仓分析（未实现）</h2>
      <div className="dashboard-section-inner">
        <div className="chart-wrap chart-fixed-height">
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  innerRadius="40%"
                  outerRadius="70%"
                  paddingAngle={2}
                  dataKey="value"
                  nameKey="name"
                  label={({ name, percent }) =>
                    `${name} ${((percent ?? 0) * 100).toFixed(0)}%`
                  }
                >
                  {pieData.map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value: number | undefined) => [
                    value != null ? formatNumber(value) : "",
                    "市值",
                  ]}
                  contentStyle={chartTooltipStyle}
                />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="empty">暂无模拟持仓数据，请先执行开盘模拟</div>
          )}
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>代码</th>
                <th>方向</th>
                <th>价格</th>
                <th>数量</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o, i) => (
                <tr key={`${o.symbol}-${i}`}>
                  <td>{o.symbol}</td>
                  <td>{o.side}</td>
                  <td>{o.price.toFixed(2)}</td>
                  <td>{formatNumber(o.quantity)}</td>
                  <td>{o.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {orders.length === 0 && (
            <div className="empty">暂无持仓</div>
          )}
        </div>
      </div>
    </section>
  );
}
