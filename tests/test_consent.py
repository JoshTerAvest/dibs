"""Tests for the v0.2 mode/consent/takeover flow (docs/SPEC-v0.2-human.md §2).

`FakePresence` replaces `dibs.presence.Presence` so these tests control "is a human active
right now?" directly instead of depending on real pynput hardware hooks or real wall-clock idle
waits. It's registered via the `fake_presence` fixture, which must be requested *before* the
test calls `make_client(...)` (fixtures always resolve before the test body runs, and Hub() --
which constructs the Presence instance -- isn't built until `make_client(...)` is called).
"""

from __future__ import annotations

import time as time_mod

import pytest

from tests.conftest import auth_headers, register
from dibs import hub as hub_mod


class FakePresence:
    """Test double for dibs.presence.Presence. Mirrors the real class's semantics (active =
    seconds-since-human < idle_after_s) but lets a test force a definite state instead of
    waiting on real timers or real hardware."""

    def __init__(self, idle_after_s, on_human_input=None):
        self.idle_after_s = idle_after_s
        self.on_human_input = on_human_input
        self._last_human_monotonic: float | None = None
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def agent_input_until(self, deadline_monotonic: float) -> None:
        pass

    def seconds_since_human(self) -> float | None:
        if self._last_human_monotonic is None:
            return None
        return max(0.0, time_mod.monotonic() - self._last_human_monotonic)

    def human_active(self) -> bool:
        s = self.seconds_since_human()
        return s is not None and s < self.idle_after_s

    def snapshot(self) -> dict:
        return {
            "active": self.human_active(),
            "last_input_ago_s": self.seconds_since_human(),
            "idle_after_s": self.idle_after_s,
        }

    # ---- test-only helpers (not part of the real Presence API) ----

    def set_active(self, active: bool) -> None:
        """Force a definite active/idle state deterministically."""
        if active:
            self._last_human_monotonic = time_mod.monotonic()
        else:
            self._last_human_monotonic = time_mod.monotonic() - (self.idle_after_s + 3600)

    def fire_human_input(self) -> None:
        """Simulate a real human mouse/key event, including the on_human_input callback."""
        self._last_human_monotonic = time_mod.monotonic()
        if self.on_human_input:
            self.on_human_input()


@pytest.fixture
def fake_presence(monkeypatch):
    monkeypatch.setattr(hub_mod.presence, "Presence", FakePresence)


# ---------------------------------------------------------------------------
# acquire flow: mode ask / hands_off / locked
# ---------------------------------------------------------------------------


def test_ask_mode_still_asks_when_human_idle(make_client, fake_presence):
    """Idle is not consent (9/4): an agent may not look at the screen just because nobody is there."""
    with make_client(mode="ask") as client:
        client.app.state.hub._presence.set_active(False)
        agent = register(client, "agent-a")
        resp = client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert resp.status_code == 202
        assert resp.json()["status"] == "awaiting_consent"


def test_ask_mode_active_creates_consent_request_then_allow_grants(make_client, fake_presence):
    with make_client(mode="ask") as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")

        resp = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "awaiting_consent"
        assert body["request_id"]
        assert "expires_at" in body
        assert "human" in body

        state = client.get("/v1/state").json()
        pending = state["consent"]["pending"]
        assert pending is not None
        assert pending["agent_id"] == agent["agent_id"]
        assert pending["request_id"] == body["request_id"]

        allow_resp = client.post(
            f"/v1/admin/consent/{body['request_id']}", json={"decision": "allow"}
        )
        assert allow_resp.status_code == 200

        granted = client.post("/v1/lease", json={"wait_s": 2}, headers=auth_headers(agent["token"]))
        assert granted.status_code == 200
        assert granted.json()["status"] == "granted"

        state2 = client.get("/v1/state").json()
        assert state2["consent"]["pending"] is None
        assert any(r["decision"] == "allow" for r in state2["consent"]["recent"])


def test_deny_then_cooldown_blocks_reacquire(make_client, fake_presence):
    with make_client(mode="ask", presence={"deny_cooldown_s": 30}) as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")

        resp = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        request_id = resp.json()["request_id"]

        deny_resp = client.post(f"/v1/admin/consent/{request_id}", json={"decision": "deny"})
        assert deny_resp.status_code == 200

        denied = client.post("/v1/lease", json={"wait_s": 1}, headers=auth_headers(agent["token"]))
        assert denied.status_code == 403
        body = denied.json()
        assert body["status"] == "denied"
        assert body["reason"] == "human_denied"
        assert 0 < body["retry_after_s"] <= 30

        # still in cooldown -- immediate 403, no new prompt
        denied2 = client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert denied2.status_code == 403
        assert denied2.json()["reason"] == "human_denied"

        state = client.get("/v1/state").json()
        assert state["consent"]["pending"] is None


def test_no_decision_times_out_as_denied(make_client, fake_presence):
    with make_client(mode="ask", presence={"consent_timeout_s": 0.3}) as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")

        first = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        assert first.status_code == 202

        resp = client.post("/v1/lease", json={"wait_s": 2}, headers=auth_headers(agent["token"]))
        assert resp.status_code == 403
        body = resp.json()
        assert body["status"] == "denied"
        assert body["reason"] == "timeout"
        assert body["retry_after_s"] == 60


def test_human_going_idle_while_pending_does_not_auto_allow(make_client, fake_presence):
    with make_client(mode="ask") as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")

        first = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        assert first.status_code == 202

        hub._presence.set_active(False)  # human walks away: the request must NOT resolve itself
        resp = client.post("/v1/lease", json={"wait_s": 1}, headers=auth_headers(agent["token"]))
        assert resp.status_code == 202
        assert resp.json()["status"] == "awaiting_consent"

        state = client.get("/v1/state").json()
        assert not any(r["decision"] == "human_idle" for r in state["consent"]["recent"])
        assert state["consent"]["pending"] is not None


def test_hands_off_mode_grants_even_while_human_active(make_client, fake_presence):
    with make_client(mode="hands_off") as client:
        client.app.state.hub._presence.set_active(True)
        agent = register(client, "agent-a")
        resp = client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert resp.status_code == 200
        assert resp.json()["status"] == "granted"


def test_locked_mode_always_denies(make_client, fake_presence):
    with make_client(mode="locked") as client:
        agent = register(client, "agent-a")
        resp = client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert resp.status_code == 403
        body = resp.json()
        assert body["status"] == "denied"
        assert body["reason"] == "locked"


def test_consent_window_skips_a_second_prompt(make_client, fake_presence):
    with make_client(mode="ask", presence={"consent_grant_s": 30}) as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")

        first = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        request_id = first.json()["request_id"]
        client.post(f"/v1/admin/consent/{request_id}", json={"decision": "allow"})

        granted = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        assert granted.status_code == 200

        client.delete("/v1/lease", headers=auth_headers(agent["token"]))

        # re-acquire inside the consent_grant_s window -- granted immediately, no new prompt
        second = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        assert second.status_code == 200
        assert second.json()["status"] == "granted"

        state = client.get("/v1/state").json()
        assert state["consent"]["pending"] is None
        assert any(w["agent_id"] == agent["agent_id"] for w in state["consent"]["windows"])


# ---------------------------------------------------------------------------
# human takeover (SPEC-v0.2 §2.3)
# ---------------------------------------------------------------------------


def test_human_takeover_revokes_lease_and_pauses(make_client, fake_presence):
    with make_client(mode="hands_off", presence={"resume_after_s": 0.2}) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        lease_resp = client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert lease_resp.status_code == 200

        hub._presence.fire_human_input()
        time_mod.sleep(0.2)  # let the call_soon_threadsafe callback run on the app's loop

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["pause_reason"] == "human_took_the_mouse"
        assert state["lease"]["holder"] is None

        # the interrupted agent's next input action -> 409 lease_required, "desk taken by human"
        blocked = client.post(
            "/v1/actions", json={"action": "key", "text": "a"}, headers=auth_headers(agent["token"])
        )
        assert blocked.status_code == 409
        body = blocked.json()
        assert body["error"] == "lease_required"
        assert body["detail"] == "desk taken by human"
        assert body["human_active"] is True

        # since 9/4 even a screenshot needs dibs, and the takeover revoked them -> 409; `wait` is free
        ro = client.post(
            "/v1/actions", json={"action": "screenshot"}, headers=auth_headers(agent["token"])
        )
        assert ro.status_code == 409
        free = client.post(
            "/v1/actions",
            json={"action": "wait", "duration": 0},
            headers=auth_headers(agent["token"]),
        )
        assert free.status_code == 200

        # human goes idle -- auto-resume once the sweeper notices (ticks every 0.5s)
        hub._presence.set_active(False)
        time_mod.sleep(1.0)
        state2 = client.get("/v1/state").json()
        assert state2["paused"] is False
        assert state2["pause_reason"] is None

        audit_rows = client.get("/v1/audit").json()
        assert any(r["action"] == "human_takeover" for r in audit_rows)


def test_manual_pause_never_auto_resumes(make_client, fake_presence):
    with make_client(presence={"resume_after_s": 0.2}) as client:
        hub = client.app.state.hub
        hub._presence.set_active(False)
        pause_resp = client.post("/v1/admin/pause", json={"reason": "manual"})
        assert pause_resp.status_code == 200

        time_mod.sleep(1.0)
        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["pause_reason"] == "manual"


def test_admin_release_route_revokes_and_pauses(make_client, fake_presence):
    with make_client(mode="hands_off") as client:
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        resp = client.post("/v1/admin/release")
        assert resp.status_code == 200

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["pause_reason"] == "human_took_the_mouse"
        assert state["lease"]["holder"] is None


def test_admin_release_pauses_even_with_nobody_holding_the_desk(make_client, fake_presence):
    with make_client() as client:
        resp = client.post("/v1/admin/release")
        assert resp.status_code == 200
        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["pause_reason"] == "human_took_the_mouse"


# ---------------------------------------------------------------------------
# admin routes: mode, consent decisions
# ---------------------------------------------------------------------------


def test_mode_route_sets_and_rejects_invalid(make_client):
    with make_client() as client:
        resp = client.post("/v1/admin/mode", json={"mode": "locked"})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "locked"

        state = client.get("/v1/state").json()
        assert state["mode"] == "locked"
        assert state["config"]["mode"] == "locked"

        bad = client.post("/v1/admin/mode", json={"mode": "not_a_mode"})
        assert bad.status_code == 400


def test_mode_route_requires_admin_when_not_loopback(make_client):
    with make_client(client_host="10.0.0.9") as client:
        resp = client.post("/v1/admin/mode", json={"mode": "locked"})
        assert resp.status_code == 401


def test_consent_decision_on_unknown_request_is_404(make_client):
    with make_client() as client:
        resp = client.post("/v1/admin/consent/does-not-exist", json={"decision": "allow"})
        assert resp.status_code == 404
        assert resp.json()["error"] == "no_pending_request"


def test_consent_decision_rejects_bad_decision_value(make_client, fake_presence):
    with make_client(mode="ask") as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")
        first = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        request_id = first.json()["request_id"]

        resp = client.post(f"/v1/admin/consent/{request_id}", json={"decision": "maybe"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# hotkeys
# ---------------------------------------------------------------------------


def test_hotkey_parse_combo():
    assert hub_mod.Hub._parse_hotkey("ctrl+alt+shift+p") == "<ctrl>+<alt>+<shift>+p"
    assert hub_mod.Hub._parse_hotkey("ctrl+alt+shift+y") == "<ctrl>+<alt>+<shift>+y"
    assert hub_mod.Hub._parse_hotkey("ctrl+alt+shift+n") == "<ctrl>+<alt>+<shift>+n"
    assert hub_mod.Hub._parse_hotkey("ctrl+alt+shift+r") == "<ctrl>+<alt>+<shift>+r"


def test_hotkey_listener_registers_all_four_combos(make_client, monkeypatch):
    captured: dict = {}

    class _CapturingGlobalHotKeys:
        def __init__(self, combos):
            captured["combos"] = combos

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    import pynput.keyboard as kb

    monkeypatch.setattr(kb, "GlobalHotKeys", _CapturingGlobalHotKeys)

    with make_client():
        pass

    assert captured.get("combos") is not None
    assert len(captured["combos"]) == 4
    for chord in (
        "<ctrl>+<alt>+<shift>+p",
        "<ctrl>+<alt>+<shift>+y",
        "<ctrl>+<alt>+<shift>+n",
        "<ctrl>+<alt>+<shift>+r",
    ):
        assert chord in captured["combos"]


def test_hotkey_allow_deny_act_on_pending_request(make_client, fake_presence):
    with make_client(mode="ask") as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")

        first = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        assert first.status_code == 202

        hub._hotkey_allow()  # simulates Ctrl+Alt+Shift+Y

        granted = client.post("/v1/lease", json={"wait_s": 1}, headers=auth_headers(agent["token"]))
        assert granted.status_code == 200
        assert granted.json()["status"] == "granted"


def test_hotkey_pause_toggles(make_client, fake_presence):
    with make_client() as client:
        hub = client.app.state.hub
        assert hub._paused is False
        hub._hotkey_pause()
        assert hub._paused is True
        assert hub._pause_manual is True
        hub._hotkey_pause()
        assert hub._paused is False


def test_hotkey_release_triggers_takeover(make_client, fake_presence):
    with make_client(mode="hands_off") as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._hotkey_release()

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["pause_reason"] == "human_took_the_mouse"
        assert state["lease"]["holder"] is None
