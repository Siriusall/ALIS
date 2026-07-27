# ALIS 云端服务器与数据库配置基线

> 基线时间：2026-07-27（UTC+8）  
> 用途：记录当前生产服务器、云端数据库、网络、服务、目录、环境变量和验收命令。  
> 安全规则：本文不保存密码、Token、JWT 密钥或私钥明文；只记录保管位置与轮换流程。

## 1. 当前拓扑

```text
用户浏览器
  -> HTTPS 443
  -> 腾讯云 kevin2 / 124.221.86.123
       - Nginx、生产前端、FastAPI、Redis
       - 日更、模拟交易、模型治理及 Evolution 调度器
       - 内置 Evolution Worker 已关闭
       |
       | PostgreSQL TLS，固定公网 IP 白名单
       v
     腾讯云 Kevin / 122.51.159.195
       - PostgreSQL 16
       - alis_sim 主业务库
       - alis_data 数据平台库
       - 40GB 系统盘 + 100GB 数据盘
       ^
       | PostgreSQL TLS，固定公网 IP 白名单
       |
     阿里云 Evolution Worker / 47.119.146.111
       - 独立因子研究与策略迭代 Worker
       - 通过 evolution_tasks 的 PostgreSQL 租约领取任务
```

腾讯云两台服务器当前虽分别使用 `10.0.0.4` 和 `10.0.0.12`，但私网 TCP 5432 实测不通，不能仅凭同一 CIDR 判断位于可互通 VPC。生产数据库连接暂时统一使用公网地址、TLS 和 `/32` 白名单。后续建立跨账号 VPC 对等连接或其他受控私网通道前，不切回私网 URL。

## 2. 服务器清单

| 节点 | 云厂商 | 规格 | 公网 IP | 已知私网 IP | 主要职责 |
|---|---|---:|---|---|---|
| `kevin2` / `VM-0-4-ubuntu` | 腾讯云轻量 | 4 核 4GB，40GB | `124.221.86.123` | `10.0.0.4` | Nginx、前端、API、Redis、调度器、模拟交易主流程 |
| `Kevin` / `VM-0-12-ubuntu` | 腾讯云轻量 | 4 核 4GB，40GB + 100GB | `122.51.159.195` | `10.0.0.12` | PostgreSQL 16 双库和数据存储 |
| Evolution Worker | 阿里云 ECS | 2 核 2GB，40GB | `47.119.146.111` | `172.18.239.180` | 因子研究、策略迭代、OOS 与候选验证任务 |

生产域名：

- `https://xlytism.com`
- `https://www.xlytism.com`
- 根域名与 `www` A 记录均指向 `124.221.86.123`。

## 3. kevin2 应用服务器

### 3.1 部署目录

```text
/opt/alis/releases/20260726-1919
/opt/alis/current -> /opt/alis/releases/20260726-1919
/etc/alis/alis.env
/etc/systemd/system/alis-api.service
```

后端工作目录：

```text
/opt/alis/current/backend
```

后端启动命令：

```text
/opt/alis/current/backend/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --workers 1
```

### 3.2 服务职责

- `alis-api.service`：单进程 FastAPI。
- Nginx：终止 HTTPS、托管生产前端、反向代理 `/api` 到 `127.0.0.1:8000`。
- Redis：`redis://127.0.0.1:6379/0`。
- Evolution scheduler 仍在 API 后台运行。
- Evolution Worker 通过 `ALIS_EVOLUTION_WORKER_ENABLED=false` 关闭；健康接口显示 `disabled_reason=external_worker`，该状态按委托运行处理为健康。
- `autonomous_daily` 由调度器按交易日和执行条件唤醒；数据库中的 `paused` 不等同于系统服务停止。

### 3.3 环境变量

环境文件：

```text
/etc/alis/alis.env
```

权限应为 `600`。至少包含：

```dotenv
APP_ENV=production
DATABASE_URL=postgresql://alis_app:<redacted>@122.51.159.195:5432/alis_sim?sslmode=require
DATA_PLATFORM_DATABASE_URL=postgresql://alis_app:<redacted>@122.51.159.195:5432/alis_data?sslmode=require
DATABASE_CONNECT_TIMEOUT_SECONDS=<configured>
REDIS_URL=redis://127.0.0.1:6379/0
JWT_SECRET=<redacted>
TUSHARE_TOKEN=<redacted>
DEEPSEEK_API_KEY=<redacted>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
ALIS_EVOLUTION_WORKER_ENABLED=false
```

只核对键名和脱敏 URL：

```bash
sudo grep -E '^(DATABASE_URL|DATA_PLATFORM_DATABASE_URL|ALIS_EVOLUTION_WORKER_ENABLED)=' /etc/alis/alis.env |
sudo sed -E 's#(://[^:]+:)[^@]+@#\1***@#'
```

### 3.4 验收命令

```bash
sudo systemctl is-active alis-api
curl -fsS http://127.0.0.1:8000/api/health
sudo journalctl -u alis-api -n 80 --no-pager
```

验收结果应包含：

- `ready=true`、`degraded=false`；
- 主业务库和数据平台库均为 `status=ok`；
- Redis `available=true`；
- Evolution scheduler `healthy=true`；
- 本地 Evolution Worker `disabled=true`、`disabled_reason=external_worker`。

## 4. Kevin 数据库服务器

### 4.1 PostgreSQL

```text
版本：PostgreSQL 16
服务：postgresql.service / PostgreSQL 16 main cluster
配置：/etc/postgresql/16/main/postgresql.conf
访问控制：/etc/postgresql/16/main/pg_hba.conf
数据目录：/srv/alis-postgresql/16/main
角色：alis_app
数据库：alis_sim、alis_data
```

`listen_addresses` 当前应覆盖：

```text
127.0.0.1,10.0.0.12
```

腾讯云公网 IP 通过 NAT 到服务器私网监听地址。不要因公网访问需求直接把 `5432` 放开给全网。

### 4.2 数据盘

```text
系统盘：40GB
数据盘：100GB
PostgreSQL 数据目录：/srv/alis-postgresql/16/main
```

首轮恢复记录约为：`alis_data` 43GB、`alis_sim` 149MB；这是迁移时快照，不是实时容量。

容量检查：

```bash
df -h /srv/alis-postgresql
sudo -u postgres psql -Atqc "SELECT datname,pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE datname IN ('alis_sim','alis_data') ORDER BY datname;"
```

### 4.3 网络白名单与 pg_hba

腾讯云防火墙/安全组的 TCP 5432 只允许：

```text
124.221.86.123/32  # kevin2
47.119.146.111/32  # 阿里 Evolution Worker
```

禁止：

```text
0.0.0.0/0
```

`pg_hba.conf` 对两个来源和两个数据库使用：

```conf
hostssl alis_sim  alis_app 124.221.86.123/32 scram-sha-256
hostssl alis_data alis_app 124.221.86.123/32 scram-sha-256
hostssl alis_sim  alis_app 47.119.146.111/32 scram-sha-256
hostssl alis_data alis_app 47.119.146.111/32 scram-sha-256
```

检查命令：

```bash
sudo systemctl is-active postgresql
sudo ss -ltnp | grep 5432
sudo -u postgres psql -Atqc 'show ssl; show listen_addresses;'
sudo grep -nE '124\.221\.86\.123|47\.119\.146\.111' /etc/postgresql/16/main/pg_hba.conf
```

## 5. 阿里云 Evolution Worker

### 5.1 部署目录和服务

```text
/opt/alis-worker/backend
/opt/alis-worker/backend/.venv
/etc/alis/worker.env
/etc/systemd/system/alis-evolution-worker.service
```

Python 入口：

```text
app.evolution_worker_main
```

Systemd 核心配置：

```ini
[Service]
Type=simple
WorkingDirectory=/opt/alis-worker/backend
EnvironmentFile=/etc/alis/worker.env
Environment=PYTHONPATH=/opt/alis-worker/backend
ExecStart=/opt/alis-worker/backend/.venv/bin/python -m app.evolution_worker_main
Restart=always
RestartSec=10
User=root
```

### 5.2 环境变量

```dotenv
DATABASE_URL=postgresql://alis_app:<redacted>@122.51.159.195:5432/alis_sim?sslmode=require
DATA_PLATFORM_DATABASE_URL=postgresql://alis_app:<redacted>@122.51.159.195:5432/alis_data?sslmode=require
DATABASE_CONNECT_TIMEOUT_SECONDS=<configured>
ALIS_EVOLUTION_WORKER_ENABLED=true
TUSHARE_TOKEN=<redacted>
DEEPSEEK_API_KEY=<redacted>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT_SECONDS=60
DEEPSEEK_DAILY_TOKEN_BUDGET=10000000
```

`/etc/alis/worker.env` 权限应为 `600`。Worker 不运行 Nginx、前端、API、Redis 或调度器。

### 5.3 租约和恢复合同

- 调度器在 kevin2 创建或推进 `evolution_tasks`。
- 阿里 Worker 通过 PostgreSQL 租约领取任务。
- 租约、心跳、检查点和重试状态持久化在数据库中。
- Worker 重启后只恢复符合过期或死亡实例条件的任务。
- kevin2 内置 Worker 与阿里 Worker 不应长期同时运行；数据库租约虽能防重，但正常生产拓扑只保留阿里 Worker。

### 5.4 验收命令

```bash
systemctl is-active alis-evolution-worker
systemctl show alis-evolution-worker -p NRestarts -p ExecMainStatus
journalctl -u alis-evolution-worker -n 50 --no-pager
```

健康日志：

```text
Standalone evolution worker ready: started=True recovered=0
```

`recovered=0` 表示启动时没有需接管的过期任务，不是错误。密码轮换故障曾产生历史重启计数；当前稳定性以进程持续 `active`、`ExecMainStatus=0` 和最新日志无新错误为准。

## 6. 密钥与密码管理

不在 Git、本文、工单或聊天中保存以下明文：

- `alis_app` 数据库密码；
- `JWT_SECRET`；
- `TUSHARE_TOKEN`；
- `DEEPSEEK_API_KEY`；
- SSH 私钥。

生产秘密仅保存在：

```text
kevin2：/etc/alis/alis.env
阿里云：/etc/alis/worker.env
```

数据库服务器只保存 PostgreSQL SCRAM verifier，不需要保存应用密码明文。轮换 `alis_app` 密码时，必须依次更新 PostgreSQL、kevin2 和阿里 Worker，并在两台消费者上先做 `SELECT 1` 测试，再重启服务。用于传递密码的临时文件验证完成后使用 `shred -u` 删除。

## 7. HTTPS 与域名

- Nginx 监听 80/443；HTTP 重定向到 HTTPS。
- Let's Encrypt 使用 ECDSA 证书。
- 2026-07-26 记录的证书有效期至 2026-10-24。
- Certbot 定时续期已启用。
- 应定期验证根域名和 `www` 均返回同一生产系统。

```bash
curl -I http://xlytism.com
curl -fsS https://xlytism.com/api/health
curl -fsS https://www.xlytism.com/api/health
sudo certbot renew --dry-run
```

## 8. 当前已知问题

### 8.1 腾讯云私网不通

`10.0.0.4 -> 10.0.0.12:5432` 实测失败。当前使用公网 TLS `/32` 白名单，不影响正确性，但增加网络延迟并依赖公网链路。建立受控私网连接后，应先验证双库、延迟和回滚，再修改连接 URL。

### 8.2 只读聚合接口偏慢

2026-07-27 在 kevin2 本机携带生产认证 Token 实测：

```text
/api/model-strategy/evolution/owner-summary  200，约 11.79 秒
/api/decision/summary                        200，约 37.74 秒
```

前端只读请求当前 20 秒超时，两个接口并行时可能同时显示“读取超时”。这不是数据缺失。短期可将只读超时提高到 60 秒，长期必须缓存或拆解重查询，使决策摘要回到秒级；不能用推测值填充页面。

### 8.3 自动备份未落地

当前只确认完成首轮迁移备份与恢复校验，尚未建立独立介质上的周期全量备份、WAL 归档和恢复演练。该事项仍是运维 P0。

## 9. 日常检查清单

### kevin2

```bash
systemctl is-active alis-api
curl -fsS http://127.0.0.1:8000/api/health
sudo journalctl -u alis-api -n 50 --no-pager
```

### Kevin 数据库

```bash
sudo systemctl is-active postgresql
df -h /srv/alis-postgresql
sudo ss -ltnp | grep 5432
```

### 阿里 Worker

```bash
systemctl is-active alis-evolution-worker
systemctl show alis-evolution-worker -p ExecMainStatus
journalctl -u alis-evolution-worker -n 30 --no-pager
```

## 10. 故障恢复顺序

1. 数据库不可达：先检查 PostgreSQL、5432 白名单、`pg_hba.conf` 和 TLS URL，不先改业务代码。
2. kevin2 API 失败：检查 `/etc/alis/alis.env`、数据库 `SELECT 1`、`journalctl`，再重启 `alis-api`。
3. 阿里 Worker 失败：检查 `/etc/alis/worker.env` 和双库 `SELECT 1`，再重启 Worker。
4. 阿里 Worker 暂时不可恢复时，可在部署了当前切换代码的 kevin2 将 `ALIS_EVOLUTION_WORKER_ENABLED=true` 并重启 API，作为受控回退；恢复阿里 Worker 前不得保持两个 Worker 长期并行。
5. 任一变更后都先验证 `/api/health`、Worker 最新日志和任务进度，再清理备份或临时凭据。

