"""Windows UI Automation tree reads. Owner: desk agent (uia extension).

Read-only: builds a flat, depth-limited list of UI Automation elements under a resolved window,
in ABSOLUTE screen pixels (matching desk.py's contract -- actions.py converts to screenshot
space, same as it does for windows/clicks).

Backend: the pure-Python `uiautomation` package (comtypes-based, pypi `uiautomation`). See
DECISIONS.md for why, and for the pywinauto fallback that was considered.

Tests inject a fake tree by monkeypatching `get_tree` directly (see tests/test_uia.py) so
`find`'s ranking, `near`, role filtering and coordinate conversion are testable without real
Windows UIA.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from . import desk

try:
    import uiautomation as auto

    _HAVE_UIA = True
except Exception:  # pragma: no cover - only exercised off-Windows / without the package
    auto = None
    _HAVE_UIA = False


class UiaError(RuntimeError):
    """Raised when a UIA tree read fails (no backend, bad window, COM error)."""


@dataclass
class UiaNode:
    id: int
    role: str
    name: str
    value: str | None
    rect: tuple[int, int, int, int]  # absolute screen px (left, top, right, bottom)
    depth: int
    enabled: bool
    offscreen: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "rect": list(self.rect),
            "depth": self.depth,
            "enabled": self.enabled,
            "offscreen": self.offscreen,
        }

    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return round((left + right) / 2), round((top + bottom) / 2)


# ---------------------------------------------------------------------------
# Window resolution (same policy as desk.focus_window, minus the actual focusing)
# ---------------------------------------------------------------------------


def resolve_window(*, hwnd: int | None = None, title: str | None = None) -> desk.Window:
    """hwnd, else a case-insensitive title substring, else the current foreground window."""
    windows = desk.list_windows()
    if hwnd is not None:
        for w in windows:
            if w.hwnd == hwnd:
                return w
        raise UiaError(f"no window with hwnd {hwnd}")
    if title is not None:
        needle = title.lower()
        for w in windows:
            if needle in w.title.lower():
                return w
        raise UiaError(f"no window matching title {title!r}")
    for w in windows:
        if w.foreground:
            return w
    if windows:
        return windows[0]
    raise UiaError("no windows available")


# ---------------------------------------------------------------------------
# Tree walk (real UIA backend)
# ---------------------------------------------------------------------------


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


_com_init_done = threading.local()


def _ensure_com_initialized() -> None:
    """UIA needs COM initialized on the calling thread. actions.run_action is dispatched via
    asyncio.to_thread onto a reused thread-pool thread, so each such thread needs its own
    one-time CoInitialize -- `uiautomation`'s calls otherwise fail with
    "CoInitialize has not been called" the first time a new pool thread is used."""
    if getattr(_com_init_done, "done", False):
        return
    try:
        # Keep a reference on the thread-local -- UIAutomationInitializerInThread calls
        # UninitializeUIAutomationInCurrentThread from __del__, so an unreferenced instance
        # undoes its own CoInitialize the moment this function returns.
        _com_init_done.initializer = auto.UIAutomationInitializerInThread(debug=False)
    except Exception:
        pass
    _com_init_done.done = True


def _walk_real(hwnd: int, max_depth: int, max_nodes: int, roles: set[str] | None) -> list[UiaNode]:
    if not _HAVE_UIA:
        raise UiaError(
            "uiautomation package not available (Windows-only; install with `uv sync --extra dev`)"
        )
    _ensure_com_initialized()
    root = _safe(lambda: auto.ControlFromHandle(hwnd))
    if root is None:
        raise UiaError(f"could not get a UIA element for hwnd {hwnd}")

    nodes: list[UiaNode] = []
    next_id = [0]

    def extract(control):
        role = _safe(lambda: control.ControlTypeName, "") or ""
        name = _safe(lambda: control.Name, "") or ""
        value = None

        def _get_value():
            vp = control.GetValuePattern()
            return vp.Value if vp else None

        value = _safe(_get_value, None)
        if not value:

            def _get_toggle_or_legacy():
                lp = control.GetLegacyIAccessiblePattern()
                return lp.Value if lp else None

            value = _safe(_get_toggle_or_legacy, None)

        rect_obj = _safe(lambda: control.BoundingRectangle, None)
        if rect_obj is not None:
            rect = (
                int(rect_obj.left),
                int(rect_obj.top),
                int(rect_obj.right),
                int(rect_obj.bottom),
            )
        else:
            rect = (0, 0, 0, 0)
        enabled = bool(_safe(lambda: control.IsEnabled, True))
        offscreen = bool(_safe(lambda: control.IsOffscreen, False))
        return role, name, value, rect, enabled, offscreen

    def visit(control, depth: int) -> None:
        if len(nodes) >= max_nodes or depth > max_depth:
            return
        role, name, value, rect, enabled, offscreen = extract(control)
        children = _safe(lambda: list(control.GetChildren()), []) or []
        has_named_children = any((_safe(lambda c=c: c.Name, "") or "") for c in children)
        useful = bool(name) or bool(value) or has_named_children or depth == 0
        role_ok = roles is None or role in roles
        if useful and role_ok:
            next_id[0] += 1
            nodes.append(
                UiaNode(
                    id=next_id[0],
                    role=role,
                    name=name,
                    value=value,
                    rect=rect,
                    depth=depth,
                    enabled=enabled,
                    offscreen=offscreen,
                )
            )
        for child in children:
            if len(nodes) >= max_nodes:
                break
            visit(child, depth + 1)

    visit(root, 0)
    return nodes


def get_tree(
    window: desk.Window, *, max_depth: int = 6, max_nodes: int = 400, roles: set[str] | None = None
) -> list[UiaNode]:
    """Read the UIA tree under `window`, retrying a couple of times.

    Chromium/Electron apps (Chrome, VS Code, Slack, ...) only publish their accessibility tree
    once an assistive-tech client connects; `uiautomation` usually triggers that on the first
    query but sometimes only a beat later. If the first read comes back with nothing but the
    root, retry with a short pause before giving up -- see README for the
    `--force-renderer-accessibility` Chrome flag fallback.
    """
    last_exc: Exception | None = None
    attempts = 3
    for attempt in range(attempts):
        try:
            nodes = _walk_real(window.hwnd, max_depth, max_nodes, roles)
        except UiaError as e:
            last_exc = e
            nodes = None
        if nodes is not None and (len(nodes) > 1 or attempt == attempts - 1):
            return nodes
        if attempt < attempts - 1:
            time.sleep(0.25)
    if last_exc is not None:
        raise last_exc
    return []


def render_text(nodes: list[UiaNode]) -> str:
    """Compact one-line-per-node text rendering, indented by depth -- what an LLM reads."""
    lines = []
    for n in nodes:
        bits = [f"[{n.id}] {n.role}"]
        if n.name:
            bits.append(f'"{n.name}"')
        if n.value:
            bits.append(f"= {n.value!r}")
        flags = []
        if not n.enabled:
            flags.append("disabled")
        if n.offscreen:
            flags.append("offscreen")
        if flags:
            bits.append(f"({', '.join(flags)})")
        lines.append("  " * n.depth + " ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# find / near ranking
# ---------------------------------------------------------------------------


def find_nodes(
    nodes: list[UiaNode], text: str, *, role: str | None = None, exact: bool = False
) -> list[UiaNode]:
    """Case-insensitive substring match on name or value, optionally filtered by role.
    Order preserved (tree document order -- earlier matches are usually the more prominent
    ones)."""
    needle = text if exact else text.lower()
    out = []
    for n in nodes:
        if role is not None and n.role != role:
            continue
        name = n.name if exact else n.name.lower()
        value = (n.value or "") if exact else (n.value or "").lower()
        if exact:
            hit = name == needle or value == needle
        else:
            hit = needle in name or needle in value
        if hit:
            out.append(n)
    return out


def _dist(a: UiaNode, b: UiaNode) -> float:
    ax, ay = a.center()
    bx, by = b.center()
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def rank_by_near(matches: list[UiaNode], near_matches: list[UiaNode]) -> list[UiaNode]:
    """Reorder `matches` by distance from the nearest node in `near_matches`."""
    if not near_matches:
        return matches
    scored = sorted(matches, key=lambda m: min(_dist(m, nm) for nm in near_matches))
    return scored
