"""Tests for the v0.2 mode/consent/takeover flow (docs/SPEC-v0.2-human.md §2).

`FakePresence` replaces `dibs.presence.Presence` so these tests control "is a human active
right now?" directly instead of depending on real pynput hardware hooks or real wall-clock idle
waits. It's registered via the `fake_presence` fixture, which must be requested *before* the
test calls `make_client(...)` (fixtures always resolve before the test body runs, and Hub() --
which constructs the Presence instance -- isn't built until `make_client(...)` is called).
"""

from __future__ import annotations

import threading
import time as time_mod

import pytest

from dibs import hub as hub_mod
from tests.conftest import auth_headers, register


class FakePresence:
    """Test double for dibs.presence.Presence. Mirrors the real class's semantics (active =
    seconds-since-human < idle_after_s) but lets a test force a definite state instead of
    waiting on real timers or real hardware."""

    def __init__(self, idle_after_s, on_human_input=None, on_escape=None):
        self.idle_after_s = idle_after_s
        self.on_human_input = on_human_input
        self.on_escape = on_escape
        self._last_human_monotonic: float | None = None
        self._move_streak_start: float | None = None
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

    def move_streak_s(self) -> float | None:
        if self._move_streak_start is None:
            return None
        return max(0.0, time_mod.monotonic() - self._move_streak_start)

    # ---- test-only helpers (not part of the real Presence API) ----

    def set_active(self, active: bool) -> None:
        """Force a definite active/idle state deterministically."""
        if active:
            self._last_human_monotonic = time_mod.monotonic()
        else:
            self._last_human_monotonic = time_mod.monotonic() - (self.idle_after_s + 3600)

    def fire_human_input(self, kind: str | None = None) -> None:
        """Simulate a real human mouse/key event, including the on_human_input callback.
        `kind` mirrors the real Presence's move/scroll/click/key attribution (item 2); omit it
        to keep the old click-equivalent (immediate takeover) behavior existing callers rely on.
        """
        self._last_human_monotonic = time_mod.monotonic()
        if kind in ("move", "scroll"):
            if self._move_streak_start is None:
                self._move_streak_start = time_mod.monotonic()
        else:
            self._move_streak_start = None
        if self.on_human_input:
            if kind is None:
                self.on_human_input()
            else:
                self.on_human_input(kind)

    def fire_escape(self) -> None:
        """Simulate a real Escape press: the escape hook first, then the normal key attribution,
        exactly as Presence._on_press does."""
        if self.on_escape:
            self.on_escape()
        self.fire_human_input("key")


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
    with make_client(
        mode="hands_off", presence={"resume_after_s": 0.2, "consent_grace_s": 0}
    ) as client:
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


# ---------------------------------------------------------------------------
# consent-to-stillness grace window (item 1)
# ---------------------------------------------------------------------------


def test_grace_suppresses_input_right_after_consent_allow(make_client, fake_presence):
    with make_client(mode="ask", presence={"consent_grace_s": 0.3}) as client:
        hub = client.app.state.hub
        hub._presence.set_active(True)
        agent = register(client, "agent-a")

        first = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        request_id = first.json()["request_id"]
        client.post(f"/v1/admin/consent/{request_id}", json={"decision": "allow"})

        granted = client.post("/v1/lease", json={"wait_s": 0}, headers=auth_headers(agent["token"]))
        assert granted.json()["status"] == "granted"

        state = client.get("/v1/state").json()
        assert state["human"]["takeover_armed"] is False
        assert state["human"]["takeover_arms_at"] is not None

        # The accept gesture itself (mouse off the Allow button etc.) bleeds in right away --
        # it must not revoke the lease.
        hub._presence.fire_human_input()
        time_mod.sleep(0.05)
        state2 = client.get("/v1/state").json()
        assert state2["paused"] is False
        assert state2["lease"]["holder"]["agent_id"] == agent["agent_id"]

        # Human input after the grace window elapses still triggers takeover as today.
        time_mod.sleep(0.35)
        state3 = client.get("/v1/state").json()
        assert state3["human"]["takeover_armed"] is True
        hub._presence.fire_human_input()
        time_mod.sleep(0.05)
        state4 = client.get("/v1/state").json()
        assert state4["paused"] is True
        assert state4["pause_reason"] == "human_took_the_mouse"
        assert state4["lease"]["holder"] is None


def test_grace_applies_to_promptless_grant(make_client, fake_presence):
    """hands_off mode grants without a consent prompt, but the human may still be at the
    keyboard from typing the request -- grace must apply there too."""
    with make_client(mode="hands_off", presence={"consent_grace_s": 0.3}) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")

        granted = client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert granted.json()["status"] == "granted"

        hub._presence.fire_human_input()
        time_mod.sleep(0.05)
        state = client.get("/v1/state").json()
        assert state["paused"] is False
        assert state["lease"]["holder"] is not None

        time_mod.sleep(0.35)
        hub._presence.fire_human_input()
        time_mod.sleep(0.05)
        state2 = client.get("/v1/state").json()
        assert state2["paused"] is True
        assert state2["lease"]["holder"] is None


# ---------------------------------------------------------------------------
# two-tier takeover (item 2)
# ---------------------------------------------------------------------------


def test_brief_mouse_move_pauses_but_keeps_lease(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 2.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_human_input("move")
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["pause_reason"] == "human_took_the_mouse"
        assert state["lease"]["holder"] is not None
        assert state["lease"]["holder"]["agent_id"] == agent["agent_id"]

        audit_rows = client.get("/v1/audit").json()
        takeover_rows = [r for r in audit_rows if r["action"] == "human_takeover"]
        assert takeover_rows
        assert takeover_rows[-1]["input"]["tier"] == "pause"


def test_scroll_only_pauses_but_keeps_lease(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 2.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_human_input("scroll")
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is not None


def test_repeated_moves_in_pause_tier_record_one_takeover(make_client, fake_presence):
    """While paused with the lease intact, further move events are not new takeovers: one
    audit row and one overlay flash, not one per mouse event."""
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 2.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        for _ in range(25):
            hub._presence.fire_human_input("move")
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is not None

        audit_rows = client.get("/v1/audit").json()
        takeover_rows = [r for r in audit_rows if r["action"] == "human_takeover"]
        assert len(takeover_rows) == 1
        assert takeover_rows[0]["input"]["tier"] == "pause"


def test_click_revokes_immediately_even_if_brief(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 2.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_human_input("click")
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is None

        audit_rows = client.get("/v1/audit").json()
        takeover_rows = [r for r in audit_rows if r["action"] == "human_takeover"]
        assert takeover_rows[-1]["input"]["tier"] == "revoke"


def test_key_press_revokes_immediately(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 2.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_human_input("key")
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is None


def test_sustained_movement_escalates_pause_to_revoke(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 0.2}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_human_input("move")
        time_mod.sleep(0.05)
        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is not None  # still tier=pause, lease kept

        # movement continues past revoke_after_s -- next event escalates to a full revoke
        time_mod.sleep(0.2)
        hub._presence.fire_human_input("move")
        time_mod.sleep(0.05)
        state2 = client.get("/v1/state").json()
        assert state2["paused"] is True
        assert state2["lease"]["holder"] is None

        audit_rows = client.get("/v1/audit").json()  # newest first
        tiers = [r["input"]["tier"] for r in audit_rows if r["action"] == "human_takeover"]
        assert tiers[0] == "revoke"
        assert tiers[-1] == "pause"


def test_explicit_release_always_revokes_even_during_brief_move(make_client, fake_presence):
    with make_client(mode="hands_off", presence={"consent_grace_s": 0}) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub.human_release()

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is None


# ---------------------------------------------------------------------------
# wait_s long-polls a takeover pause instead of hard-erroring (item 3)
# ---------------------------------------------------------------------------


def test_wait_s_runs_the_action_once_resumed_mid_wait(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 5.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        # A brief mouse move -- tier=pause, keeps the lease, pauses automatically.
        hub._presence.fire_human_input("move")
        time_mod.sleep(0.05)
        assert client.get("/v1/state").json()["paused"] is True

        def _resume_later() -> None:
            time_mod.sleep(0.2)
            hub._loop.call_soon_threadsafe(hub.resume)

        threading.Thread(target=_resume_later, daemon=True).start()

        start = time_mod.monotonic()
        resp = client.post(
            "/v1/actions",
            json={"action": "key", "text": "a", "wait_s": 3},
            headers=auth_headers(agent["token"]),
        )
        elapsed = time_mod.monotonic() - start
        assert resp.status_code == 200
        assert elapsed < 3  # returned once resumed, not after the full wait_s


def test_manual_pause_returns_423_immediately_even_with_wait_s(make_client, fake_presence):
    with make_client(mode="hands_off") as client:
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        client.post("/v1/admin/pause", json={"reason": "manual"})

        start = time_mod.monotonic()
        resp = client.post(
            "/v1/actions",
            json={"action": "key", "text": "a", "wait_s": 5},
            headers=auth_headers(agent["token"]),
        )
        elapsed = time_mod.monotonic() - start
        assert resp.status_code == 423
        assert elapsed < 1.0


def test_wait_s_times_out_with_waited_seconds_in_detail(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 5.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_human_input("move")
        time_mod.sleep(0.05)

        resp = client.post(
            "/v1/actions",
            json={"action": "key", "text": "a", "wait_s": 1},
            headers=auth_headers(agent["token"]),
        )
        assert resp.status_code == 423
        body = resp.json()
        assert "human_took_the_mouse" in body["detail"]
        assert "waited" in body["detail"]
        assert "still active" in body["detail"]


def test_wait_s_zero_returns_423_immediately(make_client, fake_presence):
    with make_client(
        mode="hands_off", presence={"consent_grace_s": 0, "revoke_after_s": 5.0}
    ) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_human_input("move")
        time_mod.sleep(0.05)

        start = time_mod.monotonic()
        resp = client.post(
            "/v1/actions",
            json={"action": "key", "text": "a", "wait_s": 0},
            headers=auth_headers(agent["token"]),
        )
        elapsed = time_mod.monotonic() - start
        assert resp.status_code == 423
        assert elapsed < 0.3


# ---------------------------------------------------------------------------
# grace cancel: Esc and the band's Cancel button
# ---------------------------------------------------------------------------


def test_escape_during_grace_hands_the_desk_back(make_client, fake_presence):
    with make_client(mode="hands_off", presence={"consent_grace_s": 5.0}) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        assert hub._takeover_grace_active()

        hub._presence.fire_escape()
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is None
        assert not hub._takeover_grace_active()
        assert ("hide_grace", (), {}) in hub.overlay.calls
        rows = client.get("/v1/audit").json()
        cancel = [r for r in rows if r["action"] == "grace_cancelled"]
        assert cancel and cancel[-1]["input"]["via"] == "esc"


def test_escape_outside_grace_is_just_a_key(make_client, fake_presence):
    with make_client(mode="hands_off", presence={"consent_grace_s": 0}) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))

        hub._presence.fire_escape()
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True  # a key press is a revoke-tier takeover
        assert state["lease"]["holder"] is None
        rows = client.get("/v1/audit").json()
        assert not [r for r in rows if r["action"] == "grace_cancelled"]
        assert ("hide_grace", (), {}) not in hub.overlay.calls


def test_cancel_button_callback_hands_the_desk_back(make_client, fake_presence):
    with make_client(mode="hands_off", presence={"consent_grace_s": 5.0}) as client:
        hub = client.app.state.hub
        agent = register(client, "agent-a")
        client.post("/v1/lease", json={}, headers=auth_headers(agent["token"]))
        show = [c for c in hub.overlay.calls if c[0] == "show_grace"]
        assert show and show[-1][1][1] == "agent-a"  # the band names the agent

        hub._grace_cancelled()
        time_mod.sleep(0.05)

        state = client.get("/v1/state").json()
        assert state["paused"] is True
        assert state["lease"]["holder"] is None
        rows = client.get("/v1/audit").json()
        assert [r for r in rows if r["action"] == "grace_cancelled"][-1]["input"]["via"] == "button"
