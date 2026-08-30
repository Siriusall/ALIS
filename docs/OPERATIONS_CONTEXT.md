# ALIS 零上下文接手与线上运维手册

> 目标：让没有历史对话、没有本地记忆的新机器安全接手 ALIS。
>
> 本文不保存密码、Token、私钥或数据库凭据。敏感值只能从操作者自己的 SSH 配置、目标服务器 `/etc/alis/*.env` 或安全凭据管理中取得。

## 1. 必须先建立的正确认知

1. **当前生产是三台物理服务器**：应用机、数据库机、计算机。
2. 计算机上同时运行两个逻辑 Worker：`control` 和 `research`。逻辑任务池不是两台服务器。
3. 历史上存在过独立控制机 `47.119.146.111`，现已退出当前生产拓扑，不应部署、恢复或纳入任务中心。
4. 前端大量读取持久化控制台快照。页面旧不等于浏览器缓存，必须同时检查数据库、快照、API 和前端 release。
5. PostgreSQL 跨节点访问通过 WireGuard，但底层仍消耗数据库机公网出流量。
6. 排障先看实时服务、进程、数据库任务状态和 release symlink，再看源码；不要假设 Git 状态就是线上状态。
7. 不提交 `.env`、密钥、数据库口令、发布压缩包、`.alis-release-manifest` 或 `commit.txt`。

## 2. 仓库与本地环境

- 仓库：`https://github.com/Siriusall/ALIS.git`
- 默认分支：`main`
- Windows 主工作区：`C:\Users\Administrator\Desktop\ALIS`
- Python：3.11
- Node.js：18+
- 后端：FastAPI、Psycopg、PostgreSQL、Redis、Pandas、LightGBM
- 前端：React、TypeScript、Vite、Vitest

主要目录：

| 路径 | 作用 |
| --- | --- |
| `backend/app/api/routes/` | HTTP API |
| `backend/app/services/` | 业务、任务编排、快照、状态服务 |
| `backend/app/data_platform/` | 数据源、PIT/OOS 数据契约 |
| `backend/migrations/` | 数据库迁移 |
| `backend/tests/` | 后端测试 |
| `frontend/src/` | React 控制台 |
| `deploy/` | systemd 与部署脚本 |
| `README.md` | 产品和研究架构；其中“四节点”描述是旧逻辑拆分，不代表当前物理服务器数量 |

本地初始化：

```powershell
npm run install:backend
npm run install:frontend
npm run check:env
```

常用验证：

```powershell
$env:PYTHONPATH='backend'
python -m pytest backend/tests/test_target.py -q
npm --prefix frontend run test
npm --prefix frontend run build
git diff --check
```

优先运行与改动相关的定向测试。Windows 完整测试偶尔会因临时 SQLite 文件锁出现无关 `WinError 32`，必须根据 traceback 判断。

GitHub 直连失败时，本机 Clash/Mihomo 曾在 `127.0.0.1:7897` 提供 HTTP 代理。只对单次命令使用：

```powershell
git -c http.proxy=http://127.0.0.1:7897 fetch origin
git -c http.proxy=http://127.0.0.1:7897 push origin <branch>
```

## 3. 当前三台生产服务器

以下地址用于节点定位，不包含凭据。新机器需要自行配置 SSH key 和 `~/.ssh/config`。

| 物理节点 | SSH 别名 | 公网 IP | 用户 | WireGuard IP | 职责 |
| --- | --- | --- | --- | --- | --- |
| 应用机 | `alis-app` | `124.221.86.123` | `ubuntu` | `10.44.0.2` | Nginx、前端、FastAPI、Redis、调度、告警 |
| 数据库机 | `alis-db` | `122.51.159.195` | `ubuntu` | `10.44.0.1` | PostgreSQL 双库、权威任务状态、数据库本地快照 |
| 计算机 | `alis-research-worker` | `124.223.225.117` | `ubuntu` | `10.44.0.4` | `control` Worker 与 `research` Worker |

当前不使用：

| 旧节点 | 状态 |
| --- | --- |
| `47.119.146.111`，旧 SSH 别名 `alis-control-worker` | 已退出三机生产拓扑；不得自动恢复其 Worker 或 WireGuard |

数据流：

```text
Browser -> 应用机 Nginx/Frontend -> alis-api
应用机 -> WireGuard -> 数据库机 PostgreSQL
计算机 control/research Workers -> WireGuard -> 数据库机 PostgreSQL
数据库机本地 snapshot worker -> localhost PostgreSQL -> durable snapshots
```

## 4. 节点路径和服务

### 4.1 应用机

- 后端：`/opt/alis/current/backend`
- 前端：`/opt/alis/current/frontend/dist`
- API：`alis-api.service`
- 告警：`alis-alert-worker.service`
- 环境：`/etc/alis/alis.env`
- 健康检查：`curl -fsS http://127.0.0.1:8000/api/health`

### 4.2 数据库机

- 后端入口：`/opt/alis-db-snapshot/backend`
- release：`/opt/alis-db-snapshot/releases`
- PostgreSQL：`postgresql@16-main.service`
- 快照：`alis-db-snapshot-worker.service`
- 快照环境：`/etc/alis/db-snapshot-worker.env`
- 出网保护：`alis-db-egress-guard.service`
- 出网脚本：`/usr/local/sbin/alis-db-egress-guard`

数据库机环境中的 `DATABASE_URL` 和 `DATA_PLATFORM_DATABASE_URL` 必须指向 `127.0.0.1` 或 `localhost`。

### 4.3 计算机

- 后端入口：`/opt/alis-research-worker/backend`
- release：`/opt/alis-research-worker/releases`
- 共享虚拟环境：`/opt/alis-research-worker/shared/app-venv`
- 控制 Worker：`alis-control-worker.service`
- 控制环境：`/etc/alis/control-worker.env`，必须设置或解析为 `ALIS_WORKER_POOL=control`
- 研究 Worker：`alis-research-worker.service`
- 研究环境：`/etc/alis/research-worker.env`，`ALIS_WORKER_POOL=research`

两个 Worker 在同一台物理计算机上，共用代码和虚拟环境，但领取不同逻辑任务池。不要给 `alis-research-worker.service` 添加 `Conflicts=alis-control-worker.service`，也不要在研究部署时禁用控制 Worker。

## 5. 零上下文首次检查

先检查 Git：

```powershell
git status --short
git branch --show-current
git fetch origin
git log -5 --oneline --decorate --all
```

再检查三台服务器：

```powershell
ssh alis-app "hostname; systemctl list-units --type=service --state=running | grep alis; readlink -f /opt/alis/current"
ssh alis-db "hostname; systemctl list-units --type=service --state=running | grep -E 'alis|postgres'; readlink -f /opt/alis-db-snapshot/backend"
ssh alis-research-worker "hostname; systemctl list-units --type=service --state=running | grep alis; readlink -f /opt/alis-research-worker/backend"
```

检查网络和重复进程：

```powershell
ssh alis-db "sudo wg show; sudo tc -s class show dev eth0; sudo ss -tpn state established"
ssh alis-research-worker "ps -eo pid,ppid,user,etimes,cmd | grep -E '/opt/alis|worker' | grep -v grep"
ssh alis-research-worker "systemctl show alis-control-worker alis-research-worker -p Environment --no-pager"
```

开始修改前回答：

1. 当前线上 release commit 是什么？
2. 三台机器哪些服务真实运行？
3. 计算机上的两个 Worker pool 是否分别为 `control` 和 `research`？
4. 数据库连接来自应用机还是计算机？
5. 问题属于代码、数据契约、快照、任务租约还是部署版本？

## 6. 截至 2026-08-30 23:45 的临时完全停机状态

> 这是有时效的线上状态，不是永久架构。数据库服务器流量包于 **2026-09-11** 重置。

为保护剩余约 3GB，当前状态：

- 三台机器的 `wg-quick@wg0.service` 均已停止并禁用。
- 应用机 `alis-api`、`alis-alert-worker` 已停止并禁用。
- 计算机 `alis-control-worker`、`alis-research-worker` 已停止并禁用。
- 数据库机 `alis-db-snapshot-worker` 已停止并禁用；它在 WireGuard 关闭后仍会尝试连接应用机 Redis。
- PostgreSQL 保持运行，仅供数据库机本地访问。
- `alis-db-egress-guard` 保持运行。
- 旧 `47.119.146.111` 节点不在恢复计划中。

持久化恢复顺序：

- `2026-09-11 00:05 CST`：三台当前生产服务器恢复 WireGuard。
- `2026-09-11 00:10 CST`：应用服务、数据库快照、计算机两个 Worker 恢复。
- `2026-09-11 00:10 CST`：数据库机移除紧急低速 override，恢复长期出网规则。

主要 timer：

```text
alis-resume-wireguard-sep11.timer
alis-resume-app-sep11.timer
alis-resume-research-sep11.timer
alis-resume-db-snapshot-sep11.timer
alis-db-egress-normalize-sep11.timer
```

其中计算机的 `alis-resume-research-sep11.service` 会同时启用 `alis-control-worker` 和 `alis-research-worker`。

在 9 月 11 日前不要把服务“修复”为自启，除非用户明确接受立即恢复公网流量。

## 7. 长期数据库机月度出网保护

规则来自：

- `deploy/alis-db-egress-guard.sh`
- `deploy/alis-db-egress-guard.service`
- `deploy/alis-db-egress-guard-account.service`
- `deploy/alis-db-egress-guard-account.timer`

统一保护器同时执行三层保护：

1. `eth0` 总出口始终不超过 `448Kbit/s`。即使计量 timer 完全失效并连续跑满 31 天，理论上也约为 150GB。
2. 每分钟读取网卡累计发送字节并持久化到 `/var/lib/alis-egress-guard/state`，重启和规则重载不会清零月度账本。
3. 每月 11 日切换账期；达到 120GB 后将总出口降到 `128Kbit/s`，达到 150GB 后熔断到 `1Kbit/s`。

正常模式下计算机、应用机和其他目标的保底速率分别为 `192Kbit/s`、`192Kbit/s` 和 `64Kbit/s`，三类可在 `448Kbit/s` 总上限内借用空闲带宽。150GB 熔断线为云厂商 200GB 套餐预留至少 50GB，用于统计口径差异和保护器之外的云平台流量。

首次在账期中途安装时，可在 `/etc/alis/db-egress-guard.env` 设置 `ALIS_EGRESS_INITIAL_USED_GB` 注入云平台已经统计的用量。紧急冻结使用 `ALIS_EGRESS_FORCE_FREEZE=1`，不要通过停止保护器实现冻结。

验证：

```powershell
ssh alis-db "systemctl is-active alis-db-egress-guard"
ssh alis-db "systemctl is-active alis-db-egress-guard-account.timer"
ssh alis-db "sudo /usr/local/sbin/alis-db-egress-guard status"
ssh alis-db "sudo tc class show dev eth0"
ssh alis-db "sudo tc filter show dev eth0 parent 1:"
```

Windows 手工 `scp` 新 `.sh` 文件可能带 CRLF。如果日志出现 `/usr/bin/env: 'bash\r': No such file or directory`：

```powershell
ssh alis-db "sudo sed -i 's/\r$//' /usr/local/sbin/alis-db-egress-guard"
```

## 8. 快照和前端数据不一致

按顺序检查：

1. 数据库源表是否有新值。
2. `alis-db-snapshot-worker` 是否运行并完成刷新。
3. 快照 payload 和 `generated_at` 是否更新。
4. API 是否返回新快照并带 `Cache-Control: no-store`。
5. 前端是否部署最新构建 hash。
6. 最后才检查 Nginx、CDN 和浏览器缓存。

关键代码：

- `backend/app/services/console_snapshots.py`
- `backend/app/api/routes/console_snapshots.py`
- `frontend/src/api.ts`
- `frontend/src/pages.tsx`

不要用前端硬编码值掩盖数据库或快照错误。

## 9. 研究任务、OOS 和防阻塞

- 研究波次使用 quorum 和硬超时，不等待 100% 子任务。
- watchdog 修复停滞的 `running` 和 `retry_wait` 任务。
- 专家池更新必须非破坏式；没有新候选时保留旧冠军和备份。
- 严格 PIT 不足时，可通过 `ALIS_FROZEN_REPLAY_OOS_FALLBACK=true` 使用候选前冻结回放窗口。
- `strategy_environment_backtest` 和 `strategy_evolution_cycle` 是本地 OOS 计算，不属于可选公网下载任务。
- 公网预算在任务领取和已领取可选任务执行前都会检查。

关键代码：

- `backend/app/services/continuous_strategy_research.py`
- `backend/app/services/evolution_orchestrator.py`
- `backend/app/data_platform/services/oos_hard_data.py`
- `backend/app/services/sealed_strategy_holdout.py`
- `backend/app/services/network_budget.py`

## 10. 发布与部署

发布包必须包含 `.alis-release-manifest`：

```text
format=alis_release_bundle_v2
git_commit=<40 character sha>
sim_migration_head=<revision>
data_migration_head=<revision>
```

不要复用目录里的旧压缩包。每次部署重新生成 bundle 并核对 SHA。

研究/计算机部署：

```bash
deploy/deploy-research-worker.sh BUNDLE
```

它部署共享计算代码。部署后必须分别核对计算机上的 `alis-control-worker` 和 `alis-research-worker`，不能只看研究服务。

数据库快照部署：

```bash
deploy/deploy-db-snapshot-worker.sh BUNDLE
```

该脚本会验证 localhost 数据库 URL、migration head，并安装数据库快照与出网保护。

部署后至少验证：

```bash
systemctl is-active <service>
systemctl show <service> -p Environment --no-pager
journalctl -u <service> -n 50 --no-pager
readlink -f <backend-symlink>
```

然后验证数据库任务状态、API 和浏览器真实页面。`systemctl active` 不代表数据路径正确。

## 11. 任务和数据库检查

生产凭据只从服务器环境文件加载，不复制进命令历史或文档。

重点表：

- `evolution_tasks`
- `runtime_worker_heartbeats`
- `strategy_environment_experts`
- 控制台快照表
- 模型、策略、回放和 OOS 证据表

查看活动任务：

```sql
SELECT id, task_type, worker_pool, status, phase, progress,
       lease_owner, lease_expires_at, next_run_at, updated_at
FROM evolution_tasks
WHERE status IN ('queued', 'running', 'retry_wait')
ORDER BY priority DESC, id;
```

发现 `running` 任务时不要直接删除。先判断对应逻辑 Worker、lease 和 watchdog 恢复规则。

## 12. 常见故障

### 页面值旧

先查快照和部署版本，不要只要求硬刷新。

### 任务中心没有实时流

检查计算机两个 Worker 心跳、任务租约、快照 TTL 和部署 commit。

### 数据库出流量增加

```powershell
ssh alis-db "sudo wg show; sudo tcpdump -nn -i wg0 -q -c 200"
ssh alis-research-worker "sudo ss -tpn dst 10.44.0.1:5432"
ssh alis-research-worker "systemctl list-units --type=service --state=running | grep alis"
```

重点检查两个 Worker 是否使用正确 pool、是否存在重复进程、是否执行全表回放。不要只依赖应用层预算。

### GitHub 连接失败

DNS 正常但 `github.com:443` 超时时，临时使用本地代理，不要重复创建提交。

### systemd drop-in 错误

文件必须包含真实换行。安装后执行：

```bash
systemd-analyze verify /etc/systemd/system/<unit>
systemctl daemon-reload
systemctl show <unit> -p Environment
```

## 13. 协作规则

1. 新工作使用 `codex/` 前缀分支，除非用户指定。
2. 修改前 `git fetch`，避免覆盖另一台机器的提交。
3. 推送前运行定向测试、构建和 `git diff --check`。
4. 不提交凭据和本地 release 产物。
5. 不在未确认线上状态时重启 PostgreSQL、WireGuard 或运行中 Worker。
6. 不用 destructive SQL 清空专家池、任务或快照解除阻塞。
7. 线上 hotfix 必须写回 Git，否则下次部署会覆盖。
8. 每次线上变更记录 commit、物理节点、逻辑服务、时间、验证和回滚方法。

## 14. 新接手者第一小时

- [ ] 阅读 `README.md` 和本文，并以本文的三机物理拓扑为准。
- [ ] 配置三个当前 SSH alias，不复制他人私钥。
- [ ] `git fetch origin`，确认基于最新远端。
- [ ] 检查三台机器服务、release symlink、WireGuard 和 timer。
- [ ] 确认是否仍处于 2026-09-11 前完全停机期。
- [ ] 在计算机上分别核对 control/research 两个逻辑 Worker。
- [ ] 明确问题属于前端、快照、数据平台、研究编排、模型还是部署。
- [ ] 运行最小相关测试建立基线。
- [ ] 修改后同时验证代码、服务、数据库和浏览器结果。

如果本文与实时系统冲突，以实时状态为准，并在确认后更新本文。
