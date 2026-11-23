"""End-to-end duty-window behaviour through the HTTP drill interface."""

from __future__ import annotations

from conftest import set_clock, status, tick

from backupdesk.state import parse_db_ts


def test_no_job_before_window_then_one_job_in_window(client, engine):
    set_clock(client, "2026-11-23T06:29:59Z")  # 01:29:59 EST
    payload = tick(client)["status"]
    assert payload["today"]["job"] is None
    assert payload["window"]["phase"] == "before"

    payload = set_clock(client, "2026-11-23T06:30:00Z")  # exactly 01:30 EST
    assert payload["today"]["job"]["status"] == "completed"
    artifact = payload["today"]["artifact"]
    assert artifact is not None
    assert payload["today"]["download_ready"] is True

    payload = tick(client)["status"]
    assert payload["today"]["job"]["status"] == "completed"
    assert payload["today"]["job"]["attempts"] == 1


def test_late_start_inside_window_runs_immediately(client):
    # Restart at 02:45 local, window ends 03:00: catch-up still happens.
    payload = set_clock(client, "2026-11-23T07:45:00Z")
    assert payload["today"]["job"]["status"] == "completed"


def test_missed_window_does_not_run_after_0300(client):
    set_clock(client, "2026-11-23T08:00:00Z")  # exactly 03:00, closed
    payload = tick(client)["status"]
    assert payload["window"]["phase"] == "closed"
    assert payload["today"]["job"] is None
    assert payload["today"]["artifact"] is None


def test_fall_back_window_runs_once_from_first_0130(client, engine):
    # 2026-11-01: 01:30 EDT = 05:30Z.
    payload = set_clock(client, "2026-11-01T05:30:00Z")
    assert payload["window"]["duration_seconds"] == 9000
    job = payload["today"]["job"]
    assert job["status"] == "completed"
    first_artifact = payload["today"]["artifact"]
    assert first_artifact is not None

    # Still inside the window at the second 01:30 (EST, 06:30Z): no second job,
    # the same artifact survives untouched.
    second = set_clock(client, "2026-11-01T06:30:00Z")
    assert second["window"]["phase"] == "open"
    assert second["today"]["job"]["status"] == "completed"
    assert second["today"]["job"]["attempts"] == 1
    assert second["today"]["artifact"]["sha256"] == first_artifact["sha256"]

    # 07:59Z is the last open minute; 08:00Z closes the window.
    assert set_clock(client, "2026-11-01T07:59:00Z")["window"]["phase"] == "open"
    closed = set_clock(client, "2026-11-01T08:00:00Z")
    assert closed["window"]["phase"] == "closed"
    assert closed["today"]["job"]["status"] == "completed"


def test_same_local_date_only_one_job_even_under_parallel_ticks(client, engine):
    # Set the clock without running a tick, so the job stays unclaimed.
    engine.clock().set_simulated(parse_db_ts("2026-11-23T06:31:00.000000+00:00"))
    state = client.application.config["DESK_STATE"]
    window = engine.current_window(engine.clock().now())
    state.ensure_job(window, engine.clock().now(), trigger="test")
    now = engine.clock().now()
    claims = [
        state.claim_job(window.local_date.isoformat(), f"owner-{index}", now, 60)
        for index in range(5)
    ]
    assert claims.count(True) == 1
    assert claims.count(False) == 4


def test_spring_forward_day_window_matches_real_30_minutes(client):
    payload = set_clock(client, "2026-03-08T06:30:00Z")  # 01:30 EST
    assert payload["window"]["duration_seconds"] == 1800
    assert payload["today"]["job"]["status"] == "completed"


def test_download_and_verify_endpoints(client):
    payload = set_clock(client, "2026-11-23T06:30:00Z")
    artifact = payload["today"]["artifact"]
    response = client.get(f"/download/{artifact['local_date']}")
    assert response.status_code == 200
    assert response.headers["X-Artifact-SHA256"] == artifact["sha256"]
    assert "attachment" in response.headers["Content-Disposition"]

    verify = client.get(f"/verify/{artifact['local_date']}").get_json()
    assert verify["verification"]["ok"] is True
    assert verify["sha256_on_disk"] == artifact["sha256"]
    assert verify["matches_record"] is True
    assert verify["invariants"]["ok"] is True

    assert client.get("/download/2026-01-01").status_code == 404
    assert client.get("/download/not-a-date").status_code == 404


def test_index_and_status_render(client):
    set_clock(client, "2026-11-23T06:30:00Z")
    page = client.get("/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "值班台账备份台" in body
    assert "ops-ledger-2026-11-23.sqlite3" in body
    assert client.get("/status").status_code == 200
    assert client.get("/status?format=json").is_json


def test_drill_clock_rejects_garbage(client):
    response = client.post("/drill/clock", json={"utc": "yesterday"})
    assert response.status_code == 400
    response = client.post("/drill/clock", json={})
    assert response.status_code == 400
    payload = status(client)
    assert payload["clock"]["mode"] == "real"


def test_real_clock_endpoint_restores_mode(client):
    set_clock(client, "2026-11-01T05:30:00Z")
    assert status(client)["clock"]["mode"] == "simulated"
    response = client.post(
        "/drill/real-clock", json={}, headers={"Accept": "application/json"}
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["clock"]["mode"] == "real"
    assert payload["clock"]["simulated_utc"] is not None  # value kept, mode ignored
    assert status(client)["clock"]["mode"] == "real"


def test_html_drill_forms_work_without_token(client):
    response = client.post(
        "/drill/clock", data={"utc": "2026-11-01T05:30:00Z"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "2026-11-01" in response.get_data(as_text=True)
    assert status(client)["clock"]["mode"] == "simulated"

    assert client.post("/drill/tick", data={}).status_code == 302
    assert client.post("/drill/restart", data={}).status_code == 302
    response = client.post("/drill/real-clock", data={}, follow_redirects=True)
    assert response.status_code == 200
    assert status(client)["clock"]["mode"] == "real"
