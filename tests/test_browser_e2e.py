"""End-to-end display test: drives a REAL Playwright-launched Chromium window through the
dibs hub, in-process, against the REAL desktop (mouse/keyboard/screen) -- not the live
`:7474` server (there is a real instance of that running for the user; this suite never
touches it). Only ever touches its own throwaway Chromium window.

Marked `display` (run explicitly with `pytest -m display`); auto-skipped if playwright or its
Chromium browser isn't installed, or if not running on Windows (dibs.desk is Windows-only).

What this proves: that a real agent driving dibs by computing screenshot-space coordinates
from a screenshot/DOM inspection -- exactly the workflow `scroll`, `scroll_pages`, and clicks
are built for -- actually lands on the right pixel and produces the right effect in a real,
DPI-scaled browser window. Unlike tests/test_actions.py (desk.* fully mocked) and
tests/test_desk_display.py (desk.* directly, no hub), this is the only suite that exercises the
full stack (hub -> actions -> desk -> real Windows APIs) against a page whose content and
DOM state we can independently verify.

Run: `uv run pytest -q -m display tests/test_browser_e2e.py`
Setup (once): `uv run playwright install chromium`
"""

from __future__ import annotations

import http.server
import io
import itertools
import sys
import threading
import time
import uuid
from typing import Any

import pytest
from PIL import Image

from dibs import actions, desk
from dibs.config import Settings
from dibs.hub import Hub

pytestmark = pytest.mark.display

if sys.platform != "win32":
    pytest.skip("dibs.desk is Windows-only", allow_module_level=True)

try:
    from playwright.async_api import async_playwright
except ImportError:
    pytest.skip("playwright not installed (uv sync --extra dev)", allow_module_level=True)

_MARKER_STEP = 500
_PAGE_HEIGHT = 20000
_VIEWPORT = {"width": 1200, "height": 900}

# A small, saturated, unlikely-to-occur-elsewhere color for the calibration marker fixed at the
# viewport's top-left corner (see `_calibrate_chrome_offset`).
_CALIBRATION_RGB = (1, 222, 3)
_CALIBRATION_SIZE = 6


def _page_html(title: str) -> str:
    markers = "".join(
        f'<div style="position:absolute;top:{y}px;left:0;">marker {y // _MARKER_STEP}</div>'
        for y in range(0, _PAGE_HEIGHT, _MARKER_STEP)
    )
    r, g, b = _CALIBRATION_RGB
    return f"""<!doctype html>
<html><head><title>{title}</title></head>
<body style="margin:0;height:{_PAGE_HEIGHT}px;position:relative;font:16px sans-serif;">
  <div id="calib" style="position:fixed;top:0;left:0;width:{_CALIBRATION_SIZE}px;
       height:{_CALIBRATION_SIZE}px;background:rgb({r},{g},{b});z-index:99999;"></div>
  <div style="position:fixed;top:0;left:20px;background:#fff;padding:4px;">
    count=<span id="count">0</span> scrollY=<span id="sy">0</span>
  </div>
  <div style="position:absolute;top:40px;left:20px;">
    <input id="q" style="font-size:16px;padding:6px;width:220px;">
    <button id="btn" style="font-size:16px;padding:6px 12px;">click me</button>
  </div>
  {markers}
  <script>
    let n = 0;
    document.getElementById('btn').addEventListener('click', () => {{
      n += 1;
      document.getElementById('count').textContent = String(n);
    }});
    window.addEventListener('scroll', () => {{
      document.getElementById('sy').textContent = String(window.scrollY);
    }});
  </script>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    html_bytes: bytes = b""

    def do_GET(self) -> None:  # noqa: N802 -- stdlib naming
        body = self.html_bytes
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:  # silence default stderr logging
        pass


@pytest.fixture
def http_server():
    title = f"dibs-e2e-{uuid.uuid4().hex[:8]}"
    handler = type("Handler", (_Handler,), {"html_bytes": _page_html(title).encode("utf-8")})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/", title
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
async def browser_page(http_server, tmp_path):
    url, title = http_server
    try:
        pw = await async_playwright().start()
    except Exception as e:  # pragma: no cover - environment-dependent
        pytest.skip(f"playwright failed to start: {e}")
        return

    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "chrome-profile"),
            headless=False,
            viewport=_VIEWPORT,
            args=[
                "--window-position=0,0",
                f"--window-size={_VIEWPORT['width']},{_VIEWPORT['height']}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
    except Exception as e:
        await pw.stop()
        pytest.skip(f"Chromium not installed -- run `uv run playwright install chromium` ({e})")
        return

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url)
        await page.wait_for_selector("#btn")
        yield page, title
    finally:
        await context.close()
        await pw.stop()


@pytest.fixture
async def real_hub(tmp_path):
    """A real Hub against the real desk -- mirrors tests/test_desk_display.py's philosophy
    (non-destructive, only ever touches test-owned windows) but through the full hub stack.
    presence/overlay/tray disabled, mode hands_off, so nothing here waits on human consent or
    pops overlay windows."""
    counter = itertools.count()
    data_dir = tmp_path / f"dibs-e2e-data{next(counter)}"
    settings = Settings(
        data_dir=str(data_dir),
        mode="hands_off",
        presence={"enabled": False},
        overlay={"enabled": False},
        tray={"enabled": False},
    )
    hub = Hub(settings)
    await hub.start()
    try:
        agent = hub.register("e2e-agent", "browser e2e test")
        agent_info = hub.authenticate(agent["token"])
        result = await hub.acquire(agent_info, ttl_s=300)
        assert result["status"] == "granted", result
        yield hub, agent_info, settings
    finally:
        await hub.stop()


def _shot_scale(settings: Settings, screen: desk.Screen) -> float:
    return actions.scale_for(screen, settings.max_long_edge, settings.max_pixels)


def _calibrate_chrome_offset(screen: desk.Screen) -> tuple[int, int]:
    """Find the absolute screen pixel of the `#calib` marker (fixed at the viewport's CSS
    top-left corner, see `_page_html`) by scanning a real, native-resolution `desk.zoom` capture
    for its distinctive color. That pixel IS the viewport's origin in absolute screen pixels --
    i.e. exactly the "chrome offset" (title bar + tabs + toolbar + any infobar) an agent would
    otherwise have to infer from window.screenX/outerHeight/innerHeight.

    Deliberately does NOT use that browser-reported math: on this machine (headed Chrome
    launched by Playwright, with the "being controlled by automated test software" infobar),
    `outerHeight - innerHeight` overcounts the true top offset by dozens of pixels -- worked out
    by comparing a `mouse_move` to the coordinate that math predicted against a `desk.zoom`
    screenshot of the actual cursor position: the button in the DOM was ~45-60px above where
    that math placed the cursor, while `document.elementFromPoint`-equivalent (a real click)
    landed at clientY ~48-60px past the intended target. Pixel-calibrating against dibs' own
    screenshot instead removes that whole class of guesswork -- it's ground truth from the same
    capture path a real screenshot-driven agent would use."""
    region_size = 600  # generously covers title bar + tabs + toolbar + infobar, if any
    shot = desk.zoom(screen, (screen.x, screen.y, screen.x + region_size, screen.y + region_size))
    assert shot.scale == 1.0, (
        f"expected native-resolution calibration capture, got scale={shot.scale}"
    )
    img = Image.open(io.BytesIO(shot.png)).convert("RGB")
    target = _CALIBRATION_RGB
    for y in range(img.height):
        for x in range(img.width):
            px = img.getpixel((x, y))
            if all(abs(px[i] - target[i]) <= 20 for i in range(3)):
                return screen.x + x, screen.y + y
    raise AssertionError(
        f"calibration marker (rgb{_CALIBRATION_RGB}) not found in the top-left "
        f"{region_size}x{region_size} of screen {screen.index} -- is the e2e Chromium window "
        "actually focused and positioned at (0,0)?"
    )


def _css_point_to_shot_coords(
    css_x: float,
    css_y: float,
    dpr: float,
    chrome_offset: tuple[int, int],
    settings: Settings,
    screen: desk.Screen,
) -> tuple[int, int, dict[str, Any]]:
    """Convert a CSS-pixel point in the page's viewport to dibs SCREENSHOT-SPACE coordinates for
    `screen`, using the marker-calibrated chrome offset (see `_calibrate_chrome_offset`):
    devicePixelRatio converts CSS px to real/physical pixels (accounting for OS display
    scaling), then dibs' own screenshot scale (from `actions.scale_for`, mirroring what
    `/v1/display`'s `screenshot.scale` reports) converts those physical pixels into the
    screenshot-space pixels dibs action coordinates use -- the same computation a real
    screenshot-driven agent has to do."""
    chrome_x, chrome_y = chrome_offset
    abs_x = chrome_x + css_x * dpr
    abs_y = chrome_y + css_y * dpr

    scale = _shot_scale(settings, screen)
    shot_x = round((abs_x - screen.x) * scale)
    shot_y = round((abs_y - screen.y) * scale)
    return (
        shot_x,
        shot_y,
        {"abs": (abs_x, abs_y), "scale": scale, "dpr": dpr, "chrome_offset": chrome_offset},
    )


async def test_scroll_click_pages_and_type_round_trip(real_hub, browser_page):
    hub, agent, settings = real_hub
    page, title = browser_page
    screen = desk.primary_screen()

    focus_result = await hub.run(agent, {"action": "focus_window", "title": title})
    assert title.lower() in focus_result.text.lower(), (
        f"expected dibs to focus the e2e Chromium window (title contains {title!r}), "
        f"got {focus_result.text!r}"
    )
    time.sleep(0.3)

    dpr = await page.evaluate("window.devicePixelRatio")
    chrome_offset = _calibrate_chrome_offset(screen)

    # --- (a) scroll: 10 clicks down at page centre moves scrollY by at least 10*50px ---
    center_x, center_y = _VIEWPORT["width"] / 2, _VIEWPORT["height"] / 2
    shot_x, shot_y, info = _css_point_to_shot_coords(
        center_x, center_y, dpr, chrome_offset, settings, screen
    )

    before = await page.evaluate("window.scrollY")
    assert before == 0, f"expected page to start at scrollY=0, got {before}"

    await hub.run(
        agent,
        {
            "action": "scroll",
            "scroll_direction": "down",
            "scroll_amount": 10,
            "coordinate": [shot_x, shot_y],
        },
    )
    time.sleep(0.3)
    after = await page.evaluate("window.scrollY")
    assert after >= 10 * 50, (
        f"scroll down 10 clicks at screenshot-coord ({shot_x},{shot_y}) "
        f"(css center {center_x},{center_y} -> abs {info['abs']}, dpr={info['dpr']}, "
        f"shot_scale={info['scale']}) only moved scrollY from {before} to {after} "
        f"(expected >= {10 * 50})"
    )

    await hub.run(
        agent,
        {
            "action": "scroll",
            "scroll_direction": "up",
            "scroll_amount": 10,
            "coordinate": [shot_x, shot_y],
        },
    )
    time.sleep(0.3)
    back = await page.evaluate("window.scrollY")
    assert back <= 50, f"scroll up 10 clicks should return near scrollY=0, got {back}"

    # --- (b) scroll_pages: down 2 grows scrollY by at least one viewport height ---
    before_pages = await page.evaluate("window.scrollY")
    await hub.run(
        agent,
        {
            "action": "scroll_pages",
            "scroll_direction": "down",
            "scroll_amount": 2,
            "coordinate": [shot_x, shot_y],
        },
    )
    time.sleep(0.5)
    after_pages = await page.evaluate("window.scrollY")
    assert after_pages - before_pages >= _VIEWPORT["height"], (
        f"scroll_pages down 2 only moved scrollY from {before_pages} to {after_pages} "
        f"(expected growth >= viewport height {_VIEWPORT['height']})"
    )

    # scroll back to top for the click/type steps below (fixed-position elements move with the
    # viewport regardless, but keep this deterministic)
    await hub.run(agent, {"action": "key", "text": "Home"})
    time.sleep(0.3)

    # --- (c) click #btn via bounding-box -> screen coords -> #count increments ---
    btn_box = await page.locator("#btn").bounding_box()
    assert btn_box is not None, "expected #btn to have a bounding box"
    btn_cx = btn_box["x"] + btn_box["width"] / 2
    btn_cy = btn_box["y"] + btn_box["height"] / 2
    btn_shot_x, btn_shot_y, btn_info = _css_point_to_shot_coords(
        btn_cx, btn_cy, dpr, chrome_offset, settings, screen
    )

    await hub.run(agent, {"action": "left_click", "coordinate": [btn_shot_x, btn_shot_y]})
    time.sleep(0.3)
    count_text = await page.locator("#count").inner_text()
    assert count_text == "1", (
        f"clicking #btn at screenshot-coord ({btn_shot_x},{btn_shot_y}) "
        f"(css center {btn_cx},{btn_cy} -> abs {btn_info['abs']}, dpr={btn_info['dpr']}, "
        f"shot_scale={btn_info['scale']}) did not increment #count: got {count_text!r}, expected '1'"
    )

    # --- (d) click #q, type "hello dibs" -> input value matches ---
    q_box = await page.locator("#q").bounding_box()
    assert q_box is not None, "expected #q to have a bounding box"
    q_cx = q_box["x"] + q_box["width"] / 2
    q_cy = q_box["y"] + q_box["height"] / 2
    q_shot_x, q_shot_y, q_info = _css_point_to_shot_coords(
        q_cx, q_cy, dpr, chrome_offset, settings, screen
    )

    await hub.run(agent, {"action": "left_click", "coordinate": [q_shot_x, q_shot_y]})
    time.sleep(0.2)
    await hub.run(agent, {"action": "type", "text": "hello dibs"})
    time.sleep(0.3)
    value = await page.locator("#q").input_value()
    assert value == "hello dibs", (
        f"typing into #q at screenshot-coord ({q_shot_x},{q_shot_y}) "
        f"(css center {q_cx},{q_cy} -> abs {q_info['abs']}, dpr={q_info['dpr']}, "
        f"shot_scale={q_info['scale']}) produced {value!r}, expected 'hello dibs'"
    )
