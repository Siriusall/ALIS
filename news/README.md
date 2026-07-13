# 历史因子采集实现（已并入主系统）

> 自 2026-07-12 起，本目录不再作为独立系统运行。Node API 和 React 前端仅保留用于迁移核对；主入口是 `quantification-main` 的“数据中台”。

历史沪深股票网页/F10 因子采集代码和 `news.db` 数据保留在这里，作为结构化主数据源缺失时的兜底来源。

统一入库接口：

```text
POST /api/data-center/web-fallback/import-legacy
POST /api/data-center/web-fallback/crawl
GET  /api/data-center/web-fallback/crawl/status
GET  /api/data-center/web-fallback/factors
```

导入后数据写入主库 `dc_web_factor_snapshot`，任务写入 `data_ingestion_runs`，来源为 `web_fallback`。不再通过 CSV、3001 端口或独立前端与主系统交换数据。

实时 Playwright/F10 实现已经迁至：

```text
quantification-main/backend/app/data_platform/web_fallback/
```

## 历史结构

- `crawler/` 迁移前 Python 爬虫快照
- `backend/` 已停用的 Node.js API
- `frontend/` 已停用的独立 React 前端

## 历史启动方式（仅排障，不作为生产入口）

```bash
# 后端
cd backend
npm install
npm run dev

# 前端
cd frontend
npm install
npm run dev

# 爬虫（也可由后端触发）
cd crawler
pip install -r requirements.txt
python main.py crawl
```

默认地址：

- 前端：http://localhost:5173
- 后端：http://localhost:3001
