"""Real-desktop display test for dibs.uia / the ui_tree, find, click_element actions.

Marked `display` — run explicitly with `pytest -m display`. Drives Calculator (launch +
focus + close, same as test_desk_display.py's pattern — no unsaved user data at risk),
through an in-process Hub built the way test_desk_display.py's real-desk tests are meant to
be: real desk, presence disabled, overlay disabled, hands_off mode. Never touches the live
:7474 server.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import win32con
import win32gui

from dibs import desk
from dibs.config import Settings
from dibs.hub import AgentInfo, Hub

pytestmark = pytest.mark.display


def _close_window(hwnd: int) -> None:
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass
    time.sleep(0.5)


def test_ui_tree_find_click_element_against_calculator(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        mode="hands_off",
        presence={"enabled": False},
        overlay={"enabled": False},
        tray={"enabled": False},
    )
    h = Hub(settings)
    agent = AgentInfo(
        agent_id="test-agent", name="uia-display-test", purpose="display test", is_admin=False
    )

    async def scenario():
        await h.start()
        try:
            pid = desk.launch("calc.exe")
            assert pid > 0
            time.sleep(1.5)
            win = desk.focus_window(title="calculator")

            try:
                tree_result = await h.run(
                    agent, {"action": "ui_tree", "hwnd": win.hwnd}, auto_lease=True, wait_s=10
                )
                assert tree_result.data is not None
                nodes = tree_result.data["nodes"]
                assert any(n["name"] == "Seven" and n["role"] == "ButtonControl" for n in nodes)
                assert "Seven" in tree_result.text

                find_result = await h.run(
                    agent,
                    {"action": "find", "text": "Seven", "hwnd": win.hwnd},
                    auto_lease=True,
                    wait_s=10,
                )
                match = find_result.data["match"]
                assert match["name"] == "Seven"
                assert match["role"] == "ButtonControl"
                assert len(match["center_shot"]) == 2

                click_result = await h.run(
                    agent,
                    {"action": "click_element", "text": "Seven", "hwnd": win.hwnd},
                    auto_lease=True,
                    wait_s=10,
                )
                assert click_result.data["node"]["name"] == "Seven"
                await asyncio.sleep(0.3)

                display_result = await h.run(
                    agent,
                    {"action": "find", "text": "Display is", "hwnd": win.hwnd},
                    auto_lease=True,
                    wait_s=10,
                )
                display_value = display_result.data["match"].get("value") or ""
                display_name = display_result.data["match"].get("name") or ""
                assert "7" in display_value or "7" in display_name
            finally:
                _close_window(win.hwnd)
                still_open = any(w.hwnd == win.hwnd for w in desk.list_windows())
                assert not still_open
        finally:
            await h.stop()

    asyncio.run(scenario())
