# PostgreSQL V1 分区与索引清单

业务状态表（账户、策略、持仓、任务、事件状态）保持普通表并使用主键/外键索引。

时间序列表按 `trade_date` 或 `available_time` 规划月/年分区：

- `daily_price`
- `daily_basic`
- `financial_indicator`
- `factor_value`
- `valuation_snapshot`
- `quant_run_task_runs`
- `quant_performance_reports`

核心索引：

- `(ts_code, trade_date)` 唯一索引；
- `(available_time, source)` 时间一致性索引；
- `(task_id, execute_date)` 任务断点索引；
- `(period_type, period, task_id)` 绩效幂等索引。

具体分区大小按线上行数和查询延迟指标扩容，不在 V1 预先引入 ClickHouse。
