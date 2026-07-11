# ALIS

ALIS 是一个面向 A 股长线投资研究的个人版数据中台与量化研究系统。当前项目由两个子系统组成：

- `quantification-main/`：主量化系统，包含 FastAPI 后端、React 前端、Tushare 数据接入、数据中台 V1、股票快照、风险检查和研究看板。
- `news/`：因子与网页数据采集系统，包含 Python Playwright 爬虫、Node.js API 和 React 查询前端。

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
主后端：FastAPI + SQLAlchemy
数据采集：Tushare API + Python 爬虫 + Playwright
因子系统后端：Node.js
数据库：SQLite/MySQL 兼容，后续建议迁移 PostgreSQL
缓存/任务队列：后续建议 Redis/BullMQ 或 Celery
文件存储：后续建议 MinIO
```

## 本地启动

主系统：

```bash
cd quantification-main/backend
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000

cd ../frontend
npm install
npm run dev
```

因子系统：

```bash
cd news/backend
npm install
npm run dev

cd ../frontend
npm install
npm run dev

cd ../crawler
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py crawl
```

## 环境变量

请在本地创建 `.env`，不要提交真实密钥。

```bash
TUSHARE_TOKEN=你的本地token
DATABASE_URL=sqlite:///.cache/quant.db
```

仓库中只应保留 `.env.example` 一类示例文件。

## 数据中台常用命令

```bash
cd quantification-main/backend

# 初始化 API 目录元数据
.venv/bin/python scripts/data_center_v1.py seed

# 探测当前 Tushare 权限
.venv/bin/python scripts/probe_tushare_permissions.py --sleep 2 --sample-rows 1

# 同步基础数据
.venv/bin/python scripts/data_center_v1.py sync-basic

# 同步行情/估值/资金/复权等市场数据
.venv/bin/python scripts/data_center_v1.py sync-market --trade-date YYYYMMDD

# 按股票批量同步财务数据
.venv/bin/python scripts/data_center_v1.py sync-financial-batch --period 20240331 --limit 200

# 构建每日股票快照
.venv/bin/python scripts/data_center_v1.py build-snapshot --trade-date YYYYMMDD

# 运行数据质量检查
.venv/bin/python scripts/data_center_v1.py quality --trade-date YYYYMMDD
```

## 文档

- [整体设计文档](docs/ALIS_DESIGN.md)
- [主量化系统说明](quantification-main/README.md)
- [因子系统说明](news/README.md)

## 注意

本项目仍处于研究系统建设阶段，不构成投资建议。任何策略、评分、筛选结果都需要回测、样本外验证和真实小仓位验证。
