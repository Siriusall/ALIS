# ALIS 实施冲突与验证登记

> 规则：现有实现与工程计划书不一致时，先登记冲突、证据、影响和迁移方案，再修改代码。未经确认的冲突不允许静默覆盖。

## 当前基线

| 项目 | 当前事实 | 证据 | 状态 |
|---|---|---|---|
| 主后端 | FastAPI，`127.0.0.1:8000` | `quantification-main/backend/app/main.py` | 运行正常 |
| 后端 | 统一FastAPI，`127.0.0.1:8000`；业务库与数据库仍物理分库 | `quantification-main/backend/app/main.py`、`app/data_platform/` | 已按用户最新决议合并 |
| 前端 | 单一React前端5173，数据中台技术台已嵌入主系统 | `quantification-main/frontend/src/components/DataCenterPage.tsx` | 已按用户最新决议合并 |
| 主数据库 | 模拟运行库 PostgreSQL `alis_sim`（由`alis_main`复制后清空交易账本并初始化20万元）；`alis_main`保留历史迁移数据 | `quantification-main/start.sh`、`backend/app/db/store.py` | 当前模拟已独立 |
| 数据中台数据库 | PostgreSQL `alis_data`；SQLite 已删除 | `quantification-main/backend/app/data_platform/db.py`、迁移脚本、`quantification-main/start.sh` | 已迁移、健康检查通过，生产强制 PostgreSQL |
| Python环境 | 现有venv Python 3.11.15 | `backend/.venv/bin/python --version` | 已按用户决议采用 |
| Redis | 本机6379可用 | `lsof`、服务健康检查 | 已有，但当前主要做缓存/简单状态 |
| Redis Streams worker | 已加入常驻消费线程、pending reclaim、成功ACK；失败次数持久化计数，超过重试预算后进入 dead-letter stream；补全任务支持从队列恢复且不会二次入队 | `quantification-main/backend/app/data_platform/redis_store.py`、`services/backfill.py`、统一`app/main.py` | P0基础闭环完成，仍需真实Redis故障注入集成测试 |
| 模拟调仓 | 已有开盘成交模拟、费用和交易约束 | `backend/app/services/execution_engine.py`、`quant_run_engine.py` | 基础能力存在 |
| 模拟执行入口 | quant-run 任务创建后自动启动；服务启动后收盘调度器每5分钟自动续跑模拟任务；旧的手动开仓路由仍保留并受安全门保护 | `quant_task_runner.py`、`quant_runs.py` | 模拟自动化已接入；真实/手动路径未放开 |
| 模拟本金 | `alis_sim.account`已初始化200000元，持仓/成交/任务为空；新建执行/回测默认预算也为20万元 | `alis_sim`、`execution_engine.py`、`schemas/backtest.py` | V1模拟账本已独立 |
| PostgreSQL | PostgreSQL 16.14已安装并启动；主库使用`alis_main`，数据平台使用`alis_data`，当前Python 3.11已安装 psycopg 3.3.4 | `brew services`、`pg_isready`、`psql`、requirements.txt | 两个后端均已接入 |
| 数据平台测试 | Python模块可编译；数据平台pytest已安装并通过6项测试（含6个子测试） | `python -m compileall`、`pytest -q` | 已通过 |

## 用户已确认的冲突处理决议（2026-07-13）

- 数据库冲突：换 PostgreSQL；SQLite 已清理，不再允许生产回退或新增 SQLite 事实库。
- 后端边界：2026-07-13 最新决议覆盖旧决议，数据中台并入8000主FastAPI；`alis_sim`和`alis_data`仍分库管理。
- Python版本：按当前环境 Python 3.11.15 运行；不强制升级3.12，依赖出现问题时再单独处理。
- 任务队列：Redis Streams 必须补齐可靠消费、ACK、pending恢复和失败落库。
- 前端边界：2026-07-13 最新决议覆盖旧决议，仅保留5173，5174技术台整体迁入主前端。
- 模型实现：按工程计划书附录B固定模型栈落地，不以现有旧模型替代计划模型。

## 冲突处理顺序

1. 建立PostgreSQL迁移和双库对账；迁移完成后删除 SQLite 源库。
2. 数据中台路由和Redis Streams worker并入8000；技术台并入5173，退役8100和5174运行入口。
3. 以现有Python 3.11环境完成依赖和运行验证。
4. 将模拟本金、自动执行、成交成本和账本先在现有模拟执行引擎中统一，再改前端按钮语义。
5. Redis Streams worker和pytest环境补齐后，才将P0任务队列标记完成。

## 已验证运行结果

```text
GET http://127.0.0.1:8000/api/health -> {"status":"ok"}
GET http://127.0.0.1:8000/api/data-center/status -> 数据中台状态（统一后端）
quantification-main/backend/scripts/check_core_contracts.py -> [OK] 3项
quantification-main/backend/app/data_platform 与 scripts -> compileall通过
```

## 未完成项

计划书 C12 的构建输入工件已生成：`data_asset_contract.yaml`、`redis_stream_contract.yaml`、`model_feature_label_contract.yaml`、`portfolio_constraints.yaml`、`llm_evidence_schema.json`、`postgresql_partition_inventory.md`、`ADR-0001-v1-baseline.md`。

- PostgreSQL双库迁移已完成；SQLite源库已删除，应用启动时强制使用 PostgreSQL。
- 全自动模拟盘主路径已接入；手动创建/暂停/继续仅作为运维和调试入口保留，不承担普通调仓审批。
- 20万元独立模拟库已初始化；历史`alis_main`保留不动；任务3已真实运行并产生5条模拟运行记录，当前资产约199984.75元，调仓理由写入每条`message/detail_json`。
- 日度自动模拟调度器已接入；策略本身的日度信号质量和完整调仓闭环仍需继续验证。
- 10% 模拟账户权益硬回撤门已接入任务运行器：权益相对持久化高水位回撤超过10%时暂停自动调仓并记录证据。
- 周/月/年绩效聚合接口已接入：`GET /api/quant-runs/performance`，基于已持久化曲线输出策略收益、基准收益和最大回撤。
- 数据平台当前测试集已执行并通过；后续需随P0扩展补充PostgreSQL/Streams集成测试。
- 模型固定构成接口已登记：`GET /api/model-strategy/constitution`。HSMM/BOCPD、局部水平 Kalman、GARCH(1,1)、经验 CVaR 已有可测试的 v1 工具实现，并已接入诊断接口；但尚未成为全市场生产评分、滚动训练和组合优化的完整闭环。
- 横截面训练当前仍以 legacy HGBR/线性回退为主；LightGBM 需显式开启且当前 macOS ARM/OpenMP 环境存在导入阻塞，因此不能宣称已完成 LightGBM 生产落地。
- 收盘批次完成后已自动调用绩效刷新接口并持久化周/月/年报告；若主API暂时不可用，任务不回滚，下一次调度会重试。
- 绩效刷新已改为任务线程延迟调用同进程聚合函数，避免单工作进程下通过HTTP自调用造成连接重置；已有任务4可手动查询并持久化4条周期报告。
- 绩效曲线已修正为“现金+持仓市值”口径，不再只统计股票市值。
- 回撤门已升级为持久化高水位计算；组合CVaR约束与自动减仓仍未完成。
- 仍保留手动任务创建/暂停/继续路径；任务4已完成“信号生成→风险门→调仓→成交→理由写入”的连续两步验收，绩效报告可持久化读取；长期多交易日稳定性仍需持续观察。
- 市场情绪与主题叙事评分 v1 已实现并提供 `/api/model-strategy/market-context`：情绪按宽度、收益、量能、涨跌停结构、资金流固定加权；主题按强度、广度、持续性、来源多样性评分，并执行来源/标的/交易日三项确认门。仍需接入数据中台的真实主题观测和调仓门控。
- 组合分配已加入 `ledoit_wolf_shrinkage_plus_constrained_cvar_v1`，并在无有效观测时降级到 `hrp_inverse_volatility_fallback_v1`；候选标的近60日尾部风险超过5%时按比例降低分配分数，并保存协方差快照和风险权重。仍需正式组合级回测验收。
- 新增 `POST /api/model-strategy/portfolio-risk` 作为组合风险模型的可回放诊断入口，返回优化器、协方差、权重和权重和；该接口已用多标的收益样本验证。正式历史回测与线上连续验收仍未完成。
- PostgreSQL 迁移后自增序列曾落后于复制数据，已在启动时对 `backtests`、执行账本和量化任务表自动执行序列对齐；2026-07-02 至 2026-07-06 的正式回测已成功落库（backtest_id=3），返回收益、回撤、夏普、换手、基准超额和交易约束拦截证据。
- 启动轮询发现标准端口已有进程时会重复拉起服务，已将 `start.sh` 改为幂等启动：健康后端和已占用前端端口不再重复创建实例，清理逻辑只处理本次启动的PID。
- 模型训练已从单次时间切分升级为“holdout + expanding walk-forward”验证；模型晋升同时要求 holdout lift 与 walk-forward 平均 lift 均为正，并记录折数和命中率。
