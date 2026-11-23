"""Snapshot consistency, integrity and download behaviour."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from backupdesk import artifacts as art
from backupdesk import ledger


def test_snapshot_under_concurrent_writes_is_consistent(tmp_path: Path, settings):
    source = settings.ledger_db
    stop = threading.Event()
    errors: list[BaseException] = []
    writes = {"count": 0}

    def writer():
        try:
            while not stop.is_set():
                ledger.write_batch(source, count=2)
                writes["count"] += 2
                time.sleep(0.005)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.05)
        partial = tmp_path / "snap.partial.sqlite3"
        info = art.snapshot_to_partial(source, partial, pages_per_step=8)
        final = tmp_path / "snap.sqlite3"
        art.publish(partial, final)
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not errors
    assert writes["count"] > 0

    report = ledger.invariant_report(final)
    assert report["ok"], report
    assert report["day_mismatches"] == 0
    assert report["journal_mode"] == "delete"
    assert art.verify_database(final, deep=True).ok
    assert art.sha256_file(final) == info.sha256
    # No WAL sidecars next to the published artifact: it is self-contained.
    assert not Path(str(final) + "-wal").exists()


def test_source_is_opened_read_only(tmp_path: Path, settings):
    # If the snapshot code tried to write, a read-only source would fail.
    source = settings.ledger_db
    before = art.sha256_file(source)
    partial = tmp_path / "ro.partial.sqlite3"
    art.snapshot_to_partial(source, partial)
    art.publish(partial, tmp_path / "ro.sqlite3")
    assert art.sha256_file(source) == before
    report = ledger.invariant_report(source)
    assert report["ok"]


def test_deadline_aborts_and_leaves_disposable_partial(tmp_path: Path, settings):
    partial = tmp_path / "deadline.partial.sqlite3"
    with pytest.raises(art.SnapshotTimeout):
        art.snapshot_to_partial(
            settings.ledger_db,
            partial,
            deadline_seconds=-1,
        )
    # The partial may exist but is never published; caller discards it.
    assert partial.exists()
    partial.unlink()
    assert not partial.exists()


def test_publish_is_atomic_and_refuses_missing_partial(tmp_path: Path):
    with pytest.raises(art.SnapshotError):
        art.publish(tmp_path / "missing.partial.sqlite3", tmp_path / "final.sqlite3")


def test_missing_or_empty_ledger_is_reported(tmp_path: Path):
    with pytest.raises(art.SnapshotError):
        art.snapshot_to_partial(tmp_path / "nope.sqlite3", tmp_path / "p.partial.sqlite3")
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    with pytest.raises(art.SnapshotError):
        art.snapshot_to_partial(empty, tmp_path / "p2.partial.sqlite3")


def test_verify_detects_corruption(tmp_path: Path, settings):
    partial = tmp_path / "v.partial.sqlite3"
    art.snapshot_to_partial(settings.ledger_db, partial)
    final = tmp_path / "v.sqlite3"
    art.publish(partial, final)
    assert art.verify_database(final, deep=True).ok

    corrupt = tmp_path / "corrupt.sqlite3"
    data = bytearray(final.read_bytes())
    # Corrupt page payload (not header/free space) so SQLite really notices.
    page_size = 4096
    for index in range(page_size + 128, min(len(data), 3 * page_size)):
        data[index] ^= 0xFF
    corrupt.write_bytes(bytes(data))
    verdict = art.verify_database(corrupt)
    assert not verdict.ok
    assert "SQLite" in verdict.detail or "失败" in verdict.detail


def test_invariant_report_detects_drift(tmp_path: Path, settings):
    copy = tmp_path / "drift.sqlite3"
    copy.write_bytes(settings.ledger_db.read_bytes())
    conn = sqlite3.connect(copy)
    conn.execute("UPDATE shift_totals SET total_cents = total_cents + 1")
    conn.commit()
    conn.close()
    report = ledger.invariant_report(copy)
    assert report["ok"] is False
    assert report["day_mismatches"] >= 1
