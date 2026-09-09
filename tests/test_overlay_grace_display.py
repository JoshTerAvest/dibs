"""Real-desktop checks for the consent-grace cue: the band is visible across the centre of the
primary monitor, the screen behind it is dimmed, the Cancel button really takes a click, and
everything clears afterwards.

Only touches the overlay's own layered windows; never any user application. Run with
`uv run pytest -q -m display tests/test_overlay_grace_display.py`.
"""

from __future__ import annotations

import sys
import time

import pytest

pytestmark = pytest.mark.display


def _grab(region):
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _halo_pixels(img, rgb, tol=60):
    """Count pixels within `tol` per channel of the halo colour."""
    r0, g0, b0 = rgb
    n = 0
    for r, g, b in img.getdata():
        if abs(r - r0) <= tol and abs(g - g0) <= tol and abs(b - b0) <= tol:
            n += 1
    return n


def _mean_luma(img):
    px = list(img.convert("L").getdata())
    return sum(px) / max(1, len(px))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows overlay only")
def test_grace_band_is_visible_dims_the_screen_and_clears():
    from dibs import overlay

    ov = overlay.Overlay(halo_color="#00e5ff", banner=True)
    ov.start()
    assert ov.available is True
    try:
        mon = ov._monitors[0]
        cx, cy = mon["x"] + mon["w"] // 2, mon["y"] + mon["h"] // 2
        band = {"left": cx - 230, "top": cy - 75, "width": 460, "height": 150}
        # a patch well above the band, on the same monitor: only the dim layer touches it
        corner = {"left": mon["x"] + 40, "top": mon["y"] + 120, "width": 200, "height": 120}
        halo = overlay._hex_to_rgb("#00e5ff")

        before_band = _halo_pixels(_grab(band), halo)
        before_luma = _mean_luma(_grab(corner))
        ov.show_grace(2.0, agent="test-agent")
        time.sleep(0.4)
        during_band = _halo_pixels(_grab(band), halo)
        during_luma = _mean_luma(_grab(corner))
        time.sleep(2.2)
        after_band = _halo_pixels(_grab(band), halo)
        after_luma = _mean_luma(_grab(corner))
    finally:
        ov.stop()

    assert during_band - before_band > 1500, f"band not visible: {before_band} -> {during_band}"
    assert abs(after_band - before_band) < 200, f"band did not clear: {before_band} -> {after_band}"
    # the dim layer is black at ~59% alpha, so whatever is there gets noticeably darker
    assert during_luma < before_luma * 0.8 or before_luma < 8, (
        f"screen not dimmed: luma {before_luma:.1f} -> {during_luma:.1f}"
    )
    assert abs(after_luma - before_luma) < 12, (
        f"dim did not clear: {before_luma:.1f} -> {after_luma:.1f}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows overlay only")
def test_grace_cancel_button_takes_a_real_click():
    from dibs import desk, overlay

    hits = []
    ov = overlay.Overlay(halo_color="#00e5ff", banner=True)
    ov.start()
    assert ov.available is True
    try:
        mon = ov._monitors[0]
        desk.set_dpi_aware()
        import win32api

        start_pos = win32api.GetCursorPos()
        ov.show_grace(4.0, agent="test-agent", on_cancel=lambda: hits.append(time.monotonic()))
        time.sleep(0.4)
        btn = ov._grace_btn
        assert btn, "no cancel button rect recorded"
        band_h = 150
        x = mon["x"] + (btn[0] + btn[2]) // 2
        y = mon["y"] + (mon["h"] - band_h) // 2 + (btn[1] + btn[3]) // 2
        desk.click(x, y)
        time.sleep(0.3)
        cleared = ov._grace_until <= time.monotonic()
        win32api.SetCursorPos(start_pos)
    finally:
        ov.stop()

    assert len(hits) == 1, f"cancel callback fired {len(hits)} times (clicked {x},{y})"
    assert cleared, "band still showing after cancel"
