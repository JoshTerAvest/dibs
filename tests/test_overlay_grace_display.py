"""Real-desktop check that the consent-grace countdown is big, centred, and goes away.

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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows overlay only")
def test_grace_countdown_is_big_centred_and_clears():
    from dibs import overlay

    ov = overlay.Overlay(halo_color="#00e5ff", banner=True)
    ov.start()
    assert ov.available is True
    try:
        mon = ov._monitors[0]
        cx, cy = mon["x"] + mon["w"] // 2, mon["y"] + mon["h"] // 2
        region = {"left": cx - 230, "top": cy - 85, "width": 460, "height": 170}
        halo = overlay._hex_to_rgb("#00e5ff")

        before = _halo_pixels(_grab(region), halo)
        ov.show_grace(2.0)
        time.sleep(0.4)
        during = _halo_pixels(_grab(region), halo)
        time.sleep(2.2)
        after = _halo_pixels(_grab(region), halo)
    finally:
        ov.stop()

    # The 84 px number plus the 3 px border are drawn in the halo colour: thousands of pixels
    # in a 460x170 box, versus whatever the desktop happens to show underneath.
    assert during - before > 1500, f"countdown not visible: before={before} during={during}"
    assert abs(after - before) < 200, f"countdown did not clear: before={before} after={after}"
