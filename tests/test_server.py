"""Tests for dibs/server.py + dibs/hub.py wired together over HTTP.

Uses fastapi.testclient.TestClient, which drives the app's lifespan (Hub.start()/stop())
via its `with` context manager. dibs.desk / dibs.actions are monkeypatched by the
`patch_desk` fixture (pulled in transitively via `make_client` -> `settings_factory`) so
nothing here touches the real desktop.
"""
from __future__ import annotations

import time

from tests.conftest import auth_headers, register


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_register_open_from_loopback_by_default(make_client):
    with make_client() as client:
        resp = client.post("/v1/agents", json={"name": "agent-a", "purpose": "test"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"].startswith("agent-a-")
        assert body["name"] == "agent-a"
        assert body["token"]
        assert body["created_at"]


def test_register_requires_admin_when_open_registration_disabled(make_client):
    with make_client(allow_local_open_registration=False) as client:
        resp = client.post("/v1/agents", json={"name": "agent-a", "purpose": "test"})
        assert resp.status_code == 401

        state_resp = client.get("/v1/state")  # dashboard_open_on_loopback still finds admin token
        assert state_resp.status_code == 200
        # fetch the admin token straight off the hub instead (no route exposes it)
        admin_token = client.app.state.hub.admin_token()
        resp2 = client.post("/v1/agents", json={"name": "agent-a", "purpose": "test"},
                             headers=auth_headers(admin_token))
        assert resp2.status_code == 200


def test_register_requires_admin_when_not_loopback(make_client):
    with make_client(client_host="10.0.0.9") as client:
        resp = client.post("/v1/agents", json={"name": "agent-a", "purpose": "test"})
        assert resp.status_code == 401

        admin_token = client.app.state.hub.admin_token()
        resp2 = client.post("/v1/agents", json={"name": "agent-a", "purpose": "test"},
                             headers=auth_headers(admin_token))
        assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def test_missing_token_rejected_on_protected_route(make_client):
    with make_client() as client:
        resp = client.post("/v1/actions", json={"action": "wait", "duration": 0})
        assert resp.status_code == 401
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "unauthorized"


def test_unknown_token_rejected(make_client):
    with make_client() as client:
        resp = client.post("/v1/actions", json={"action": "wait", "duration": 0},
                            headers=auth_headers("not-a-real-token"))
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"


def test_revoked_token_rejected(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        admin_token = client.app.state.hub.admin_token()
        del_resp = client.delete(f"/v1/agents/{agent['agent_id']}",
                                  headers=auth_headers(admin_token))
        assert del_resp.status_code == 204

        resp = client.post("/v1/actions", json={"action": "wait", "duration": 0},
                            headers=auth_headers(agent["token"]))
        assert resp.status_code == 401


def test_revoke_via_loopback_dashboard_without_token(make_client):
    with make_client() as client:  # dashboard_open_on_loopback defaults True
        agent = register(client, "agent-a")
        resp = client.delete(f"/v1/agents/{agent['agent_id']}")
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# lease
# ---------------------------------------------------------------------------

def test_lease_grant_and_state_reflects_holder(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        resp = client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "granted"
        assert body["agent_id"] == agent["agent_id"]

        state = client.get("/v1/state").json()
        assert state["lease"]["holder"]["agent_id"] == agent["agent_id"]
        holding_flags = {a["agent_id"]: a["holding"] for a in state["agents"]}
        assert holding_flags[agent["agent_id"]] is True


def test_lease_queue_when_held(make_client):
    with make_client() as client:
        a = register(client, "agent-a")
        b = register(client, "agent-b")
        client.post("/v1/lease", json={}, headers=auth_headers(a["token"]))

        resp = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(b["token"]))
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert body["position"] == 1
        assert body["holder"]["agent_id"] == a["agent_id"]


def test_lease_renew_and_not_holder(make_client):
    with make_client() as client:
        a = register(client, "agent-a")
        b = register(client, "agent-b")
        client.post("/v1/lease", json={}, headers=auth_headers(a["token"]))

        ok = client.post("/v1/lease/renew", json={"ttl_s": 30}, headers=auth_headers(a["token"]))
        assert ok.status_code == 200
        assert ok.json()["status"] == "granted"

        not_holder = client.post("/v1/lease/renew", json={}, headers=auth_headers(b["token"]))
        assert not_holder.status_code == 409
        assert not_holder.json()["error"] == "not_holder"


def test_lease_release_then_reacquire(make_client):
    with make_client() as client:
        a = register(client, "agent-a")
        b = register(client, "agent-b")
        client.post("/v1/lease", json={}, headers=auth_headers(a["token"]))

        rel = client.delete("/v1/lease", headers=auth_headers(a["token"]))
        assert rel.status_code == 204

        resp = client.post("/v1/lease", json={}, headers=auth_headers(b["token"]))
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == b["agent_id"]


def test_lease_force_release_requires_admin(make_client):
    with make_client() as client:
        a = register(client, "agent-a")
        b = register(client, "agent-b")
        client.post("/v1/lease", json={}, headers=auth_headers(a["token"]))

        forbidden = client.delete("/v1/lease?force=true", headers=auth_headers(b["token"]))
        assert forbidden.status_code == 403
        assert forbidden.json()["error"] == "admin_required"

        admin_token = client.app.state.hub.admin_token()
        ok = client.delete("/v1/lease?force=true", headers=auth_headers(admin_token))
        assert ok.status_code == 204

        state = client.get("/v1/state").json()
        assert state["lease"]["holder"] is None


def test_lease_force_release_from_loopback_dashboard_without_token(make_client):
    with make_client() as client:  # dashboard_open_on_loopback default True
        a = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(a["token"]))
        resp = client.delete("/v1/lease?force=true")
        assert resp.status_code == 204


def test_lease_expiry_via_sweeper(make_client):
    with make_client(lease_default_ttl_s=1, lease_max_ttl_s=600) as client:
        a = register(client, "agent-a")
        b = register(client, "agent-b")
        client.post("/v1/lease", json={}, headers=auth_headers(a["token"]))
        client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(b["token"]))

        # The background sweeper runs every 1s and the lease ttl is 1s, so a full promotion
        # can (worst case, depending on where the sweeper's own 1s phase lands relative to
        # when the lease was granted) take just under 3s. Poll instead of a single fixed sleep
        # so this isn't sensitive to that phase alignment.
        holder_id = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            holder = client.get("/v1/state").json()["lease"]["holder"]
            holder_id = holder["agent_id"] if holder else None
            if holder_id == b["agent_id"]:
                break
            time.sleep(0.1)

        assert holder_id == b["agent_id"]


# ---------------------------------------------------------------------------
# actions: lease gating, auto_lease, pause
# ---------------------------------------------------------------------------

def test_input_action_without_lease_is_409(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        resp = client.post("/v1/actions", json={"action": "left_click", "coordinate": [1, 1]},
                            headers=auth_headers(agent["token"]))
        assert resp.status_code == 409
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "lease_required"
        assert "holder" in body
        assert "queue_position" in body


def test_screenshot_needs_dibs_too(make_client):
    """Looking at the screen is a privacy act: even a screenshot needs dibs (9/4)."""
    with make_client() as client:
        agent = register(client, "agent-a")
        denied = client.post("/v1/actions", json={"action": "screenshot"},
                             headers=auth_headers(agent["token"]))
        assert denied.status_code == 409
        assert denied.json()["error"] == "lease_required"
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        resp = client.post("/v1/actions", json={"action": "screenshot"},
                            headers=auth_headers(agent["token"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "image" in body
        assert body["image"]["png_base64"]


def test_auto_lease_acquires_when_free(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        resp = client.post(
            "/v1/actions",
            json={"action": "left_click", "coordinate": [1, 1], "auto_lease": True},
            headers=auth_headers(agent["token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        state = client.get("/v1/state").json()
        assert state["lease"]["holder"]["agent_id"] == agent["agent_id"]


def test_input_action_with_lease_succeeds_and_touches_lease(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        resp = client.post("/v1/actions", json={"action": "key", "text": "Return"},
                            headers=auth_headers(agent["token"]))
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "result": "OK"}


def test_unknown_action_is_400(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        resp = client.post("/v1/actions", json={"action": "not_a_real_action"},
                            headers=auth_headers(agent["token"]))
        assert resp.status_code == 400
        assert resp.json()["error"] == "unknown_action"


def test_launch_disabled_by_default(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        resp = client.post("/v1/actions", json={"action": "launch", "command": "notepad.exe"},
                            headers=auth_headers(agent["token"]))
        assert resp.status_code == 403
        assert resp.json()["error"] == "launch_disabled"


def test_pause_blocks_input_but_not_read_only(make_client):
    with make_client() as client:  # dashboard_open_on_loopback default True
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        pause_resp = client.post("/v1/admin/pause", json={"reason": "manual"})
        assert pause_resp.status_code == 200

        blocked = client.post("/v1/actions", json={"action": "key", "text": "a"},
                               headers=auth_headers(agent["token"]))
        assert blocked.status_code == 423
        body = blocked.json()
        assert body["error"] == "paused"
        assert body["reason"] == "manual"

        still_ok = client.post("/v1/actions", json={"action": "wait", "duration": 0},
                                headers=auth_headers(agent["token"]))
        assert still_ok.status_code == 200

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["pause_reason"] == "manual"

        resume_resp = client.post("/v1/admin/resume")
        assert resume_resp.status_code == 200

        unblocked = client.post("/v1/actions", json={"action": "key", "text": "a"},
                                 headers=auth_headers(agent["token"]))
        assert unblocked.status_code == 200

        state2 = client.get("/v1/state").json()
        assert state2["paused"] is False
        assert state2["pause_reason"] is None


def test_admin_pause_requires_admin_when_not_loopback(make_client):
    with make_client(client_host="10.0.0.9") as client:
        resp = client.post("/v1/admin/pause", json={"reason": "manual"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------

def test_batch_stops_at_first_failure(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        resp = client.post(
            "/v1/actions/batch",
            json={"actions": [
                {"action": "wait", "duration": 0},
                {"action": "not_a_real_action"},
                {"action": "wait", "duration": 0},
            ]},
            headers=auth_headers(agent["token"]),
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 3
        assert results[0]["ok"] is True
        assert results[1]["ok"] is False
        assert results[1]["error"] == "unknown_action"
        assert results[2] == {"ok": False, "error": "not_executed",
                               "detail": "an earlier action in this batch failed"}


def test_batch_all_succeed(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        resp = client.post(
            "/v1/actions/batch",
            json={"actions": [{"action": "wait", "duration": 0}, {"action": "screenshot"}],
                  "auto_lease": True},
            headers=auth_headers(agent["token"]),
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert all(r["ok"] for r in results)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def test_audit_rows_appear_with_expected_shape(make_client):
    with make_client() as client:
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        client.post("/v1/actions", json={"action": "screenshot"},
                    headers=auth_headers(agent["token"]))
        client.post("/v1/actions", json={"action": "not_a_real_action"},
                    headers=auth_headers(agent["token"]))

        rows = client.get("/v1/audit").json()
        assert len(rows) >= 2
        row = rows[0]
        for key in ("id", "ts", "agent_id", "agent_name", "action", "input", "ok", "error",
                    "duration_ms", "screenshot_url"):
            assert key in row
        assert row["agent_id"] == agent["agent_id"]
        assert row["agent_name"] == "agent-a"

        # newest first, and the failed one is recorded too
        actions_seen = [r["action"] for r in rows]
        assert "not_a_real_action" in actions_seen
        failed_row = next(r for r in rows if r["action"] == "not_a_real_action")
        assert failed_row["ok"] is False
        assert failed_row["error"] == "unknown_action"

        # the screenshot action produced a stored screenshot file
        shot_row = next(r for r in rows if r["action"] == "screenshot")
        assert shot_row["screenshot_url"] == f"/v1/shots/{shot_row['id']}.png"
        shot_resp = client.get(shot_row["screenshot_url"])
        assert shot_resp.status_code == 200
        assert shot_resp.headers["content-type"] == "image/png"


def test_audit_filters_by_agent_id(make_client):
    with make_client() as client:
        a = register(client, "agent-a")
        b = register(client, "agent-b")
        client.post("/v1/actions", json={"action": "screenshot"}, headers=auth_headers(a["token"]))
        client.post("/v1/actions", json={"action": "screenshot"}, headers=auth_headers(b["token"]))

        rows = client.get(f"/v1/audit?agent_id={a['agent_id']}").json()
        assert len(rows) >= 1
        assert all(r["agent_id"] == a["agent_id"] for r in rows)


# ---------------------------------------------------------------------------
# state / display shapes
# ---------------------------------------------------------------------------

def test_state_shape(make_client):
    with make_client() as client:
        state = client.get("/v1/state").json()
        for key in ("version", "uptime_s", "paused", "pause_reason", "paused_at", "lease",
                    "agents", "display", "stats", "config", "mode", "human", "consent"):
            assert key in state
        assert set(state["lease"].keys()) == {"holder", "queue"}
        assert set(state["stats"].keys()) == {"actions_total", "actions_failed", "actions_last_5m"}
        for key in ("host", "port", "allow_launch", "mode", "overlay"):
            assert key in state["config"]
        assert state["mode"] in ("ask", "hands_off", "locked")
        assert set(state["human"].keys()) == {"active", "last_input_ago_s", "idle_after_s"}
        assert set(state["consent"].keys()) == {"pending", "windows", "recent"}
        assert state["consent"]["pending"] is None


def test_display_shape(make_client):
    with make_client() as client:
        display = client.get("/v1/display").json()
        assert "screens" in display and len(display["screens"]) == 2
        assert display["default_screen"] == 0
        for key in ("width", "height", "scale", "max_long_edge", "max_pixels"):
            assert key in display["screenshot"]
        screen0 = display["screens"][0]
        for key in ("index", "x", "y", "width", "height", "primary"):
            assert key in screen0


# ---------------------------------------------------------------------------
# dashboard cookie gate + shutdown (9/4 privacy rule)
# ---------------------------------------------------------------------------

def test_loopback_without_dashboard_cookie_is_401(make_client):
    with make_client() as client:
        client.cookies.clear()
        assert client.get("/v1/state").status_code == 401
        assert client.get("/v1/screenshot.png").status_code == 401
        # loading the page (like a browser) restores the cookie and the exemption
        assert client.get("/").status_code == 200
        assert client.get("/v1/state").status_code == 200


def test_admin_shutdown_needs_hook_and_calls_it(make_client):
    with make_client() as client:
        hub = client.app.state.hub
        hub.request_shutdown = None
        resp = client.post("/v1/admin/shutdown")
        assert resp.status_code == 409
        assert resp.json()["error"] == "not_serving"
        calls: list[int] = []
        hub.request_shutdown = lambda: calls.append(1)
        resp = client.post("/v1/admin/shutdown")
        assert resp.status_code == 200 and resp.json()["stopping"] is True

