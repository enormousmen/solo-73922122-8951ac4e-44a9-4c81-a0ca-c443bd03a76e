# 值班台账备份台

用 Flask + SQLite 在线备份 API 做的单实例备份台：每天纽约时间 01:30–03:00 之间
把持续写入的值班台账做成一一致、可直接打开的 SQLite 快照，网页可演练 UTC 时刻、
查状态、下载成品。

```bash
docker compose up --build -d
open http://127.0.0.1:8080/
docker compose exec desk python /app/scripts/ops.py drill --restart
```

完整说明（窗口/回拨规则、崩溃恢复、接口、配置、限制）见 [README.md](README.md)。
