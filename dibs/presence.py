"""Human presence detection via pynput. Owner: hub agent. See docs/SPEC-v0.2-human.md §1.

`Presence` answers "is a human at the keyboard/mouse right now?" by running pynput's Win32
mouse + keyboard listeners on their own (daemon) threads. Two independent filters keep
agent-generated input from being counted as human activity:

1. pynput's Win32 backend passes an `injected` flag on every callback (mouse `on_move(x, y,
   injected)`, `on_click(x, y, button, pressed, injected)`, `on_scroll(x, y, dx, dy, injected)`;
   keyboard `on_press(key, injected)` / `on_release(key, injected)` -- see
   `pynput/mouse/_win32.py` and `pynput/keyboard/_win32.py`, which set the flag from the raw
   LLMHF_INJECTED / LLKHF_INJECTED bits pyautogui's SendInput calls set). Injected events are
   always ignored.
2. `agent_input_until(deadline)` lets the hub mark a short window (action duration + grace) as
   "this is the agent, not the human" for the rare cases an injected event doesn't carry the
   flag reliably (or arrives slightly outside it).

The dibs hotkey chords (ctrl+alt+shift+<P/Y/N/R>) are also kept from reading as "the human
grabbed the desk": bare modifier presses/releases (ctrl/alt/shift alone) never count as human
activity on their own (they're extremely common as chord legs and carry little signal by
themselves), and a non-modifier key pressed *while* ctrl+alt+shift are all currently held is
assumed to be one of our own hotkeys and is ignored too. The hub additionally calls
`agent_input_until()` right after a hotkey fires, suppressing the trailing key-up events as a
second layer of defense (belt and suspenders, per SPEC-v0.2 §1/§4).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_HUMAN_CALLBACK_DEBOUNCE_S = 0.1

# Canonical modifier name -> the pynput Key.* names that count as "that modifier". Right-side
# variants (ctrl_r, alt_r/alt_gr, shift_r) and the bare `ctrl`/`alt`/`shift` some backends report
# are all folded together so "ctrl+alt+shift" is detected regardless of which physical keys were
# used.
_CHORD_MODIFIERS: dict[str, frozenset[str]] = {
    "ctrl": frozenset({"ctrl", "ctrl_l", "ctrl_r"}),
    "alt": frozenset({"alt", "alt_l", "alt_r", "alt_gr"}),
    "shift": frozenset({"shift", "shift_l", "shift_r"}),
}


class Presence:
    def __init__(
        self, idle_after_s: float, on_human_input: Callable[[], None] | None = None
    ) -> None:
        self.idle_after_s = idle_after_s
        self.on_human_input = on_human_input

        self._lock = threading.Lock()
        self._last_human_monotonic: float | None = None
        self._last_callback_monotonic: float = 0.0
        self._agent_deadline: float = 0.0
        self._mod_down: dict[str, int] = {name: 0 for name in _CHORD_MODIFIERS}

        self._mouse_listener: Any = None
        self._keyboard_listener: Any = None

    # ---- lifecycle ----

    def start(self) -> None:
        from pynput import keyboard, mouse

        self._mouse_listener = mouse.Listener(
            on_move=self._on_move,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        self._mouse_listener.daemon = True
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._keyboard_listener.daemon = True
        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop(self) -> None:
        for listener in (self._mouse_listener, self._keyboard_listener):
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:
                logger.exception("failed to stop presence listener")
        for listener in (self._mouse_listener, self._keyboard_listener):
            if listener is None:
                continue
            try:
                listener.join(timeout=1.0)
            except Exception:
                logger.exception("failed to join presence listener")
        self._mouse_listener = None
        self._keyboard_listener = None

    # ---- agent attribution ----

    def agent_input_until(self, deadline_monotonic: float) -> None:
        """Events observed before this monotonic deadline are the agent's, not the human's."""
        with self._lock:
            self._agent_deadline = max(self._agent_deadline, deadline_monotonic)

    # ---- queries ----

    def seconds_since_human(self) -> float | None:
        with self._lock:
            last = self._last_human_monotonic
        if last is None:
            return None
        return max(0.0, time.monotonic() - last)

    def human_active(self) -> bool:
        seconds = self.seconds_since_human()
        return seconds is not None and seconds < self.idle_after_s

    def snapshot(self) -> dict:
        return {
            "active": self.human_active(),
            "last_input_ago_s": self.seconds_since_human(),
            "idle_after_s": self.idle_after_s,
        }

    # ---- shared filtering ----

    def _is_agent_generated(self, injected: bool) -> bool:
        if injected:
            return True
        with self._lock:
            return time.monotonic() < self._agent_deadline

    def _register_human(self) -> None:
        now = time.monotonic()
        fire = False
        with self._lock:
            self._last_human_monotonic = now
            if now - self._last_callback_monotonic >= _HUMAN_CALLBACK_DEBOUNCE_S:
                self._last_callback_monotonic = now
                fire = True
        if fire and self.on_human_input is not None:
            try:
                self.on_human_input()
            except Exception:
                logger.exception("Presence.on_human_input callback raised")

    # ---- hotkey-chord filtering ----

    def _chord_active(self) -> bool:
        return all(count > 0 for count in self._mod_down.values())

    @staticmethod
    def _mod_name(key: Any) -> str | None:
        name = getattr(key, "name", None)
        if name is None:
            return None
        for canon, names in _CHORD_MODIFIERS.items():
            if name in names:
                return canon
        return None

    def _update_mod_state(self, key: Any, pressed: bool) -> str | None:
        mod = self._mod_name(key)
        if mod is None:
            return None
        with self._lock:
            if pressed:
                self._mod_down[mod] += 1
            else:
                self._mod_down[mod] = max(0, self._mod_down[mod] - 1)
        return mod

    # ---- mouse callbacks --
    # pynput Win32: on_move(x, y, injected), on_click(x, y, button, pressed, injected),
    # on_scroll(x, y, dx, dy, injected).

    def _on_move(self, x: int, y: int, injected: bool = False) -> None:
        if self._is_agent_generated(injected):
            return
        self._register_human()

    def _on_click(self, x: int, y: int, button: Any, pressed: bool, injected: bool = False) -> None:
        if self._is_agent_generated(injected):
            return
        self._register_human()

    def _on_scroll(self, x: int, y: int, dx: int, dy: int, injected: bool = False) -> None:
        if self._is_agent_generated(injected):
            return
        self._register_human()

    # ---- keyboard callbacks -- pynput Win32: on_press(key, injected), on_release(key, injected) ----

    def _on_press(self, key: Any, injected: bool = False) -> None:
        mod = self._update_mod_state(key, True)
        if self._is_agent_generated(injected):
            return
        if mod is not None:
            # A bare modifier leg (ctrl/alt/shift alone) isn't itself "the human wants the
            # desk" -- only a real key is.
            return
        if self._chord_active():
            # ctrl+alt+shift+<key> -- one of our own hotkeys (or shaped exactly like one).
            return
        self._register_human()

    def _on_release(self, key: Any, injected: bool = False) -> None:
        was_chord = self._chord_active()
        mod = self._update_mod_state(key, False)
        if self._is_agent_generated(injected):
            return
        if mod is not None:
            return
        if was_chord:
            return
        self._register_human()
