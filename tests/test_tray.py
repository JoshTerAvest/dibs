"""Tray tests: the pure state/menu logic without a display, plus one real-tray display test."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from dibs import tray


def _state(**over):
    base = {
        "paused": False, "pause_reason": None, "mode": "ask",
        "lease": {"holder": None, "queue": []},
        "consent": {"pending": None, "windows": [], "recent": []},
        "human": {"active": False, "last_input_ago_s": 90, "idle_after_s": 30},
    }
    base.update(over)
    return base


def _holder(name="claude-code", secs=42):
    exp = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()
    return {"agent_id": f"{name}-1a2b", "name": name, "lease_id": "l1", "acquired_at": exp, "expires_at": exp}


class FakeActions:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def get_state(self):
        return self.state

    def pause(self): self.calls.append("pause")
    def resume(self): self.calls.append("resume")
    def release(self): self.calls.append("release")
    def set_mode(self, mode): self.calls.append(("mode", mode))
    def allow(self, rid): self.calls.append(("allow", rid))
    def deny(self, rid): self.calls.append(("deny", rid))
    def quit(self): self.calls.append("quit")


# ---- derive_state precedence ----

def test_idle_and_mode_label():
    name, tip, detail = tray.derive_state(_state())
    assert name == "idle" and "Ask me" in tip and detail.startswith("Idle")


def test_locked_when_nobody_holds():
    assert tray.derive_state(_state(mode="locked"))[0] == "locked"


def test_agent_has_dibs():
    name, tip, detail = tray.derive_state(_state(lease={"holder": _holder(), "queue": []}))
    assert name == "agent" and "claude-code has dibs" in tip and "has dibs" in detail


def test_consent_beats_agent():
    st = _state(lease={"holder": _holder(), "queue": []},
                consent={"pending": {"request_id": "r1", "name": "gemini", "purpose": "file taxes"}, "windows": [], "recent": []})
    name, tip, detail = tray.derive_state(st)
    assert name == "consent" and "gemini wants the desk" in tip and "file taxes" in detail


def test_human_pause_beats_everything():
    st = _state(paused=True, pause_reason="human_took_the_mouse", lease={"holder": _holder(), "queue": []})
    name, tip, detail = tray.derive_state(st)
    assert name == "human" and "you have the desk" in tip and "You have the desk" in detail


def test_manual_pause_is_red():
    name, tip, _ = tray.derive_state(_state(paused=True, pause_reason="dashboard"))
    assert name == "paused" and "paused (dashboard)" in tip


# ---- icons / menu ----

@pytest.mark.parametrize("name", list(tray.COLORS))
def test_icons_render(name):
    img = tray.make_icon(name, 64)
    assert img.size == (64, 64) and img.getbbox() is not None


def test_menu_spec_idle():
    spec = tray.menu_spec(_state())
    texts = [e.get("text") for e in spec if e["kind"] == "item"]
    assert texts[0].startswith("Idle") and spec[0]["enabled"] is False
    assert "Open monitor" in texts and "Pause agents" in texts and "Quit dibs" in texts
    take_back = next(e for e in spec if e.get("action") == "release")
    assert take_back["enabled"] is False
    modes = next(e for e in spec if e["kind"] == "submenu")["items"]
    assert [m["checked"] for m in modes] == [True, False, False]


def test_menu_spec_pending_and_paused():
    st = _state(paused=True, pause_reason="manual", lease={"holder": _holder(), "queue": []},
                consent={"pending": {"request_id": "r9", "name": "gemini", "purpose": "x"}, "windows": [], "recent": []})
    spec = tray.menu_spec(st)
    texts = [e.get("text") for e in spec if e["kind"] == "item"]
    assert "Resume agents" in texts and "Pause agents" not in texts
    assert "Allow gemini (5 min)" in texts and "Deny gemini" in texts
    assert next(e for e in spec if e.get("action") == "release")["enabled"] is True
    assert next(e for e in spec if e.get("text") == "Deny gemini")["action"] == ("deny", "r9")


def test_create_disabled_returns_null():
    t = tray.create(FakeActions(_state()), "http://127.0.0.1:7474", enabled=False)
    assert isinstance(t, tray.NullTray)
    t.start(); t.stop()
    assert t.calls == ["start", "stop"]


def test_notify_once_per_request():
    class FakeIcon:
        def __init__(self): self.notes = []
        def notify(self, msg, title): self.notes.append((msg, title))
    t = tray.Tray(FakeActions(_state()), "http://x")
    icon = FakeIcon()
    pending = {"request_id": "r1", "name": "gemini", "purpose": "taxes"}
    t._notify_for_pending(icon, pending)
    t._notify_for_pending(icon, pending)
    t._notify_for_pending(icon, {})
    t._notify_for_pending(icon, {"request_id": "r2", "name": "codex"})
    assert [n[0] for n in icon.notes] == ["gemini wants the desk — taxes", "codex wants the desk"]
    assert icon.notes[0][1] == "dibs"


# ---- real tray (display) ----

@pytest.mark.display
def test_real_tray_cycles_states():
    actions = FakeActions(_state())
    t = tray.Tray(actions, "http://127.0.0.1:7474", poll_s=0.2)
    t.start()
    assert t.available, "pystray failed to start"
    try:
        titles = [t.last_title]
        actions.state = _state(lease={"holder": _holder(), "queue": []})
        t.refresh(); time.sleep(0.6); titles.append(t.last_title)
        actions.state = _state(consent={"pending": {"request_id": "r1", "name": "gemini", "purpose": "demo"}, "windows": [], "recent": []})
        t.refresh(); time.sleep(0.6); titles.append(t.last_title)
        actions.state = _state(paused=True, pause_reason="human_took_the_mouse")
        t.refresh(); time.sleep(0.6); titles.append(t.last_title)
        assert len(set(titles)) == 4, titles
        assert t.last_state == "human"
    finally:
        t.stop()
