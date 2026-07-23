# ALIS 数据接口文档

> 当前文档建立于 2026-07-22（UTC+8）。  
> 下方保留 2026-07-16 TuShare 官方接口审计基线；它描述调用维度、降级和完整性规则，不以接口存在代表数据已经完整。  
> 当前代码中的接口目录、权限探测结果和测试优先于历史实测数字；接口增删后必须同步更新本文件。

## 当前统一接口原则

- 全市场接口优先按交易日、报告期或公告日批量获取。
- 只有官方强制要求股票代码的接口才逐股调用。
- 主源失败必须区分等待上游、限流、权限、认证、网络和真实空结果。
- 网页/OCR 是应急备用源，必须保存来源、置信度和对账状态，不得静默覆盖结构化主源。
- 所有进入训练、回测和决策的数据必须保留点时可用性，禁止未来数据泄漏。
- 决策最新日硬门至少要求 daily、daily_basic、stk_limit 和标准化快照完整。

## 2026-07-16 TuShare 官方接口审计基线

审计日期：2026-07-16。范围为 `TUSHARE_CATALOG` 当前 34 个逻辑接口（其中 7 个在批量场景切换为对应 VIP 入口）。原则：全市场接口优先按交易日、报告期或公告日批量拉取；只有官方要求 `ts_code` 的接口才逐股请求；批量结果必须显式传 `fields` 并做股票、日期、报告期和行数校验。

| 数据域 | 接口 | 官方推荐提取维度 | ALIS执行策略 |
|---|---|---|---|
| 基础 | `stock_basic` | `list_status`，单次覆盖全市场 | 分别同步 L/D/P/G，当前股票池使用 L |
| 基础 | `trade_cal` | `exchange + start_date + end_date` | 按交易所和时间窗批量 |
| 基础 | `namechange` | 股票代码查询历史 | 逐股；空结果代表无更名事件 |
| 行情 | `daily` | `trade_date` 可取单日全市场 | 按交易日批量 |
| 行情 | `weekly` | `trade_date` 可取单周全市场 | 按周末交易日批量 |
| 行情 | `monthly` | `trade_date` 可取单月全市场 | 按月末交易日批量 |
| 行情 | `adj_factor` | `trade_date` 可取单日全市场 | 按交易日批量，禁止全市场逐股 |
| 估值 | `daily_basic` | `trade_date` 可取单日全市场 | 按交易日批量 |
| 交易状态 | `suspend_d` | `trade_date` | 按交易日批量；空结果记 `valid_empty` |
| 交易状态 | `stk_limit` | `trade_date`，单次约覆盖全市场 | 按交易日批量 |
| 资金 | `moneyflow` | `trade_date` 可取单日全市场 | 按交易日批量 |
| 财务 | `income` → `income_vip` | 普通接口逐股；VIP 用 `period` 全市场 | 显式 `fields`，按报告期走 VIP；单接口失败才逐股降级 |
| 财务 | `balancesheet` → `balancesheet_vip` | 普通接口逐股；VIP 用 `period` 全市场 | 显式 `fields`，按报告期走 VIP；单接口失败才逐股降级 |
| 财务 | `cashflow` → `cashflow_vip` | 普通接口逐股；VIP 用 `period` 全市场 | 显式 `fields`，按报告期走 VIP；单接口失败才逐股降级 |
| 财务 | `fina_indicator` → `fina_indicator_vip` | 普通接口逐股；VIP 用 `period` 全市场 | 显式 `fields`，按报告期走 VIP；单接口失败才逐股降级 |
| 财务 | `fina_audit` | `ts_code` 必填 | 逐股年度补全 |
| 财务 | `fina_mainbz` → `fina_mainbz_vip` | 普通接口逐股；VIP 用 `period + type` 全市场 | 历史按报告期走 VIP，失败后逐股降级 |
| 业绩 | `forecast` → `forecast_vip` | 普通接口支持股票/公告日；VIP 用 `period` 全市场 | 历史按报告期走 VIP，失败后逐股降级 |
| 业绩 | `express` → `express_vip` | 普通接口支持股票/公告日；VIP 用 `period` 全市场 | 历史按报告期走 VIP，失败后降级普通接口 |
| 分红 | `dividend` | 股票或公告时间窗 | 日常按公告时间窗增量，历史逐股补漏 |
| 公募基金分红 | `fund_div` | 基金代码、公告日、除息日或派息日 | 持仓 ETF 每周逐基金刷新；模拟账户按登记日资格和派息日入账 |
| 股东 | `top10_holders` | `period` + 显式 `fields` 可批量 | 报告期批量优先，失败才逐股逐报告期 |
| 股东 | `top10_floatholders` | `period` + 显式 `fields` 可批量 | 报告期批量优先，失败才逐股逐报告期 |
| 股东 | `stk_holdernumber` | 公告日期窗，单次上限3000 | 日期窗批量优先，失败才逐股；按实际返回分页 |
| 质押 | `pledge_stat` | `ts_code` | 逐股；无质押可为空 |
| 质押 | `pledge_detail` | `ts_code` | 逐股；无质押可为空 |
| 公告 | `disclosure_date` | `end_date` 可取报告期全市场，单次最大6000 | 按报告期批量 |
| 指数 | `index_basic` | `market` | 按市场批量 |
| 指数 | `index_daily` | 指数代码或交易日 | 核心指数按代码和日期窗 |
| 指数 | `index_weight` | `index_code + 月初/月末` | 每指数每月一次 |
| 宏观 | `cn_gdp` | 全量/季度 | 季度增量 |
| 宏观 | `cn_cpi` | 全量/月度 | 月度增量 |
| 宏观 | `cn_pmi` | 全量/月度 | 月度增量 |
| 宏观 | `shibor` | 日期窗 | 每日增量 |
| 外汇 | `fx_daily` | `ts_code + 日期窗` | 指定汇率代码按日期窗 |

## 已实测的当前凭证能力

- `income_vip(period=20241231)`：6585 行，6162 个代码。
- `balancesheet_vip(period=20241231)`：6736 行，6192 个代码。
- `cashflow_vip(period=20241231)`：7375 行，5646 个代码。
- `fina_indicator_vip(period=20241231)`：7977 行，7029 个代码。
- `express_vip(period=20241231)`：1409 行，1403 个代码。
- `forecast_vip(period=20241231)`：3953 行。
- `fina_mainbz_vip(period=20241231,type=P)`：31599 行。
- 上述 VIP 接口必须显式传 `fields`；仅传 `period` 在当前代理上可能返回空集。

## 完整性规则

- 行情类：`交易日 × 接口`，批量响应覆盖当日合格股票。
- 财务类：`股票 × 报告期 × 接口`，不能以“股票曾出现过”代替报告期完整。
- 事件类：以成功检查的股票/日期窗口为分母；无事件是有效结论，不要求产生事件行。
- 批量响应达到接口上限时必须分页或缩小日期窗口，禁止直接当作完整结果。
- `success`、`valid_empty`、`not_applicable` 可计入完成；`empty_pending`、失败、未请求不得计入。

## 模拟账本审计接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/model-strategy/evolution/owner-summary` | 所有者首页摘要，包含账本状态、现金漂移、持仓漂移数量和异常 |
| `GET` | `/api/model-strategy/evolution/ledger-audit` | 只读返回固定本金、成交、费用、公司行动及显式调整的独立重算结果 |

账本审计接口不会自动修改现金或持仓；未通过时自动成交使用 `paused_simulation_ledger_drift` 硬门阻断。

## 周期复盘与学习接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/quant-runs/performance` | 重新计算并幂等保存日、周、月、年 `formal_performance_report_v2` |
| `GET` | `/api/quant-runs/performance/reports` | 读取已冻结报告；可按 `task_id` 查看任务作用域 |
| `GET` | `/api/model-strategy/performance-reports` | 按 `period_type` 读取正式周期报告 |
| `GET` | `/api/model-strategy/evolution/owner-summary` | 返回最近自治任务的复盘状态、学习信号数量及具体信号 |

每份 v2 报告包含 `benchmark_coverage`、`market_state_breakdown`、`model_version_breakdown`、`decision_version_manifest`、`costs`、`turnover`、`error_breakdown` 和 `learning_signals`。基准缺失日不按零收益处理；模型分组只统计决策、复核和错误，不伪造单模型收益。学习信号只能触发后续 Challenger 与组合治理，不能直接替换 Champion。
