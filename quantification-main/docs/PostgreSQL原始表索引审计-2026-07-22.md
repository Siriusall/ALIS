# PostgreSQL 原始表索引审计（2026-07-22）

## 审计边界

- 数据库：`alis_sim`、`alis_data`
- 重点表：`dc_tushare_raw_records`、`dc_stock_daily_snapshot`
- 审计方式：只读查询 PostgreSQL 系统目录、索引大小、约束依赖、有效性和累计扫描次数；同时核对后端 SQL 查询模式。
- 本次没有执行 `DROP INDEX`、`REINDEX`、`VACUUM FULL` 或任何数据删除。
- 审计时间均为 UTC+8。

## 结论

1. `alis_data` 当前约 **39 GB**；其中 `dc_tushare_raw_records` 约 **34 GB**，索引约 **19 GB**。
2. PostgreSQL 系统目录中未发现定义完全相同的重复索引。
3. 未发现无效、未就绪或失活索引。
4. 多组索引虽然列名重叠，但列顺序或覆盖字段不同，并且代码中存在对应查询模式，不能直接视为重复。
5. 本轮可以无风险立即释放的索引空间为 **0 GB**。原先“预计释放数 GB”目前没有足够证据支持，不能为了达到空间目标冒险删除。

## 主要索引证据

| 索引 | 大小 | 累计扫描 | 判断 |
|---|---:|---:|---|
| `idx_dc_raw_api_ts_available` | 4.79 GB | 170,791 | 支撑 `api_name + ts_code + available_time` 的时点查询，保留 |
| `dc_tushare_raw_records_api_name_record_key_key` | 4.22 GB | 43,572,158 | 唯一约束，禁止删除 |
| `idx_dc_raw_api_available` | 4.08 GB | 214 | 低频观察候选，但支撑不指定股票的 `api_name + available_time` 查询；暂不删除 |
| `idx_dc_raw_api_date_ts` | 3.10 GB | 1,167 | 与 `(api_name, trade_date)` 有前缀重叠，但承担包含 `ts_code` 的查询；暂不删除 |
| `dc_tushare_raw_records_pkey` | 1.04 GB | 1,062,827 | 主键，禁止删除 |
| `idx_dc_raw_api_period_ts` | 723 MB | 583,143 | 财务期间与股票组合查询，高使用量，保留 |
| `idx_dc_raw_ts_code` | 451 MB | 322,333 | 单独按股票查询不能由以 `api_name` 开头的索引替代，保留 |
| `idx_dc_raw_api_date` | 393 MB | 181,300,130 | 高频日期查询，保留 |
| `idx_dc_raw_api_period` | 380 MB | 200,598 | 高频期间查询，保留 |
| `idx_dc_raw_trade_date` | 371 MB | 350,415 | 单独按交易日查询，保留 |

说明：扫描次数来自 PostgreSQL 累计统计，不等同于最近一天使用量；当前未记录明确的 `stats_reset` 时间，因此不能单靠扫描次数做删除决定。

## 容易误删的“重叠”组合

### `(api_name, trade_date)` 与 `(api_name, trade_date, ts_code)`

长索引理论上能支持前缀查询，但短索引明显更小，且被大量使用。删除短索引可能增加缓存压力和随机读；删除长索引则会影响带股票代码的回测、快照和数据检查查询。两者当前都保留。

### `(api_name, period)` 与 `(api_name, period, ts_code)`

一个用于期间级查询，一个用于期间加股票代码查询，累计使用量均较高，当前都保留。

### `(api_name, available_time, id)` 与 `(api_name, ts_code, available_time, id)`

两者第二列不同，不能互相完整替代。不指定 `ts_code` 时，后者无法直接完成按 `available_time` 的有序范围扫描。4.08 GB 的前者虽然低频，但涉及历史时点和市场状态查询，当前只观察、不删除。

## 后续安全流程

- [ ] 连续记录 14～30 天索引使用增量，而不是依赖累计值。
- [ ] 对 `idx_dc_raw_api_available` 对应的生产 SQL 收集 `EXPLAIN (ANALYZE, BUFFERS)` 基线。
- [ ] 如要验证删除效果，先在恢复出来的测试库执行，不在常驻生产库试删。
- [ ] 只有测试库所有日更、回测、研究、调仓和数据健康流程通过，才提出生产删除方案。
- [ ] 生产删除前保存完整索引定义，并使用 `DROP INDEX CONCURRENTLY`；出现性能回退时用 `CREATE INDEX CONCURRENTLY` 恢复。
- [ ] 单独评估索引膨胀。当前未安装 `pgstattuple`，本轮没有为审计而修改数据库扩展。

## 删除门槛

只有同时满足以下条件才允许删除候选索引：

1. 观察期内扫描增量为零或可以明确对应为已废弃代码。
2. 不属于主键、唯一约束或外键支撑索引。
3. 代码静态检索和实际慢查询中均无必要依赖。
4. 测试库删除前后执行计划与全流程回归通过。
5. 已准备可直接执行的重建 SQL 和性能回滚阈值。

