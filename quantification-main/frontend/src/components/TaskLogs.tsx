import type { LogEntry } from "../types";
import { formatGmt8Time } from "../utils/time";

interface TaskLogsProps {
  logs: LogEntry[];
}

export default function TaskLogs({ logs }: TaskLogsProps) {
  return (
    <section id="logs" className="dashboard-section dashboard-section-full" data-section="logs">
      <h2 className="dashboard-section-title">任务日志</h2>
      <div className="dashboard-section-inner logs-scroll">
        <ul className="logs">
          {logs.map((log, idx) => (
            <li key={`${log.timestamp}-${idx}`}>
              <span className="level">{log.level}</span>
              <span>{formatGmt8Time(log.timestamp)}</span>
              <span>{log.message}</span>
            </li>
          ))}
        </ul>
        {logs.length === 0 && (
          <div className="empty">
            暂无日志
            <p className="empty-hint">日志来自选股、执行、账户等操作；后端重启后会清空。</p>
          </div>
        )}
      </div>
    </section>
  );
}
