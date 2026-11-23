"""Backup desk package.

The public entry points are imported lazily so that ``python -m
backupdesk.ledger`` (used by the sample writer container) does not need Flask.
"""

from __future__ import annotations

__all__ = ["APP_VERSION", "BackupEngine", "Settings", "StateStore", "create_app"]

APP_VERSION = "1.0.0"


def __getattr__(name: str):
    if name == "Settings":
        from .config import Settings

        return Settings
    if name == "BackupEngine":
        from .engine import BackupEngine

        return BackupEngine
    if name == "StateStore":
        from .state import StateStore

        return StateStore
    if name == "create_app":
        from .web import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
