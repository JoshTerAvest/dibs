"""Tests for the dashboard: asserts the owned static files exist and are wired
together correctly, and that the mock server serves them plus /v1/state.

Owner: dashboard agent.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dashboard_mock_server import app

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dibs" / "dashboard"


def test_dashboard_files_exist():
    assert (DASHBOARD_DIR / "index.html").is_file()
    assert (DASHBOARD_DIR / "app.js").is_file()
    assert (DASHBOARD_DIR / "style.css").is_file()


def test_index_references_app_js_and_style_css():
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
    assert "app.js" in html
    assert "style.css" in html


def test_mock_server_serves_root():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "dibs" in resp.text.lower()


def test_mock_server_serves_static_assets():
    client = TestClient(app)
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_mock_server_state_endpoint():
    client = TestClient(app)
    resp = client.get("/v1/state")
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "version",
        "uptime_s",
        "paused",
        "lease",
        "agents",
        "display",
        "stats",
        "config",
        "mode",
        "human",
        "consent",
    ):
        assert key in body
    for key in ("host", "port", "allow_launch", "mode", "overlay"):
        assert key in body["config"]
    for key in ("pending", "windows", "recent"):
        assert key in body["consent"]
    for key in ("active", "last_input_ago_s", "idle_after_s"):
        assert key in body["human"]


def test_mock_server_pause_resume_round_trip():
    client = TestClient(app)
    resp = client.post("/v1/admin/pause", json={"reason": "dashboard"})
    assert resp.status_code == 200
    assert client.get("/v1/state").json()["paused"] is True

    resp = client.post("/v1/admin/resume")
    assert resp.status_code == 200
    assert client.get("/v1/state").json()["paused"] is False


def test_mock_server_screenshot_and_shot_png():
    client = TestClient(app)
    resp = client.get("/v1/screenshot.png?screen=0&scale=0.5")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"

    resp = client.get("/v1/shots/1.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_mock_server_audit_endpoint():
    client = TestClient(app)
    resp = client.get("/v1/audit?limit=50")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) > 0
    for key in ("id", "ts", "agent_id", "agent_name", "action", "input", "ok", "duration_ms"):
        assert key in rows[0]


def test_mock_server_mode_round_trip():
    client = TestClient(app)
    resp = client.post("/v1/admin/mode", json={"mode": "hands_off"})
    assert resp.status_code == 200
    assert resp.json() == {"mode": "hands_off"}
    state = client.get("/v1/state").json()
    assert state["mode"] == "hands_off"
    assert state["config"]["mode"] == "hands_off"

    # restore default so other tests (and a human reading canned state) see "ask"
    client.post("/v1/admin/mode", json={"mode": "ask"})


def test_mock_server_mode_rejects_invalid_value():
    client = TestClient(app)
    resp = client.post("/v1/admin/mode", json={"mode": "not_a_mode"})
    assert resp.status_code == 400


def test_mock_server_consent_pending_and_decision():
    client = TestClient(app)
    state = client.get("/v1/state").json()
    pending = state["consent"]["pending"]
    assert pending is not None
    for key in ("request_id", "agent_id", "name", "purpose", "requested_at", "expires_at"):
        assert key in pending

    resp = client.post(f"/v1/admin/consent/{pending['request_id']}", json={"decision": "allow"})
    assert resp.status_code == 200
    assert resp.json()["decision"] == "allow"

    state2 = client.get("/v1/state").json()
    assert state2["consent"]["pending"] is None
    recent_ids = [r["request_id"] for r in state2["consent"]["recent"]]
    assert pending["request_id"] in recent_ids
    window_agents = [w["agent_id"] for w in state2["consent"]["windows"]]
    assert pending["agent_id"] in window_agents


def test_mock_server_consent_decision_unknown_request_404():
    client = TestClient(app)
    resp = client.post("/v1/admin/consent/not-a-real-request", json={"decision": "allow"})
    assert resp.status_code == 404


def test_mock_server_release_pauses_with_human_reason():
    client = TestClient(app)
    resp = client.post("/v1/admin/release")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paused"] is True
    assert body["reason"] == "human_took_the_mouse"

    state = client.get("/v1/state").json()
    assert state["paused"] is True
    assert state["pause_reason"] == "human_took_the_mouse"
    assert state["lease"]["holder"] is None

    # leave paused=False for any test that runs after this one and checks the default
    client.post("/v1/admin/resume")
