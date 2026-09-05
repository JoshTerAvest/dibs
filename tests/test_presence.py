"""Tests for dibs/presence.py: human vs. agent-generated input, hotkey-chord filtering.

These drive the listener callbacks directly with fake events -- no real pynput hook is ever
started, so these tests are safe to run on a machine someone is actively using.
"""
from __future__ import annotations

import time

from dibs.presence import Presence


class _FakeKey:
    """Stand-in for a pynput Key/KeyCode: has `.name` (Key enum members) or `.char` (KeyCode)."""

    def __init__(self, name: str | None = None, char: str | None = None):
        self.name = name
        self.char = char


# ---------------------------------------------------------------------------
# agent-generated input is not human input
# ---------------------------------------------------------------------------

def test_injected_events_are_ignored():
    events = []
    p = Presence(idle_after_s=5, on_human_input=lambda: events.append(1))
    p._on_move(1, 2, injected=True)
    p._on_click(1, 2, "left", True, injected=True)
    p._on_scroll(1, 2, 0, 1, injected=True)
    p._on_press(_FakeKey(char="a"), injected=True)
    p._on_release(_FakeKey(char="a"), injected=True)
    assert p.seconds_since_human() is None
    assert events == []


def test_agent_input_until_window_is_ignored():
    p = Presence(idle_after_s=5)
    p.agent_input_until(time.monotonic() + 0.2)
    p._on_move(1, 2, injected=False)
    assert p.seconds_since_human() is None

    time.sleep(0.25)
    p._on_move(3, 4, injected=False)
    assert p.seconds_since_human() is not None


# ---------------------------------------------------------------------------
# real human input
# ---------------------------------------------------------------------------

def test_real_human_move_updates_seconds_since_human():
    p = Presence(idle_after_s=5)
    assert p.seconds_since_human() is None
    p._on_move(1, 2, injected=False)
    s = p.seconds_since_human()
    assert s is not None and s < 0.1


def test_real_human_click_and_scroll_count_too():
    p = Presence(idle_after_s=5)
    p._on_click(1, 2, "left", True, injected=False)
    assert p.seconds_since_human() is not None

    p = Presence(idle_after_s=5)
    p._on_scroll(1, 2, 0, -1, injected=False)
    assert p.seconds_since_human() is not None


def test_human_active_threshold():
    p = Presence(idle_after_s=0.2)
    p._on_move(1, 2, injected=False)
    assert p.human_active() is True
    time.sleep(0.3)
    assert p.human_active() is False


def test_on_human_input_callback_fires_and_is_debounced():
    calls = []
    p = Presence(idle_after_s=5, on_human_input=lambda: calls.append(time.monotonic()))
    p._on_move(1, 2, injected=False)
    p._on_move(1, 2, injected=False)  # within the 100ms debounce window -- must not fire again
    assert len(calls) == 1


def test_snapshot_shape_and_values():
    p = Presence(idle_after_s=5)
    assert p.snapshot() == {"active": False, "last_input_ago_s": None, "idle_after_s": 5}

    p._on_move(1, 2, injected=False)
    snap = p.snapshot()
    assert snap["active"] is True
    assert snap["last_input_ago_s"] is not None
    assert snap["idle_after_s"] == 5


# ---------------------------------------------------------------------------
# hotkey chords (ctrl+alt+shift+<P/Y/N/R>) don't read as "human wants the desk"
# ---------------------------------------------------------------------------

def test_bare_modifier_presses_do_not_count_as_human():
    calls = []
    p = Presence(idle_after_s=5, on_human_input=lambda: calls.append(1))
    p._on_press(_FakeKey(name="ctrl_l"), injected=False)
    p._on_press(_FakeKey(name="alt_l"), injected=False)
    p._on_press(_FakeKey(name="shift_l"), injected=False)
    assert calls == []
    assert p.seconds_since_human() is None


def test_hotkey_letter_ignored_while_full_chord_held():
    calls = []
    p = Presence(idle_after_s=5, on_human_input=lambda: calls.append(1))
    p._on_press(_FakeKey(name="ctrl_l"), injected=False)
    p._on_press(_FakeKey(name="alt_l"), injected=False)
    p._on_press(_FakeKey(name="shift_l"), injected=False)
    p._on_press(_FakeKey(char="p"), injected=False)  # the hotkey letter itself
    assert calls == []
    assert p.seconds_since_human() is None

    # releasing it (still chorded) is ignored too
    p._on_release(_FakeKey(char="p"), injected=False)
    assert calls == []


def test_non_chord_key_still_counts_as_human():
    calls = []
    p = Presence(idle_after_s=5, on_human_input=lambda: calls.append(1))
    p._on_press(_FakeKey(char="a"), injected=False)
    assert calls == [1]
    assert p.seconds_since_human() is not None


def test_partial_chord_key_still_counts_as_human():
    """Only ctrl+shift (no alt) held -- not our hotkey chord, so the key still counts."""
    calls = []
    p = Presence(idle_after_s=5, on_human_input=lambda: calls.append(1))
    p._on_press(_FakeKey(name="ctrl_l"), injected=False)
    p._on_press(_FakeKey(name="shift_l"), injected=False)
    p._on_press(_FakeKey(char="s"), injected=False)
    assert calls == [1]


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def test_stop_without_start_does_not_raise_or_hang():
    p = Presence(idle_after_s=1)
    p.stop()  # must be a safe no-op
