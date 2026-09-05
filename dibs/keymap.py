"""xdotool-style key names -> pyautogui key names. Owner: desk agent.

`to_pyautogui(name)` resolves a single key name. `parse_combo(text)` splits a
``+``-joined combo (``"ctrl+shift+t"``) into a list of resolved pyautogui names.

Named keys are case-insensitive. A single character (letter, digit, punctuation
already in pyautogui's own key set, including an uppercase letter) is passed
through unchanged -- pyautogui/Windows resolves the VK code (and applies SHIFT
for you) via ``VkKeyScanW`` when the character isn't already in its static
keyboard map.
"""

from __future__ import annotations


class UnknownKey(ValueError):
    """Raised when a key name can't be resolved to a pyautogui key."""

    def __init__(self, name: object):
        super().__init__(f"unknown key: {name!r}")
        self.name = name


# Canonical, lower-cased xdotool/X11-style name -> pyautogui key name.
_NAMED: dict[str, str] = {
    # Editing / navigation
    "return": "enter",
    "enter": "enter",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "home": "home",
    "end": "end",
    "page_up": "pageup",
    "pageup": "pageup",
    "prior": "pageup",
    "page_down": "pagedown",
    "pagedown": "pagedown",
    "next": "pagedown",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "space": "space",
    # Punctuation names
    "minus": "-",
    "plus": "+",
    "equal": "=",
    "comma": ",",
    "period": ".",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "grave": "`",
    "bracketleft": "[",
    "bracketright": "]",
    # Misc named keys
    "print": "printscreen",
    "print_screen": "printscreen",
    "printscreen": "printscreen",
    "scroll_lock": "scrolllock",
    "scrolllock": "scrolllock",
    "pause": "pause",
    "caps_lock": "capslock",
    "capslock": "capslock",
    "num_lock": "numlock",
    "numlock": "numlock",
    "menu": "apps",
    "apps": "apps",
    # Modifiers
    "super": "win",
    "super_l": "win",
    "super_r": "win",
    "win": "win",
    "windows": "win",
    "cmd": "win",
    "command": "win",
    "ctrl": "ctrl",
    "control": "ctrl",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
}

# F1..F24
for _i in range(1, 25):
    _NAMED[f"f{_i}"] = f"f{_i}"

# Numpad digits + common numpad operator keys.
for _i in range(10):
    _NAMED[f"kp_{_i}"] = f"num{_i}"
_NAMED.update(
    {
        "kp_enter": "enter",
        "kp_add": "add",
        "kp_subtract": "subtract",
        "kp_decimal": "decimal",
        "kp_separator": "separator",
        "kp_divide": "divide",
        "kp_multiply": "multiply",
        # Navigation-cluster numpad keys (same physical action regardless of numlock).
        "kp_home": "home",
        "kp_end": "end",
        "kp_up": "up",
        "kp_down": "down",
        "kp_left": "left",
        "kp_right": "right",
        "kp_page_up": "pageup",
        "kp_page_down": "pagedown",
        "kp_insert": "insert",
        "kp_delete": "delete",
    }
)


def to_pyautogui(name: str) -> str:
    """Resolve one xdotool-style key name to a pyautogui key name.

    A single character is passed through literally (case preserved) so
    pyautogui/Windows can resolve it (including SHIFT for uppercase / symbols).
    Raises UnknownKey for anything else that doesn't match the named table.
    """
    if not name:
        raise UnknownKey(name)
    if len(name) == 1:
        return name
    key = name.strip().lower()
    try:
        return _NAMED[key]
    except KeyError:
        raise UnknownKey(name) from None


def parse_combo(text: str) -> list[str]:
    """Split a ``+``-joined combo into resolved pyautogui key names.

    ``"ctrl+shift+t"`` -> ``["ctrl", "shift", "t"]``. A literal ``+`` key can be
    expressed with the ``plus`` alias, or with a doubled ``+`` (``"ctrl++"``
    means ctrl held with the literal ``+`` character).
    """
    if not text:
        raise UnknownKey(text)
    parts = text.split("+")
    tokens: list[str] = []
    i = 0
    n = len(parts)
    while i < n:
        part = parts[i]
        if part == "":
            # An empty split segment means two "+" were adjacent in the
            # original text -- that pair represents one literal "+" key.
            tokens.append("+")
            i += 2
        else:
            tokens.append(part)
            i += 1
    return [to_pyautogui(t) for t in tokens]
