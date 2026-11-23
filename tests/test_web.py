"""Token-gated drills and error handling."""

from __future__ import annotations

import importlib


def _app(settings, token, monkeypatch):
    import backupdesk.web as web

    monkeypatch.setenv("DRILL_TOKEN", token)
    importlib.reload(web)
    try:
        return web.create_app(settings, start_scheduler=False)
    finally:
        monkeypatch.delenv("DRILL_TOKEN", raising=False)


def test_token_is_enforced_on_every_drill_endpoint(settings, monkeypatch):
    app = _app(settings, "s3cret", monkeypatch)
    client = app.test_client()
    payload = {"utc": "2026-11-01T05:30:00Z"}
    assert client.post("/drill/clock", json=payload).status_code == 403
    assert (
        client.post("/drill/clock", json=payload, headers={"X-Drill-Token": "nope"}).status_code
        == 403
    )
    assert (
        client.post(
            "/drill/clock?format=json",
            json=payload,
            headers={"X-Drill-Token": "s3cret"},
        ).status_code
        == 200
    )
    assert client.post("/drill/tick", data={}).status_code == 403
    assert client.post("/drill/restart", data={}).status_code == 403
    assert client.post("/drill/real-clock", data={}).status_code == 403
    assert client.post("/drill/tick", data={"drill_token": "s3cret"}).status_code == 302
    # Read-only endpoints stay open.
    assert client.get("/status.json").status_code == 200
    app.config["DESK_ENGINE"].stop()


def test_token_form_renders_hidden_when_unset(settings):
    from backupdesk.web import create_app

    app = create_app(settings, start_scheduler=False)
    body = app.test_client().get("/").get_data(as_text=True)
    assert 'name="drill_token"' not in body
    app.config["DESK_ENGINE"].stop()


def test_disabled_drill_returns_403(settings):
    settings.drill_enabled = False
    from backupdesk.web import create_app

    app = create_app(settings, start_scheduler=False)
    client = app.test_client()
    response = client.post("/drill/clock", json={"utc": "2026-11-01T05:30:00Z"})
    assert response.status_code == 403
    app.config["DESK_ENGINE"].stop()


def test_unknown_date_and_bad_download(client):
    assert client.get("/download/2026-13-01").status_code == 404
    assert client.get("/verify/2026-13-01").status_code == 404
    assert client.get("/verify/2026-01-01").status_code == 404
