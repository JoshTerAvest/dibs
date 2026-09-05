"""System tray icon: the state at a glance, the human's controls one right-click away, and a toast
when an agent asks for the desk.

Design: docs/DESIGN-PRINCIPLES.md (Familiarity: same five colours everywhere; Simplicity: short
plain labels; Flexibility: every decision has a word as well as a colour).

The tray is polling-based so the hub's integration surface stays tiny: give it a `TrayActions`
object and a dashboard URL, call `start()` / `stop()`. Every `TrayActions` method may be called
from the tray's own threads.
"""
from __future__ import annotations

import logging
import threading
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from PIL import Image, ImageColor, ImageDraw, ImageFilter

log = logging.getLogger("dibs.tray")

# One colour per state, shared with the overlay and the dashboard.
COLORS = {
    "idle": "#8a8f9c",
    "locked": "#8a8f9c",
    "agent": "#00e5ff",
    "human": "#33d17a",
    "consent": "#f5a623",
    "paused": "#e2434b",
}
MODE_LABELS = {"ask": "Ask me", "hands_off": "Hands-off", "locked": "Locked"}
HUMAN_PAUSE_REASONS = {"human_took_the_mouse", "human_release"}


class TrayActions(Protocol):
    def get_state(self) -> dict[str, Any]: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def release(self) -> None: ...
    def set_mode(self, mode: str) -> None: ...
    def allow(self, request_id: str) -> None: ...
    def deny(self, request_id: str) -> None: ...
    def quit(self) -> None: ...


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a display)
# ---------------------------------------------------------------------------

def _seconds_left(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max(0, int((dt - now).total_seconds()))
    except Exception:  # noqa: BLE001
        return None


def derive_state(state: dict[str, Any], app_name: str = "dibs") -> tuple[str, str, str]:
    """-> (state_name, tooltip, detail). Precedence: paused > human > consent > agent > locked > idle.

    `human` is the special pause caused by the human (takeover or explicit release)."""
    paused = bool(state.get("paused"))
    reason = state.get("pause_reason")
    holder = (state.get("lease") or {}).get("holder")
    pending = (state.get("consent") or {}).get("pending")
    mode = state.get("mode", "ask")
    mode_label = MODE_LABELS.get(mode, mode)

    if paused and reason in HUMAN_PAUSE_REASONS:
        return "human", f"{app_name} — you have the desk", "You have the desk — agents paused"
    if paused:
        why = reason or "manual"
        return "paused", f"{app_name} — paused ({why})", f"Paused — {why}"
    if pending:
        name = pending.get("name") or pending.get("agent_id") or "an agent"
        purpose = pending.get("purpose") or ""
        detail = f"{name} wants the desk" + (f" — {purpose}" if purpose else "")
        return "consent", f"{app_name} — {name} wants the desk", detail
    if holder:
        name = holder.get("name") or holder.get("agent_id") or "an agent"
        left = _seconds_left(holder.get("expires_at"))
        left_txt = f" ({left} s)" if left is not None else ""
        return "agent", f"{app_name} — {name} has dibs{left_txt}", f"{name} has dibs{left_txt}"
    if mode == "locked":
        return "locked", f"{app_name} — locked", "Locked — agents can't take the desk"
    return "idle", f"{app_name} — idle · {mode_label}", f"Idle — {mode_label} mode"


def make_icon(state_name: str, size: int = 64) -> Image.Image:
    """Flat icon that still reads at 16 px: a disc for live states, a ring for idle, a barred ring for locked.

    Same shapes and colour constants as before (docs/DESIGN-REVIEW-2026-09-04.md is unaffected),
    plus a soft outer glow behind the shape (a blurred, low-alpha disc of the state colour) so
    the icon reads a little warmer/friendlier in the taskbar without changing what it says."""
    color = COLORS.get(state_name, COLORS["idle"])
    rgb = ImageColor.getrgb(color)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pad = size // 8
    box = (pad, pad, size - pad, size - pad)

    # Soft glow layer: a larger, blurred, translucent disc underneath the crisp shape.
    # Alpha falls off smoothly to 0 at the edge because of the blur, not a hard ring.
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_pad = max(0, pad - size // 12)
    ImageDraw.Draw(glow).ellipse((glow_pad, glow_pad, size - glow_pad, size - glow_pad), fill=rgb + (150,))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=size / 9))
    img = Image.alpha_composite(img, glow)

    d = ImageDraw.Draw(img)
    if state_name in ("idle", "locked"):
        d.ellipse(box, outline=color, width=max(3, size // 9))
        if state_name == "locked":
            y = size // 2
            d.rectangle((size // 3, y - size // 14, size - size // 3, y + size // 14), fill=color)
    else:
        d.ellipse(box, fill=color)
        # small dark centre dot so the disc has a focal point when it's tiny
        c = size // 2
        r = size // 10
        d.ellipse((c - r, c - r, c + r, c + r), fill="#14161c")
    return img


_ICON_CACHE: dict[str, Image.Image] = {}


def icon_for(state_name: str) -> Image.Image:
    if state_name not in _ICON_CACHE:
        _ICON_CACHE[state_name] = make_icon(state_name)
    return _ICON_CACHE[state_name]


def menu_spec(state: dict[str, Any], app_name: str = "dibs") -> list[dict[str, Any]]:
    """Transport-free description of the menu, so tests don't need pystray.

    Each entry: {"kind": "item"|"sep"|"submenu", "text", "action", "enabled", "default", "checked", "radio", "items"}."""
    name, _tooltip, detail = derive_state(state, app_name)
    paused = bool(state.get("paused"))
    holder = (state.get("lease") or {}).get("holder")
    pending = (state.get("consent") or {}).get("pending")
    mode = state.get("mode", "ask")

    items: list[dict[str, Any]] = [
        {"kind": "item", "text": detail, "action": None, "enabled": False},
        {"kind": "item", "text": "Open monitor", "action": "open", "enabled": True, "default": True},
    ]
    if paused:
        items.append({"kind": "item", "text": "Resume agents", "action": "resume", "enabled": True})
    else:
        items.append({"kind": "item", "text": "Pause agents", "action": "pause", "enabled": True})
    items.append({"kind": "item", "text": "Take the desk back", "action": "release", "enabled": bool(holder)})
    if pending:
        who = pending.get("name") or "agent"
        items.append({"kind": "item", "text": f"Allow {who} (5 min)", "action": ("allow", pending.get("request_id")), "enabled": True})
        items.append({"kind": "item", "text": f"Deny {who}", "action": ("deny", pending.get("request_id")), "enabled": True})
    items.append({"kind": "sep"})
    items.append({
        "kind": "submenu", "text": "Mode", "items": [
            {"kind": "item", "text": label, "action": ("mode", key), "enabled": True,
             "checked": mode == key, "radio": True}
            for key, label in MODE_LABELS.items()
        ],
    })
    items.append({"kind": "sep"})
    items.append({"kind": "item", "text": f"Quit {app_name}", "action": "quit", "enabled": True})
    return items


# ---------------------------------------------------------------------------
# Tray implementations
# ---------------------------------------------------------------------------

class NullTray:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def refresh(self) -> None:
        self.calls.append("refresh")


class Tray:
    """pystray-backed tray icon. `start()` spawns the icon thread and a 1 s poller."""

    def __init__(self, actions: TrayActions, dashboard_url: str, app_name: str = "dibs",
                 poll_s: float = 1.0) -> None:
        self.actions = actions
        self.dashboard_url = dashboard_url
        self.app_name = app_name
        self.poll_s = poll_s
        self.available = False
        self._icon: Any = None
        self._icon_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._last_key: tuple[Any, ...] | None = None
        self._notified_request: str | None = None
        self.last_title: str | None = None
        self.last_state: str | None = None

    # -- lifecycle --
    def start(self) -> None:
        try:
            import pystray  # noqa: F401  (import here so a broken pystray only disables the tray)
        except Exception as e:  # noqa: BLE001
            log.warning("tray unavailable (%s)", e)
            self.available = False
            return
        try:
            state = self._safe_state()
            name, title, _ = derive_state(state, self.app_name)
            self._icon = pystray.Icon(self.app_name, icon=icon_for(name), title=title,
                                      menu=self._build_menu(state))
            self.last_title, self.last_state = title, name
            self._stop.clear()
            self._ready.clear()
            self._icon_thread = threading.Thread(target=self._run_icon, name="dibs-tray", daemon=True)
            self._icon_thread.start()
            self._poll_thread = threading.Thread(target=self._poll, name="dibs-tray-poll", daemon=True)
            self._poll_thread.start()
            self.available = True
        except Exception:  # noqa: BLE001
            log.exception("tray failed to start")
            self.available = False

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        icon, self._icon = self._icon, None
        if icon is not None:
            self._ready.wait(2.0)   # stop() before run() has begun would strand the icon thread
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                log.debug("icon.stop failed", exc_info=True)
        for t in (self._poll_thread, self._icon_thread):
            if t is not None and t.is_alive():
                t.join(timeout=3.0)
        self._poll_thread = self._icon_thread = None
        self.available = False

    def refresh(self) -> None:
        self._wake.set()

    # -- internals --
    def _on_ready(self, icon: Any) -> None:
        icon.visible = True
        self._ready.set()

    def _run_icon(self) -> None:
        try:
            self._icon.run(setup=self._on_ready)
        except Exception:  # noqa: BLE001
            log.exception("tray icon loop died")
            self.available = False

    def _safe_state(self) -> dict[str, Any]:
        try:
            return self.actions.get_state() or {}
        except Exception:  # noqa: BLE001
            log.debug("get_state failed", exc_info=True)
            return {}

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                self._render(self._safe_state())
            except Exception:  # noqa: BLE001
                log.exception("tray render failed")
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _render(self, state: dict[str, Any]) -> None:
        icon = self._icon
        if icon is None:
            return
        name, title, detail = derive_state(state, self.app_name)
        holder = (state.get("lease") or {}).get("holder") or {}
        pending = (state.get("consent") or {}).get("pending") or {}
        key = (name, title, detail, pending.get("request_id"), state.get("mode"), holder.get("name"), bool(state.get("paused")))
        if key != self._last_key:
            self._last_key = key
            icon.icon = icon_for(name)
            icon.title = title[:120]
            icon.menu = self._build_menu(state)
            try:
                icon.update_menu()
            except Exception:  # noqa: BLE001
                log.debug("update_menu failed", exc_info=True)
            self.last_title, self.last_state = title, name
        self._notify_for_pending(icon, pending)

    def _notify_for_pending(self, icon: Any, pending: dict[str, Any]) -> None:
        rid = pending.get("request_id") if pending else None
        if not rid or rid == self._notified_request:
            if not rid:
                self._notified_request = None
            return
        self._notified_request = rid
        who = pending.get("name") or "an agent"
        purpose = pending.get("purpose") or ""
        msg = f"{who} wants the desk" + (f" — {purpose}" if purpose else "")
        try:
            icon.notify(msg, self.app_name)
        except Exception:  # noqa: BLE001
            log.debug("notify failed", exc_info=True)

    def _call(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("tray action failed")
        self._wake.set()

    def _build_menu(self, state: dict[str, Any]) -> Any:
        import pystray

        def to_items(spec: list[dict[str, Any]]) -> list[Any]:
            out: list[Any] = []
            for e in spec:
                if e["kind"] == "sep":
                    out.append(pystray.Menu.SEPARATOR)
                elif e["kind"] == "submenu":
                    out.append(pystray.MenuItem(e["text"], pystray.Menu(*to_items(e["items"]))))
                else:
                    out.append(pystray.MenuItem(
                        e["text"], self._action_for(e.get("action")),
                        enabled=e.get("enabled", True), default=e.get("default", False),
                        checked=(lambda item, c=e.get("checked", False): c) if e.get("radio") else None,
                        radio=e.get("radio", False),
                    ))
            return out

        return pystray.Menu(*to_items(menu_spec(state, self.app_name)))

    def _action_for(self, action: Any) -> Callable[..., None] | None:
        if action is None:
            return None
        a = self.actions
        if action == "open":
            return lambda *_: self._call(lambda: webbrowser.open(self.dashboard_url))
        if action == "pause":
            return lambda *_: self._call(a.pause)
        if action == "resume":
            return lambda *_: self._call(a.resume)
        if action == "release":
            return lambda *_: self._call(a.release)
        if action == "quit":
            return lambda *_: self._call(a.quit)
        if isinstance(action, tuple):
            kind, arg = action
            if kind == "allow":
                return lambda *_: self._call(lambda: a.allow(arg))
            if kind == "deny":
                return lambda *_: self._call(lambda: a.deny(arg))
            if kind == "mode":
                return lambda *_: self._call(lambda: a.set_mode(arg))
        return None


def create(actions: TrayActions, dashboard_url: str, enabled: bool = True,
           app_name: str = "dibs") -> Tray | NullTray:
    if not enabled:
        return NullTray()
    try:
        import pystray  # noqa: F401
    except Exception as e:  # noqa: BLE001
        log.warning("tray disabled: pystray unavailable (%s)", e)
        return NullTray()
    return Tray(actions, dashboard_url, app_name=app_name)
