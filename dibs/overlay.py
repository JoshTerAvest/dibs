"""On-screen presence overlay: cursor halo, banner, click flash, consent prompt.

Owner: overlay agent. See docs/SPEC-v0.3-visual.md §1. The hub only ever talks to `OverlayBase`
and gets an instance from `create(settings)`; `NullOverlay` is the headless/no-Tk fallback.
Every public method must be safe to call from any thread.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import math
import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

from . import desk
from .tray import HUMAN_PAUSE_REASONS

log = logging.getLogger("dibs.overlay")

class OverlayBase:
    def start(self) -> None: pass
    def stop(self) -> None: pass
    def set_holder(self, name: str | None, purpose: str = "", expires_at: str | None = None) -> None: pass
    def set_paused(self, reason: str | None) -> None: pass
    def flash_click(self, x: int, y: int, button: str = "left") -> None: pass
    def show_typing(self, active: bool) -> None: pass
    def show_human(self, seconds: float = 2.0) -> None: pass
    def prompt_consent(self, request_id: str, name: str, purpose: str, timeout_s: float,
                       on_decision: Callable[[str, bool], None]) -> None: pass
    def dismiss_consent(self, request_id: str) -> None: pass
    def notify(self, text: str, seconds: float = 2.0) -> None: pass


class NullOverlay(OverlayBase):
    """No-op overlay that records calls (for tests and headless runs)."""
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
    def start(self) -> None: self._rec("start")
    def stop(self) -> None: self._rec("stop")
    def set_holder(self, name, purpose="", expires_at=None): self._rec("set_holder", name, purpose, expires_at)
    def set_paused(self, reason): self._rec("set_paused", reason)
    def flash_click(self, x, y, button="left"): self._rec("flash_click", x, y, button)
    def show_typing(self, active): self._rec("show_typing", active)
    def show_human(self, seconds=2.0): self._rec("show_human", seconds)
    def prompt_consent(self, request_id, name, purpose, timeout_s, on_decision):
        self._rec("prompt_consent", request_id, name, purpose, timeout_s)
    def dismiss_consent(self, request_id): self._rec("dismiss_consent", request_id)
    def notify(self, text, seconds=2.0): self._rec("notify", text, seconds)


# ---------------------------------------------------------------------------
# Win32 Plumbing
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000

WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_APP = 0x8000
WM_LBUTTONDOWN = 0x0201

AC_SRC_ALPHA = 1
ULW_ALPHA = 2
HWND_MESSAGE = -3

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]
class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)

_user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_long
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.CreateWindowExW.argtypes = [
    ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID
]
_user32.CreateWindowExW.restype = wintypes.HWND

def _get_ex_style(hwnd: int) -> int:
    GWL_EXSTYLE = -20
    return _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF

class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

def _get_monitors():
    monitors = []
    def callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
        r = lprcMonitor.contents
        monitors.append({"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top})
        return 1
    MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(RECT), wintypes.LPARAM)
    _user32.EnumDisplayMonitors(0, None, MonitorEnumProc(callback), 0)
    return monitors

def _update_layered(hwnd, img: Image.Image, x: int, y: int, alpha: int = 255):
    if not hwnd: return
    w, h = img.size
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    ppvBits = ctypes.c_void_p()
    hdc = _user32.GetDC(hwnd)
    memdc = _gdi32.CreateCompatibleDC(hdc)
    hbitmap = _gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(ppvBits), None, 0)
    old_bitmap = _gdi32.SelectObject(memdc, hbitmap)
    
    raw = img.tobytes("raw", "BGRa")
    ctypes.memmove(ppvBits, raw, len(raw))
    
    ptDst = POINT(x, y)
    ptSrc = POINT(0, 0)
    size = SIZE(w, h)
    blend = BLENDFUNCTION(0, 0, alpha, AC_SRC_ALPHA)
    
    _user32.UpdateLayeredWindow(hwnd, hdc, ctypes.byref(ptDst), ctypes.byref(size),
                                memdc, ctypes.byref(ptSrc), 0, ctypes.byref(blend), ULW_ALPHA)
    
    _gdi32.SelectObject(memdc, old_bitmap)
    _gdi32.DeleteObject(hbitmap)
    _gdi32.DeleteDC(memdc)
    _user32.ReleaseDC(hwnd, hdc)


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _seconds_until(iso_str: str | None) -> float | None:
    if not iso_str: return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return (dt - now).total_seconds()
    except Exception:
        return None

def _create_font(size: int, bold=False):
    import os
    font_path = os.path.join(os.environ["WINDIR"], "Fonts", "segoeuib.ttf" if bold else "segoeui.ttf")
    try:
        return ImageFont.truetype(font_path, size)
    except Exception:
        return ImageFont.load_default()

class Overlay(OverlayBase):
    def __init__(self, *, halo_color: str = "#00e5ff", banner: bool = True) -> None:
        self.halo_color = halo_color
        self.banner = banner
        self.available = False
        
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready_evt = threading.Event()
        self._q: queue.Queue[Callable[[], None]] = queue.Queue()
        
        self._state = "hidden"
        self._holder_name: str | None = None
        self._holder_purpose = ""
        self._expires_at: str | None = None
        self._paused_reason: str | None = None
        self._typing = False
        self._notify_text: str | None = None
        self._notify_until = 0.0
        
        self._hwnd_msg = 0
        self._hwnd_cursor = 0
        self._hwnd_banner = 0
        self._hwnds_edges = []
        self._hwnd_consent = 0
        
        self._monitors = []
        self._t0 = 0.0
        
        self._cursor_base_img = None
        self._edge_imgs = {}
        
        self._click_flashes = []
        self._consent_state = None
        
        self._wndproc_c = WNDPROC(self._wndproc)

    def start(self) -> None:
        try:
            with self._lock:
                if self._thread is not None and self._thread.is_alive(): return
                self._ready_evt.clear()
                self.available = True
                self._thread = threading.Thread(target=self._run, name="dibs-overlay", daemon=True)
                self._thread.start()
            ready = self._ready_evt.wait(timeout=5.0)
            if not ready or not self.available:
                self.available = False
        except Exception:
            self.available = False

    def stop(self) -> None:
        try:
            with self._lock:
                thread = self._thread
            if thread is None or not thread.is_alive():
                self.available = False
                return
            self._post(lambda: _user32.PostMessageW(self._hwnd_msg, WM_QUIT, 0, 0))
            thread.join(timeout=3.0)
            with self._lock:
                self._thread = None
            self.available = False
        except Exception:
            pass

    def _post(self, fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        if not self.available: return
        self._q.put_nowait(lambda: fn(*args, **kwargs))
        if self._hwnd_msg:
            _user32.PostMessageW(self._hwnd_msg, WM_APP, 0, 0)

    def set_holder(self, name, purpose="", expires_at=None): self._post(self._impl_set_holder, name, purpose, expires_at)
    def set_paused(self, reason): self._post(self._impl_set_paused, reason)
    def flash_click(self, x, y, button="left"): self._post(self._impl_flash_click, int(x), int(y))
    def show_typing(self, active): self._post(self._impl_show_typing, bool(active))
    def show_human(self, seconds=2.0): self._post(self._impl_show_human, float(seconds))
    def prompt_consent(self, request_id, name, purpose, timeout_s, on_decision):
        self._post(self._impl_prompt_consent, request_id, name, purpose, float(timeout_s), on_decision)
    def dismiss_consent(self, request_id): self._post(self._impl_dismiss_consent, request_id)
    def notify(self, text, seconds=2.0): self._post(self._impl_notify, text, float(seconds))

    def _run(self) -> None:
        try:
            desk.set_dpi_aware()
            self._monitors = _get_monitors()
            self._t0 = time.monotonic()
            
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_c
            class_name = ctypes.c_wchar_p("DibsOverlayClass")
            wc.lpszClassName = class_name
            wc.hInstance = 0
            _user32.RegisterClassW(ctypes.byref(wc))
            
            self._hwnd_msg = _user32.CreateWindowExW(0, class_name, ctypes.c_wchar_p("Msg"), 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
            
            def create_layered(hit_test=False):
                ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
                if not hit_test:
                    ex |= WS_EX_TRANSPARENT
                hwnd = _user32.CreateWindowExW(ex, class_name, ctypes.c_wchar_p("Overlay"), WS_POPUP, -1000, -1000, 10, 10, None, None, None, None)
                _user32.ShowWindow(hwnd, 5) # SW_SHOW
                return hwnd

            self._hwnd_cursor = create_layered()
            self._hwnd_banner = create_layered()
            self._hwnds_edges = [create_layered() for _ in range(4 * len(self._monitors))]
            self._hwnd_consent = create_layered(hit_test=True)
            
            _user32.SetTimer(self._hwnd_msg, 1, 33, 0)
            self.available = True
            self._ready_evt.set()
            
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0):
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
                
        except Exception as e:
            log.warning("dibs.overlay failed to start: %s", e)
            self.available = False
            self._ready_evt.set()
        finally:
            self.available = False
            if self._hwnd_msg: _user32.DestroyWindow(self._hwnd_msg)
            if self._hwnd_cursor: _user32.DestroyWindow(self._hwnd_cursor)
            if self._hwnd_banner: _user32.DestroyWindow(self._hwnd_banner)
            for h in self._hwnds_edges: _user32.DestroyWindow(h)
            if self._hwnd_consent: _user32.DestroyWindow(self._hwnd_consent)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            self._on_timer()
            return 0
        elif msg == WM_APP:
            while True:
                try: fn = self._q.get_nowait()
                except queue.Empty: break
                try: fn()
                except Exception: log.exception("dibs.overlay queued fn failed")
            return 0
        elif msg == WM_LBUTTONDOWN and hwnd == self._hwnd_consent:
            x = lparam & 0xFFFF
            y = (lparam >> 16) & 0xFFFF
            self._on_consent_click(x, y)
            return 0
        elif msg == WM_QUIT:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _impl_set_holder(self, name, purpose, expires_at):
        self._holder_name = name
        self._holder_purpose = purpose or ""
        self._expires_at = expires_at
        self._state = "active" if name else "hidden"
        self._cursor_base_img = None # redraw name tag
        self._update_all()

    def _impl_set_paused(self, reason):
        if reason is None:
            self._paused_reason = None
            self._state = "active" if self._holder_name else "hidden"
        else:
            self._paused_reason = reason
            self._state = "paused"
        self._update_all()

    def _impl_show_typing(self, active):
        self._typing = active
        self._update_all()

    def _impl_show_human(self, seconds):
        self._state = "human"
        self._update_all()
        def revert():
            if self._state == "human":
                self._state = "paused" if self._paused_reason else ("active" if self._holder_name else "hidden")
                self._update_all()
        threading.Timer(seconds, lambda: self._post(revert)).start()

    def _impl_flash_click(self, x, y):
        self._click_flashes.append({"x": x, "y": y, "t": time.monotonic()})

    def _impl_notify(self, text, seconds):
        self._notify_text = text
        self._notify_until = time.monotonic() + max(0.0, seconds)
        self._update_all()

    def _impl_prompt_consent(self, request_id, name, purpose, timeout_s, on_decision):
        if self._consent_state:
            self._impl_dismiss_consent(self._consent_state["id"])
        self._consent_state = {
            "id": request_id, "name": name, "purpose": purpose,
            "deadline": time.monotonic() + timeout_s, "cb": on_decision,
            "btn_allow": None, "btn_deny": None, "decided": False
        }
        self._update_consent()

    def _impl_dismiss_consent(self, request_id):
        if self._consent_state and self._consent_state["id"] == request_id:
            self._consent_state["decided"] = True
            self._consent_state = None
            _update_layered(self._hwnd_consent, Image.new("RGBA", (1,1)), 0, 0, 0)

    def _on_consent_click(self, x, y):
        st = self._consent_state
        if not st or st["decided"]: return
        allow = st["btn_allow"]
        deny = st["btn_deny"]
        decided = None
        if allow and allow[0] <= x <= allow[2] and allow[1] <= y <= allow[3]: decided = True
        elif deny and deny[0] <= x <= deny[2] and deny[1] <= y <= deny[3]: decided = False
        
        if decided is not None:
            st["decided"] = True
            try: st["cb"](st["id"], decided)
            except Exception: pass
            self._consent_state = None
            _update_layered(self._hwnd_consent, Image.new("RGBA", (1,1)), 0, 0, 0)

    def _on_timer(self):
        t = time.monotonic()
        
        # Cursor
        if self._state == "active" and self._holder_name:
            try:
                cx, cy = desk.cursor_position()
            except Exception:
                cx, cy = 0, 0
                
            if not self._cursor_base_img:
                self._cursor_base_img = self._render_cursor_base()
            
            img = self._cursor_base_img.copy()
            draw = ImageDraw.Draw(img)
            
            active_flashes = []
            for f in self._click_flashes:
                age = t - f["t"]
                if age < 0.3:
                    active_flashes.append(f)
                    p = age / 0.3
                    r = 10 + 50 * p
                    w = max(1, 3 - int(3 * p))
                    dx = f["x"] - cx + 120
                    dy = f["y"] - cy + 120
                    draw.ellipse((dx-r, dy-r, dx+r, dy+r), outline=self.halo_color, width=w)
            self._click_flashes = active_flashes
            
            sine = (math.sin((t - self._t0) * math.pi * 2 / 2.4) + 1) / 2
            alpha = int(255 * (0.92 + 0.08 * sine))
            
            _update_layered(self._hwnd_cursor, img, cx - 120, cy - 120, alpha)
        else:
            _update_layered(self._hwnd_cursor, Image.new("RGBA", (1,1)), 0, 0, 0)
            
        self._update_edges(t)
        
        if self._notify_text and t >= self._notify_until:
            self._notify_text = None
            self._update_banner()
            
        if self._consent_state:
            if t > self._consent_state["deadline"]:
                self._impl_dismiss_consent(self._consent_state["id"])
            else:
                self._update_consent()

    def _update_all(self):
        self._update_banner()

    def _render_cursor_base(self):
        img = Image.new("RGBA", (240, 240), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        rgb = _hex_to_rgb(self.halo_color)
        
        for r in range(40, 0, -2):
            a = int(60 * (1 - r/40))
            draw.ellipse((120-r, 120-r, 120+r, 120+r), fill=(*rgb, a))
            
        draw.ellipse((120-14, 120-14, 120+14, 120+14), outline=(*rgb, 200), width=2)
        
        name = self._holder_name or ""
        font = _create_font(12)
        tw = int(draw.textlength(name, font=font))
        lx, ly = 120 + 20, 120 + 20
        draw.rounded_rectangle((lx, ly, lx + tw + 16, ly + 22), radius=6, fill="#14161c")
        draw.text((lx + 8, ly + 2), name, fill="#ffffff", font=font)
        
        if self._typing:
            tag = "⌨ typing…"
            tw2 = int(draw.textlength(tag, font=font))
            tx, ty = 120 + 20, 120 - 24
            draw.rounded_rectangle((tx, ty, tx + tw2 + 16, ty + 22), radius=6, fill="#14161c")
            draw.text((tx + 8, ty + 2), tag, fill="#ffffff", font=font)
            
        return img

    def _get_edge_img(self, color, width, height, is_vertical, is_end):
        key = (color, width, height, is_vertical, is_end)
        if key in self._edge_imgs: return self._edge_imgs[key]
        
        img = Image.new("RGBA", (width, height), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        rgb = _hex_to_rgb(color)
        
        if is_vertical:
            for x in range(width):
                a = int(102 * (1 - x/width))
                dx = width - 1 - x if is_end else x
                draw.line((dx, 0, dx, height), fill=(*rgb, a))
        else:
            for y in range(height):
                a = int(102 * (1 - y/height))
                dy = height - 1 - y if is_end else y
                draw.line((0, dy, width, dy), fill=(*rgb, a))
                
        self._edge_imgs[key] = img
        return img

    def _update_edges(self, t):
        if self._state == "hidden":
            for h in self._hwnds_edges:
                _update_layered(h, Image.new("RGBA", (1,1)), 0, 0, 0)
            return

        if self._state == "active":
            color = self.halo_color
            sine = (math.sin((t - self._t0) * math.pi * 2 / 2.4) + 1) / 2
            alpha = int(255 * (0.8 + 0.2 * sine))
        elif self._state == "paused":
            color = "#e2434b"
            alpha = 200
        elif self._state == "human":
            color = "#33d17a"
            alpha = 255
        else:
            color = "#8a8f9c"
            alpha = 255

        idx = 0
        thickness = 90
        for m in self._monitors:
            mx, my, mw, mh = m["x"], m["y"], m["w"], m["h"]
            img = self._get_edge_img(color, mw, thickness, False, False)
            _update_layered(self._hwnds_edges[idx], img, mx, my, alpha)
            idx += 1
            img = self._get_edge_img(color, mw, thickness, False, True)
            _update_layered(self._hwnds_edges[idx], img, mx, my + mh - thickness, alpha)
            idx += 1
            img = self._get_edge_img(color, thickness, mh - 2*thickness, True, False)
            _update_layered(self._hwnds_edges[idx], img, mx, my + thickness, alpha)
            idx += 1
            img = self._get_edge_img(color, thickness, mh - 2*thickness, True, True)
            _update_layered(self._hwnds_edges[idx], img, mx + mw - thickness, my + thickness, alpha)
            idx += 1

    def _update_banner(self):
        if not self.banner or self._state == "hidden":
            _update_layered(self._hwnd_banner, Image.new("RGBA", (1,1)), 0, 0, 0)
            return
            
        bw, bh = 520, 36
        bg, border, fg = "#14161c", self.halo_color, "#f5f5f7"
        hint = ""
        text = ""
        
        if self._state == "active":
            rem = _seconds_until(self._expires_at)
            ex_txt = f" · expires in {int(rem)}s" if rem is not None else ""
            p = self._holder_purpose[:57] + "…" if len(self._holder_purpose) > 60 else self._holder_purpose
            text = f"🤖 {self._holder_name} has dibs on the desk — {p}{ex_txt}"
            hint = "Ctrl+Alt+Shift+P pause · R take back"
        elif self._state == "paused":
            if self._paused_reason in HUMAN_PAUSE_REASONS:
                bg, border, fg = "#0f2418", "#33d17a", "#b8f5c9"
                text = "You have the desk — agents paused"
            else:
                bg, border, fg = "#2a1013", "#e2434b", "#ffb4b8"
                text = f"Paused — {self._paused_reason or 'manual'}"
        elif self._state == "human":
            bg, border, fg = "#0f2418", "#33d17a", "#b8f5c9"
            text = "You have the desk — agents paused"

        img = Image.new("RGBA", (bw, bh + (30 if self._notify_text else 0)), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        
        rgb = _hex_to_rgb(border)
        draw.rounded_rectangle((0, 0, bw-1, bh-1), radius=18, fill=(*(_hex_to_rgb(bg)), 224), outline=(*rgb, 255), width=1)
        
        font = _create_font(13)
        font_sm = _create_font(11)
        
        if hint:
            tw = int(draw.textlength(text, font=font))
            draw.text((bw//2 - tw//2, 4), text, fill=fg, font=font)
            hw = int(draw.textlength(hint, font=font_sm))
            draw.text((bw//2 - hw//2, 18), hint, fill="#9aa0ad", font=font_sm)
        else:
            tw = int(draw.textlength(text, font=font))
            draw.text((bw//2 - tw//2, 8), text, fill=fg, font=font)
            
        if self._notify_text:
            draw.rounded_rectangle((0, bh + 4, bw-1, bh + 28), radius=8, fill=(*(_hex_to_rgb(bg)), 224))
            nw = int(draw.textlength(self._notify_text, font=font))
            draw.text((bw//2 - nw//2, bh + 8), self._notify_text, fill="#f5f5f7", font=font)

        scr = self._monitors[0] if self._monitors else {"x": 0, "y": 0, "w": 1920}
        x = scr["x"] + (scr["w"] - bw) // 2
        y = scr["y"] + 8
        _update_layered(self._hwnd_banner, img, x, y, 255)

    def _update_consent(self):
        st = self._consent_state
        if not st: return
        w, h = 360, 150
        img = Image.new("RGBA", (w, h), (0,0,0,0))
        draw = ImageDraw.Draw(img)
        
        bg = _hex_to_rgb("#181b22")
        border = _hex_to_rgb("#f5a623")
        draw.rounded_rectangle((0, 0, w-1, h-1), radius=8, fill=(*bg, 255), outline=(*border, 255), width=2)
        
        font_b = _create_font(15, bold=True)
        font = _create_font(13)
        font_s = _create_font(12)
        
        draw.text((16, 16), f"{st['name']} wants the desk", fill="#ffffff", font=font_b)
        draw.text((16, 40), st['purpose'], fill="#c7cbd4", font=font)
        
        rem = max(0, int(st["deadline"] - time.monotonic()))
        draw.text((16, 64), f"expires in {rem}s", fill="#8b93a3", font=font_s)
        
        draw.rounded_rectangle((16, 90, 106, 120), radius=4, fill="#2f6f4f")
        draw.text((32, 96), "Allow 5 min", fill="#ffffff", font=font_s)
        st["btn_allow"] = (16, 90, 106, 120)
        
        draw.rounded_rectangle((116, 90, 176, 120), radius=4, fill="#7a2f36")
        draw.text((132, 96), "Deny", fill="#ffffff", font=font_s)
        st["btn_deny"] = (116, 90, 176, 120)
        
        scr = self._monitors[0] if self._monitors else {"x": 0, "y": 0, "w": 1920, "h": 1080}
        x = scr["x"] + scr["w"] - w - 24
        y = scr["y"] + scr["h"] - h - 24
        
        _update_layered(self._hwnd_consent, img, x, y, 255)

def create(settings: Any) -> OverlayBase:
    ov = getattr(settings, "overlay", None)
    if ov is None or not getattr(ov, "enabled", False):
        return NullOverlay()
    try:
        return Overlay(halo_color=getattr(ov, "halo_color", "#00e5ff"), banner=getattr(ov, "banner", True))
    except Exception as e:
        log.warning("overlay unavailable (%s); using NullOverlay", e)
        return NullOverlay()
