# ALIS 长线投资研究与模拟交易系统

ALIS 是面向 A 股长线投资的研究型平台，连接行情、财务、公告、行业、估值、组合、风险、决策快照和复盘。系统用于研究与模拟交易，不承诺投资收益；真实交易必须经过人工确认。

## 当前架构

- `backend/`：FastAPI 后端、任务调度、研究与模拟交易接口
- `frontend/`：React + Vite 管理看板
- `backend/app/data_platform/`：数据采集、质量、补全、失败队列和 Redis Streams
- PostgreSQL：主业务库 `alis_sim`、数据中台库 `alis_data`
- Redis：任务队列、运行日志和缓存协调
- TuShare：行情、财务和其他数据来源
- `docs/`：计划书、设计、契约、实现矩阵、审计和对话记录

生产运行只使用 PostgreSQL，不回退 SQLite。

## 本地配置

复制模板并填写配置：

```powershell
Copy-Item .env.example .env
```

至少配置：

```env
TUSHARE_TOKEN=你的Token
DATABASE_URL=postgresql://用户名:密码@127.0.0.1:5432/alis_sim
DATA_PLATFORM_DATABASE_URL=postgresql://用户名:密码@127.0.0.1:5432/alis_data
JWT_SECRET=请替换为随机密钥
```

`.env` 含数据库密码、JWT 密钥和 TuShare Token，禁止提交到 GitHub；`.env.example` 才是可共享模板。

## Windows 启动

推荐在 `quantification-main` 目录执行：

```powershell
.\start.ps1
```

它会启动 Redis、后端和前端。也可以分别启动：

```powershell
.\run_backend.ps1
cd frontend
npm run dev
```

双击 `run_backend.bat` 也可以启动后端。首次安装依赖：

```powershell
npm install
npm run install:frontend
npm run install:backend
```

## 服务地址

- 后端 API：http://127.0.0.1:8000
- Swagger：http://127.0.0.1:8000/docs
- 前端：http://127.0.0.1:5173
- 健康检查：http://127.0.0.1:8000/api/health

健康检查应返回 `ready: true`，并显示两个 PostgreSQL 数据库和 Redis 均可用。

## 数据补全

数据中台支持：

1. 生成数据补全计划；
2. 点击执行后按依赖和优先级自动执行；
3. 记录成功、失败、阻塞和重试；
4. 服务重启后继续未完成任务；
5. 通过 TuShare 可获取的数据持续补全。

接口入口：

- `/api/data-center/backfill/plan`
- `/api/data-center/backfill/run`
- `/api/data-center/backfill/status`
- `/api/data-center/status`

“全部完成”取决于 TuShare 权限、接口覆盖、历史数据可用性和限流；数据源不存在的数据无法自动生成。

## 本地归档与恢复

- 原始数据冷归档使用 ZSTD Parquet，必须显式调用归档接口；归档只写副本，不会删除 PostgreSQL 在线数据。默认目录为 `backend/.cache/cold-archive/`，可通过 `ALIS_COLD_ARCHIVE_ROOT` 改到外接硬盘。
- 本地全量备份：`./scripts/backup_local_postgres.sh`，默认写入 `.cache/backups/`，包含 `alis_sim` 和 `alis_data` 两个数据库及 SHA-256 清单。
- 恢复：`./scripts/restore_local_postgres.sh <备份目录>`。只有确认要清空并还原目标库时才增加 `--clean`。
- 时间点恢复（WAL）需要明确磁盘保留策略，默认不自动启用；先运行 `./scripts/configure_local_wal_archive.sh` 查看启用命令。

## 主要能力

- 市场总览、状态机、情绪和风险预算门控
- 股票、行业、公告事件、主题和预期研究
- 估值、股票池、投资逻辑和贝叶斯复核
- 组合管理、Black-Litterman-like 目标权重解释和持仓风险
- 回测、holdout、walk-forward 和多周期样本外验收
- 决策证据哈希冻结、复盘和交易归因
- 数据质量、统一 SLO、模型漂移、Champion/Challenger 和回退门禁

## 文档入口

- `docs/ALIS长线投资系统工程计划书.md`：完整计划书
- `docs/ALIS_DESIGN.md`：总体设计
- `docs/ALIS_IMPLEMENTATION_AUDIT.md`：计划对照、完成度和实现冲突的统一审计
- `docs/*_contract.yaml`、`docs/*_schema.json`：数据和接口契约
- `docs/conversations/`：项目对话记录

## 验证

后端测试：

```powershell
cd backend
python -m pytest tests -q
```

前端构建：

```powershell
cd frontend
npm run build
```

Linux/macOS 可使用 `start.sh`；Windows 使用 `start.ps1`。
