"""Unit tests for dibs.desk.scroll's per-click pacing (no display needed: win32api is mocked).

Real failure 2026-09-09: sending the whole `scroll_amount` in one pyautogui.scroll()/hscroll()
call let Chrome's smooth-scrolling collapse a burst of wheel deltas into a much smaller move than
requested. The first fix attempt (splitting into one pyautogui.scroll()/hscroll() call per click)
turned out to hit a SECOND bug: pyautogui's Windows backend passes the raw click count straight
through as mouse_event()'s dwData, but Windows expects dwData in units of WHEEL_DELTA (120) per
notch -- so even one-call-per-click barely moved the page. The real fix calls
win32api.mouse_event directly with dwData=+-WHEEL_DELTA per click, `SCROLL_CLICK_DELAY_S` apart.
"""

from __future__ import annotations

import pytest
import win32con

from dibs import desk


@pytest.fixture(autouse=True)
def no_dpi(monkeypatch):
    monkeypatch.setattr(desk, "set_dpi_aware", lambda: None)


@pytest.fixture
def no_motion(monkeypatch):
    monkeypatch.setattr(desk, "_motion_enabled", False)


def test_scroll_down_sends_one_wheel_event_per_click_with_delay(monkeypatch, no_motion):
    calls: list[tuple] = []
    sleeps: list[float] = []
    monkeypatch.setattr(desk.win32api, "mouse_event", lambda *args: calls.append(args))
    monkeypatch.setattr(desk.time, "sleep", lambda s: sleeps.append(s))

    desk.scroll("down", 5)

    assert calls == [(win32con.MOUSEEVENTF_WHEEL, 0, 0, -desk._WHEEL_DELTA, 0)] * 5
    assert sleeps == [desk.SCROLL_CLICK_DELAY_S] * 4  # gap between clicks, not after the last


def test_scroll_up_sends_one_wheel_event_per_click_with_full_wheel_delta(monkeypatch, no_motion):
    calls: list[tuple] = []
    monkeypatch.setattr(desk.win32api, "mouse_event", lambda *args: calls.append(args))
    monkeypatch.setattr(desk.time, "sleep", lambda s: None)

    desk.scroll("up", 3)

    assert calls == [(win32con.MOUSEEVENTF_WHEEL, 0, 0, desk._WHEEL_DELTA, 0)] * 3


def test_scroll_horizontal_uses_hwheel_flag(monkeypatch, no_motion):
    calls: list[tuple] = []
    monkeypatch.setattr(desk.win32api, "mouse_event", lambda *args: calls.append(args))
    monkeypatch.setattr(desk.time, "sleep", lambda s: None)

    desk.scroll("left", 2)
    assert calls == [(win32con.MOUSEEVENTF_HWHEEL, 0, 0, -desk._WHEEL_DELTA, 0)] * 2
    calls.clear()
    desk.scroll("right", 4)
    assert calls == [(win32con.MOUSEEVENTF_HWHEEL, 0, 0, desk._WHEEL_DELTA, 0)] * 4


def test_scroll_single_click_no_sleep(monkeypatch, no_motion):
    sleeps: list[float] = []
    monkeypatch.setattr(desk.win32api, "mouse_event", lambda *args: None)
    monkeypatch.setattr(desk.time, "sleep", lambda s: sleeps.append(s))

    desk.scroll("down", 1)

    assert sleeps == []


def test_scroll_moves_cursor_to_coordinate_even_with_motion_disabled(monkeypatch, no_motion):
    moved: list[tuple[int, int]] = []
    monkeypatch.setattr(desk.win32api, "mouse_event", lambda *args: None)
    monkeypatch.setattr(desk.pyautogui, "moveTo", lambda x, y: moved.append((x, y)))
    monkeypatch.setattr(desk.time, "sleep", lambda s: None)

    desk.scroll("down", 1, 900, 700)

    assert moved == [(900, 700)]


def test_scroll_bad_direction_rejected(no_motion):
    with pytest.raises(desk.DeskError):
        desk.scroll("sideways", 1)
