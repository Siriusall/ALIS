# ALIS 计划书实现审计（当前版本）

| 计划层级 | 验收项 | 当前证据 | 状态 |
|---|---|---|---|
| P0 | PostgreSQL 双库、生产禁用 SQLite | `start.sh`、数据库初始化、SQLite 文件清理 | 已完成 |
| P0 | Redis Streams ACK/pending/retry/dead-letter | `quantification-main/backend/app/data_platform/redis_store.py`、6项测试 | 已完成，待真实故障注入 |
| P0 | 数据来源、增量补全、失败续跑 | `backfill.py`、数据资产契约 | 基础完成，接口覆盖持续验收 |
| P1 | 风险、财务、行业、公告基础引擎 | 现有服务与数据中台路由 | 基础完成，需真实数据质量验收 |
| P2 | HSMM/BOCPD/Kalman/GARCH/CVaR | 诊断接口、评分门控、组合风险接口 | 已接入 V1 |
| P2 | 情绪与主题叙事 | `/api/model-strategy/market-context` | V1完成，待真实观测接入 |
| P3 | 自动模拟调仓与解释 | 任务4连续两步、detail_json、绩效报告 | 已验证，待长期运行 |
| P3 | 高水位回撤与组合约束 | `quant_risk.py`、CVaR/HRP分配 | 已接入，待多周期回测 |
| P4 | Holdout + expanding walk-forward 晋升 | `model_training.py` | 已完成 |
| P4 | Champion/Challenger、漂移治理、自动调优 | 模型版本表、walk-forward 晋升、`/api/model-strategy/drift` PSI 漂移诊断、`/api/model-strategy/promote/{id}` 晋升门、`/api/model-strategy/compare` 对比门、`/api/model-strategy/auto-tune` 候选编排、训练期固定参数网格、策略页调优按钮 | 部分完成：候选评估和有限参数搜索已完成，长期自动调优仍未完成 |

## 当前禁止宣称完成的项目

1. 全市场所有数据接口均已正确返回。
2. LightGBM 已在当前 macOS ARM 环境稳定生产运行。
3. 组合模型已通过长期、多周期、样本外回测。
4. 模拟盘已连续运行足够长时间证明稳定性。
5. P4 的长期自动调优调度周期已完成验证。

该文件是当前实现状态，不替代计划书，也不把“接口存在”误判为“生产验收完成”。
