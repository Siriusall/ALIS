# ALIS 计划书实现矩阵

> 基线：《ALIS 长线投资系统工程计划书》V1.0
>
> 更新日期：2026-07-13
> 验收规则：`接口存在`或`页面存在`不等于完成。只有数据、计算、持久化、解释、前端下钻和测试同时具备，才可标记为“已完成”。

## 统一状态定义

| 状态 | 定义 |
|---|---|
| 已完成 | 当前阶段计划要求已经形成端到端闭环，并有测试或运行证据 |
| 基础可用 | 主链路可运行，但仍缺SLO、全量覆盖或生产级验证 |
| 部分实现 | 已有真实接口、数据表或页面，但计划输出尚未贯通 |
| 建设中 | 已有初步代码或页面，核心业务逻辑仍不完整 |
| 未实现 | 尚无可用业务链路，只有规划或占位入口 |

## 前端与业务18模块

| 模块 | 当前实现 | 后端/事实证据 | 状态 | 未完成项 |
|---|---|---|---|---|
| 市场总览 | 已重构为市场环境、情绪、风险预算、主题可用性、账户回撤、模型和执行聚合页，并回填逐日透明状态历史 | `/api/decision/summary`、`market-context/history`、账户与执行账本 | 部分实现 | 真实HSMM状态概率、合格主题观测；当前透明状态机对缺失观测明确降级 |
| 数据中台 | 七模块技术工作台已并入主前端5173 | `/api/data-center/*`、PostgreSQL `alis_data`、Redis Streams | 基础可用 | 全接口长期正确性、统一SLO、空间保护、对象存储和Parquet归档 |
| 股票研究中心 | 已有独立页面，可读取真实财务、行情、来源期和可用时间，输出覆盖率受控的多维研究状态 | `/api/research/stocks/{ts_code}`、`research_score_snapshots`、`research_engine.py` | 部分实现 | 完整行业化模板、治理/现金流字段覆盖和跨期证据快照自动化 |
| 行业研究中心 | 已有独立页面、行业指标列表和基础生命周期评分 | `/api/research/industries*`、行业指标定义/数值表、`industry_event_engine.py` | 部分实现 | 动态因子/Kalman行业状态、公司映射、行业内相对位置和真实景气驱动 |
| 公告事件中心 | 已有独立页面，真实公告原文、规则事件分类、半衰期衰减和跟进状态已入库展示 | `/api/research/events*`、`dc_announcement_raw/event`、CNINFO 增量同步 | 部分实现 | 完整事件类型、原文证据校验、自动触发投资逻辑复核及长期跟踪闭环 |
| 市场情绪中心 | 已有独立页面，使用真实涨跌宽度、收益、成交额和涨跌停结构 | `/api/model-strategy/market-context/live`、`dc_market_context_snapshot` | 部分实现 | 北向/替代资金、龙头持续性、基本面背离等合格观测及完整历史曲线 |
| 主题叙事中心 | 有导航和真实能力状态页，评分原语已实现 | `score_theme_narratives`、市场总览主题确认门 | 建设中 | 自动主题发现、生命周期、扩散图、公司真实业务暴露和多来源观测持久化 |
| 预期变化中心 | 已有独立页面，可从真实财务数据生成基础趋势、低置信度季节性预测基线，并接入带披露日期的公司业绩预告；预告与一致预期严格区分，缺少财报时仅展示为不可评分证据 | `/api/research/expectations/{ts_code}`、`earnings_forecast_baselines`、Tushare `forecast` 原始记录 | 建设中 | 外部一致预期、超预期判断、分析师修正广度、1/2/4季度状态跟踪 |
| 估值中心 | 已有独立页面，可用 PostgreSQL 历史截面计算 PE/PB 历史分位、同业比较和相对三情景 | `/api/research/valuations/{ts_code}`、`valuation_snapshots`、`dc_stock_daily_snapshot` | 部分实现 | 分类估值模型、现金流估值、安全边际门控和长周期快照版本化 |
| 股票池与筛选中心 | 候选筛选、三级池事实表和候选自动进入观察池均已存在；确认模拟买卖会同步持仓池，并沿受限状态机写入持仓/退出审计历史 | `/api/candidates`、`/api/research/pools`、`/api/research/position-state`、`stock_pool_memberships`、`position_state_history` | 部分实现 | 核心候选的证据门控升级、自动淘汰和完整解释贯通 |
| 投资逻辑中心 | 已有逻辑卡片、假设和证据状态，并提供显式贝叶斯复核接口 | `/api/research/theses*`、`/api/research/theses/bayesian-review`、`investment_theses` | 部分实现 | 自动证据采集、定时复核、催化剂与自动失效判定仍未闭环 |
| 组合管理中心 | 持仓、账户、模拟成交和 CVaR/HRP 分配器可用，并输出透明 BL-like 目标权重与调整项 | `/api/positions`、`/api/model-strategy/black-litterman`、组合风险接口 | 部分实现 | 完整协方差 Black-Litterman、行业/资产约束统一执行和真实再平衡解释 |
| 持仓风险中心 | 有组合风险、逐仓逻辑、压力测试、减仓建议，并提供单笔成交归因接口 | `/api/research/portfolio-risk/live`、`/api/research/attribution/trade`、`position-state` | 部分实现 | 真实成交批次接入、自动减仓闭环和长期压力参数校准 |
| 回测与模型验证 | 支持 holdout、expanding walk-forward，并提供多周期统一样本外聚合验收接口 | `/api/backtest*`、`/api/model-strategy/oos/report`、模型/回测表 | 部分实现 | 全生产模型冻结验收集、组合级长期验证和统计显著性 |
| 决策日志 | 决策快照自动生成证据哈希/版本字段，可查询并校验冻结上下文 | `/api/research/decisions`、`/api/research/evidence/freeze`、`decision_snapshots` | 部分实现 | 冻结全部输入数据的可重放存档和跨版本差异校验 |
| 复盘中心 | 已有独立复盘页面，可读取冻结决策、创建复盘记录，并根据 PostgreSQL 决策后行情预填方向性价格表现；实际成交收益与完整归因仍明确区分 | `/api/research/decisions`、`/api/research/reviews`、`/api/research/reviews/suggestion/{id}`、`decision_snapshots`、`review_records` | 部分实现 | 匹配成交批次的真实收益、基准/行业/选股/执行归因、错误改进任务的追踪闭环 |
| 系统任务/质量/模型治理 | 数据任务、质量、失败队列、漂移与模型状态可见，并提供统一 SLO 评估接口 | data-center、`/api/model-strategy/slo/evaluate`、Redis Streams、模型版本表 | 部分实现 | 长期 SLO 样本、告警、审计和运行状态总览仍需生产窗口验收 |
| 策略工作台 | 训练、比较、漂移诊断、晋升门和有限参数搜索已有前端入口；自动调优与回退均强制 holdout、walk-forward 和正向 lift 门禁 | train/compare/drift/promote/auto-tune/rollback API、模型版本表 | 部分实现 | 完整Champion/Challenger差异报告、长期自动调优调度和全量样本外验收 |

### 模块统计

| 状态 | 数量 |
|---|---:|
| 已完成 | 0 |
| 基础可用 | 1 |
| 部分实现 | 13 |
| 建设中 | 3 |
| 未实现 | 1 |
| 合计 | 18 |

说明：当前仍有1个能力明确未实现，不代表其余模块接近完成。多数模块只有部分链路；按计划书完整验收标准，18个模块目前均仍有未完成项。

## 后端里程碑

| 里程碑 | 计划书完成定义 | 当前状态 | 当前判断 |
|---|---|---|---|
| M0 / P0 | 数据资产、全量补全、日常增量、质量、失败队列、空间保护 | 基础可用 | PostgreSQL双库、生产禁用SQLite、Redis Streams、补数和质量链路已具备；长期正确性、SLO、空间保护及冷存储未完成 |
| M1 / P1 | 风险过滤、企业分类、行业模板、公告事件、多维研究状态 | 部分实现 | 研究接口、事实表和部分页面存在；真实数据覆盖、行业化模板、跨期证据和事件跟踪尚未闭环 |
| M2 / P2 | 市场状态、情绪、主题、预期变化和动态门控 | 部分实现 | 透明状态机、情绪观测和风险预算门已进入市场总览；正式HSMM、主题发现、预期版本和完整门控未贯通 |
| M3 / P3 | 投资逻辑、估值、股票池、组合、持仓状态机、快照复盘 | 部分实现 | 模拟执行、逻辑/假设、股票池、决策快照、复盘表和接口已存在；估值、自动验证、回放与组合解释未闭环 |
| M4 / P4 | 模型版本、样本外验证、漂移、降级、LLM证据、Champion/Challenger | 部分实现 | walk-forward、漂移、比较、晋升和有限调参存在；完整回退、LLM证据治理、冻结验收集和长期调度未完成 |

## 当前固定实施顺序

1. 补齐P1真实研究证据：行业化企业研究、公告跟踪、历史研究快照。
2. 补齐P2真实观测：主题发现、主题公司映射、预期版本和正式市场状态训练数据。
3. 贯通P3：估值、安全边际、投资逻辑自动复核、组合动作、决策冻结和复盘回放。
4. 统一P4治理：SLO、模型降级与回退、Champion/Challenger、LLM证据和冻结验收集。
5. 未通过样本外、漂移和组合风险门控的模型，不得自动晋升。
