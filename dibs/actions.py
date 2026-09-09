"""Action models + dispatcher. Owner: desk agent.

Coordinates in requests are in SCREENSHOT space of the target screen; this module maps them to
absolute pixels using the deterministic scale for that screen and calls desk.*.
"""

from __future__ import annotations

import base64
import math
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator
from pydantic import ValidationError as PydanticValidationError

from . import desk, keymap, uia

# Actions that don't change screen state (no typing/presence bookkeeping needed). They STILL
# need dibs: looking at the human's screen is a privacy act, so the human decides who may look.
READ_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "screenshot",
        "zoom",
        "cursor_position",
        "list_windows",
        "get_clipboard",
        "wait",
        "ui_tree",
        "find",
    }
)

# The only actions that need neither dibs nor consent and work while paused.
FREE_ACTIONS: frozenset[str] = frozenset({"wait"})

ALL_ACTIONS: frozenset[str] = READ_ONLY_ACTIONS | frozenset(
    {
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "triple_click",
        "left_click_drag",
        "mouse_move",
        "left_mouse_down",
        "left_mouse_up",
        "scroll",
        "scroll_pages",
        "type",
        "key",
        "hold_key",
        "focus_window",
        "set_clipboard",
        "launch",
        "click_element",
    }
)


class ActionError(ValueError):
    """Invalid action request (400). `code` is a short machine string, e.g. unknown_key."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


@dataclass
class ActionResult:
    text: str | None = None
    data: dict[str, Any] | None = None  # structured extras (cursor json, windows list, pid...)
    image: desk.Shot | None = None  # for screenshot / zoom

    def to_dict(self) -> dict[str, Any]:
        """JSON shape used by REST: {ok, result?, data?, image?{png_base64,width,height,scale,screen}}."""
        out: dict[str, Any] = {"ok": True}
        if self.text is not None:
            out["result"] = self.text
        if self.data is not None:
            out["data"] = self.data
        if self.image is not None:
            out["image"] = {
                "png_base64": base64.b64encode(self.image.png).decode("ascii"),
                "width": self.image.width,
                "height": self.image.height,
                "scale": self.image.scale,
                "screen": self.image.screen.index,
            }
        return out


def scale_for(screen: desk.Screen, max_long_edge: int, max_pixels: int) -> float:
    long_edge = max(screen.width, screen.height)
    return min(
        1.0, max_long_edge / long_edge, math.sqrt(max_pixels / (screen.width * screen.height))
    )


# ---------------------------------------------------------------------------
# Pydantic action models
# ---------------------------------------------------------------------------

Coordinate = Annotated[list[int], Field(min_length=2, max_length=2)]


class ActionBase(BaseModel):
    screen: int | None = None


class ScreenshotAction(ActionBase):
    action: Literal["screenshot"]


class ZoomAction(ActionBase):
    action: Literal["zoom"]
    region: Annotated[list[int], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def _check_region(self) -> ZoomAction:
        x0, y0, x1, y1 = self.region
        if not (x1 > x0 and y1 > y0):
            raise ValueError("region must have x1 > x0 and y1 > y0")
        return self


class ClickAction(ActionBase):
    action: Literal["left_click", "right_click", "middle_click", "double_click", "triple_click"]
    coordinate: Coordinate | None = None
    text: str | None = None


class DragAction(ActionBase):
    action: Literal["left_click_drag"]
    start_coordinate: Coordinate
    coordinate: Coordinate
    text: str | None = None


class MouseMoveAction(ActionBase):
    action: Literal["mouse_move"]
    coordinate: Coordinate


class MouseButtonAction(ActionBase):
    action: Literal["left_mouse_down", "left_mouse_up"]


class CursorPositionAction(ActionBase):
    action: Literal["cursor_position"]


class ScrollAction(ActionBase):
    action: Literal["scroll"]
    scroll_direction: Literal["up", "down", "left", "right"]
    scroll_amount: int = Field(1, ge=1, le=50)
    coordinate: Coordinate | None = None
    text: str | None = None


class ScrollPagesAction(ActionBase):
    action: Literal["scroll_pages"]
    scroll_direction: Literal["up", "down"]
    scroll_amount: int = Field(1, ge=1, le=50)
    coordinate: Coordinate | None = None


class TypeAction(ActionBase):
    action: Literal["type"]
    text: str = Field(max_length=10000)


class KeyAction(ActionBase):
    action: Literal["key"]
    text: str
    repeat: int = Field(1, ge=1, le=100)


class HoldKeyAction(ActionBase):
    action: Literal["hold_key"]
    text: str
    duration: float = Field(ge=0, le=300)


class WaitAction(ActionBase):
    action: Literal["wait"]
    duration: float = Field(ge=0, le=300)


class ListWindowsAction(ActionBase):
    action: Literal["list_windows"]


class FocusWindowAction(ActionBase):
    action: Literal["focus_window"]
    hwnd: int | None = None
    title: str | None = None

    @model_validator(mode="after")
    def _check_target(self) -> FocusWindowAction:
        if self.hwnd is None and self.title is None:
            raise ValueError("focus_window requires hwnd or title")
        return self


class GetClipboardAction(ActionBase):
    action: Literal["get_clipboard"]


class SetClipboardAction(ActionBase):
    action: Literal["set_clipboard"]
    text: str


class LaunchAction(ActionBase):
    action: Literal["launch"]
    command: str


class UiTreeAction(ActionBase):
    action: Literal["ui_tree"]
    hwnd: int | None = None
    title: str | None = None
    max_depth: int = Field(6, ge=1, le=20)
    max_nodes: int = Field(400, ge=1, le=2000)
    roles: list[str] | None = None


class FindAction(ActionBase):
    action: Literal["find"]
    text: str
    role: str | None = None
    near: str | None = None
    hwnd: int | None = None
    title: str | None = None
    exact: bool = False


class ClickElementAction(ActionBase):
    action: Literal["click_element"]
    text: str
    role: str | None = None
    near: str | None = None
    hwnd: int | None = None
    title: str | None = None
    exact: bool = False
    button: Literal["left", "right", "middle"] = "left"
    double: bool = False


Action = Annotated[
    ScreenshotAction
    | ZoomAction
    | ClickAction
    | DragAction
    | MouseMoveAction
    | MouseButtonAction
    | CursorPositionAction
    | ScrollAction
    | ScrollPagesAction
    | TypeAction
    | KeyAction
    | HoldKeyAction
    | WaitAction
    | ListWindowsAction
    | FocusWindowAction
    | GetClipboardAction
    | SetClipboardAction
    | LaunchAction
    | UiTreeAction
    | FindAction
    | ClickElementAction,
    Field(discriminator="action"),
]

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def validate(action: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a raw action dict (pydantic models inside). Raises ActionError."""
    if not isinstance(action, dict):
        raise ActionError("invalid_action", "action must be a JSON object")
    name = action.get("action")
    if name not in ALL_ACTIONS:
        raise ActionError("unknown_action", f"unknown action: {name!r}")
    try:
        model = _ACTION_ADAPTER.validate_python(action)
    except PydanticValidationError as e:
        raise ActionError("invalid_action", str(e)) from e
    return model.model_dump()


def _resolve_screen(action_screen: int | None, screen_index: int | None) -> desk.Screen:
    idx = action_screen if action_screen is not None else screen_index
    screens = desk.list_screens()
    if idx is None:
        for s in screens:
            if s.primary:
                return s
        return screens[0]
    for s in screens:
        if s.index == idx:
            return s
    raise ActionError("unknown_screen", f"no screen with index {idx}")


def _to_absolute(coord: list[int], screen: desk.Screen, scale: float) -> tuple[int, int]:
    x, y = coord
    shot_w = round(screen.width * scale)
    shot_h = round(screen.height * scale)
    if not (0 <= x < shot_w) or not (0 <= y < shot_h):
        raise ActionError(
            "coordinate_out_of_bounds",
            f"({x},{y}) outside [0,{shot_w}) x [0,{shot_h}) for screen {screen.index}",
        )
    return screen.x + round(x / scale), screen.y + round(y / scale)


def _modifiers(data: dict[str, Any]) -> list[str] | None:
    text = data.get("text")
    if not text:
        return None
    try:
        return keymap.parse_combo(text)
    except keymap.UnknownKey as e:
        raise ActionError("unknown_key", str(e)) from e


def _screen_containing(rect: tuple[int, int, int, int], screens: list[desk.Screen]) -> desk.Screen:
    left, top, right, bottom = rect
    cx, cy = (left + right) / 2, (top + bottom) / 2
    for s in screens:
        if s.x <= cx < s.x + s.width and s.y <= cy < s.y + s.height:
            return s
    for s in screens:
        if s.primary:
            return s
    return screens[0]


def _rect_to_shot(rect: tuple[int, int, int, int], screen: desk.Screen, scale: float) -> list[int]:
    left, top, right, bottom = rect
    return [
        round((left - screen.x) * scale),
        round((top - screen.y) * scale),
        round((right - screen.x) * scale),
        round((bottom - screen.y) * scale),
    ]


def _nearby_names(nodes: list[uia.UiaNode], limit: int = 10) -> list[str]:
    names = [n.name or n.value or "" for n in nodes if (n.name or n.value)]
    return names[:limit]


_CLICK_BUTTON = {
    "left_click": "left",
    "right_click": "right",
    "middle_click": "middle",
    "double_click": "left",
    "triple_click": "left",
}
_CLICK_COUNT = {"double_click": 2, "triple_click": 3}


def run_action(
    action: dict[str, Any],
    *,
    screen_index: int | None = None,
    max_long_edge: int = 1568,
    max_pixels: int = 1_150_000,
    allow_launch: bool = False,
) -> ActionResult:
    """Validate, resolve the screen (action['screen'] > screen_index > primary), map coordinates,
    call desk.*, return ActionResult. Raises ActionError (400-class) or desk.DeskError (500-class)."""
    data = validate(action)
    name = data["action"]
    screen = _resolve_screen(data.get("screen"), screen_index)
    scale = scale_for(screen, max_long_edge, max_pixels)

    def to_abs(coord: list[int]) -> tuple[int, int]:
        return _to_absolute(coord, screen, scale)

    def from_abs(ax: int, ay: int) -> tuple[int, int]:
        return round((ax - screen.x) * scale), round((ay - screen.y) * scale)

    if name == "screenshot":
        shot = desk.screenshot(screen, max_long_edge=max_long_edge, max_pixels=max_pixels)
        return ActionResult(image=shot)

    if name == "zoom":
        x0, y0, x1, y1 = data["region"]
        ax0, ay0 = to_abs([x0, y0])
        ax1, ay1 = to_abs([x1, y1])
        shot = desk.zoom(
            screen, (ax0, ay0, ax1, ay1), max_long_edge=max_long_edge, max_pixels=max_pixels
        )
        return ActionResult(image=shot)

    if name in _CLICK_BUTTON:
        coord = data.get("coordinate")
        ax = ay = None
        if coord:
            ax, ay = to_abs(coord)
        desk.click(
            ax,
            ay,
            button=_CLICK_BUTTON[name],
            clicks=_CLICK_COUNT.get(name, 1),
            modifiers=_modifiers(data),
        )
        if ax is None or ay is None:
            ax, ay = desk.cursor_position()
        return ActionResult(text="OK", data={"absolute": [ax, ay]})

    if name == "left_click_drag":
        ax0, ay0 = to_abs(data["start_coordinate"])
        ax1, ay1 = to_abs(data["coordinate"])
        desk.drag(ax0, ay0, ax1, ay1, modifiers=_modifiers(data))
        return ActionResult(text="OK", data={"absolute": [ax1, ay1]})

    if name == "mouse_move":
        ax, ay = to_abs(data["coordinate"])
        desk.mouse_move(ax, ay)
        return ActionResult(text="OK", data={"absolute": [ax, ay]})

    if name == "left_mouse_down":
        desk.mouse_down("left")
        return ActionResult(text="OK")

    if name == "left_mouse_up":
        desk.mouse_up("left")
        return ActionResult(text="OK")

    if name == "cursor_position":
        ax, ay = desk.cursor_position()
        x, y = from_abs(ax, ay)
        return ActionResult(
            text=f"X={x},Y={y}",
            data={"x": x, "y": y, "screen": screen.index, "absolute": [ax, ay]},
        )

    if name == "scroll":
        coord = data.get("coordinate")
        ax = ay = None
        if coord:
            ax, ay = to_abs(coord)
        desk.scroll(
            data["scroll_direction"], data["scroll_amount"], ax, ay, modifiers=_modifiers(data)
        )
        return ActionResult(text="OK")

    if name == "scroll_pages":
        coord = data.get("coordinate")
        if coord:
            ax, ay = to_abs(coord)
            desk.click(ax, ay)
        combo = "Page_Up" if data["scroll_direction"] == "up" else "Page_Down"
        keys = keymap.parse_combo(combo)
        desk.press_key(keys, repeat=data["scroll_amount"])
        return ActionResult(text="OK")

    if name == "type":
        desk.type_text(data["text"])
        return ActionResult(text="OK")

    if name == "key":
        try:
            keys = keymap.parse_combo(data["text"])
        except keymap.UnknownKey as e:
            raise ActionError("unknown_key", str(e)) from e
        desk.press_key(keys, repeat=data.get("repeat", 1))
        return ActionResult(text="OK")

    if name == "hold_key":
        try:
            keys = keymap.parse_combo(data["text"])
        except keymap.UnknownKey as e:
            raise ActionError("unknown_key", str(e)) from e
        desk.hold_key(keys, data["duration"])
        return ActionResult(text="OK")

    if name == "wait":
        time.sleep(data["duration"])
        return ActionResult(text="OK")

    if name == "list_windows":
        windows = desk.list_windows()
        lines = []
        for w in windows:
            mark = "*" if w.foreground else " "
            left, top, right, bottom = w.rect
            lines.append(f"{mark}{w.hwnd}\t{w.process}\t[{left},{top},{right},{bottom}]\t{w.title}")
        return ActionResult(text="\n".join(lines), data={"windows": [w.to_dict() for w in windows]})

    if name == "focus_window":
        win = desk.focus_window(hwnd=data.get("hwnd"), title=data.get("title"))
        return ActionResult(text=f"focused {win.title!r}", data=win.to_dict())

    if name == "get_clipboard":
        return ActionResult(text=desk.get_clipboard())

    if name == "set_clipboard":
        desk.set_clipboard(data["text"])
        return ActionResult(text="OK")

    if name == "launch":
        if not allow_launch:
            raise ActionError("launch_disabled", "launch is disabled by config")
        pid = desk.launch(data["command"])
        return ActionResult(text=f"OK pid={pid}", data={"pid": pid})

    if name == "ui_tree":
        try:
            win = uia.resolve_window(hwnd=data.get("hwnd"), title=data.get("title"))
        except uia.UiaError as e:
            raise ActionError("window_not_found", str(e)) from e
        roles = set(data["roles"]) if data.get("roles") else None
        try:
            nodes = uia.get_tree(
                win, max_depth=data["max_depth"], max_nodes=data["max_nodes"], roles=roles
            )
        except uia.UiaError as e:
            raise ActionError("uia_error", str(e)) from e
        target_screen = (
            screen
            if data.get("screen") is not None
            else _screen_containing(win.rect, desk.list_screens())
        )
        node_scale = scale_for(target_screen, max_long_edge, max_pixels)
        out_nodes = []
        for n in nodes:
            d = n.to_dict()
            d["rect_shot"] = _rect_to_shot(n.rect, target_screen, node_scale)
            out_nodes.append(d)
        text = uia.render_text(nodes)
        return ActionResult(
            text=text, data={"nodes": out_nodes, "hwnd": win.hwnd, "window_title": win.title}
        )

    if name == "find":
        try:
            win = uia.resolve_window(hwnd=data.get("hwnd"), title=data.get("title"))
        except uia.UiaError as e:
            raise ActionError("window_not_found", str(e)) from e
        try:
            nodes = uia.get_tree(win, max_depth=10, max_nodes=1000, roles=None)
        except uia.UiaError as e:
            raise ActionError("uia_error", str(e)) from e
        matches = uia.find_nodes(
            nodes, data["text"], role=data.get("role"), exact=data.get("exact", False)
        )
        if not matches:
            raise ActionError(
                "not_found",
                f"no element matching {data['text']!r} in {win.title!r}; nearby: "
                f"{_nearby_names(nodes)}",
            )
        if data.get("near"):
            near_matches = uia.find_nodes(nodes, data["near"])
            matches = uia.rank_by_near(matches, near_matches)
        target_screen = (
            screen
            if data.get("screen") is not None
            else _screen_containing(win.rect, desk.list_screens())
        )
        node_scale = scale_for(target_screen, max_long_edge, max_pixels)

        def node_out(n: uia.UiaNode) -> dict[str, Any]:
            d = n.to_dict()
            d["rect_shot"] = _rect_to_shot(n.rect, target_screen, node_scale)
            cx, cy = n.center()
            d["center"] = [cx, cy]
            d["center_shot"] = [
                round((cx - target_screen.x) * node_scale),
                round((cy - target_screen.y) * node_scale),
            ]
            return d

        best = node_out(matches[0])
        alternates = [node_out(m) for m in matches[1:6]]
        return ActionResult(
            text=f"found {matches[0].role} {matches[0].name!r}",
            data={"match": best, "alternates": alternates, "hwnd": win.hwnd},
        )

    if name == "click_element":
        try:
            win = uia.resolve_window(hwnd=data.get("hwnd"), title=data.get("title"))
        except uia.UiaError as e:
            raise ActionError("window_not_found", str(e)) from e
        try:
            nodes = uia.get_tree(win, max_depth=10, max_nodes=1000, roles=None)
        except uia.UiaError as e:
            raise ActionError("uia_error", str(e)) from e
        matches = uia.find_nodes(
            nodes, data["text"], role=data.get("role"), exact=data.get("exact", False)
        )
        if not matches:
            raise ActionError(
                "not_found",
                f"no element matching {data['text']!r} in {win.title!r}; nearby: "
                f"{_nearby_names(nodes)}",
            )
        if data.get("near"):
            near_matches = uia.find_nodes(nodes, data["near"])
            matches = uia.rank_by_near(matches, near_matches)
        node = matches[0]
        ax, ay = node.center()
        clicks = 2 if data.get("double") else 1
        desk.click(ax, ay, button=data.get("button", "left"), clicks=clicks)
        return ActionResult(
            text=f"clicked {node.role} {node.name!r}",
            data={"absolute": [ax, ay], "node": node.to_dict(), "hwnd": win.hwnd},
        )

    raise ActionError("unknown_action", f"unhandled action {name!r}")


def is_read_only(action_name: str) -> bool:
    return action_name in READ_ONLY_ACTIONS
