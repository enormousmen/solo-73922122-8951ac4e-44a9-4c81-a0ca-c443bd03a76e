"""Operator scripts: seed the sample ledger and run the delivery drill."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Allow running as a plain script (``python scripts/ops.py``) from any cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def seed(db: Path, days: int, per_day: int, force: bool) -> None:
    from backupdesk import ledger

    created = ledger.initialize(db, days=days, per_day=per_day, force=force)
    report = ledger.invariant_report(db) if db.exists() else {}
    print(json.dumps({"db": str(db), "created": created, **report}, ensure_ascii=False, indent=2))


def http(method: str, url: str, payload: dict | None = None, token: str | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Drill-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type", "")
        if body and "json" in content_type:
            return response.status, json.loads(body)
        return response.status, body


def wait_ready(base: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = http("GET", f"{base}/healthz")
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1.0)
    raise SystemExit(f"服务在 {timeout:.0f}s 内未就绪: {base}")


def drill(base: str, token: str | None, restart: bool) -> int:
    """Replay the two required drills against a running instance."""

    wait_ready(base)
    checks: list[tuple[str, bool, str]] = []

    # 1) Fall-back day: window is 150 real minutes and only one job exists even
    #    though 01:30 occurs twice.
    status, payload = http(
        "POST", f"{base}/drill/clock?format=json", {"utc": "2026-11-01T05:30:00Z"}, token
    )
    assert status == 200, payload
    window = payload["window"]
    job_first = payload["today"]["job"]
    art_first = payload["today"]["artifact"]
    checks.append(
        (
            "回拨日窗口 01:30–03:00 = 9000 秒",
            window["duration_seconds"] == 9000,
            json.dumps(window, ensure_ascii=False),
        )
    )
    checks.append(
        ("回拨日首次 01:30 完成备份", job_first is not None and job_first["status"] == "completed", str(job_first)),
    )
    status, payload = http(
        "POST", f"{base}/drill/clock?format=json", {"utc": "2026-11-01T06:30:00Z"}, token
    )
    job_second = payload["today"]["job"]
    checks.append(
        (
            "第二次 01:30 不另起作业，沿用同一成品",
            job_second["status"] == "completed"
            and job_second["attempts"] == 1
            and payload["today"]["artifact"]["sha256"] == art_first["sha256"],
            str(job_second),
        )
    )

    # 2) Interrupted backup: simulate a dead process mid-run and restart.
    status, payload = http(
        "POST", f"{base}/drill/clock?format=json", {"utc": "2026-11-02T06:30:00Z"}, token
    )
    assert payload["today"]["job"]["status"] == "completed"
    if restart:
        status, payload = http("POST", f"{base}/drill/restart?format=json", {}, token)
        checks.append(
            (
                "重启恢复后成品仍在且可下载",
                payload["status"]["today"]["download_ready"] is True,
                str(payload["status"]["today"]["job"]),
            )
        )
        code, _ = http("GET", f"{base}/download/2026-11-02")
        checks.append(("下载接口返回 200", code == 200, str(code)))

    ok = all(item[1] for item in checks)
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} :: {detail[:300]}")
    print(json.dumps({"ok": ok, "checks": len(checks)}, ensure_ascii=False))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份台运维脚本")
    sub = parser.add_subparsers(dest="cmd", required=True)

    seed_p = sub.add_parser("seed", help="生成样例台账")
    seed_p.add_argument("--db", default=os.environ.get("LEDGER_DB", "/data/ledger/ops_ledger.sqlite3"))
    seed_p.add_argument("--days", type=int, default=5)
    seed_p.add_argument("--per-day", type=int, default=240)
    seed_p.add_argument("--force", action="store_true")

    drill_p = sub.add_parser("drill", help="对运行中的实例跑演练")
    drill_p.add_argument("--base", default=os.environ.get("DESK_BASE", "http://127.0.0.1:8080"))
    drill_p.add_argument("--token", default=os.environ.get("DRILL_TOKEN") or None)
    drill_p.add_argument("--restart", action="store_true", help="包含模拟重启恢复步骤")

    args = parser.parse_args(argv)
    if args.cmd == "seed":
        seed(Path(args.db), args.days, args.per_day, args.force)
        return 0
    if args.cmd == "drill":
        return drill(args.base.rstrip("/"), args.token, args.restart)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
