"""Shared fixtures for the backup desk tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backupdesk import ledger  # noqa: E402
from backupdesk.config import Settings  # noqa: E402
from backupdesk.web import create_app  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        ledger_db=data / "ledger" / "ops_ledger.sqlite3",
        backup_dir=data / "backups",
        state_db=data / "state" / "backupdesk.sqlite3",
        poll_seconds=0.2,
        retry_seconds=0.5,
    )
    settings.ensure_dirs()
    ledger.initialize(settings.ledger_db, days=2, per_day=60)
    return settings


@pytest.fixture()
def app(settings: Settings):
    application = create_app(settings, start_scheduler=False)
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def engine(app):
    return app.config["DESK_ENGINE"]


def set_clock(client, utc: str):
    response = client.post(
        "/drill/clock", json={"utc": utc}, headers={"Accept": "application/json"}
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def tick(client):
    response = client.post("/drill/tick", json={}, headers={"Accept": "application/json"})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def status(client) -> dict:
    return client.get("/status.json").get_json()
