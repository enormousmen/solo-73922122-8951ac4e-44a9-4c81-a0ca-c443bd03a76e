"""Backup desk: one Flask instance, one scheduler, one coherent snapshot per day."""

from __future__ import annotations

import hmac
import logging
import os
from datetime import date

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import artifacts as art
from . import ledger
from .config import Settings
from .engine import BackupEngine
from .state import InstanceLock, StateStore

log = logging.getLogger("backupdesk.web")


def create_app(settings: Settings | None = None, *, start_scheduler: bool | None = None) -> Flask:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()

    app = Flask(__name__)
    app.config["DESK_SETTINGS"] = settings
    app.secret_key = settings.secret_key
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    state = StateStore(settings.state_db)
    state.initialize()

    engine = BackupEngine(settings, state)
    engine.store.ensure_dirs()

    app.config["DESK_STATE"] = state
    app.config["DESK_ENGINE"] = engine

    drill_token = os.environ.get("DRILL_TOKEN", "").strip()
    if start_scheduler is None:
        start_scheduler = os.environ.get("DISABLE_SCHEDULER", "").strip().lower() not in {
            "1",
            "true",
            "yes",
        }

    @app.before_request
    def _guard_drill() -> None:
        if not request.path.startswith("/drill"):
            return
        if not drill_token:
            return
        supplied = request.headers.get("X-Drill-Token") or request.form.get("drill_token", "")
        if not hmac.compare_digest(supplied, drill_token):
            abort(403, description="演练接口需要正确的 X-Drill-Token")

    def _wants_json() -> bool:
        return (
            request.args.get("format") == "json"
            or request.headers.get("Accept") == "application/json"
            or request.path.endswith(".json")
        )

    @app.get("/")
    def index():
        status = engine.status()
        return render_template(
            "index.html",
            status=status,
            ledger_summary=ledger.invariant_report(settings.ledger_db)
            if art.ledger_exists(settings.ledger_db)
            else None,
            drill_token_required=bool(drill_token),
        )

    @app.get("/status")
    @app.get("/status.json")
    def status():
        payload = engine.status()
        if _wants_json():
            return jsonify(payload)
        return render_template("status.html", status=payload)

    @app.get("/healthz")
    def healthz():
        return jsonify(
            {
                "ok": True,
                "version": settings.app_version,
                "engine_thread_alive": engine.is_running,
            }
        )

    @app.get("/download/<day>")
    def download(day: str):
        try:
            date.fromisoformat(day)
        except ValueError:
            abort(404)
        record = state.get_artifact_by_date(day)
        if record is None:
            abort(404)
        path = engine.store.final_path(day)
        if not path.is_file():
            abort(410, description="成品文件已丢失，等待窗口内重做")
        response = send_file(
            path,
            mimetype="application/vnd.sqlite3",
            as_attachment=True,
            download_name=art.final_name(day),
            conditional=False,
        )
        response.headers["X-Artifact-SHA256"] = record["sha256"]
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/verify/<day>")
    def verify(day: str):
        try:
            date.fromisoformat(day)
        except ValueError:
            abort(404)
        path = engine.store.final_path(day)
        if not path.is_file():
            abort(404)
        verdict = art.verify_database(path, deep=True)
        record = state.get_artifact_by_date(day)
        digest = art.sha256_file(path)
        return jsonify(
            {
                "local_date": day,
                "verification": verdict.as_dict(),
                "sha256_on_disk": digest,
                "sha256_recorded": record["sha256"] if record else None,
                "matches_record": bool(record and record["sha256"] == digest),
                "invariants": ledger.invariant_report(path),
            }
        )

    @app.post("/drill/clock")
    def drill_clock():
        if not settings.drill_enabled:
            abort(403, description="演练功能已关闭")
        payload = request.get_json(silent=True) or request.form
        raw = payload.get("utc")
        if not raw:
            return _drill_error("缺少 utc 参数", 400)
        from .clock import parse_utc

        try:
            moment = parse_utc(raw)
        except ValueError as exc:
            return _drill_error(str(exc), 400)
        engine.clock().set_simulated(moment)
        state.add_event(moment, "drill_clock_set", f"演练时钟设为 {raw}")
        # Evaluate immediately so a request at 01:30 starts the job at once.
        engine.tick()
        if _wants_json():
            return jsonify(engine.status())
        return redirect(url_for("index"))

    @app.post("/drill/real-clock")
    def drill_real_clock():
        if not settings.drill_enabled:
            abort(403, description="演练功能已关闭")
        engine.clock().use_real()
        state.add_event(engine.clock().real_now(), "drill_clock_real", "演练时钟恢复为真实时间")
        if _wants_json():
            return jsonify(engine.status())
        return redirect(url_for("index"))

    @app.post("/drill/tick")
    def drill_tick():
        if not settings.drill_enabled:
            abort(403, description="演练功能已关闭")
        result = engine.tick()
        if _wants_json():
            return jsonify({"tick": result, "status": engine.status()})
        return redirect(url_for("index"))

    @app.post("/drill/restart")
    def drill_restart():
        if not settings.drill_enabled:
            abort(403, description="演练功能已关闭")
        # Simulate a process restart: drop the in-memory view and re-run the
        # same startup recovery path used at boot.
        engine.recover_startup(engine.clock().now())
        result = engine.tick()
        if _wants_json():
            return jsonify({"tick": result, "status": engine.status()})
        return redirect(url_for("index"))

    def _drill_error(message: str, code: int):
        if _wants_json():
            return jsonify({"error": message}), code
        return render_template("error.html", message=message), code

    @app.errorhandler(403)
    def _forbidden(exc):
        if _wants_json():
            return jsonify({"error": str(exc.description)}), 403
        return render_template("error.html", message=str(exc.description)), 403

    @app.errorhandler(404)
    def _not_found(exc):
        if _wants_json():
            return jsonify({"error": "未找到"}), 404
        return render_template("error.html", message="未找到"), 404

    @app.errorhandler(410)
    def _gone(exc):
        if _wants_json():
            return jsonify({"error": str(exc.description)}), 410
        return render_template("error.html", message=str(exc.description)), 410

    @app.template_filter("shortts")
    def shortts(value):
        if not value:
            return "—"
        return value.replace("T", " ").replace("+00:00", "Z")[:23]

    if start_scheduler:
        lock = InstanceLock(settings.data_dir / "backupdesk.lock")
        try:
            lock.acquire()
        except RuntimeError as exc:
            log.error("%s", exc)
            raise
        app.config["DESK_LOCK"] = lock
        # Recovery must finish before the scheduler can observe the clock,
        # otherwise a leftover "running" job could be mistaken for live work.
        try:
            report = engine.recover_startup(engine.clock().now())
            app.config["DESK_STARTUP_REPORT"] = report
            log.info("startup recovery: %s", report)
        except Exception:
            log.exception("startup recovery failed")
        engine.start()
        log.info("backup desk %s started as %s", settings.app_version, engine.owner)

    @app.teardown_appcontext
    def _noop(_exc):  # pragma: no cover - keeps Flask happy on older versions
        return None

    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    settings.ensure_dirs()
    if os.environ.get("SEED_SAMPLE_LEDGER", "").strip().lower() in {"1", "true", "yes"}:
        created = ledger.initialize(settings.ledger_db)
        log.info("sample ledger %s", "created" if created else "already present")
    app = create_app(settings)
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    engine: BackupEngine = app.config["DESK_ENGINE"]
    try:
        try:
            from waitress import serve

            serve(app, host=host, port=port, threads=8)
        except ImportError:
            log.warning("waitress not installed; using Flask development server")
            app.run(host=host, port=port, threaded=True)
    finally:
        engine.stop()
        lock = app.config.get("DESK_LOCK")
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
