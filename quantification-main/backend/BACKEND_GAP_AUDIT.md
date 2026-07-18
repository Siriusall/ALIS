# 后端缺口审计

更新时间：2026-07-17

依据：

- `backend/app/services/backend_acceptance.py`
- `backend/app/services/market_context.py`
- `backend/app/api/routes/decision.py`
- `backend/app/services/quant_risk.py`
- `backend/app/services/quant_task_runner.py`
- `backend/app/data_platform/services/daily_update.py`
- `backend/app/services/research_engine.py`
- 本轮新增测试结果

说明：

- 这份清单只记录“现在还没闭环”的后端项。
- 已完成的闭环不再按旧问题重复记为缺口。
- 结论以当前代码和测试为准，不沿用旧判断。

## 当前总判断

当前后端不是“主链路不可用”，而是：

- 决策主链路已经补上关键硬门控；
- HSMM 影子观察期与验收门槛已经比之前闭环很多；
- 剩余主要是计划书里的扩展能力和统一验收视图。

一句话判断：

- 主流程：可用
- 关键风险门：已显著补强
- 计划书完全闭环：仍未完成

## 本轮已补齐，不再列为旧缺口

### 1. HSMM 不再只看 OOS 是否通过

已完成：

- `market_state_model_governance()` 现在会区分：
  - `shadow_oos_validated_live_observation_pending`
  - `shadow_live_observation`
  - `shadow_live_observation_review_ready`
  - 以及验证失败 / 历史不足等状态
- 新增 live observation 证据字段：
  - `live_observation_start`
  - `live_observation_end`
  - `live_observation_weeks`
  - `minimum_live_observation_weeks`

影响：

- HSMM 已经不再是“只要 OOS passed 就算差不多了”。

### 2. P4_HSMM 验收标准已经收紧

已完成：

- `backend_acceptance_matrix()` 现在要求：
  - 只有 `shadow_live_observation_review_ready` 才算 `passed`
  - 仅 OOS 通过但影子观察不足时是 `partial`

影响：

- 计划书里的“进入真实影子期”不再被弱化成存在性检查。

### 3. 决策门控已补“最新交易日三件套硬拦截”

已完成：

- `/api/decision/summary` 现在会检查最新交易日是否同时完成：
  - `daily`
  - `daily_basic`
  - `stk_limit`
  - `dc_stock_daily_snapshot`
- 若缺任一项，直接：
  - `tone = stop`
  - `reason_code = latest_market_day_not_materialized`

影响：

- 不会再出现“数据库里有旧数据，但最新交易日没走完，系统却进入调仓门控”的错误流程。

### 4. 自动量化任务也接入了同一条硬门

已完成：

- `evaluate_latest_market_day_gate()` 已加入量化任务启动流程
- 最新交易日未物化时：
  - 自动任务 pause
  - 尝试触发收盘日更

影响：

- 现在不只是前端 stop，后台自动流程也不会越门运行。

### 5. 日更状态已能区分“有快照”与“可决策”

已完成：

- `daily_update_status()` / `simple_health()` 新增：
  - `latest_decision_ready_trade_date`

影响：

- 系统现在能明确知道：
  - 最新快照到哪天
  - 真正可用于决策的是哪天

## 当前仍然存在的核心缺口

## P0：仍影响计划书完全闭环

### 1. HSMM 上线评审还没有真正落地成“晋升流程”

现状：

- 影子观察状态、达标状态、验收部分已经补上；
- 但“达标后如何进入正式上线评审 / 谁批准 / 如何记录审批结果”还没有形成完整治理流程。

当前证据：

- `market_context.py` 已能返回 `shadow_live_observation_review_ready`
- 但 `production_eligible` 仍然固定是 `False`
- 也没有独立的“上线评审完成”状态

结论：

- HSMM 这项已从“明显缺失”降到“最后一段晋升治理未闭环”

建议优先级：

- 高

### 2. 多周期统一验收仍未完成

现状：

- `research_engine.capability_registry()` 中：
  - `validation` 仍是 `partial`
  - 缺口仍写的是：`多周期统一验收`

结论：

- 这是当前最明确仍未补掉的计划书验收项之一

建议优先级：

- 高

### 3. 周/月/年绩效与归因的“强验收”仍偏弱

现状：

- `P3_REPORTS` 现在仍然只检查 `quant_performance_reports` 是否存在记录
- 这不能证明：
  - 周/月/年都齐
  - 回撤/成本/归因字段都齐

结论：

- 这项现在更像“存在性通过”，不是“计划书强验收”

建议优先级：

- 高

## P1：主流程可用，但计划书扩展能力仍是 partial

### 4. 股票研究中心：全市场扩展财务字段仍未完全补齐

当前证据：

- `research_engine.capability_registry()`：
  - `stock_research = partial`
  - 缺口：`全市场两年扩展财务字段仍需逐公司补齐`

结论：

- 单股研究可用
- 全市场覆盖还没有完全达标

### 5. 行业研究中心：行业专属序列仍不足

当前证据：

- `industry = partial`
- 缺口：`产品价格、产能、政策供给等行业专属序列`

结论：

- 行业状态已有框架
- 但计划书要求的行业专属证据仍没补齐

### 6. 公告事件中心：文本抽取验收未闭环

当前证据：

- `events = partial`
- 缺口：`回购、合同、重组和定增的全量文本抽取验收`

结论：

- 事件中心能用
- 抽取覆盖与验收还不够硬

### 7. 主题叙事中心：文本聚类与主营暴露仍未补齐

当前证据：

- `themes = partial`
- 缺口：
  - `政策/新闻文本聚类`
  - `主营收入级业务暴露`

### 8. 预期变化中心：缺一致预期源

当前证据：

- `expectation = partial`
- 缺口：`分析师一致预期源`

### 9. 系统治理：SLO 统一视图仍未完成

当前证据：

- `governance = partial`
- 缺口：`SLO统一视图`

## 当前真实完成度判断

按“主流程能不能安全用”看：

- 已达到较高完成度

按“计划书是否完全闭环”看：

- 仍明显未完成

更准确的说法是：

- 决策门控、日更顺序、影子观察期这些真正危险的坑，已经补掉了一批
- 现在剩下的主要是：
  - 治理最终闭环
  - 多周期统一验收
  - 扩展研究能力的计划书覆盖

## 建议的下一优先级

建议顺序：

1. HSMM 最后一段“上线评审 / 晋升治理”
2. 多周期统一验收
3. 报告完整性强验收
4. 股票研究扩展财务字段缺口盘点
5. 行业专属序列最小模板
6. 公告抽取验收闭环
7. SLO 统一视图

## 当前可验证证据

本轮已通过的新增测试：

- `backend/tests/test_market_context_governance.py`
- `backend/tests/test_backend_acceptance.py`
- `backend/tests/test_decision_gate.py`
- `backend/tests/test_quant_risk_latest_market_day.py`
- `backend/tests/test_daily_update_status.py`

验证命令：

- `PYTHONPATH=backend python -m pytest backend/tests/test_market_context_governance.py backend/tests/test_backend_acceptance.py backend/tests/test_decision_gate.py backend/tests/test_quant_risk_latest_market_day.py backend/tests/test_daily_update_status.py -q`

结果：

- `8 passed`
