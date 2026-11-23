"""Scheduler and backup engine.

One worker thread evaluates the New York duty window on every tick.  The
effective clock may be simulated from the web UI; all scheduling decisions,
leases and status timestamps use that same clock so a drill behaves like the
real thing.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import artifacts as art
from .clock import DutyWindow, iso_utc, local_date_of, window_for
from .config import Settings
from .state import (
    ACTIVE_STATUSES,
    JOB_COMPLETED,
    JOB_EXPIRED,
    StateStore,
    new_owner_id,
    parse_db_ts,
)

log = logging.getLogger("backupdesk.engine")

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 10


class BackupEngine:
    def __init__(self, settings: Settings, state: StateStore):
        self.settings = settings
        self.state = state
        self.tz = ZoneInfo(settings.local_tz)
        self.store = art.ArtifactStore(settings.backup_dir)
        self.owner = new_owner_id()
        self._stop = threading.Event()
        # Serialises tick() and startup recovery: only one state transition (and
        # thus at most one backup) happens at a time within the process.  The
        # cross-process guarantee is the job's pending->running update.
        self._tick_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._last_tick_error: str | None = None
        self._last_tick_at: datetime | None = None
        self._started_at = datetime.now().astimezone()
        # (size, mtime_ns) of final files already verified in this process, so
        # the 1-second scheduler does not re-run integrity checks on every tick.
        self._verified: dict[str, tuple[int, int]] = {}

    # ------------------------------------------------------------------
    # clock / window helpers
    # ------------------------------------------------------------------
    def clock(self):
        from .clock import Clock

        return Clock(self.state, self.tz)

    def current_window(self, now: datetime) -> DutyWindow:
        return window_for(local_date_of(now, self.tz), self.tz)

    def next_window(self, now: datetime) -> DutyWindow:
        """First window that has not started yet at ``now``."""

        today = self.current_window(now)
        if now < today.start_utc:
            return today
        return window_for(today.local_date + timedelta(days=1), self.tz)

    def window_for_date(self, day: date) -> DutyWindow:
        return window_for(day, self.tz)

    def _event(self, now: datetime, kind: str, detail: str) -> None:
        try:
            self.state.add_event(now, kind, detail)
        except Exception:  # pragma: no cover - events must never break backups
            log.exception("failed to record event %s", kind)

    # ------------------------------------------------------------------
    # artifact adoption / verification
    # ------------------------------------------------------------------
    def _quarantine(self, path, now: datetime, reason: str):
        target_dir = self.store.root / "quarantine"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{path.name}.{int(time.time())}.bad"
        try:
            path.replace(target)
        except OSError:
            pass
        self._event(now, "artifact_quarantined", f"{path.name}: {reason}")

    @staticmethod
    def _fingerprint(path) -> tuple[int, int]:
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns)

    def _check_final(self, day, path) -> bool:
        """Fast re-check for an already attested final file."""

        try:
            fingerprint = self._fingerprint(path)
        except OSError:
            return False
        cached = self._verified.get(day.isoformat())
        if cached == fingerprint:
            return True
        return False

    def adopt_final(self, day: date, now: datetime, reason: str, deep: bool = True) -> dict | None:
        """Publish an already-complete final file into state.

        This is how a backup that finished writing but did not manage to record
        itself survives a restart: the original bytes are kept, never redone.
        """

        path = self.store.final_path(day)
        if not path.is_file():
            return None
        fingerprint = self._fingerprint(path)
        verdict = art.verify_database(path, deep=deep)
        if not verdict.ok:
            self._quarantine(path, now, verdict.detail)
            if self.state.get_artifact_by_date(day.isoformat()):
                self.state.reset_for_redo(
                    day.isoformat(),
                    f"成品校验失败（{verdict.detail[:120]}），仅可在原窗口内重做",
                    now,
                )
            return None
        self._verified[day.isoformat()] = fingerprint
        digest = art.sha256_file(path)
        existing = self.state.get_artifact_by_date(day.isoformat())
        created_at = now
        if existing:
            created_at = parse_db_ts(existing["created_at"]) or now
            if existing["sha256"] != digest:
                self._event(
                    now,
                    "artifact_drift",
                    f"{path.name} 内容与登记不一致，以磁盘原件为准并更新校验值",
                )
        window = self.window_for_date(day)
        artifact_id = self.state.upsert_artifact(
            local_date=day.isoformat(),
            filename=path.name,
            sha256=digest,
            size_bytes=path.stat().st_size,
            created_at=created_at,
            source_db=str(self.settings.ledger_db),
            pages=verdict.pages,
            adopted=existing is None,
        )
        self.state.mark_completed_from_artifact(
            day.isoformat(),
            artifact_id,
            now,
            reason,
            window_start=window.start_utc,
            window_end=window.end_utc,
        )
        if existing is None:
            self._event(
                now,
                "artifact_adopted",
                f"{path.name} 已完成但未登记，沿用原件并开放下载",
            )
        return self.state.get_artifact_by_date(day.isoformat())

    def scan_existing_artifacts(self, now: datetime) -> int:
        adopted = 0
        for date_text, path in self.store.list_finals():
            try:
                day = date.fromisoformat(date_text)
            except ValueError:
                self._quarantine(path, now, "文件名日期不合法")
                continue
            before = self.state.get_artifact_by_date(date_text)
            result = self.adopt_final(day, now, "启动时接管已完成成品")
            if result is not None:
                adopted += 1
                if before is None:
                    self._event(now, "artifact_scan", f"{path.name} 通过校验，已登记")
            else:
                self._event(now, "artifact_scan_bad", f"{path.name} 校验失败，已隔离")
        return adopted

    # ------------------------------------------------------------------
    # startup recovery
    # ------------------------------------------------------------------
    def recover_startup(self, now: datetime) -> dict:
        """Run once before serving traffic.  Requires the instance lock."""

        with self._tick_lock:
            return self._recover_startup_locked(now)

    def _recover_startup_locked(self, now: datetime) -> dict:
        self.store.ensure_dirs()
        report = {"removed_partials": [], "adopted": 0, "reset_running": [], "expired": []}
        report["removed_partials"] = self.store.cleanup_partials()
        found = self.scan_existing_artifacts(now)
        report["adopted"] = found

        for job in self.state.list_jobs(limit=10_000):
            day = date.fromisoformat(job["local_date"])
            window = self.window_for_date(day)
            if window.end_utc <= now and job["status"] in ACTIVE_STATUSES:
                # Partial work is only ever reusable inside its original window.
                self.state.mark_expired_job_failed(
                    job["local_date"], "窗口已关闭，半成品不可再用", now
                )
                self.store.cleanup_partials(day)
                report["expired"].append(job["local_date"])
                self._event(
                    now,
                    "job_expired",
                    f"{job['local_date']} 窗口已关闭，半成品已丢弃",
                )
                continue
            if job["status"] in ACTIVE_STATUSES:
                artifact = self.state.get_artifact_by_date(job["local_date"])
                if artifact and self.store.final_path(day).is_file():
                    if self.adopt_final(day, now, "启动恢复：沿用已完成原件") is not None:
                        continue
                    # Adoption failed verification: the file was quarantined and
                    # the artifact record reset; fall through to redo.
                if job["status"] == "running":
                    previous = self.state.recover_running_job(job["local_date"], now)
                    report["reset_running"].append(job["local_date"])
                    self._event(
                        now,
                        "job_reset",
                        f"{job['local_date']} 上次进程中断（{previous or '未知'}），在窗口内重做",
                    )
        return report

    # ------------------------------------------------------------------
    # one scheduling tick
    # ------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> dict:
        with self._tick_lock:
            return self._tick_locked(now)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _tick_locked(self, now: datetime | None = None) -> dict:
        clock = self.clock()
        now = now or clock.now()
        self._last_tick_at = now
        did: dict = {"now": iso_utc(now), "actions": []}
        try:
            window = self.current_window(now)
            did["window"] = window.to_dict(self.tz)
            did["phase"] = window.phase(now)

            # Close anything from earlier windows first.
            for stale in self.state.expire_stale_jobs(now):
                day = date.fromisoformat(stale["local_date"])
                self.store.cleanup_partials(day)
                did["actions"].append(f"expired:{stale['local_date']}")
                self._event(
                    now, "job_expired", f"{stale['local_date']} 窗口关闭，作业停止并丢弃半成品"
                )

            if not window.contains(now):
                did["actions"].append("idle")
                self._last_tick_error = None
                return did

            day = window.local_date
            existing_artifact = self.state.get_artifact_by_date(day.isoformat())
            final_file = self.store.final_path(day)
            if existing_artifact and self._check_final(day, final_file):
                job = self.state.get_job(day.isoformat())
                if not job or job["status"] != JOB_COMPLETED:
                    self.adopt_final(day, now, "窗口内发现已完成成品，直接沿用")
                    did["actions"].append("adopted")
                else:
                    did["actions"].append("done")
                self._last_tick_error = None
                return did
            if existing_artifact and final_file.is_file():
                verdict = art.verify_database(final_file)
                if verdict.ok:
                    self._verified[day.isoformat()] = self._fingerprint(final_file)
                    self.adopt_final(day, now, "窗口内发现已完成成品，直接沿用")
                    did["actions"].append("done")
                    self._last_tick_error = None
                    return did
                # Recorded but now unusable: quarantine, then redo in-window.
                self._quarantine(final_file, now, verdict.detail)
                self.state.reset_for_redo(
                    day.isoformat(),
                    f"已登记成品校验失败（{verdict.detail[:120]}），窗口内重做",
                    now,
                )
                did["actions"].append("redo_after_corruption")
            elif existing_artifact and not final_file.is_file():
                self.state.reset_for_redo(
                    day.isoformat(), "已登记成品文件缺失，窗口内重做", now
                )
                did["actions"].append("redo_after_missing")

            self.state.ensure_job(window, now, trigger="schedule")
            job = self.state.get_job(day.isoformat())
            if job and job["status"] in (JOB_COMPLETED, JOB_EXPIRED):
                if job["status"] != JOB_COMPLETED:
                    did["actions"].append(f"{job['status']}:{day.isoformat()}")
                self._last_tick_error = None
                return did

            if not self.state.claim_job(day.isoformat(), self.owner, now, LEASE_SECONDS):
                did["actions"].append("busy")
                self._last_tick_error = None
                return did

            did["actions"].append("run")
            self._run_claimed(day, window, now)
            self._last_tick_error = None
        except Exception as exc:  # keep the scheduler alive, surface in /status
            self._last_tick_error = f"{type(exc).__name__}: {exc}"
            log.error("tick failed: %s\n%s", exc, traceback.format_exc())
            did["error"] = self._last_tick_error
        return did

    def _run_claimed(self, day: date, window: DutyWindow, now: datetime) -> None:
        self._execute(day, window, now)

    def _execute(self, day: date, window: DutyWindow, now: datetime) -> None:
        """Run one claimed job.  Never leaves it stuck in ``running``."""

        try:
            self._execute_claimed(day, window, now)
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            log.exception("unexpected failure running %s", day)
            fail_time = self.clock().now()
            still_open = window.end_utc > fail_time
            retry_at = (
                fail_time + timedelta(seconds=max(1.0, self.settings.retry_seconds))
                if still_open
                else None
            )
            self.state.defer_job(
                day.isoformat(),
                self.owner,
                fail_time,
                f"{type(exc).__name__}: {exc}",
                retry_at,
            )

    def _execute_claimed(self, day: date, window: DutyWindow, now: datetime) -> None:
        # Adopt a final that appeared after the claim (e.g. crashed after
        # publishing but before recording).
        if self.adopt_final(day, now, "已有成品，跳过重复制作") is not None:
            self._event(now, "job_done", f"{day.isoformat()} 沿用已完成成品")
            return

        partial = self.store.new_partial_path(day)
        stop_heartbeat = threading.Event()
        cancel = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
                try:
                    beat_time = self.clock().now()
                    self.state.heartbeat(day.isoformat(), self.owner, beat_time, LEASE_SECONDS)
                    # A closed window means partial work must not be reused.
                    if beat_time >= window.end_utc:
                        cancel.set()
                except Exception:
                    log.exception("heartbeat failed")

        beat_thread = threading.Thread(target=heartbeat, name="heartbeat", daemon=True)
        beat_thread.start()
        self._event(now, "job_started", f"{day.isoformat()} 开始制作快照")
        try:
            # Work may not outlive the window: a partial copy is only valid if
            # it is produced before 03:00 local.
            remaining = (window.end_utc - self.clock().now()).total_seconds()
            if remaining <= 5:
                raise art.SnapshotTimeout("窗口剩余时间不足，本次不再开工")
            info = art.snapshot_to_partial(
                self.settings.ledger_db,
                partial,
                pages_per_step=self.settings.pages_per_step,
                sleep_seconds=self.settings.step_sleep_ms / 1000.0,
                deadline_seconds=max(1.0, remaining - 2),
                cancel_event=cancel,
            )
            verdict = art.verify_database(info.path, deep=True)
            if not verdict.ok:
                raise art.SnapshotError(f"成品校验失败: {verdict.detail}")
            final = self.store.final_path(day)
            if final.exists():
                # Never overwrite a completed original.
                self.store.discard_partial(partial)
                if self.adopt_final(day, self.clock().now(), "成品已存在，保留原件") is not None:
                    self._event(now, "job_done", f"{day.isoformat()} 保留已有成品")
                    return
                raise art.SnapshotError("目标成品存在但不可用，已在窗口内重试")
            art.publish(partial, final)
            finished = self.clock().now()
            artifact_id = self.state.upsert_artifact(
                local_date=day.isoformat(),
                filename=final.name,
                sha256=info.sha256,
                size_bytes=info.size_bytes,
                created_at=finished,
                source_db=str(self.settings.ledger_db),
                pages=info.pages,
                duration_ms=info.duration_ms,
            )
            self.state.complete_job(day.isoformat(), self.owner, artifact_id, finished)
            self._verified[day.isoformat()] = self._fingerprint(final)
            self._event(
                finished,
                "job_completed",
                f"{day.isoformat()} 快照完成 {info.size_bytes} 字节 sha256={info.sha256[:12]}…",
            )
            self.state.set_setting("last_success_utc", iso_utc(finished))
        except Exception as exc:
            fail_time = self.clock().now()
            self.store.discard_partial(partial)
            log.exception("backup for %s failed", day)
            still_open = window.end_utc > fail_time
            retry_at = None
            job = self.state.get_job(day.isoformat())
            attempts = (job["attempts"] if job else 1) or 1
            if still_open:
                delay = min(
                    max(1.0, self.settings.retry_seconds) * min(attempts, 6),
                    max(1.0, (window.end_utc - fail_time).total_seconds() - 5),
                )
                retry_at = fail_time + timedelta(seconds=delay)
            self.state.defer_job(
                day.isoformat(),
                self.owner,
                fail_time,
                f"{type(exc).__name__}: {exc}",
                retry_at,
            )
            self._event(
                fail_time,
                "job_error",
                f"{day.isoformat()} 失败：{exc}"
                + (f"，窗口内将重试（第 {attempts + 1} 次）" if still_open else "，窗口已关闭"),
            )
        finally:
            stop_heartbeat.set()
            beat_thread.join(timeout=5)

    # ------------------------------------------------------------------
    # worker thread
    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # tick already guards; belt and braces
                log.exception("scheduler tick crashed")
            self._stop.wait(max(0.2, self.settings.poll_seconds))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def invariant_checks(self, now: datetime) -> list[str]:
        """Cheap sanity checks surfaced on /status and asserted in tests."""

        problems: list[str] = []
        running: dict[str, int] = {}
        completed_without_artifact = 0
        for job in self.state.list_jobs(limit=10_000):
            if job["status"] == "running":
                running[job["local_date"]] = running.get(job["local_date"], 0) + 1
            if job["status"] == JOB_COMPLETED and not job["artifact_id"]:
                completed_without_artifact += 1
        for day, count in running.items():
            if count > 1:
                problems.append(f"{day} 有 {count} 个运行中作业")
        if completed_without_artifact:
            problems.append(f"{completed_without_artifact} 个已完成作业没有成品登记")
        for artifact in self.state.list_artifacts(limit=10_000):
            path = self.store.final_path(artifact["local_date"])
            if not path.is_file():
                # Expected only outside the window, where a redo is not allowed.
                window = self.window_for_date(date.fromisoformat(artifact["local_date"]))
                if window.contains(now):
                    problems.append(f"{artifact['local_date']} 登记的成品文件缺失（窗口内待重做）")
        return problems

    def status(self) -> dict:
        clock = self.clock()
        now = clock.now()
        window = self.current_window(now)
        next_window = self.next_window(now)
        today = window.local_date.isoformat()
        job = self.state.get_job(today)
        artifact = self.state.get_artifact_by_date(today)
        artifact_file = self.store.final_path(today)
        ledger_present = art.ledger_exists(self.settings.ledger_db)
        return {
            "app": {
                "version": self.settings.app_version,
                "owner": self.owner,
                "started_at": self._started_at.isoformat(),
                "engine_thread_alive": self.is_running,
                "last_tick_at": iso_utc(self._last_tick_at) if self._last_tick_at else None,
                "last_tick_error": self._last_tick_error,
                "invariants": self.invariant_checks(now),
            },
            "clock": clock.snapshot(),
            "window": {
                **window.to_dict(self.tz),
                "phase": window.phase(now),
            },
            "next_window": next_window.to_dict(self.tz),
            "today": {
                "local_date": today,
                "job": job,
                "artifact": artifact,
                "artifact_file_present": artifact_file.is_file(),
                "download_ready": bool(artifact and artifact_file.is_file()),
            },
            "ledger": {
                "path": str(self.settings.ledger_db),
                "present": ledger_present,
                "size_bytes": self.settings.ledger_db.stat().st_size if ledger_present else 0,
                "wal_present": art.ledger_exists(
                    self.settings.ledger_db.with_name(self.settings.ledger_db.name + "-wal")
                ),
            },
            "jobs": self.state.list_jobs(limit=14),
            "artifacts": self._artifact_rows(),
            "events": self.state.recent_events(limit=40),
            "drill_enabled": self.settings.drill_enabled,
            "paths": {
                "backup_dir": str(self.store.root),
                "partial_dir": str(self.store.partial_dir),
                "ledger_db": str(self.settings.ledger_db),
            },
            "generated_at": iso_utc(now),
        }

    def _artifact_rows(self) -> list[dict]:
        rows = []
        for record in self.state.list_artifacts(limit=30):
            path = self.store.final_path(record["local_date"])
            rows.append(
                {
                    **record,
                    "present": path.is_file(),
                    "download_url": f"/download/{record['local_date']}",
                }
            )
        return rows
