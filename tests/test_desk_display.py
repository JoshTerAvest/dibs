"""Real-machine display tests for dibs.desk.

Marked `display` — run explicitly with `pytest -m display`. Non-destructive: restores the
cursor position and clipboard.

IMPORTANT (see docs/SPEC.md deviation note / final report): this file deliberately does NOT
type into Notepad, despite that being the SPEC's suggested target. On this machine, Windows 11's
modern Notepad is a single-instance, tabbed, session-persisting app: `focus_window(title="Notepad")`
can match a pre-existing tab holding the user's real unsaved content instead of a fresh window from
`launch()`. That happened during development of this test and corrupted a real personal document
(stray characters inserted at the very start of an unsaved walk-note transcript) — see the final
report for details; the affected window was left open, untouched, for the user to fix by hand.

Instead, the unicode-typing / key-combo / clipboard round trip is verified against the Windows Run
dialog (Win+R): it is transient, per-invocation, holds no persistent user data, and is cancelled
with Escape (never Enter, so nothing is ever executed). `launch()` + `focus_window()` + closing a
window are verified against Calculator, which also holds no unsaved user data worth protecting.
Kept short — a few seconds of actual mouse/keyboard activity.
"""

from __future__ import annotations

import struct
import time

import pytest
import win32con
import win32gui

from dibs import desk

pytestmark = pytest.mark.display

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def restore_cursor():
    start = desk.cursor_position()
    yield
    try:
        desk.mouse_move(*start)
    except Exception:
        pass


def _png_dims(png: bytes) -> tuple[int, int]:
    assert png[:8] == PNG_MAGIC
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def _close_window(win: desk.Window) -> None:
    try:
        win32gui.PostMessage(win.hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass
    time.sleep(0.5)


def test_list_screens_returns_two_primary_at_origin():
    screens = desk.list_screens()
    assert len(screens) == 2
    assert screens[0].primary is True
    assert (screens[0].x, screens[0].y) == (0, 0)
    assert screens[0].width == 2560 and screens[0].height == 1440
    assert screens[1].primary is False


def test_screenshot_each_screen_png_and_dims_match_scale():
    for screen in desk.list_screens():
        shot = desk.screenshot(screen)
        assert shot.png[:8] == PNG_MAGIC
        w, h = _png_dims(shot.png)
        assert (w, h) == (shot.width, shot.height)
        assert shot.width == round(screen.width * shot.scale)
        assert shot.height == round(screen.height * shot.scale)


def test_zoom_region_native_when_under_limits():
    screen = desk.primary_screen()
    region = (screen.x, screen.y, screen.x + 400, screen.y + 300)
    shot = desk.zoom(screen, region)
    assert shot.png[:8] == PNG_MAGIC
    w, h = _png_dims(shot.png)
    assert (w, h) == (shot.width, shot.height)
    # 400x300 is well under both the long-edge and pixel-count limits, so it
    # should come back at native resolution, unscaled.
    assert shot.scale == 1.0
    assert (shot.width, shot.height) == (400, 300)


def test_mouse_move_cursor_position_round_trip():
    screen = desk.primary_screen()
    cx, cy = screen.x + screen.width // 2, screen.y + screen.height // 2
    desk.mouse_move(cx, cy)
    time.sleep(0.05)
    x, y = desk.cursor_position()
    assert abs(x - cx) <= 2
    assert abs(y - cy) <= 2


def test_list_windows_has_entries_and_a_foreground_window():
    windows = desk.list_windows()
    assert len(windows) > 0
    assert any(w.foreground for w in windows)
    assert windows[0].foreground  # foreground sorted first


def test_clipboard_round_trip():
    try:
        original = desk.get_clipboard()
    except desk.DeskError:
        original = ""
    marker = "dibs display test éè \U0001f600"
    try:
        desk.set_clipboard(marker)
        time.sleep(0.05)
        assert desk.get_clipboard() == marker
    finally:
        try:
            desk.set_clipboard(original or "")
        except Exception:
            pass


def test_launch_focus_and_close_calculator():
    """launch() + focus_window() + closing a window, against an app with no unsaved
    user data at risk (unlike Notepad on this machine — see module docstring)."""
    pid = desk.launch("calc.exe")
    assert pid > 0
    time.sleep(1.2)

    win = desk.focus_window(title="calculator")
    assert win32gui.GetForegroundWindow() == win.hwnd
    assert "calculator" in win.title.lower()

    _close_window(win)
    time.sleep(0.3)
    still_open = any(w.hwnd == win.hwnd for w in desk.list_windows())
    assert not still_open


def test_type_and_key_combo_round_trip_via_run_dialog():
    """Unicode SendInput typing + key-combo (ctrl+a/ctrl+c) + clipboard round trip,
    against the transient Run dialog. Cancelled with Escape — never Enter — so nothing
    is ever executed. See module docstring for why this replaces a Notepad-based test."""
    try:
        original_clipboard = desk.get_clipboard()
    except desk.DeskError:
        original_clipboard = ""

    desk.press_key(["win", "r"])
    time.sleep(0.5)
    win = desk.focus_window(title="Run")
    assert win32gui.GetForegroundWindow() == win.hwnd

    try:
        marker = "dibs test héllo wörld \U0001f44b"
        desk.type_text(marker)
        time.sleep(0.2)

        desk.press_key(["ctrl", "a"])
        time.sleep(0.1)
        desk.press_key(["ctrl", "c"])
        time.sleep(0.2)

        clipboard = desk.get_clipboard()
        assert clipboard == marker
    finally:
        # Escape cancels the dialog without running anything, regardless of what's in it.
        desk.press_key(["escape"])
        time.sleep(0.3)
        try:
            desk.set_clipboard(original_clipboard)
        except Exception:
            pass

    still_open = any(w.title.strip().lower() == "run" for w in desk.list_windows())
    assert not still_open
