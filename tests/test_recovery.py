"""Crash/restart behaviour: adoption of finished files, retry of partials."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from backupdesk import artifacts as art
from backupdesk.config import Settings
from backupdesk.state import JOB_COMPLETED, JOB_EXPIRED, parse_db_ts
from backupdesk.web import create_app


def _make_engine(settings: Settings):
    app = create_app(settings, start_scheduler=False)
    return app, app.config["DESK_ENGINE"], app.config["DESK_STATE"]

def _orphan_final_without_record(settings: Settings) -> Path:
    """Simulate: snapshot published, process died before the state commit."""

    partial = settings.backup_dir / "partial" / "orphan.partial.sqlite3"
    art.snapshot_to_partial(settings.ledger_db, partial)
    final = settings.backup_dir / "ops-ledger-2026-11-01.sqlite3"
    art.publish(partial, final)
    return final


def test_completed_file_is_adopted_after_restart(settings):
    final = _orphan_final_without_record(settings)
    digest = art.sha256_file(final)
    mtime = final.stat().st_mtime_ns

    app, engine, state = _make_engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc))
    now = engine.clock().now()
    report = engine.recover_startup(now)
    assert report["adopted"] >= 1

    job = state.get_job("2026-11-01")
    artifact = state.get_artifact_by_date("2026-11-01")
    assert job["status"] == JOB_COMPLETED
    assert job["artifact_id"] == artifact["id"]
    assert artifact["sha256"] == digest
    assert "接管" in job["error"] or "沿用" in job["error"]

    # Original bytes are kept exactly: nothing is re-snapshotted or rewritten.
    assert art.sha256_file(final) == digest
    assert final.stat().st_mtime_ns == mtime

    engine.tick(now)
    assert state.get_job("2026-11-01")["status"] == JOB_COMPLETED
    assert art.sha256_file(final) == digest


def test_running_job_from_dead_process_is_redone_in_window(settings):
    app, engine, state = _make_engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))
    now = engine.clock().now()
    window = engine.current_window(now)
    state.ensure_job(window, now, trigger="test")
    assert state.claim_job("2026-11-01", "dead-process", now, 60)
    state.heartbeat("2026-11-01", "dead-process", now, 60)
    # A stale half-written partial from the dead process.
    stale = settings.backup_dir / "partial" / "ops-ledger-2026-11-01.dead.partial.sqlite3"
    stale.write_bytes(b"half written garbage")

    report = engine.recover_startup(now)
    assert "2026-11-01" in report["reset_running"]
    assert not stale.exists(), "未完成半成品必须被丢弃"

    engine.tick(now)
    job = state.get_job("2026-11-01")
    assert job["status"] == JOB_COMPLETED
    assert job["attempts"] == 2
    assert art.verify_database(engine.store.final_path("2026-11-01"), deep=True).ok


def test_partial_cannot_be_redone_after_window_closes(settings):
    app, engine, state = _make_engine(settings)
    # Old job left running two days ago.
    old_day = "2026-11-20"
    window = engine.window_for_date(datetime(2026, 11, 20).date())
    started = window.start_utc
    state.ensure_job(window, started, trigger="test")
    assert state.claim_job(old_day, "dead-process", started, 60)
    stale = settings.backup_dir / "partial" / f"ops-ledger-{old_day}.dead.partial.sqlite3"
    stale.write_bytes(b"partial")

    engine.clock().set_simulated(datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))
    report = engine.recover_startup(engine.clock().now())
    assert old_day in report["expired"]
    assert not stale.exists()
    job = state.get_job(old_day)
    assert job["status"] != JOB_COMPLETED
    assert job["status"] in {"failed", JOB_EXPIRED}
    assert engine.store.final_path(old_day).exists() is False

    # And no late-night retry happens for the old date.
    engine.tick()
    assert state.get_job(old_day)["status"] != "running"
    assert list(engine.store.partial_dir.glob(f"ops-ledger-{old_day}*")) == []


def test_registered_artifact_survives_state_wipe_by_rescan(settings, tmp_path):
    app, engine, state = _make_engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))
    engine.tick()
    digest = state.get_artifact_by_date("2026-11-01")["sha256"]

    # Wipe only the coordination DB; the artifact stays on disk.
    state_db = settings.state_db
    state_db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(state_db) + suffix).unlink(missing_ok=True)

    app2, engine2, state2 = _make_engine(settings)
    engine2.clock().set_simulated(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc))
    engine2.recover_startup(engine2.clock().now())
    artifact = state2.get_artifact_by_date("2026-11-01")
    assert artifact["sha256"] == digest
    assert state2.get_job("2026-11-01")["status"] == JOB_COMPLETED
    assert art.sha256_file(engine2.store.final_path("2026-11-01")) == digest


def test_corrupt_final_is_quarantined_and_window_redoes_it(settings):
    bad = settings.backup_dir / "ops-ledger-2026-11-01.sqlite3"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not a sqlite database, just noise" * 20)

    app, engine, state = _make_engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))
    report = engine.recover_startup(engine.clock().now())
    assert report["adopted"] == 0
    assert not bad.exists()
    assert list((settings.backup_dir / "quarantine").iterdir())
    # Startup recovery alone never fabricates an artifact from a bad file.
    assert state.get_artifact_by_date("2026-11-01") is None

    # Inside the window a fresh snapshot is produced and published.
    engine.tick()
    artifact = state.get_artifact_by_date("2026-11-01")
    assert artifact is not None
    assert art.verify_database(engine.store.final_path("2026-11-01"), deep=True).ok


def test_failed_attempt_retries_inside_window_only(settings, monkeypatch):
    app, engine, state = _make_engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 23, 7, 40, tzinfo=timezone.utc))
    calls = {"n": 0}
    real = art.snapshot_to_partial

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise art.SnapshotError("模拟磁盘抖动")
        return real(*args, **kwargs)

    monkeypatch.setattr("backupdesk.engine.art.snapshot_to_partial", flaky)
    engine.tick()
    job = state.get_job("2026-11-23")
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job["next_attempt_at"]

    engine.clock().set_simulated(datetime(2026, 11, 23, 7, 41, tzinfo=timezone.utc))
    engine.tick()
    job = state.get_job("2026-11-23")
    assert job["status"] == JOB_COMPLETED
    assert job["attempts"] == 2


def test_retry_stops_at_window_end(settings, monkeypatch):
    app, engine, state = _make_engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 23, 7, 59, 50, tzinfo=timezone.utc))

    def always_fail(*args, **kwargs):
        raise art.SnapshotError("模拟持续失败")

    monkeypatch.setattr("backupdesk.engine.art.snapshot_to_partial", always_fail)
    engine.tick()
    # Just before 03:00 a retry is still legitimately queued for the same
    # window; it is not silently carried past the close.
    job = state.get_job("2026-11-23")
    assert job["status"] == "pending"
    assert parse_db_ts(job["next_attempt_at"]) < engine.window_for_date(
        datetime(2026, 11, 23).date()
    ).end_utc

    engine.clock().set_simulated(datetime(2026, 11, 23, 8, 1, tzinfo=timezone.utc))
    engine.tick()
    job = state.get_job("2026-11-23")
    assert job["status"] in {"failed", JOB_EXPIRED}
    assert job["finished_at"] is not None
    assert state.get_artifact_by_date("2026-11-23") is None
    # No partial survives a closed window.
    assert list(engine.store.partial_dir.glob("ops-ledger-2026-11-23*")) == []
