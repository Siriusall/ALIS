import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";
import type { Overview } from "../types";
import type { Candidate } from "../types";
import { chartLinePrimary, chartTooltipStyle } from "../chartTheme";

interface DashboardOverviewProps {
  overview: Overview | null;
  candidates: Candidate[];
}

export default function DashboardOverview({
  overview,
  candidates,
}: DashboardOverviewProps) {
  const topSymbols = (Array.isArray(candidates) ? candidates : [])
    .slice(0, 5)
    .map((c) => c.symbol)
    .join(", ");

  const chartData = (Array.isArray(candidates) ? candidates : [])
    .slice(0, 10)
    .map((c) => ({ name: c.symbol, 信心值: c.confidence }));

  return (
    <section id="overview" className="dashboard-section" data-section="overview">
      <h2 className="dashboard-section-title">全局概览</h2>
      <div className="dashboard-section-inner">
        <div className="metrics-row">
          <div className="card metric-card">
            <h3>候选数</h3>
            <div className="metric">{overview?.total_candidates ?? "--"}</div>
          </div>
          <div className="card metric-card">
            <h3>平均信心值</h3>
            <div className="metric">{overview?.avg_confidence ?? "--"}</div>
          </div>
          <div className="card metric-card">
            <h3>今日最强信号</h3>
            <div className="metric">{overview?.top_signal || "--"}</div>
            <div className="sub">{topSymbols}</div>
          </div>
          <div className="card metric-card">
            <h3>最后刷新</h3>
            <div className="metric">
              {overview?.last_run
                ? new Date(overview.last_run).toLocaleString()
                : "--"}
            </div>
          </div>
        </div>
        <div className="chart-wrap chart-fixed-height">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <XAxis dataKey="name" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} width={32} />
              <Tooltip
                formatter={(value: number | undefined) => [
                  value != null ? value.toFixed(2) : "",
                  "信心值",
                ]}
                contentStyle={chartTooltipStyle}
              />
              <Bar dataKey="信心值" radius={[4, 4, 0, 0]}>
                {chartData.map((_, i) => (
                  <Cell key={i} fill={chartLinePrimary} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </section>
  );
}
