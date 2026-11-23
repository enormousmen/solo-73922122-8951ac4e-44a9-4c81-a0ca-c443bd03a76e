"""Add regression coverage for in-window redo of corrupt/missing artifacts."""

from __future__ import annotations

from datetime import datetime, timezone

from backupdesk import artifacts as art


def _engine(settings):
    from backupdesk.web import create_app

    app = create_app(settings, start_scheduler=False)
    return app.config["DESK_ENGINE"], app.config["DESK_STATE"]


def test_missing_final_after_record_is_redone_in_window(settings):
    engine, state = _engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))
    engine.tick()
    artifact = state.get_artifact_by_date("2026-11-01")
    assert artifact is not None
    path = engine.store.final_path("2026-11-01")
    old_digest = artifact["sha256"]
    path.unlink()

    # Change the ledger so the redo is provably a fresh snapshot, not a replay.
    from backupdesk import ledger as ledger_mod

    ledger_mod.write_batch(settings.ledger_db, count=5)

    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    result = engine.tick()
    assert "redo_after_missing" in result["actions"]
    new_artifact = state.get_artifact_by_date("2026-11-01")
    assert path.is_file()
    assert art.verify_database(path, deep=True).ok
    assert new_artifact["sha256"] == art.sha256_file(path)
    assert new_artifact["sha256"] != old_digest
    assert ledger_mod.invariant_report(path)["ok"] is True


def test_corrupt_final_after_record_is_quarantined_and_redone(settings):
    engine, state = _engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))
    engine.tick()
    path = engine.store.final_path("2026-11-01")
    data = bytearray(path.read_bytes())
    for index in range(4096 + 128, min(len(data), 3 * 4096)):
        data[index] ^= 0xFF
    path.write_bytes(bytes(data))

    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    result = engine.tick()
    assert "redo_after_corruption" in result["actions"]
    assert path.is_file()
    assert art.verify_database(path, deep=True).ok
    assert list((settings.backup_dir / "quarantine").iterdir())


def test_missing_final_outside_window_is_not_redone(settings):
    engine, state = _engine(settings)
    engine.clock().set_simulated(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))
    engine.tick()
    path = engine.store.final_path("2026-11-01")
    path.unlink()

    engine.clock().set_simulated(datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc))
    result = engine.tick()
    assert "redo_after_missing" not in result["actions"]
    assert result["actions"] == ["idle"]
    assert not path.exists()
    # The completed record is kept (with download showing as unavailable), so
    # the operator can see what happened rather than silently losing the day.
    assert state.get_artifact_by_date("2026-11-01") is not None
