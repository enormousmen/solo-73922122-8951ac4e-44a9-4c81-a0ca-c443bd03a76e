"""Sample operations ledger and a continuous writer for the demo/compose stack.

The ledger keeps a running aggregate in the same transaction as every insert.
That gives snapshots a checkable invariant:

    sum(ops_entries.amount_cents) == sum(shift_totals.total_cents)

A copy that ignores WAL contents, or that is taken while a transaction is
half-applied, will violate it; a snapshot taken through the SQLite backup API
never will.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc

ACTORS = ["ny-desk-01", "ny-desk-02", "nj-desk-01", "remote-vpn-07", "audit-bot"]
ACTIONS = [
    ("接入告警", 0),
    ("处置工单", 0),
    ("值班交接", 0),
    ("批次对账", 1250),
    ("费用冲正", -840),
    ("导出报表", 0),
    ("设备巡检", 0),
    ("加签审批", 300),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at  TEXT NOT NULL,
    local_date   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    detail       TEXT NOT NULL,
    amount_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shift_totals (
    local_date  TEXT PRIMARY KEY,
    entries     INTEGER NOT NULL,
    total_cents INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_local_date ON ops_entries(local_date);
"""

SEED_DAYS = 5
SEED_PER_DAY = 240


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _local_date(moment: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return moment.date().isoformat()


def _insert_entry(conn: sqlite3.Connection, moment: datetime, rng: random.Random) -> None:
    action, amount = rng.choice(ACTIONS)
    amount += rng.randrange(0, 50) * 10
    day = _local_date(moment)
    conn.execute(
        "INSERT INTO ops_entries(occurred_at,local_date,actor,action,detail,amount_cents) "
        "VALUES(?,?,?,?,?,?)",
        (
            moment.isoformat(timespec="seconds"),
            day,
            rng.choice(ACTORS),
            action,
            f"{action} #{rng.randrange(100000, 999999)}，班次 {day}",
            amount,
        ),
    )
    conn.execute(
        "INSERT INTO shift_totals(local_date,entries,total_cents) VALUES(?,1,?) "
        "ON CONFLICT(local_date) DO UPDATE SET "
        "entries=entries+1, total_cents=total_cents+excluded.total_cents",
        (day, amount),
    )


def initialize(path: Path, days: int = SEED_DAYS, per_day: int = SEED_PER_DAY, force: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0 and not force:
        return False
    if force:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(path) + suffix).unlink()
            except FileNotFoundError:
                pass
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('ledger_version','1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('created_at',?)",
            (datetime.now(UTC).isoformat(timespec="seconds"),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('ledger_name','值班运维台账')"
        )
        existing = conn.execute("SELECT count(*) FROM ops_entries").fetchone()[0]
        conn.commit()
        if existing == 0:
            rng = random.Random(20261123)
            now = datetime.now(UTC)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for day_offset in range(days, 0, -1):
                    base = now - timedelta(days=day_offset)
                    for index in range(per_day):
                        moment = base + timedelta(minutes=index * (24 * 60 // per_day))
                        _insert_entry(conn, moment, rng)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        conn.commit()
    finally:
        conn.close()
    return True


def invariant_report(path: Path) -> dict:
    """Read-only invariant check used by tests and the status page."""

    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        conn.execute("PRAGMA query_only=ON")
        entries = conn.execute("SELECT count(*) FROM ops_entries").fetchone()[0]
        entry_sum = conn.execute(
            "SELECT COALESCE(sum(amount_cents),0) FROM ops_entries"
        ).fetchone()[0]
        totals = conn.execute(
            "SELECT COALESCE(sum(entries),0), COALESCE(sum(total_cents),0) FROM shift_totals"
        ).fetchone()
        mismatch = conn.execute(
            "SELECT count(*) FROM ("
            "  SELECT e.local_date, count(*) AS n, sum(e.amount_cents) AS s"
            "  FROM ops_entries e GROUP BY e.local_date"
            ") e LEFT JOIN shift_totals t ON t.local_date=e.local_date "
            "WHERE t.local_date IS NULL OR t.entries<>e.n OR t.total_cents<>e.s"
        ).fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        ok = entry_sum == totals[1] and entries == totals[0] and mismatch == 0
        return {
            "ok": ok,
            "entries": entries,
            "entry_sum_cents": entry_sum,
            "totals_entries": totals[0],
            "totals_cents": totals[1],
            "day_mismatches": mismatch,
            "journal_mode": journal,
        }
    finally:
        conn.close()


def write_batch(path: Path, count: int = 3, seed: int | None = None) -> int:
    conn = connect(path)
    rng = random.Random(seed)
    now = datetime.now(UTC)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for _ in range(count):
            _insert_entry(conn, now, rng)
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_writer(path: Path, interval: float, batch: int) -> None:
    stopping = {"flag": False}

    def handle(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    initialize(path)
    while not stopping["flag"]:
        try:
            write_batch(path, count=batch)
        except sqlite3.Error as exc:
            print(f"writer: {exc}", file=sys.stderr, flush=True)
            time.sleep(1.0)
            continue
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="样例值班运维台账")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init_p = sub.add_parser("init", help="创建样例台账")
    init_p.add_argument("--db", default=os.environ.get("LEDGER_DB", "/data/ledger/ops_ledger.sqlite3"))
    init_p.add_argument("--days", type=int, default=SEED_DAYS)
    init_p.add_argument("--per-day", type=int, default=SEED_PER_DAY)
    init_p.add_argument("--force", action="store_true")

    write_p = sub.add_parser("write", help="持续写入，模拟值班台账在线")
    write_p.add_argument("--db", default=os.environ.get("LEDGER_DB", "/data/ledger/ops_ledger.sqlite3"))
    write_p.add_argument("--interval", type=float, default=0.5)
    write_p.add_argument("--batch", type=int, default=3)

    check_p = sub.add_parser("check", help="校验台账聚合一致性")
    check_p.add_argument("--db", default=os.environ.get("LEDGER_DB", "/data/ledger/ops_ledger.sqlite3"))

    args = parser.parse_args(argv)
    path = Path(args.db)
    if args.cmd == "init":
        created = initialize(path, days=args.days, per_day=args.per_day, force=args.force)
        print("created" if created else "exists")
        return 0
    if args.cmd == "write":
        run_writer(path, interval=args.interval, batch=args.batch)
        return 0
    if args.cmd == "check":
        report = invariant_report(path)
        print(report)
        return 0 if report["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
