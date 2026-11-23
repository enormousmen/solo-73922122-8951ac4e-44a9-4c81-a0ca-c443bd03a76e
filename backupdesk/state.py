"""Durable coordination state for the backup desk (separate from the ledger).

The ledger database is only ever *read* through SQLite's online backup API.
Everything the desk needs to remember across restarts -- one job per local
date, leases, the drill clock and artifact records -- lives here.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .clock import UTC, DutyWindow

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"  # terminal failure for a closed window
JOB_EXPIRED = "expired"  # window closed before work finished, nothing reusable
ACTIVE_STATUSES = (JOB_PENDING, JOB_RUNNING)


def db_ts(moment: datetime) -> str:
    """Canonical, lexicographically sortable UTC timestamp for SQLite."""

    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_db_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def new_owner_id() -> str:
    import socket

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:10]}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    local_date      TEXT PRIMARY KEY,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    status          TEXT NOT NULL,
    owner           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    trigger         TEXT,
    requested_at    TEXT,
    started_at      TEXT,
    heartbeat_at    TEXT,
    lease_expires   TEXT,
    finished_at     TEXT,
    next_attempt_at TEXT,
    artifact_id     INTEGER,
    error           TEXT,
    CHECK (status IN ('pending','running','completed','failed','expired'))
);

CREATE TABLE IF NOT EXISTS artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    local_date    TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    source_db     TEXT NOT NULL,
    pages         INTEGER,
    duration_ms   INTEGER,
    adopted       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc    TEXT NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_at ON events(at_utc DESC, id DESC);
"""


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    # ---- settings -----------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---- events -------------------------------------------------------
    def add_event(self, at: datetime, kind: str, detail: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events(at_utc,kind,detail) VALUES(?,?,?)",
                (db_ts(at), kind, detail),
            )

    def recent_events(self, limit: int = 30) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT at_utc,kind,detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- jobs ---------------------------------------------------------
    def ensure_job(self, window: DutyWindow, now: datetime, trigger: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO jobs"
                "(local_date,window_start,window_end,status,trigger,requested_at) "
                "VALUES(?,?,?,'pending',?,?)",
                (
                    window.local_date.isoformat(),
                    db_ts(window.start_utc),
                    db_ts(window.end_utc),
                    trigger,
                    db_ts(now),
                ),
            )

    def get_job(self, local_date: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE local_date=?", (local_date,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 14) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY local_date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_job(self, local_date: str, owner: str, now: datetime, lease_seconds: int) -> bool:
        """Atomically move a pending, in-window job to running.

        ``UPDATE ... WHERE status='pending'`` is the single-writer gate: even if
        several callers race, exactly one gets ``rowcount == 1``.
        """

        stamp = db_ts(now)
        lease = db_ts(now + timedelta(seconds=lease_seconds))
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='running', owner=?, attempts=attempts+1, "
                "started_at=COALESCE(started_at,?), heartbeat_at=?, lease_expires=?, "
                "error=NULL, next_attempt_at=NULL "
                "WHERE local_date=? AND status='pending' "
                "AND window_start<=? AND window_end>? "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                (owner, stamp, stamp, lease, local_date, stamp, stamp, stamp),
            )
            return cur.rowcount == 1

    def heartbeat(self, local_date: str, owner: str, now: datetime, lease_seconds: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE jobs SET heartbeat_at=?, lease_expires=? "
                "WHERE local_date=? AND status='running' AND owner=?",
                (db_ts(now), db_ts(now + timedelta(seconds=lease_seconds)), local_date, owner),
            )
            return cur.rowcount == 1

    def complete_job(
        self, local_date: str, owner: str, artifact_id: int, now: datetime
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status='completed', finished_at=?, heartbeat_at=?, "
                "lease_expires=NULL, artifact_id=?, error=NULL "
                "WHERE local_date=? AND owner=? AND status='running'",
                (db_ts(now), db_ts(now), artifact_id, local_date, owner),
            )

    def defer_job(
        self,
        local_date: str,
        owner: str,
        now: datetime,
        error: str,
        next_attempt_at: datetime | None,
    ) -> None:
        """Return a running job to pending (in-window retry) or expire it."""

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT window_end FROM jobs WHERE local_date=?", (local_date,)
            ).fetchone()
            if row is None:
                return
            in_window = parse_db_ts(row["window_end"]) > now
            if in_window and next_attempt_at is not None:
                conn.execute(
                    "UPDATE jobs SET status='pending', owner=NULL, error=?, "
                    "next_attempt_at=?, lease_expires=NULL "
                    "WHERE local_date=? AND owner=? AND status='running'",
                    (error[:2000], db_ts(next_attempt_at), local_date, owner),
                )
            else:
                # Window closed -> the partial is worthless (expired).  Open
                # window but no retry scheduled -> a hard failure.
                conn.execute(
                    "UPDATE jobs SET status=?, finished_at=?, owner=NULL, error=?, "
                    "lease_expires=NULL "
                    "WHERE local_date=? AND owner=? AND status='running'",
                    (
                        JOB_EXPIRED if not in_window else JOB_FAILED,
                        db_ts(now),
                        error[:2000],
                        local_date,
                        owner,
                    ),
                )

    def mark_completed_from_artifact(
        self,
        local_date: str,
        artifact_id: int,
        now: datetime,
        reason: str,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO jobs"
                "(local_date,window_start,window_end,status,finished_at,artifact_id,error) "
                "VALUES(?,?,?,'completed',?,?,?)",
                (
                    local_date,
                    db_ts(window_start or now),
                    db_ts(window_end or now),
                    db_ts(now),
                    artifact_id,
                    reason[:2000],
                ),
            )
            conn.execute(
                "UPDATE jobs SET status='completed', artifact_id=?, finished_at=COALESCE(finished_at,?), "
                "lease_expires=NULL, next_attempt_at=NULL, error=? "
                "WHERE local_date=? AND status IN ('pending','running','failed','expired')",
                (artifact_id, db_ts(now), reason[:2000], local_date),
            )

    def expire_stale_jobs(self, now: datetime) -> list[dict]:
        """Close out pending/running work whose window has passed.

        Returns the jobs that changed so the caller can discard partial files.
        """

        changed: list[dict] = []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('pending','running') "
                "AND window_end<=?",
                (db_ts(now),),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE jobs SET status=?, finished_at=?, owner=NULL, lease_expires=NULL, "
                    "error=? WHERE local_date=? AND status IN ('pending','running')",
                    (
                        JOB_EXPIRED,
                        db_ts(now),
                        "窗口已关闭，未完成的作业不再续跑",
                        row["local_date"],
                    ),
                )
                changed.append(dict(row))
        return changed

    def recover_running_job(self, local_date: str, now: datetime) -> str | None:
        """Take over a job left running by a previous process.

        Only called while holding the instance lock, so no other process can be
        working on it.  The caller decides whether the artifact is usable.
        """

        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE local_date=?", (local_date,)).fetchone()
            if row is None or row["status"] != JOB_RUNNING:
                return None
            conn.execute(
                "UPDATE jobs SET status='pending', owner=NULL, lease_expires=NULL, "
                "next_attempt_at=NULL, error=? WHERE local_date=? AND status='running'",
                ("检测到上次进程中断，原窗口内重做", local_date),
            )
        return row["owner"]

    def reset_for_redo(self, local_date: str, reason: str, now: datetime) -> None:
        """Forget a completed record whose file is gone/invalid, inside window."""

        with self.transaction() as conn:
            conn.execute("DELETE FROM artifacts WHERE local_date=?", (local_date,))
            conn.execute(
                "UPDATE jobs SET status='pending', owner=NULL, artifact_id=NULL, "
                "finished_at=NULL, lease_expires=NULL, next_attempt_at=NULL, error=? "
                "WHERE local_date=?",
                (reason[:2000], local_date),
            )

    def mark_expired_job_failed(self, local_date: str, error: str, now: datetime) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, finished_at=?, owner=NULL, lease_expires=NULL, error=? "
                "WHERE local_date=? AND status IN ('pending','running')",
                (JOB_EXPIRED, db_ts(now), error[:2000], local_date),
            )

    # ---- artifacts ----------------------------------------------------
    def upsert_artifact(
        self,
        local_date: str,
        filename: str,
        sha256: str,
        size_bytes: int,
        created_at: datetime,
        source_db: str,
        pages: int | None = None,
        duration_ms: int | None = None,
        adopted: bool = False,
    ) -> int:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM artifacts WHERE local_date=?", (local_date,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE artifacts SET filename=?, sha256=?, size_bytes=?, created_at=?, "
                    "source_db=?, pages=COALESCE(?,pages), duration_ms=COALESCE(?,duration_ms), "
                    "adopted=MAX(adopted,?) WHERE id=?",
                    (
                        filename,
                        sha256,
                        size_bytes,
                        db_ts(created_at),
                        source_db,
                        pages,
                        duration_ms,
                        1 if adopted else 0,
                        existing["id"],
                    ),
                )
                return int(existing["id"])
            cur = conn.execute(
                "INSERT INTO artifacts"
                "(local_date,filename,sha256,size_bytes,created_at,source_db,pages,duration_ms,adopted) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    local_date,
                    filename,
                    sha256,
                    size_bytes,
                    db_ts(created_at),
                    source_db,
                    pages,
                    duration_ms,
                    1 if adopted else 0,
                ),
            )
            return int(cur.lastrowid)

    def get_artifact_by_date(self, local_date: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE local_date=?", (local_date,)
            ).fetchone()
        return dict(row) if row else None

    def list_artifacts(self, limit: int = 30) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts ORDER BY local_date DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


class InstanceLock:
    """OS-level exclusive lock: exactly one backup desk per data volume."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(
                f"另一个备份台实例正占用 {self.path}，本进程拒绝启动"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nowner={new_owner_id()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
