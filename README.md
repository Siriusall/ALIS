# ALIS

ALIS 是一个面向 A 股长线投资研究的个人版数据中台与量化研究系统。当前由一个统一应用和一个历史采集目录组成：

- `quantification-main/`：统一应用，包含 FastAPI 后端、React 前端、数据中台、策略、回测、持仓、风控和研究看板。
- `news/`：历史网页采集实现，只作为迁移来源保留；数据通过主系统 `web_fallback` 接口统一入库，不再独立运行 Node.js API 和 React 前端。

项目目标不是做短线高频交易，而是逐步建设一套可以支撑长线研究的基础设施：

```text
数据采集 -> 数据校验 -> 原始数据留痕 -> 标准化入库 -> 因子/事件/行业状态 -> 投资逻辑验证 -> 组合管理
```

## 当前重点

ALIS 当前优先建设数据中台，先解决数据获取、增量更新、数据质量、运行记录和可追溯问题，再逐步接入公告事件、行业景气、估值、投资逻辑卡片和组合状态机。

已经落地的主模块包括：

- Tushare API 目录初始化与权限探测
- 股票基础信息、交易日历、行情、估值、财务、股东、质押、指数等数据同步
- 原始接口数据留痕表
- 数据源、采集任务、数据质量检查表
- 股票每日快照构建
- 巨潮公告抓取与粗粒度事件识别
- 行业指标 CSV/JSON 导入
- 前端数据中台页面和同步控制台

## 技术栈

```text
前端：React + TypeScript + Vite
统一后端：FastAPI（业务接口与数据中台接口同属 8000）
数据采集：Tushare API + Python 爬虫 + Playwright
网页兜底采集：数据中台内置 Playwright 适配器
数据库：当前模拟主后端使用 PostgreSQL `alis_sim`（历史库 `alis_main` 保留），数据中台使用 PostgreSQL `alis_data`；SQLite 已清理，生产禁止回退到 SQLite
缓存/任务队列：Redis + Redis Streams
文件存储：MinIO（本地）/S3兼容对象存储（云端）
```

## 本地启动

统一启动（后端 8000、前端 5173）：

```bash
cd quantification-main
./start.sh
```

网页/F10 兜底采集已并入统一后端，可从“数据中台”页面启动。

```bash
curl -X POST 'http://127.0.0.1:8000/api/data-center/web-fallback/crawl?codes=000001&limit=1'
```

## 环境变量

请在本地创建 `.env`，不要提交真实密钥。

```bash
TUSHARE_TOKEN=你的本地token
DATABASE_URL=postgresql://sirius@127.0.0.1:5432/alis_sim
DATA_PLATFORM_DATABASE_URL=postgresql://sirius@127.0.0.1:5432/alis_data
# LightGBM native runtime is opt-in until macOS OpenMP loading is verified.
ALIS_ENABLE_LIGHTGBM=0
```

仓库中只应保留 `.env.example` 一类示例文件。

## 数据中台常用命令

```bash
cd quantification-main/backend

# 初始化 API 目录元数据
.venv/bin/python -m scripts.data_center_v1 seed

# 探测当前 Tushare 权限
.venv/bin/python -m scripts.probe_tushare_permissions --sleep 2 --sample-rows 1

# 同步基础数据
.venv/bin/python -m scripts.data_center_v1 sync-basic

# 同步行情/估值/资金/复权等市场数据
.venv/bin/python -m scripts.data_center_v1 sync-market --trade-date YYYYMMDD

# 按股票批量同步财务数据
.venv/bin/python -m scripts.data_center_v1 sync-financial-batch --period 20240331 --limit 200

# 构建每日股票快照
.venv/bin/python -m scripts.data_center_v1 build-snapshot --trade-date YYYYMMDD

# 运行数据质量检查
.venv/bin/python -m scripts.data_center_v1 quality --trade-date YYYYMMDD
```

## 文档

- [整体设计文档](docs/ALIS_DESIGN.md)
- [主量化系统说明](quantification-main/README.md)
- [因子系统说明](news/README.md)

## 注意

当前阶段为20万元全自动模拟盘，不连接真实券商账户。系统仍处于研究系统建设阶段，不构成投资建议。任何策略、评分、筛选结果都需要回测、样本外验证和模拟盘过程验证。
