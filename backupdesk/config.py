"""Runtime configuration for the backup desk."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_VERSION = "1.0.0"


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


@dataclass
class Settings:
    data_dir: Path
    ledger_db: Path
    backup_dir: Path
    state_db: Path
    local_tz: str = "America/New_York"
    poll_seconds: float = 1.0
    retry_seconds: float = 10.0
    pages_per_step: int = 256
    step_sleep_ms: int = 0
    drill_enabled: bool = True
    secret_key: str = field(default="backupdesk-local-development", repr=False)
    app_version: str = APP_VERSION

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = _env_path("DATA_DIR", Path("/data"))
        return cls(
            data_dir=data_dir,
            ledger_db=_env_path("LEDGER_DB", data_dir / "ledger" / "ops_ledger.sqlite3"),
            backup_dir=_env_path("BACKUP_DIR", data_dir / "backups"),
            state_db=_env_path("STATE_DB", data_dir / "state" / "backupdesk.sqlite3"),
            local_tz=os.environ.get("LOCAL_TZ", "America/New_York"),
            poll_seconds=_env_float("SCHEDULER_POLL_SECONDS", 1.0),
            retry_seconds=_env_float("RETRY_SECONDS", 10.0),
            pages_per_step=_env_int("BACKUP_PAGES_PER_STEP", 256),
            step_sleep_ms=_env_int("BACKUP_STEP_SLEEP_MS", 0),
            drill_enabled=_env_bool("DRILL_ENABLED", True),
            secret_key=os.environ.get("SECRET_KEY", "backupdesk-local-development"),
            app_version=os.environ.get("APP_VERSION", APP_VERSION),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_db.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
