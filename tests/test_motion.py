"""Tests for dibs/desk.py's human-like mouse motion (SPEC-v0.3 §2).

`motion_path` is a pure function and is tested exhaustively with no display needed. The single
`display`-marked test drives the real cursor around a small square on this machine (it is
expected that the user sees the cursor move) -- run explicitly with `pytest -m display`.
"""
from __future__ import annotations

import math

import pytest

from dibs import desk


# ---------------------------------------------------------------------------
# motion_path() -- pure function
# ---------------------------------------------------------------------------

def test_motion_path_starts_and_ends_exactly():
    path = desk.motion_path(100, 200, 500, 350, speed=1.0, seed=1)
    t0, x0, y0 = path[0]
    tn, xn, yn = path[-1]
    assert t0 == 0.0
    assert (x0, y0) == (100.0, 200.0)
    assert (xn, yn) == (500.0, 350.0)
    assert tn > 0.0


def test_motion_path_time_is_monotonic():
    path = desk.motion_path(0, 0, 800, 600, speed=1.0, seed=2)
    times = [t for t, _, _ in path]
    assert times == sorted(times)
    assert len(set(times)) == len(times)  # strictly increasing, no repeats


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0, 4.0])
def test_motion_path_step_count_is_sane(speed):
    path = desk.motion_path(0, 0, 1000, 0, speed=speed, seed=3)
    duration = path[-1][0]
    # 90 Hz sampling: step count should track duration*90 within a couple of steps.
    expected_steps = round(duration * desk._MOTION_HZ)
    assert abs((len(path) - 1) - expected_steps) <= 1
    assert 2 <= len(path) <= 200


def test_motion_path_duration_formula_matches_estimate():
    for dist, speed in [(50, 1.0), (300, 1.0), (2000, 1.0), (1000, 2.0), (1000, 0.5)]:
        path = desk.motion_path(0, 0, dist, 0, speed=speed, seed=4)
        duration = path[-1][0]
        expected = desk._clamp(0.12 + (dist / 2200.0) / speed, 0.12, 0.8)
        assert duration == pytest.approx(expected, abs=1e-9)


def test_motion_path_curvature_is_bounded():
    # A horizontal chord: perpendicular deviation from it is just |y|.
    x0, y0, x1, y1 = 0, 0, 600, 0
    dist = math.hypot(x1 - x0, y1 - y0)
    path = desk.motion_path(x0, y0, x1, y1, speed=1.0, seed=5)
    # Every sample's deviation from the straight chord must stay within the spec's
    # control-point budget (offset <= 10% of the distance; the curve's max deviation from the
    # chord is at most that full offset).
    max_dev = max(abs(y) for _, _, y in path)
    assert max_dev <= 0.10 * dist + 1e-6


def test_motion_path_curvature_has_some_signal():
    """The curve should actually bow away from the straight line (not degenerate to a line)."""
    path = desk.motion_path(0, 0, 600, 0, speed=1.0, seed=6)
    max_dev = max(abs(y) for _, _, y in path)
    assert max_dev > 0.5  # a few px of bow, given ~4-10% of 600px offset halved by the bezier


def test_motion_path_seed_pins_the_curve():
    a = desk.motion_path(0, 0, 600, 0, speed=1.0, seed=42)
    b = desk.motion_path(0, 0, 600, 0, speed=1.0, seed=42)
    assert a == b


def test_motion_path_snaps_for_tiny_moves():
    path = desk.motion_path(100, 100, 105, 103, speed=1.0, seed=7)
    assert len(path) == 2
    assert path[0] == (0.0, 100.0, 100.0)
    assert path[1][1:] == (105.0, 103.0)
    assert path[1][0] <= 0.05


def test_motion_path_snap_threshold_is_12px():
    just_under = desk.motion_path(0, 0, 11, 0, speed=1.0, seed=8)
    just_over = desk.motion_path(0, 0, 13, 0, speed=1.0, seed=8)
    assert len(just_under) == 2
    assert len(just_over) > 2


def test_motion_path_zero_distance_does_not_crash():
    path = desk.motion_path(50, 50, 50, 50, speed=1.0, seed=9)
    assert path[0] == (0.0, 50.0, 50.0)
    assert path[-1][1:] == (50.0, 50.0)


# ---------------------------------------------------------------------------
# estimate_motion_s()
# ---------------------------------------------------------------------------

def test_estimate_motion_s_matches_motion_path_duration():
    desk.configure_motion(enabled=True, speed=1.0)
    try:
        est = desk.estimate_motion_s((0, 0), (400, 300))
        path = desk.motion_path(0, 0, 400, 300, speed=1.0)
        assert est == pytest.approx(path[-1][0], abs=1e-9)
    finally:
        desk.configure_motion(enabled=True, speed=1.0)


def test_estimate_motion_s_zero_when_motion_disabled():
    desk.configure_motion(enabled=False)
    try:
        assert desk.estimate_motion_s((0, 0), (1000, 1000)) == 0.0
    finally:
        desk.configure_motion(enabled=True, speed=1.0)


def test_estimate_motion_s_snap_for_tiny_distance():
    desk.configure_motion(enabled=True, speed=1.0)
    try:
        assert desk.estimate_motion_s((0, 0), (3, 4)) == desk._SNAP_DURATION_S
    finally:
        desk.configure_motion(enabled=True, speed=1.0)


def test_estimate_motion_s_respects_explicit_speed_override():
    desk.configure_motion(enabled=True, speed=1.0)
    try:
        fast = desk.estimate_motion_s((0, 0), (1000, 0), speed=4.0)
        slow = desk.estimate_motion_s((0, 0), (1000, 0), speed=0.5)
        assert fast < slow
    finally:
        desk.configure_motion(enabled=True, speed=1.0)


# ---------------------------------------------------------------------------
# configure_motion()
# ---------------------------------------------------------------------------

def test_configure_motion_defaults():
    desk.configure_motion()
    assert desk._motion_enabled is True
    assert desk._motion_speed == 1.0


def test_configure_motion_clamps_speed_above_zero():
    desk.configure_motion(enabled=True, speed=0.0)
    try:
        assert desk._motion_speed > 0
    finally:
        desk.configure_motion(enabled=True, speed=1.0)


def test_configure_motion_disabled_skips_curving(monkeypatch):
    """With motion disabled, mouse_move must fall back to a plain pyautogui.moveTo (old
    instant behaviour) rather than driving a path."""
    desk.configure_motion(enabled=False)
    try:
        calls = []
        monkeypatch.setattr(desk.pyautogui, "moveTo", lambda x, y: calls.append((x, y)))

        def _boom(*a, **k):
            raise AssertionError("path should not be driven while motion is disabled")

        monkeypatch.setattr(desk, "_drive_motion_path", _boom)
        desk.mouse_move(123, 456)
        assert calls == [(123, 456)]
    finally:
        desk.configure_motion(enabled=True, speed=1.0)


# ---------------------------------------------------------------------------
# Real cursor -- needs a real Windows desktop.
# ---------------------------------------------------------------------------

@pytest.mark.display
def test_real_mouse_moves_around_a_square_and_returns():
    import win32api

    desk.configure_motion(enabled=True, speed=3.0)  # keep the whole test comfortably under 4s
    try:
        origin = win32api.GetCursorPos()
        ox, oy = origin
        corners = [(ox + 300, oy), (ox + 300, oy + 300), (ox, oy + 300), (ox, oy)]
        for cx, cy in corners:
            desk.mouse_move(cx, cy)
            landed = win32api.GetCursorPos()
            assert abs(landed[0] - cx) <= 1
            assert abs(landed[1] - cy) <= 1
    finally:
        desk.mouse_move(ox, oy)
        desk.configure_motion(enabled=True, speed=1.0)
