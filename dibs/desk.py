"""Windows desktop primitives: screens, screenshots, mouse, keyboard, windows, clipboard.

Owner: desk agent. Keep these signatures — actions.py, hub.py and tests call them.
All coordinates here are ABSOLUTE virtual-desktop pixels (real pixels; the process must be
Per-Monitor-V2 DPI aware — call set_dpi_aware() once at startup). Scaling to/from screenshot
space is actions.py's job.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import math
import os
import random
import struct
import subprocess
import time
from dataclasses import dataclass, field

import mss
import pyautogui
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from PIL import Image

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.01


@dataclass(frozen=True)
class Screen:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "primary": self.primary,
        }


@dataclass
class Shot:
    png: bytes
    width: int  # pixels of the returned PNG
    height: int
    scale: float  # returned px / captured px (<= 1.0)
    screen: Screen
    region: tuple[int, int, int, int] | None = None  # absolute (l, t, r, b) if a zoom


@dataclass
class Window:
    hwnd: int
    title: str
    process: str
    rect: tuple[int, int, int, int]  # (left, top, right, bottom) absolute
    visible: bool
    foreground: bool
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "process": self.process,
            "rect": list(self.rect),
            "visible": self.visible,
            "foreground": self.foreground,
        }


class DeskError(RuntimeError):
    """Raised for any failed primitive (bad window, failsafe, unknown key...)."""


class FailsafeTriggered(DeskError):
    """pyautogui FAILSAFE fired (mouse flung to a corner during an action)."""


# ---------------------------------------------------------------------------
# DPI awareness
# ---------------------------------------------------------------------------

_dpi_aware_done = False


def set_dpi_aware() -> None:
    """Make this process Per-Monitor-V2 DPI aware. Idempotent. Call before anything else."""
    global _dpi_aware_done
    if _dpi_aware_done:
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        if not ok:
            raise OSError("SetProcessDpiAwarenessContext returned FALSE")
    except Exception:
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE == 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass  # already set by manifest, unsupported OS, etc: swallow
    _dpi_aware_done = True


# ---------------------------------------------------------------------------
# Screens / screenshots
# ---------------------------------------------------------------------------


def _scale_for(width: int, height: int, max_long_edge: int, max_pixels: int) -> float:
    long_edge = max(width, height)
    return min(1.0, max_long_edge / long_edge, math.sqrt(max_pixels / (width * height)))


def list_screens() -> list[Screen]:
    """Monitors in stable order; index 0 is the primary (its top-left is (0,0))."""
    set_dpi_aware()
    raw = []
    for hmon, _hdc, _rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmon)
        left, top, right, bottom = info["Monitor"]
        is_primary = bool(info.get("Flags", 0) & win32con.MONITORINFOF_PRIMARY)
        raw.append(
            {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "primary": is_primary,
            }
        )

    primaries = [m for m in raw if m["primary"]]
    others = sorted((m for m in raw if not m["primary"]), key=lambda m: (m["x"], m["y"]))
    ordered = primaries + others

    return [
        Screen(
            index=i, x=m["x"], y=m["y"], width=m["width"], height=m["height"], primary=m["primary"]
        )
        for i, m in enumerate(ordered)
    ]


def primary_screen() -> Screen:
    screens = list_screens()
    for s in screens:
        if s.primary:
            return s
    return screens[0]


def _grab_rgb(left: int, top: int, width: int, height: int) -> Image.Image:
    with mss.mss() as sct:
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=3)
    return buf.getvalue()


def screenshot(screen: Screen, *, max_long_edge: int = 1568, max_pixels: int = 1_150_000) -> Shot:
    """Capture one whole screen, downscale to fit the limits (LANCZOS), PNG-encode."""
    set_dpi_aware()
    img = _grab_rgb(screen.x, screen.y, screen.width, screen.height)
    scale = _scale_for(screen.width, screen.height, max_long_edge, max_pixels)
    if scale < 1.0:
        new_w, new_h = round(screen.width * scale), round(screen.height * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
    else:
        new_w, new_h = img.width, img.height
    return Shot(png=_encode_png(img), width=new_w, height=new_h, scale=scale, screen=screen)


def zoom(
    screen: Screen,
    region_abs: tuple[int, int, int, int],
    *,
    max_long_edge: int = 1568,
    max_pixels: int = 1_150_000,
) -> Shot:
    """Capture an absolute region at native resolution; downscale only if it exceeds the limits."""
    set_dpi_aware()
    left, top, right, bottom = region_abs
    width, height = right - left, bottom - top
    img = _grab_rgb(left, top, width, height)
    scale = _scale_for(width, height, max_long_edge, max_pixels)
    if scale < 1.0:
        new_w, new_h = round(width * scale), round(height * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
    else:
        new_w, new_h = width, height
    return Shot(
        png=_encode_png(img),
        width=new_w,
        height=new_h,
        scale=scale,
        screen=screen,
        region=region_abs,
    )


# ---------------------------------------------------------------------------
# Human-like mouse motion (SPEC-v0.3 §2).
#
# `motion_path` is a pure function: given a start/end point and a speed multiplier it returns
# a list of (t_s, x, y) samples along an eased, gently-curved path. `_drive_motion_path` walks
# that list in real time via `win32api.SetCursorPos` (fast -- pyautogui's per-call pause is far
# too slow for 90 Hz steps). `configure_motion` is the on/off + speed knob the hub wires up from
# `settings.motion`; when disabled every mover below falls back to the old instant
# `pyautogui.moveTo` behaviour.
# ---------------------------------------------------------------------------

_motion_enabled = True
_motion_speed = 1.0

_MOTION_HZ = 90  # sample rate for the driven path
_SNAP_PX = 12.0  # moves shorter than this snap instead of curving
_SNAP_DURATION_S = 0.02
_CLICK_SETTLE_RANGE_S = (0.04, 0.09)
_DRAG_DURATION_RANGE_S = (0.25, 0.6)


def configure_motion(enabled: bool = True, speed: float = 1.0) -> None:
    """Module-level motion settings, set once at hub startup from `settings.motion`."""
    global _motion_enabled, _motion_speed
    _motion_enabled = bool(enabled)
    _motion_speed = max(0.01, float(speed))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _duration_for_distance(dist_px: float, speed: float) -> float:
    speed = max(speed, 1e-6)
    return _clamp(0.12 + (dist_px / 2200.0) / speed, 0.12, 0.8)


def _minimum_jerk(u: float) -> float:
    """s(t) = 10t^3 - 15t^4 + 6t^5 -- ease-in-out with zero velocity/acceleration at both ends."""
    return 10 * u**3 - 15 * u**4 + 6 * u**5


def _bezier_path(
    x0: float, y0: float, x1: float, y1: float, duration: float, seed: int | None = None
) -> list[tuple[float, float, float]]:
    """Quadratic-Bezier path from (x0,y0) to (x1,y1) over `duration` seconds, sampled at
    `_MOTION_HZ`, eased by `_minimum_jerk`. The control point is offset from the chord's
    midpoint perpendicular to it by +-(4-10%) of the distance; `random.Random(seed)` picks the
    magnitude and sign so callers (and tests) can pin it via `seed`."""
    dist = math.hypot(x1 - x0, y1 - y0)
    n_steps = max(2, round(duration * _MOTION_HZ))
    rnd = random.Random(seed)
    if dist > 1e-6:
        pct = rnd.uniform(0.04, 0.10)
        sign = rnd.choice((-1.0, 1.0))
        offset = sign * pct * dist
        perp_x, perp_y = -(y1 - y0) / dist, (x1 - x0) / dist
    else:
        offset = 0.0
        perp_x = perp_y = 0.0
    mid_x, mid_y = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    ctrl_x, ctrl_y = mid_x + perp_x * offset, mid_y + perp_y * offset

    path: list[tuple[float, float, float]] = []
    for i in range(n_steps + 1):
        u = i / n_steps
        s = _minimum_jerk(u)
        x = (1 - s) ** 2 * x0 + 2 * (1 - s) * s * ctrl_x + s**2 * x1
        y = (1 - s) ** 2 * y0 + 2 * (1 - s) * s * ctrl_y + s**2 * y1
        path.append((duration * u, x, y))
    return path


def motion_path(
    x0: float, y0: float, x1: float, y1: float, speed: float = 1.0, seed: int | None = None
) -> list[tuple[float, float, float]]:
    """Pure function: a smooth, slightly-curved path from (x0,y0) to (x1,y1). Moves shorter than
    `_SNAP_PX` snap (two points, near-instant); longer moves get a Bezier eased by minimum-jerk,
    with duration `clamp(0.12 + dist/2200 / speed, 0.12, 0.8)`. Starts/ends exactly on the given
    points; time is strictly monotonic."""
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < _SNAP_PX:
        return [(0.0, float(x0), float(y0)), (_SNAP_DURATION_S, float(x1), float(y1))]
    duration = _duration_for_distance(dist, speed)
    return _bezier_path(x0, y0, x1, y1, duration, seed)


def estimate_motion_s(
    from_xy: tuple[float, float], to_xy: tuple[float, float], speed: float | None = None
) -> float:
    """Best-effort duration of a human-like move between two absolute points, honouring
    `configure_motion` (0.0 when motion is disabled)."""
    if not _motion_enabled:
        return 0.0
    speed = _motion_speed if speed is None else speed
    dist = math.hypot(to_xy[0] - from_xy[0], to_xy[1] - from_xy[1])
    if dist < _SNAP_PX:
        return _SNAP_DURATION_S
    return _duration_for_distance(dist, speed)


def _drive_motion_path(path: list[tuple[float, float, float]]) -> None:
    """Walk a `motion_path()` result in real time via SetCursorPos (fast; avoids pyautogui's
    per-call pause), then land exactly on the target and do one `pyautogui.moveTo` so its
    failsafe corner-check still fires."""
    if not path:
        return
    start = time.perf_counter()
    for t, x, y in path:
        wait = start + t - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        win32api.SetCursorPos((round(x), round(y)))
    last_x, last_y = path[-1][1], path[-1][2]
    win32api.SetCursorPos((round(last_x), round(last_y)))
    pyautogui.moveTo(round(last_x), round(last_y))


def _move_human_like(x: float, y: float) -> None:
    x0, y0 = win32api.GetCursorPos()
    _drive_motion_path(motion_path(x0, y0, x, y, _motion_speed))


def _settle() -> None:
    time.sleep(random.uniform(*_CLICK_SETTLE_RANGE_S))


# ---------------------------------------------------------------------------
# Mouse / keyboard (pyautogui)
# ---------------------------------------------------------------------------


def _guard(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except pyautogui.FailSafeException as e:
            raise FailsafeTriggered(str(e)) from e

    return wrapper


def cursor_position() -> tuple[int, int]:
    """Absolute cursor position."""
    set_dpi_aware()
    return win32api.GetCursorPos()


@_guard
def mouse_move(x: int, y: int) -> None:
    set_dpi_aware()
    if not _motion_enabled:
        pyautogui.moveTo(x, y)
        return
    _move_human_like(x, y)


@_guard
def click(
    x: int | None,
    y: int | None,
    *,
    button: str = "left",
    clicks: int = 1,
    modifiers: list[str] | None = None,
) -> None:
    """button in {left,right,middle}; clicks 1/2/3; None coords = current position.
    modifiers are pyautogui key names held for the duration of the click. When a coordinate is
    given and motion is enabled, travels there along a human-like path and settles 40-90ms
    before pressing (SPEC-v0.3 §2)."""
    set_dpi_aware()
    modifiers = modifiers or []
    held: list[str] = []
    try:
        for m in modifiers:
            pyautogui.keyDown(m)
            held.append(m)
        if x is not None and y is not None:
            if _motion_enabled:
                _move_human_like(x, y)
                _settle()
            else:
                pyautogui.moveTo(x, y)
        interval = 0.05 if clicks > 1 else 0.0
        pyautogui.click(clicks=clicks, interval=interval, button=button)
    finally:
        for m in reversed(held):
            pyautogui.keyUp(m)


@_guard
def drag(
    x0: int, y0: int, x1: int, y1: int, *, modifiers: list[str] | None = None, duration: float = 0.3
) -> None:
    """Travels to (x0,y0) along a human-like path, presses, then drags to (x1,y1) along a
    second human-like path over 0.25-0.6s (clamped from `duration`), then releases. With motion
    disabled this is the old instant moveTo/mouseDown/moveTo/mouseUp."""
    set_dpi_aware()
    modifiers = modifiers or []
    held: list[str] = []
    try:
        for m in modifiers:
            pyautogui.keyDown(m)
            held.append(m)
        if _motion_enabled:
            _move_human_like(x0, y0)
            _settle()
            pyautogui.mouseDown()
            drag_duration = _clamp(float(duration) if duration else 0.3, *_DRAG_DURATION_RANGE_S)
            _drive_motion_path(_bezier_path(x0, y0, x1, y1, drag_duration))
            pyautogui.mouseUp()
        else:
            pyautogui.moveTo(x0, y0)
            pyautogui.mouseDown()
            pyautogui.moveTo(x1, y1, duration=duration)
            pyautogui.mouseUp()
    finally:
        for m in reversed(held):
            pyautogui.keyUp(m)


@_guard
def mouse_down(button: str = "left") -> None:
    set_dpi_aware()
    pyautogui.mouseDown(button=button)


@_guard
def mouse_up(button: str = "left") -> None:
    set_dpi_aware()
    pyautogui.mouseUp(button=button)


@_guard
def scroll(
    direction: str,
    amount: int,
    x: int | None = None,
    y: int | None = None,
    *,
    modifiers: list[str] | None = None,
) -> None:
    """direction in {up,down,left,right}; amount in wheel clicks. When a coordinate is given
    and motion is enabled, travels there along a human-like path first (SPEC-v0.3 §2)."""
    set_dpi_aware()
    modifiers = modifiers or []
    held: list[str] = []
    try:
        for m in modifiers:
            pyautogui.keyDown(m)
            held.append(m)
        if x is not None and y is not None:
            if _motion_enabled:
                _move_human_like(x, y)
            else:
                pyautogui.moveTo(x, y)
        if direction == "up":
            pyautogui.scroll(amount)
        elif direction == "down":
            pyautogui.scroll(-amount)
        elif direction == "left":
            pyautogui.hscroll(-amount)
        elif direction == "right":
            pyautogui.hscroll(amount)
        else:
            raise DeskError(f"unknown scroll direction: {direction!r}")
    finally:
        for m in reversed(held):
            pyautogui.keyUp(m)


@_guard
def press_key(keys: list[str], *, repeat: int = 1) -> None:
    """keys = pyautogui key names already resolved by keymap (a chord: hotkey semantics)."""
    set_dpi_aware()
    for _ in range(repeat):
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)


@_guard
def hold_key(keys: list[str], duration: float) -> None:
    set_dpi_aware()
    held: list[str] = []
    try:
        for k in keys:
            pyautogui.keyDown(k)
            held.append(k)
        time.sleep(duration)
    finally:
        for k in reversed(held):
            pyautogui.keyUp(k)


# ---------------------------------------------------------------------------
# Unicode typing via SendInput (pyautogui.write only handles ASCII reliably)
# ---------------------------------------------------------------------------

_PUL = ctypes.POINTER(ctypes.wintypes.ULONG)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", _PUL),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.wintypes.DWORD),
        ("wParamL", ctypes.wintypes.WORD),
        ("wParamH", ctypes.wintypes.WORD),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", _PUL),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", _INPUTUNION)]


_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_RETURN = 0x0D
_VK_TAB = 0x09


def _send_input(ki: _KEYBDINPUT) -> None:
    inp = _INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=ki))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _send_unicode_unit(code_unit: int, *, key_up: bool) -> None:
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if key_up else 0)
    ki = _KEYBDINPUT(0, code_unit, flags, 0, ctypes.pointer(ctypes.wintypes.ULONG(0)))
    _send_input(ki)


def _send_vk(vk: int, *, key_up: bool) -> None:
    flags = _KEYEVENTF_KEYUP if key_up else 0
    ki = _KEYBDINPUT(vk, 0, flags, 0, ctypes.pointer(ctypes.wintypes.ULONG(0)))
    _send_input(ki)


def _tap_vk(vk: int) -> None:
    _send_vk(vk, key_up=False)
    _send_vk(vk, key_up=True)


def type_text(text: str, *, interval: float = 0.01) -> None:
    """Type literal text at the keyboard focus. Handles non-ASCII via unicode SendInput,
    one UTF-16 code unit (surrogate pairs included) per keydown/keyup."""
    set_dpi_aware()
    for ch in text:
        if ch == "\n":
            _tap_vk(_VK_RETURN)
        elif ch == "\t":
            _tap_vk(_VK_TAB)
        elif ch == "\r":
            pass  # usually paired with \n; nothing to send on its own
        else:
            units = struct.unpack(f"<{len(ch.encode('utf-16-le')) // 2}H", ch.encode("utf-16-le"))
            for unit in units:
                _send_unicode_unit(unit, key_up=False)
                _send_unicode_unit(unit, key_up=True)
        if interval:
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

_DWMWA_CLOAKED = 14


def _is_cloaked(hwnd: int) -> bool:
    try:
        val = ctypes.wintypes.DWORD(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), _DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)
        )
        return val.value != 0
    except Exception:
        return False


_PROCESS_QUERY_LIMITED_INFORMATION = getattr(win32con, "PROCESS_QUERY_LIMITED_INFORMATION", 0x1000)


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    handle = None
    try:
        handle = win32api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        path = win32process.GetModuleFileNameEx(handle, 0)
        return os.path.basename(path)
    except Exception:
        return ""
    finally:
        if handle is not None:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass


def list_windows() -> list[Window]:
    """Top-level visible windows with a non-empty title, foreground first."""
    set_dpi_aware()
    fg = win32gui.GetForegroundWindow()
    windows: list[Window] = []

    def _cb(hwnd, _extra):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        try:
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        except Exception:
            ex_style = 0
        if ex_style & win32con.WS_EX_TOOLWINDOW:
            return
        if _is_cloaked(hwnd):
            return
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        windows.append(
            Window(
                hwnd=hwnd,
                title=title,
                process=_process_name(pid),
                rect=rect,
                visible=True,
                foreground=(hwnd == fg),
            )
        )

    win32gui.EnumWindows(_cb, None)
    windows.sort(key=lambda w: 0 if w.foreground else 1)
    return windows


def _find_window(*, hwnd: int | None, title: str | None) -> Window:
    windows = list_windows()
    if hwnd is not None:
        for w in windows:
            if w.hwnd == hwnd:
                return w
        raise DeskError(f"no window with hwnd {hwnd}")
    if title is not None:
        needle = title.lower()
        for w in windows:
            if needle in w.title.lower():
                return w
        raise DeskError(f"no window matching title {title!r}")
    raise DeskError("focus_window requires hwnd or title")


@_guard
def focus_window(*, hwnd: int | None = None, title: str | None = None) -> Window:
    """Bring a window to the foreground (restore if minimized). title = case-insensitive substring."""
    set_dpi_aware()
    win = _find_window(hwnd=hwnd, title=title)
    target = win.hwnd

    if win32gui.IsIconic(target):
        win32gui.ShowWindow(target, win32con.SW_RESTORE)

    for _attempt in range(2):
        if win32gui.GetForegroundWindow() == target:
            break
        # Windows refuses SetForegroundWindow from a background process unless the
        # calling thread "owns" the foreground; a synthetic ALT tap is the standard
        # workaround (it makes the caller's thread the one that last changed focus).
        try:
            pyautogui.keyDown("alt")
            pyautogui.keyUp("alt")
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(target)
        except Exception:
            pass
        time.sleep(0.05)

    if win32gui.GetForegroundWindow() != target:
        raise DeskError(f"failed to focus window {target} ({win.title!r})")

    for w in list_windows():
        if w.hwnd == target:
            return w
    # Window vanished between the focus call and the re-list; return best-effort info.
    return Window(
        hwnd=target,
        title=win.title,
        process=win.process,
        rect=win.rect,
        visible=True,
        foreground=True,
    )


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


def _open_clipboard(retries: int = 10, delay: float = 0.05) -> None:
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as e:  # clipboard can be transiently held by another process
            last_exc = e
            time.sleep(delay)
    raise DeskError(f"could not open clipboard: {last_exc}")


def get_clipboard() -> str:
    _open_clipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return ""
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard(text: str) -> None:
    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


# ---------------------------------------------------------------------------
# Process launch
# ---------------------------------------------------------------------------

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def launch(command: str) -> int:
    """Start a process detached; return pid."""
    proc = subprocess.Popen(
        command,
        shell=True,
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
    )
    return proc.pid
