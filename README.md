# 值班台账备份台 (backupdesk)

单实例 Flask 备份台。每天在纽约时间 01:30–03:00 之间把值班运维台账做成一份
**可直接打开的一致 SQLite 快照**，供值班员留档、下载和演练。

---

## 交付清单

| 需求 | 实现位置 |
| --- | --- |
| 台账持续写入时也能生成一致快照 | `backupdesk/artifacts.py:snapshot_to_partial`，只读打开源库 + SQLite 在线备份 API；写副本时先转成单文件 rollback-journal 模式 |
| 纽约时间每天 01:30 开工 | `backupdesk/clock.py:window_for`，`01:30` 用 `fold=0` |
| 停机错过、恢复后当天 03:00 前自动补跑 | 调度器每次 tick 只要当前时刻在窗口内就建单补跑（`engine.tick`）；不是“只在 01:30 触发” |
| 秋季回拨：从第一次 01:30 起、按实际时间到 03:00 | 窗口端点是两个真实 UTC 时刻，回拨日固定为 150 分钟；同一当地日期只有一个窗口对象 |
| 同一当地日期只允许一个作业 | `jobs.local_date` 主键 + `claim_job` 的 `UPDATE ... WHERE status='pending'` 单写者闸门；另有进程级 `flock` 单实例锁 |
| 已完成但没来得及登记，重启后沿用原件继续下载 | 启动时扫描 `backups/ops-ledger-<date>.sqlite3`，校验通过即接管（`adopt_final`），绝不重做或改写原件字节 |
| 半成品只能在原窗口内重做 | 半成品只写 `backups/partial/`，启动即清空；窗口关闭后的 pending/running 作业一律置 `expired` 并丢弃半成品 |
| Docker Compose + 样例台账 | `Dockerfile`、`docker-compose.yml`、`backupdesk/ledger.py` |
| 网页设置演练 UTC 时刻、查状态、下载 | `/`、`/status.json`、`/drill/*`、`/download/<date>` |

## 快速开始

```bash
docker compose up --build -d
docker compose ps
open http://127.0.0.1:8080/          # 或在浏览器里打开
```

启动后：

- `desk` 容器先建好样例台账（最近 5 天、每天 240 条），再启动备份台；
- `ledger-writer` 容器每 0.5 秒追加 3 条，模拟台账持续写入（WAL 模式）；
- 页面顶部显示今日窗口、生效时钟、今日作业/成品、台账源和事件日志。

常用命令：

```bash
# 真实运行：调度器每天自动在 01:30 之后建单（真实时钟）
docker compose logs -f desk

# 演练：把生效时钟拨到指定 UTC 时刻
curl -s -X POST http://127.0.0.1:8080/drill/clock \
     -H 'Content-Type: application/json' \
     -d '{"utc":"2026-11-01T05:30:00Z"}' | python3 -m json.tool

# 一键跑回拨 + 重启恢复演练
docker compose exec desk python /app/scripts/ops.py drill --restart

# 下载今天的成品
curl -OJ http://127.0.0.1:8080/download/2026-11-01
```

## 演练时钟说明

- `POST /drill/clock {"utc": "..."}` 把**生效时钟**固定到该时刻；不会自己走动。
  翻到窗口内的下一个时刻再 `POST /drill/tick`（或页面“推进 tick”）。
- 所有调度判断、租约、作业时间戳、事件日志都使用这个生效时钟，所以演练和真实运行走同一套代码。
- `POST /drill/real-clock` 恢复真实时间。
- `POST /drill/restart` 在进程内重跑一遍启动恢复流程（清半成品、接管成品、重置中断作业），用来验证“重启后如何”。
  它不重启容器，但走的是与真实启动完全相同的 `recover_startup`。
- 生产/共享环境请设置 `DRILL_TOKEN`，之后 `/drill/*` 需要 `X-Drill-Token` 头（或表单字段）。网页表单也会要求填写。

## 关键设计

### 快照一致性

源台账以 `mode=ro` 打开，用 `sqlite3.Connection.backup()` 复制。它在一个读事务内
按页复制：**备份开始后**源库的写入不会进入副本，副本内部也不会出现“写了一半”的事务。
副本落地后强制 `journal_mode=DELETE`，再跑 `quick_check`（成品发布前跑 `integrity_check`），
所以下载到的一定是单文件、可直接打开的库，没有 `-wal`/`-shm` 依赖。

样例台账的每个写入事务同时更新 `ops_entries` 和 `shift_totals`。测试和 `/verify/<date>`
会核对 `sum(amount_cents) == sum(total_cents)` 且按日一致 —— 忽略 WAL 或复制到半写事务
的副本必然不满足。

### 原子发布 + 崩溃安全

1. 副本写入 `backups/partial/ops-ledger-<date>.<random>.partial.sqlite3`；
2. 校验、`fsync` 文件；
3. `os.replace` 到 `backups/ops-ledger-<date>.sqlite3`，再 `fsync` 目录 —— 最终文件名
   要么不存在，要么就是完整文件；
4. 最后在同一状态库事务里登记 artifact 并把作业置 `completed`。

第 3 步之后、第 4 步之前崩溃：重启扫描到最终文件 → 校验 → 接管 → 可下载，**原件字节
和 mtime 都不变**。第 3 步之前崩溃：只留下 `partial/` 垃圾，启动时清掉，窗口内重做。

### 窗口与同一日唯一作业

- 窗口 = `[当地日期 01:30, 当地日期 03:00)`，两个端点各自按 IANA 规则解析成 UTC：
  - 2026-11-01（回拨）：05:30Z → 08:00Z，150 分钟；
  - 2026-03-08（春季）：06:30Z → 07:00Z，30 分钟；
  - 平常日：06:30Z → 08:00Z，90 分钟。
- `01:30` 固定用 `fold=0`（第一次出现的 01:30）；回拨日重复的 01:00–02:00 不会产生第二个窗口。
- `jobs` 表以当地日期为主键。`claim_job` 是单条带条件的 UPDATE，并发调用只有一个能拿到 `rowcount=1`；
  进程级 `flock(/data/backupdesk.lock)` 再保证一个数据卷只有一个备份台。

### 中断恢复

启动顺序：拿实例锁 → `recover_startup` → 启动调度线程。恢复逻辑：

| 磁盘/状态 | 处理 |
| --- | --- |
| 有最终文件，无论是否登记 | 校验通过→接管并可下载；校验失败→移入 `quarantine/` |
| 作业 `running` 但无最终文件，窗口内 | 重置为 `pending`，丢弃半成品，重做 |
| 作业 `pending/running`，窗口已关闭 | 置 `expired`，丢弃半成品，**不补做** |
| 已登记成品文件丢失/损坏，窗口内 | 重新制作；窗口外只保留登记并标记文件缺失 |
| 失败但窗口还有时间 | 退避重试（`retry_seconds × 尝试次数`，不超过窗口结束） |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 状态页 + 演练控制 |
| GET | `/status.json`（或 `/status?format=json`） | 完整结构化状态 |
| GET | `/healthz` | 容器健康检查 |
| GET | `/download/<YYYY-MM-DD>` | 下载成品，带 `X-Artifact-SHA256` |
| GET | `/verify/<YYYY-MM-DD>` | 对成品做 `integrity_check` + 聚合一致性核对 |
| POST | `/drill/clock` | 设置演练 UTC 时刻，立即评估一次 |
| POST | `/drill/tick` | 用当前生效时钟推进一次调度 |
| POST | `/drill/restart` | 重跑启动恢复流程 |
| POST | `/drill/real-clock` | 恢复真实时钟 |

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `/data` | 数据根目录 |
| `LEDGER_DB` | `$DATA_DIR/ledger/ops_ledger.sqlite3` | 台账（只读访问） |
| `BACKUP_DIR` | `$DATA_DIR/backups` | 成品、半成品、隔离区 |
| `STATE_DB` | `$DATA_DIR/state/backupdesk.sqlite3` | 作业/成品/事件状态库（与台账分离） |
| `LOCAL_TZ` | `America/New_York` | 窗口时区 |
| `SCHEDULER_POLL_SECONDS` | `1` | 调度轮询间隔 |
| `RETRY_SECONDS` | `10` | 失败重试基数（窗口内） |
| `BACKUP_PAGES_PER_STEP` | `256` | 在线备份每步页数 |
| `BACKUP_STEP_SLEEP_MS` | `0` | 每步休眠，仅用于放大/演练 |
| `DRILL_TOKEN` | 空 | 设置后 `/drill/*` 需要 `X-Drill-Token` |
| `SEED_SAMPLE_LEDGER` | 空 | `1` 时启动时创建样例台账 |
| `DISABLE_SCHEDULER` | 空 | `1` 时只起 Web，不起调度线程（测试用） |

## 测试与自检

```bash
python3 -m pip install -r requirements.txt pytest pyflakes
python3 -m pytest -q
python3 -m pyflakes backupdesk scripts tests
```

覆盖：正常日/回拨日/春季日窗口边界、`fold=0`、同日唯一作业、并发认领、并发写入下快照一致性、
损坏检测、原子发布、成品接管（字节与 mtime 不变）、半成品窗口内重做、窗口外丢弃、
失败重试与窗口结束、离线下成品丢失/损坏的窗口内重做、状态库被清空后靠磁盘重新登记、
DRILL_TOKEN 门禁与页面表单路径。

## 已知限制（未完成项）

- 本环境没有容器运行时，`docker compose up` 未实际执行；Compose/Dockerfile 已做 YAML/指令静态校验，
  镜像构建与容器内健康检查需在有 Docker 的机器上复核。
- 前台 `docker compose` 单副本模型，不支持多副本横向扩展：`flock` 只对同一数据卷/同一主机生效；
  跨主机需要换共享存储锁或外部协调服务。
- 演练时钟是固定时刻，不会随真实时间推进；演练“跨小时”要显式再设一次时刻。
- `quarantine/` 只增不删，需要人工/外部保留策略清理。
- 未接外部告警；失败信息只在页面、事件日志和 `/status.json` 暴露。
- 备份对象是 SQLite 台账；如果台账迁到别的数据库引擎，`artifacts.snapshot_to_partial` 需要换实现。
