"""Cycles the presence overlay through every visible state for ~10 s on the real Windows
desktop, printing what it's showing at each step. Saves a screenshot of the primary screen
(taken while the cursor halo + banner are visible) to docs/overlay-demo.png so the state can
be reviewed without watching the screen live.

Usage:
    uv run python examples/overlay_demo.py
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyautogui
import win32api

from dibs import overlay

_START = time.monotonic()


def _log(msg: str) -> None:
    print(f"[t={time.monotonic() - _START:5.2f}s] {msg}", flush=True)


def _screenshot_after(delay_s: float, out_path: Path) -> None:
    def _grab() -> None:
        time.sleep(delay_s)
        try:
            img = pyautogui.screenshot()
            img.save(out_path)
            _log(f"screenshot saved -> {out_path}")
        except Exception as e:
            _log(f"screenshot failed: {e}")

    threading.Thread(target=_grab, daemon=True, name="overlay-demo-screenshot").start()


def main() -> None:
    global _START
    _START = time.monotonic()

    out_path = Path(__file__).resolve().parent.parent / "docs" / "overlay-demo.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ov = overlay.Overlay(halo_color="#00e5ff", banner=True)
    _log("starting Win32 layered overlay thread")
    ov.start()
    if not ov.available:
        _log("Win32 overlay unavailable on this machine; nothing to demo.")
        return

    try:
        _log("holder set: demo-agent is using the desk (banner + halo visible)")
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=25)).isoformat()
        ov.set_holder("demo-agent", "cycling overlay demo states", expires_at=expires_at)

        cx, cy = win32api.GetCursorPos()
        _screenshot_after(0.6, out_path)
        time.sleep(1.0)

        _log("click flashes around the cursor")
        for dx, dy in [(0, 0), (90, 0), (0, 90)]:
            ov.flash_click(cx + dx, cy + dy)
            time.sleep(0.3)

        _log("typing tag on")
        ov.show_typing(True)
        time.sleep(1.0)
        ov.show_typing(False)

        _log("consent prompt shown bottom-right (auto-dismissed in 4s)")
        decisions: list[tuple[str, bool]] = []
        ov.prompt_consent(
            "demo-1",
            "demo-agent",
            "wants to click around the desk for the overlay demo",
            10.0,
            lambda rid, ok: decisions.append((rid, ok)),
        )
        time.sleep(4.0)
        ov.dismiss_consent("demo-1")
        _log(f"consent decisions recorded by callback: {decisions}")

        _log("paused (red banner & edges)")
        ov.set_paused("demo pause")
        time.sleep(1.0)
        ov.set_paused(None)

        _log("human takeover (green banner & edges, auto-reverts after 2s)")
        ov.show_human(2.0)
        time.sleep(2.2)

        _log("hidden (idle)")
        ov.set_holder(None)
        time.sleep(0.3)

        _log("CPU usage of overlay should be < 3% during active states, 0% during idle.")
    finally:
        ov.stop()
        _log("stopped")


if __name__ == "__main__":
    main()
