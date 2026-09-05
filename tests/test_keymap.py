"""Tests for dibs.keymap. Pure string mapping — no display needed."""

from __future__ import annotations

import pytest

from dibs import keymap

NAMED_KEYS = [
    ("Return", "enter"),
    ("Enter", "enter"),
    ("Tab", "tab"),
    ("Escape", "escape"),
    ("Esc", "escape"),
    ("BackSpace", "backspace"),
    ("Delete", "delete"),
    ("Insert", "insert"),
    ("Home", "home"),
    ("End", "end"),
    ("Page_Up", "pageup"),
    ("Page_Down", "pagedown"),
    ("Up", "up"),
    ("Down", "down"),
    ("Left", "left"),
    ("Right", "right"),
    ("space", "space"),
    ("minus", "-"),
    ("plus", "+"),
    ("equal", "="),
    ("comma", ","),
    ("period", "."),
    ("slash", "/"),
    ("backslash", "\\"),
    ("semicolon", ";"),
    ("apostrophe", "'"),
    ("grave", "`"),
    ("bracketleft", "["),
    ("bracketright", "]"),
    ("Print", "printscreen"),
    ("Scroll_Lock", "scrolllock"),
    ("Pause", "pause"),
    ("Caps_Lock", "capslock"),
    ("Num_Lock", "numlock"),
    ("Menu", "apps"),
    ("KP_0", "num0"),
    ("KP_1", "num1"),
    ("KP_9", "num9"),
    ("KP_Enter", "enter"),
    ("KP_Add", "add"),
    ("KP_Subtract", "subtract"),
    ("KP_Multiply", "multiply"),
    ("KP_Divide", "divide"),
    ("KP_Decimal", "decimal"),
    ("super", "win"),
    ("Super_L", "win"),
    ("win", "win"),
    ("cmd", "win"),
    ("ctrl", "ctrl"),
    ("Control_L", "ctrl"),
    ("control", "ctrl"),
    ("alt", "alt"),
    ("Alt_L", "alt"),
    ("shift", "shift"),
    ("Shift_L", "shift"),
]

F_KEYS = [(f"F{i}", f"f{i}") for i in range(1, 25)]


@pytest.mark.parametrize("name,expected", NAMED_KEYS + F_KEYS)
def test_to_pyautogui_named_keys(name, expected):
    assert keymap.to_pyautogui(name) == expected


@pytest.mark.parametrize("name,expected", [(n.upper(), e) for n, e in NAMED_KEYS])
def test_to_pyautogui_case_insensitive(name, expected):
    assert keymap.to_pyautogui(name) == expected


@pytest.mark.parametrize("ch", list("abcXYZ0123!@#"))
def test_single_character_passthrough(ch):
    # Named keys are resolved via the alias table; anything else that's a single
    # character is passed through literally (case preserved) for pyautogui/VkKeyScanW.
    assert keymap.to_pyautogui(ch) == ch


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ctrl+shift+t", ["ctrl", "shift", "t"]),
        ("alt+F4", ["alt", "f4"]),
        ("ctrl+c", ["ctrl", "c"]),
        ("super+r", ["win", "r"]),
        ("shift", ["shift"]),
        ("a", ["a"]),
        ("A", ["A"]),
        ("ctrl+shift+Left", ["ctrl", "shift", "left"]),
        ("Control_L+Alt_L+Delete", ["ctrl", "alt", "delete"]),
    ],
)
def test_parse_combo(text, expected):
    assert keymap.parse_combo(text) == expected


def test_parse_combo_literal_plus_via_alias():
    assert keymap.parse_combo("ctrl+plus") == ["ctrl", "+"]


def test_parse_combo_doubled_plus_is_literal_plus_key():
    assert keymap.parse_combo("ctrl++") == ["ctrl", "+"]
    assert keymap.parse_combo("+") == ["+"]


@pytest.mark.parametrize("name", ["", None])
def test_to_pyautogui_empty_raises(name):
    with pytest.raises(keymap.UnknownKey):
        keymap.to_pyautogui(name)


def test_to_pyautogui_unknown_name_raises():
    with pytest.raises(keymap.UnknownKey):
        keymap.to_pyautogui("not_a_real_key")


def test_to_pyautogui_out_of_range_fkey_raises():
    with pytest.raises(keymap.UnknownKey):
        keymap.to_pyautogui("F25")


def test_parse_combo_unknown_key_raises():
    with pytest.raises(keymap.UnknownKey):
        keymap.parse_combo("ctrl+not_a_key")


def test_parse_combo_empty_raises():
    with pytest.raises(keymap.UnknownKey):
        keymap.parse_combo("")


def test_unknown_key_carries_the_offending_name():
    try:
        keymap.to_pyautogui("bogus")
    except keymap.UnknownKey as e:
        assert e.name == "bogus"
    else:
        pytest.fail("expected UnknownKey")
