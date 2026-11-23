"""Consistent SQLite snapshots with an atomic, crash-safe publish protocol.

Lifecycle of one artifact:

1. Snapshot the live ledger straight into ``partial/`` using SQLite's online
   backup API.  The source is opened read-only; WAL lets the ledger keep taking
   writes while the copy is consistent as of the backup's read transaction.
2. Convert the copy back to a single-file rollback-journal database, run
   ``quick_check`` (and ``integrity_check`` on demand), fsync the bytes.
3. ``os.replace`` it to the final name -- atomic on the same filesystem -- then
   fsync the directory.  A final filename therefore never points at a
   half-written database.
4. Only then record the artifact and mark the job complete, in one state DB
   transaction.

If the process dies between 3 and 4 the next startup finds a valid final file
and adopts it.  Files in ``partial/`` are always disposable and are only ever
redone inside the original window.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

FINAL_RE = re.compile(r"^ops-ledger-(\d{4}-\d{2}-\d{2})\.sqlite3$")
PARTIAL_SUFFIX = ".partial.sqlite3"


class SnapshotError(RuntimeError):
    pass


class SnapshotTimeout(SnapshotError):
    pass


@dataclass
class SnapshotInfo:
    path: Path
    pages: int
    sha256: str
    size_bytes: int
    duration_ms: int


@dataclass
class Verification:
    ok: bool
    detail: str
    pages: int | None = None
    tables: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "pages": self.pages,
            "tables": list(self.tables),
        }


def final_name(day: date | str) -> str:
    text = day.isoformat() if isinstance(day, date) else day
    return f"ops-ledger-{text}.sqlite3"


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _readonly_uri(path: Path) -> str:
    from urllib.parse import quote

    return f"file:{quote(str(path.resolve()))}?mode=ro"


def ledger_exists(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return tuple(row[0] for row in rows)


def verify_database(path: Path, deep: bool = False) -> Verification:
    """Check that ``path`` is a self-contained, readable SQLite database."""

    if not path.is_file():
        return Verification(False, "文件不存在")
    if path.stat().st_size < 512:
        return Verification(False, "文件过小，不是完整数据库")
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=15)
        try:
            header = path.open("rb").read(16)
            if header != b"SQLite format 3\x00":
                return Verification(False, "SQLite 文件头不正确")
            check = "integrity_check" if deep else "quick_check"
            row = conn.execute(f"PRAGMA {check}").fetchone()
            result = (row[0] if row else "no result").strip()
            if result.lower() != "ok":
                return Verification(False, f"{check} 失败: {result[:300]}")
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            tables = _table_names(conn)
            if not tables:
                return Verification(False, "数据库中没有用户表", pages=pages)
            return Verification(True, "ok", pages=int(pages), tables=tables)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Verification(False, f"SQLite 校验失败: {exc}")


def snapshot_to_partial(
    source: Path,
    partial: Path,
    *,
    pages_per_step: int = 256,
    sleep_seconds: float = 0.0,
    deadline_seconds: float = 600.0,
    on_progress=None,
    cancel_event=None,
) -> SnapshotInfo:
    """Copy ``source`` into a consistent, single-file database at ``partial``.

    ``sqlite3.Connection.backup`` runs the whole copy inside one call and
    reports progress through the callback, so the callback is where the
    deadline/cancellation checks live.  Raising from it aborts the backup and
    leaves a throwaway partial file.
    """

    if not ledger_exists(source):
        raise SnapshotError(f"台账数据库不存在或为空: {source}")
    partial.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    step = max(1, int(pages_per_step))

    try:
        src = sqlite3.connect(_readonly_uri(source), uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise SnapshotError(f"无法只读打开台账: {exc}") from exc

    try:
        src.execute("PRAGMA busy_timeout=30000")
        src.execute("PRAGMA query_only=ON")
        dst = sqlite3.connect(str(partial), timeout=30)
        try:
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.execute("PRAGMA synchronous=FULL")
            final_pages = {"total": 0}

            def progress(_status, remaining, total):
                final_pages["total"] = int(total)
                elapsed = time.monotonic() - started
                if cancel_event is not None and cancel_event.is_set():
                    raise SnapshotTimeout("任务已取消（窗口关闭），半成品作废")
                if elapsed > deadline_seconds:
                    raise SnapshotTimeout(
                        f"快照超时（>{deadline_seconds:.0f}s），已放弃半成品"
                    )
                if on_progress is not None:
                    on_progress(elapsed, int(remaining), int(total))

            src.backup(dst, pages=step, progress=progress, sleep=sleep_seconds)
            # Source may be in WAL mode: page 1 carries the WAL flag.  Force the
            # copy back to a self-contained rollback-journal database.
            mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            if mode and str(mode[0]).lower() != "delete":
                raise SnapshotError(f"无法将快照转为单文件模式: {mode[0]}")
            dst.execute("PRAGMA synchronous=FULL")
            row = dst.execute("PRAGMA quick_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                raise SnapshotError(f"快照自检失败: {row[0] if row else '无结果'}")
            pages = int(dst.execute("PRAGMA page_count").fetchone()[0])
            if final_pages["total"] and pages < final_pages["total"]:
                raise SnapshotError("快照页数少于源库，副本不完整")
        finally:
            dst.close()
    except SnapshotTimeout:
        raise
    except sqlite3.Error as exc:
        raise SnapshotError(f"快照失败: {exc}") from exc
    finally:
        src.close()

    for companion in (Path(str(partial) + "-wal"), Path(str(partial) + "-shm")):
        if companion.exists():
            companion.unlink()
    fsync_file(partial)
    if not ledger_exists(partial):
        raise SnapshotError("快照文件未生成")
    digest = sha256_file(partial)
    return SnapshotInfo(
        path=partial,
        pages=pages,
        sha256=digest,
        size_bytes=partial.stat().st_size,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def publish(partial: Path, final: Path) -> None:
    """Atomically expose a verified snapshot under its final name."""

    if not ledger_exists(partial):
        raise SnapshotError("待发布的快照不存在")
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, final)
    fsync_dir(final.parent)


def remove_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.partial_dir = self.root / "partial"

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.partial_dir.mkdir(parents=True, exist_ok=True)

    def final_path(self, day: date | str) -> Path:
        return self.root / final_name(day)

    def new_partial_path(self, day: date | str) -> Path:
        text = day.isoformat() if isinstance(day, date) else day
        return self.partial_dir / f"ops-ledger-{text}.{uuid.uuid4().hex[:8]}{PARTIAL_SUFFIX}"

    def partials_for(self, day: date | str) -> list[Path]:
        text = day.isoformat() if isinstance(day, date) else day
        return sorted(self.partial_dir.glob(f"ops-ledger-{text}.*{PARTIAL_SUFFIX}"))

    def cleanup_partials(self, day: date | str | None = None) -> list[str]:
        removed = []
        targets = self.partials_for(day) if day is not None else sorted(
            self.partial_dir.glob(f"*{PARTIAL_SUFFIX}")
        )
        for path in targets:
            if remove_quietly(path):
                removed.append(path.name)
            for companion in (Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                remove_quietly(companion)
        return removed

    def discard_partial(self, path: Path) -> None:
        remove_quietly(path)
        for companion in (Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            remove_quietly(companion)

    def list_finals(self) -> list[tuple[str, Path]]:
        found = []
        if not self.root.is_dir():
            return found
        for entry in sorted(self.root.iterdir()):
            match = FINAL_RE.match(entry.name)
            if match and entry.is_file():
                found.append((match.group(1), entry))
        return found

    def inspect(self, day: date | str) -> dict | None:
        path = self.final_path(day)
        if not path.is_file():
            return None
        verification = verify_database(path)
        return {
            "path": path,
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "verification": verification,
        }
