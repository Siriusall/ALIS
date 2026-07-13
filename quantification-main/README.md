# 量化系统（A股真实数据 + 资金模拟）

## 目录结构
- `backend/` FastAPI 后端（TuShare 数据 + 执行模拟）
- `frontend/` React 看板（展示真实行情 + 模拟资金/下单）
- `backend/app/data_platform/` 数据中台模块；与业务接口共同运行在8000，前端直接调用 `/api/data-center/*`
- `deploy/` Nginx 与 systemd 配置示例
- `docs/` 文档中心（结构说明、保留策略、索引）

## 整理说明（2026-03-08）

- 本次执行“只整理、不删除”。
- 新增文档入口：`docs/README.md`
- 结构说明：`docs/structure.md`
- 保留策略：`docs/retention.md`

## 服务器部署（Ubuntu 24.04 + systemd + Nginx）

### 1. 安装依赖
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip nginx
```

### 2. 部署后端
```bash
sudo mkdir -p /opt/quantification
sudo cp -r backend /opt/quantification/
python3.11 -m venv /opt/quantification/venv
/opt/quantification/venv/bin/pip install -r /opt/quantification/backend/requirements.txt
```

**在服务器上创建后端 `.env`（否则用 SQLite，MySQL 里看不到用户）：**
```bash
sudo tee /opt/quantification/backend/.env << 'EOF'
DATABASE_URL=mysql+pymysql://用户:密码@localhost:3306/quant
TUSHARE_TOKEN=你的token
EOF
sudo chown www-data:www-data /opt/quantification/backend/.env
```

复制 systemd 服务：
```bash
sudo cp deploy/quant-backend.service /etc/systemd/system/quant-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now quant-backend
```

### 3. 部署前端
```bash
cd frontend
npm install
npm run build
```

复制静态文件：
```bash
sudo mkdir -p /var/www/quant-dashboard
sudo cp -r frontend/dist/* /var/www/quant-dashboard/
```

### 4. 配置 Nginx
```bash
sudo cp deploy/nginx-quant.conf /etc/nginx/sites-available/quant
sudo ln -s /etc/nginx/sites-available/quant /etc/nginx/sites-enabled/quant
sudo nginx -t
sudo systemctl reload nginx
```

### 5. 访问
浏览器访问 `http://服务器IP/` 即可。

## 环境变量与数据库

后端从项目根目录或 `backend` 目录下的 `.env` 读取配置（需在启动前存在）。

| 变量 | 说明 |
|------|------|
| `TUSHARE_TOKEN` | 掘金/Tushare 等数据源 Token（按需配置） |
| `DATABASE_URL` | 主业务 PostgreSQL 连接，当前本地默认使用 `postgresql://sirius@127.0.0.1:5432/alis_sim`。 |
| `DATA_PLATFORM_DATABASE_URL` | 数据中台 PostgreSQL 连接；中台接口已并入本后端，但继续与业务事实库物理分库。 |

当前应用运行统一使用 **PostgreSQL**；首次启动时自动建表并执行内建迁移。

## 统一缓存与初始化

行情、快照、日线等数据会合并为「按日 + 按标的」的统一缓存（`backend/.cache/all_in_one/`）。选股、信号、持仓等接口已优先使用该缓存，一次更新、一次获取。

### 日常推荐
- **初始化**：`POST /api/init` 会做日线滚动、行情/快照预热、日线预拉，并**仅合并当日** all-in-one，适合每日开盘前或盘中调用。
- **业务接口**：`/api/candidates`、`/api/overview`、`/api/signals/{symbol}`、`/api/positions` 等已在后端走统一缓存，前端照常调用即可。

### 统一缓存读取
- **接口**：`GET /api/cache/all?date=YYYYMMDD`，可选 `symbols=000001,000002,...`。
- **必填**：`date` 为单个交易日（YYYYMMDD），避免一次拉取全部历史。
- **大包保护**：不传 `symbols` 时，若该日记录数超过 5 万条会返回 400，需按标的列表拆分请求。

### 历史全量合并（full-sync）
需要为历史所有交易日生成合并文件时：
- **触发**：`POST /api/init/full-sync`，任务在后台异步执行，支持断点续跑（已存在的日期会跳过）。
- **状态**：`GET /api/init/full-sync/status` 返回 `idle` / `running` / `completed` / `failed` 及进度（`total_dates`、`processed_dates`、`current_date` 等）。
- 若任务已在运行，再次 `POST /api/init/full-sync` 不会重复启动，仅返回当前状态。

## 本地开发

### 一键启动（Windows PowerShell）
在项目根目录执行，会打开两个窗口分别跑后端和前端：
```powershell
.\start.ps1
```
若提示无法执行脚本，先运行：`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

### 手动启动

**后端**
```bash
cd backend
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
（默认建议不加 `--reload`，改代码后手动重启后端。）

**前端**
```bash
cd frontend
npm install
npm run dev
```
