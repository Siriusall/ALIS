import type { ExecutionResult } from "../types";

interface TradeHistoryProps {
  execution: ExecutionResult | null;
}

function formatNumber(value: number) {
  return Intl.NumberFormat("zh-CN").format(value);
}

export default function TradeHistory({ execution }: TradeHistoryProps) {
  const orders = execution?.orders ?? [];

  return (
    <section id="trade" className="dashboard-section" data-section="trade">
      <h2 className="dashboard-section-title">交易记录（未实现）</h2>
      <div className="dashboard-section-inner">
        {execution && (
          <div className="sub">
            执行时间：{new Date(execution.executed_at).toLocaleString()}
          </div>
        )}
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
                <tr key={`${o.symbol}-${o.price}-${i}`}>
                  <td>{o.symbol}</td>
                  <td>{o.side}</td>
                  <td>{o.price.toFixed(2)}</td>
                  <td>{formatNumber(o.quantity)}</td>
                  <td>{o.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!execution && <div className="empty">尚未执行</div>}
      </div>
    </section>
  );
}
