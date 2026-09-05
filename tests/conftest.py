"""Shared fixtures for the hub-owned tests.

dibs/desk.py and dibs/actions.py are real (desk-agent-owned) implementations that touch
the actual Windows desktop. These fixtures monkeypatch the boundary the hub talks to --
`dibs.desk.list_screens` / `set_dpi_aware` / `cursor_position` and
`dibs.actions.run_action` -- so hub/server tests never touch the real screen, mouse, or
keyboard. `pynput.keyboard.GlobalHotKeys` is also replaced with a no-op so `Hub.start()`
doesn't register a real global hotkey during tests.
"""

from __future__ import annotations

import io
import itertools

import pytest
from PIL import Image

from dibs import actions as actions_mod
from dibs import desk as desk_mod
from dibs.config import Settings


def _tiny_png() -> bytes:
    img = Image.new("RGB", (4, 3), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


TINY_PNG = _tiny_png()

FAKE_SCREENS = [
    desk_mod.Screen(index=0, x=0, y=0, width=1920, height=1080, primary=True),
    desk_mod.Screen(index=1, x=1920, y=0, width=1920, height=1080, primary=False),
]


class FakeCursor:
    """A mutable box so a test can move the 'cursor' the human-override sweeper reads."""

    def __init__(self, pos: tuple[int, int] = (100, 100)):
        self.pos = pos


def _fake_run_action(
    action, *, screen_index=None, max_long_edge=1568, max_pixels=1_150_000, allow_launch=False
):
    name = action.get("action")
    if name not in actions_mod.ALL_ACTIONS:
        raise actions_mod.ActionError("unknown_action", f"unknown action: {name!r}")
    screen = FAKE_SCREENS[0]
    if name == "screenshot":
        shot = desk_mod.Shot(png=TINY_PNG, width=4, height=3, scale=1.0, screen=screen)
        return actions_mod.ActionResult(image=shot)
    if name == "zoom":
        shot = desk_mod.Shot(
            png=TINY_PNG, width=4, height=3, scale=1.0, screen=screen, region=(0, 0, 4, 3)
        )
        return actions_mod.ActionResult(image=shot)
    if name == "cursor_position":
        return actions_mod.ActionResult(
            text="X=1,Y=1", data={"x": 1, "y": 1, "screen": 0, "absolute": [1, 1]}
        )
    if name == "list_windows":
        return actions_mod.ActionResult(text="", data={"windows": []})
    if name == "get_clipboard":
        return actions_mod.ActionResult(text="clip")
    if name == "wait":
        return actions_mod.ActionResult(text="OK")
    if name == "launch":
        if not allow_launch:
            raise actions_mod.ActionError("launch_disabled", "launch is disabled by config")
        return actions_mod.ActionResult(text="OK pid=1234", data={"pid": 1234})
    # left_click / type / key / scroll / focus_window / set_clipboard / etc.
    return actions_mod.ActionResult(text="OK")


@pytest.fixture
def fake_cursor() -> FakeCursor:
    return FakeCursor()


@pytest.fixture
def patch_desk(monkeypatch, fake_cursor: FakeCursor) -> FakeCursor:
    monkeypatch.setattr(desk_mod, "set_dpi_aware", lambda: None)
    monkeypatch.setattr(desk_mod, "list_screens", lambda: list(FAKE_SCREENS))
    monkeypatch.setattr(desk_mod, "cursor_position", lambda: fake_cursor.pos)
    monkeypatch.setattr(actions_mod, "run_action", _fake_run_action)
    return fake_cursor


@pytest.fixture
def no_hotkey(monkeypatch):
    class _NoopGlobalHotKeys:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    import pynput.keyboard as kb

    monkeypatch.setattr(kb, "GlobalHotKeys", _NoopGlobalHotKeys)


@pytest.fixture
def settings_factory(tmp_path, patch_desk, no_hotkey):
    """Settings() with `presence.enabled=False` and `overlay.enabled=False` by default so
    ordinary tests never spin up a real pynput mouse/keyboard hook (which would pick up
    whatever the person at this desk is actually doing and make `mode: ask` consent-gating
    nondeterministic) or a real Tk overlay window (which is slow to create/destroy hundreds of
    times a test run and has caused observed cross-test timing flakiness). Tests that exercise
    presence/consent directly either override `presence=` explicitly (still merged under
    `enabled: False`) or use the `fake_presence` fixture (tests/test_consent.py), which replaces
    `dibs.hub.presence.Presence` outright, making the "enabled" flag moot. Tests that need the
    real overlay pass `overlay={"enabled": True}` explicitly (marked `display`).
    """
    counter = itertools.count()

    def make(**overrides) -> Settings:
        data_dir = overrides.pop("data_dir", None) or str(tmp_path / f"data{next(counter)}")
        presence_overrides = overrides.pop("presence", {})
        presence_cfg = {"enabled": False, **presence_overrides}
        overlay_overrides = overrides.pop("overlay", {})
        overlay_cfg = {"enabled": False, **overlay_overrides}
        tray_cfg = {"enabled": False, **overrides.pop("tray", {})}
        # Lease/server mechanics tests aren't about consent; since 9/4 `ask` mode never grants
        # without a human decision, so default to hands_off and let consent tests pass mode='ask'.
        overrides.setdefault("mode", "hands_off")
        return Settings(
            data_dir=data_dir,
            presence=presence_cfg,
            overlay=overlay_cfg,
            tray=tray_cfg,
            **overrides,
        )

    return make


@pytest.fixture
def make_client(settings_factory):
    """Factory for a `fastapi.testclient.TestClient` bound to a fresh app + data_dir.

    `client_host` controls the ASGI scope's `request.client.host` (defaults to the loopback
    address 127.0.0.1, so loopback-exemption behavior is on by default -- pass a non-loopback
    host to test the exemption is properly scoped).
    """
    from fastapi.testclient import TestClient

    from dibs.server import create_app

    def make(*, client_host: str = "127.0.0.1", **settings_overrides):
        settings = settings_factory(**settings_overrides)
        app = create_app(settings)
        client = TestClient(app, client=(client_host, 51000))
        client.get("/")  # like a browser: loads the page, receives the dashboard cookie
        return client

    return make


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client, name: str, purpose: str = "test", **kwargs) -> dict:
    resp = client.post("/v1/agents", json={"name": name, "purpose": purpose}, **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()
