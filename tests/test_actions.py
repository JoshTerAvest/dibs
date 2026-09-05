"""Tests for dibs.actions — validation + dispatch. desk.* is fully monkeypatched (no display)."""
from __future__ import annotations

import base64

import pytest

from dibs import actions, desk

TWO_SCREENS = [
    desk.Screen(index=0, x=0, y=0, width=2560, height=1440, primary=True),
    desk.Screen(index=1, x=2560, y=0, width=2560, height=1440, primary=False),
]


@pytest.fixture(autouse=True)
def fake_screens(monkeypatch):
    monkeypatch.setattr(desk, "list_screens", lambda: TWO_SCREENS)
    yield


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def record(self, name):
        def _fn(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None
        return _fn


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    for name in ("mouse_move", "click", "drag", "mouse_down", "mouse_up", "scroll",
                 "type_text", "press_key", "hold_key", "set_clipboard"):
        monkeypatch.setattr(desk, name, r.record(name))
    return r


# --- coordinate mapping ------------------------------------------------------

def test_screenshot_space_to_absolute_mapping(rec):
    actions.run_action({"action": "mouse_move", "coordinate": [715, 402]})
    assert len(rec.calls) == 1
    name, args, _kwargs = rec.calls[0]
    assert name == "mouse_move"
    ax, ay = args
    assert abs(ax - 1280) <= 1
    assert abs(ay - 720) <= 1


def test_coordinate_out_of_bounds_rejected(rec):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "mouse_move", "coordinate": [10000, 10000]})
    assert ei.value.code == "coordinate_out_of_bounds"
    assert rec.calls == []


def test_negative_coordinate_rejected(rec):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "mouse_move", "coordinate": [-1, 5]})
    assert ei.value.code == "coordinate_out_of_bounds"


def test_screen_1_offsets_by_its_origin(rec):
    actions.run_action({"action": "mouse_move", "coordinate": [715, 402], "screen": 1})
    _name, args, _kwargs = rec.calls[0]
    ax, ay = args
    assert abs(ax - (2560 + 1280)) <= 1
    assert abs(ay - 720) <= 1


def test_unknown_screen(rec):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "mouse_move", "coordinate": [1, 1], "screen": 7})
    assert ei.value.code == "unknown_screen"


# --- clicks / drag / scroll with modifiers -----------------------------------

def test_left_click_no_coordinate_uses_current_position(rec, monkeypatch):
    monkeypatch.setattr(actions.desk, "cursor_position", lambda: (100, 100))
    actions.run_action({"action": "left_click"})
    _name, args, kwargs = rec.calls[0]
    assert args[0] is None and args[1] is None
    assert kwargs["button"] == "left" and kwargs["clicks"] == 1


def test_double_click_and_triple_click_map_clicks(rec):
    actions.run_action({"action": "double_click", "coordinate": [0, 0]})
    actions.run_action({"action": "triple_click", "coordinate": [0, 0]})
    assert rec.calls[0][2]["clicks"] == 2
    assert rec.calls[1][2]["clicks"] == 3
    assert rec.calls[0][2]["button"] == "left"


def test_right_and_middle_click_button(rec):
    actions.run_action({"action": "right_click", "coordinate": [0, 0]})
    actions.run_action({"action": "middle_click", "coordinate": [0, 0]})
    assert rec.calls[0][2]["button"] == "right"
    assert rec.calls[1][2]["button"] == "middle"


def test_click_with_modifier_text_resolves_via_keymap(rec):
    actions.run_action({"action": "right_click", "coordinate": [0, 0], "text": "ctrl+shift"})
    _name, _args, kwargs = rec.calls[0]
    assert kwargs["modifiers"] == ["ctrl", "shift"]


def test_click_unknown_modifier_raises_unknown_key(rec):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "left_click", "coordinate": [0, 0], "text": "ctrl+bogus"})
    assert ei.value.code == "unknown_key"
    assert rec.calls == []


def test_drag_maps_both_endpoints(rec):
    actions.run_action({"action": "left_click_drag", "start_coordinate": [0, 0], "coordinate": [715, 402]})
    name, args, _kwargs = rec.calls[0]
    assert name == "drag"
    x0, y0, x1, y1 = args
    assert (x0, y0) == (0, 0)
    assert abs(x1 - 1280) <= 1 and abs(y1 - 720) <= 1


def test_scroll_direction_and_amount_passthrough(rec):
    actions.run_action({"action": "scroll", "scroll_direction": "down", "scroll_amount": 5})
    _name, args, _kwargs = rec.calls[0]
    assert args[0] == "down" and args[1] == 5


def test_mouse_down_up_dispatch(rec):
    actions.run_action({"action": "left_mouse_down"})
    actions.run_action({"action": "left_mouse_up"})
    assert rec.calls[0] == ("mouse_down", ("left",), {})
    assert rec.calls[1] == ("mouse_up", ("left",), {})


# --- type / key / hold_key ----------------------------------------------------

def test_type_passes_text_through(rec):
    actions.run_action({"action": "type", "text": "héllo \U0001F44B"})
    _name, args, _kwargs = rec.calls[0]
    assert args[0] == "héllo \U0001F44B"


def test_type_text_too_long_rejected():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "type", "text": "x" * 10001})
    assert ei.value.code == "invalid_action"


def test_key_resolves_combo_and_repeat(rec):
    actions.run_action({"action": "key", "text": "ctrl+shift+t", "repeat": 3})
    name, args, kwargs = rec.calls[0]
    assert name == "press_key"
    assert args[0] == ["ctrl", "shift", "t"]
    assert kwargs["repeat"] == 3


def test_key_unknown_raises_unknown_key(rec):
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "key", "text": "not_a_key"})
    assert ei.value.code == "unknown_key"


def test_key_repeat_out_of_range_rejected():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "key", "text": "a", "repeat": 0})
    assert ei.value.code == "invalid_action"
    with pytest.raises(actions.ActionError) as ei2:
        actions.run_action({"action": "key", "text": "a", "repeat": 101})
    assert ei2.value.code == "invalid_action"


def test_hold_key_duration_bounds(rec):
    actions.run_action({"action": "hold_key", "text": "shift", "duration": 0.1})
    assert rec.calls[0] == ("hold_key", (["shift"], 0.1), {})
    with pytest.raises(actions.ActionError):
        actions.run_action({"action": "hold_key", "text": "shift", "duration": -1})
    with pytest.raises(actions.ActionError):
        actions.run_action({"action": "hold_key", "text": "shift", "duration": 301})


def test_scroll_amount_bounds():
    with pytest.raises(actions.ActionError):
        actions.run_action({"action": "scroll", "scroll_direction": "up", "scroll_amount": 0})
    with pytest.raises(actions.ActionError):
        actions.run_action({"action": "scroll", "scroll_direction": "up", "scroll_amount": 51})


def test_scroll_bad_direction_rejected():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "scroll", "scroll_direction": "sideways", "scroll_amount": 1})
    assert ei.value.code == "invalid_action"


# --- wait ----------------------------------------------------------------------

def test_wait_sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.time, "sleep", lambda s: calls.append(s))
    result = actions.run_action({"action": "wait", "duration": 0.25})
    assert calls == [0.25]
    assert result.to_dict() == {"ok": True, "result": "OK"}


def test_wait_duration_bounds():
    with pytest.raises(actions.ActionError):
        actions.run_action({"action": "wait", "duration": -1})
    with pytest.raises(actions.ActionError):
        actions.run_action({"action": "wait", "duration": 301})


# --- screenshot / zoom -----------------------------------------------------------

def test_screenshot_result_shape(monkeypatch):
    shot = desk.Shot(png=b"PNGDATA", width=1430, height=804, scale=0.5586, screen=TWO_SCREENS[0])
    monkeypatch.setattr(desk, "screenshot", lambda screen, **kw: shot)
    result = actions.run_action({"action": "screenshot"})
    d = result.to_dict()
    assert d["ok"] is True
    assert "result" not in d
    assert d["image"]["width"] == 1430
    assert d["image"]["height"] == 804
    assert d["image"]["scale"] == 0.5586
    assert d["image"]["screen"] == 0
    assert base64.b64decode(d["image"]["png_base64"]) == b"PNGDATA"


def test_zoom_maps_region_and_calls_desk_zoom(monkeypatch):
    captured = {}

    def fake_zoom(screen, region_abs, **kw):
        captured["screen"] = screen
        captured["region"] = region_abs
        return desk.Shot(png=b"Z", width=100, height=100, scale=1.0, screen=screen, region=region_abs)

    monkeypatch.setattr(desk, "zoom", fake_zoom)
    result = actions.run_action({"action": "zoom", "region": [0, 0, 100, 100]})
    assert captured["region"][0:2] == (0, 0)
    assert result.to_dict()["image"]["width"] == 100


def test_zoom_region_must_be_ordered():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "zoom", "region": [100, 100, 50, 50]})
    assert ei.value.code == "invalid_action"


def test_zoom_region_wrong_length_rejected():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "zoom", "region": [0, 0, 100]})
    assert ei.value.code == "invalid_action"


# --- cursor_position -------------------------------------------------------------

def test_cursor_position_round_trip(monkeypatch):
    monkeypatch.setattr(desk, "cursor_position", lambda: (1280, 720))
    result = actions.run_action({"action": "cursor_position"})
    assert result.data["screen"] == 0
    assert result.data["absolute"] == [1280, 720]
    assert abs(result.data["x"] - 715) <= 1
    assert abs(result.data["y"] - 402) <= 1
    assert result.text == f"X={result.data['x']},Y={result.data['y']}"


# --- windows ------------------------------------------------------------------

def test_list_windows_text_table_and_data(monkeypatch):
    win = desk.Window(hwnd=42, title="Notepad", process="notepad.exe", rect=(0, 0, 100, 100),
                       visible=True, foreground=True)
    monkeypatch.setattr(desk, "list_windows", lambda: [win])
    result = actions.run_action({"action": "list_windows"})
    assert "42" in result.text
    assert "notepad.exe" in result.text
    assert "Notepad" in result.text
    assert "*" in result.text
    assert result.data == {"windows": [win.to_dict()]}


def test_focus_window_by_title(monkeypatch):
    win = desk.Window(hwnd=7, title="Notepad", process="notepad.exe", rect=(0, 0, 1, 1),
                       visible=True, foreground=True)
    calls = {}

    def fake_focus(*, hwnd=None, title=None):
        calls["hwnd"], calls["title"] = hwnd, title
        return win

    monkeypatch.setattr(desk, "focus_window", fake_focus)
    result = actions.run_action({"action": "focus_window", "title": "notepad"})
    assert calls == {"hwnd": None, "title": "notepad"}
    assert result.data["hwnd"] == 7


def test_focus_window_requires_hwnd_or_title():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "focus_window"})
    assert ei.value.code == "invalid_action"


# --- clipboard -----------------------------------------------------------------

def test_clipboard_get(monkeypatch):
    monkeypatch.setattr(desk, "get_clipboard", lambda: "hello")
    result = actions.run_action({"action": "get_clipboard"})
    assert result.text == "hello"


def test_clipboard_set(rec):
    actions.run_action({"action": "set_clipboard", "text": "world"})
    name, args, _kwargs = rec.calls[0]
    assert name == "set_clipboard"
    assert args[0] == "world"


# --- launch ----------------------------------------------------------------------

def test_launch_disabled_by_default(monkeypatch):
    monkeypatch.setattr(desk, "launch", lambda cmd: 4242)
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "launch", "command": "notepad.exe"})
    assert ei.value.code == "launch_disabled"


def test_launch_allowed_when_configured(monkeypatch):
    monkeypatch.setattr(desk, "launch", lambda cmd: 4242)
    result = actions.run_action({"action": "launch", "command": "notepad.exe"}, allow_launch=True)
    assert result.data == {"pid": 4242}
    assert "4242" in result.text


# --- validation / read-only set / to_dict shapes ---------------------------------

def test_unknown_action_rejected():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({"action": "frobnicate"})
    assert ei.value.code == "unknown_action"


def test_missing_action_field_rejected():
    with pytest.raises(actions.ActionError) as ei:
        actions.run_action({})
    assert ei.value.code == "unknown_action"


def test_read_only_set_matches_spec():
    assert actions.READ_ONLY_ACTIONS == frozenset({
        "screenshot", "zoom", "cursor_position", "list_windows", "get_clipboard", "wait",
    })
    for name in actions.READ_ONLY_ACTIONS:
        assert actions.is_read_only(name)
    assert not actions.is_read_only("left_click")


def test_all_actions_covers_every_table_entry():
    expected = {
        "screenshot", "zoom", "left_click", "right_click", "middle_click", "double_click",
        "triple_click", "left_click_drag", "mouse_move", "left_mouse_down", "left_mouse_up",
        "cursor_position", "scroll", "type", "key", "hold_key", "wait", "list_windows",
        "focus_window", "get_clipboard", "set_clipboard", "launch",
    }
    assert actions.ALL_ACTIONS == frozenset(expected)


def test_to_dict_text_only():
    result = actions.ActionResult(text="OK")
    assert result.to_dict() == {"ok": True, "result": "OK"}


def test_to_dict_with_data():
    result = actions.ActionResult(text="X=1,Y=2", data={"x": 1})
    assert result.to_dict() == {"ok": True, "result": "X=1,Y=2", "data": {"x": 1}}


def test_to_dict_image_only_has_no_result_key():
    shot = desk.Shot(png=b"abc", width=1, height=1, scale=1.0, screen=TWO_SCREENS[0])
    result = actions.ActionResult(image=shot)
    d = result.to_dict()
    assert "result" not in d
    assert d["image"]["png_base64"] == base64.b64encode(b"abc").decode("ascii")
