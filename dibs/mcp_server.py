"""MCP streamable-HTTP server exposing dibs's computer-use primitives. Owner: mcp agent.

`build_mcp_app(hub)` returns an ASGI app. Prefer mounting it with `mount_mcp_app`:

    from dibs.mcp_server import mount_mcp_app
    mount_mcp_app(app, hub, path="/mcp")

so both `POST /mcp` (no trailing slash) and `POST /mcp/` (and any `/mcp/<sub-path>`) reach it
directly. A bare `app.mount("/mcp", build_mcp_app(hub))` (Starlette `Mount` only) is NOT
enough: `Mount`'s path regex requires the request path to start with `"/mcp/"`, so an exact
`"/mcp"` request doesn't match it and falls through to Starlette's redirect-slashes fallback,
which 307-redirects to `"/mcp/"` *before* this module's auth wrapper (or anything else mounted
here) ever runs -- a missing/bad bearer token would come back as a 307, not a 401, and some
HTTP clients (including streaming POSTs) don't reliably follow 307s. `mount_mcp_app` closes
that gap by also registering an exact-path `Route("/mcp", endpoint=authed_app)` alongside the
`Mount` -- the two match disjoint path sets (an exact string vs. "prefix + /..."), so there's
no ambiguity, and both dispatch to the *same* authed app instance, so auth still runs first no
matter which one matched. If you can't change how the parent app mounts things, `build_mcp_app`
alone still works for `"/mcp/"` and any deeper sub-path -- just not the bare `"/mcp"` string.

and the full MCP endpoint is `http://<host>:<port>/mcp` — no trailing slash needed (see
`_RawStreamableHTTPApp` below for why any path forwarded to it "just works", unlike a bare
FastMCP mount).

Auth: every request must carry `Authorization: Bearer <agent token>`. Checked here, before the
request reaches any MCP machinery — a bad/missing token gets a plain 401 JSON body, never an MCP
protocol-level error. The authenticated `AgentInfo` is stashed in a contextvar that the tool
functions below read.

Lifespan: FastMCP's streamable-HTTP transport needs its `session_manager.run()` async context
open for the life of the process (it owns the task group that runs each request). When this app
is mounted as a sub-app under FastAPI (`app.mount(...)`), Starlette does NOT propagate the
sub-app's lifespan automatically, so relying on FastMCP's own built-in lifespan would mean it
silently never starts. We handle this two ways so server.py doesn't have to do anything special:

  1. The returned app is self-starting: on the very first HTTP request it receives, it starts the
     session manager (idempotent, lock-guarded) before dispatching.
  2. It also exposes `.dibs_lifespan`, an `@asynccontextmanager` function with the standard
     `async def lifespan(app)` shape, so server.py MAY fold it into FastAPI's own lifespan
     (`FastAPI(lifespan=mcp_app.dibs_lifespan)`, or `async with mcp_app.dibs_lifespan(): ...`
     inside a combined lifespan) for a clean, explicit shutdown. Whether or not it does, the
     self-start fallback means requests work either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from starlette.routing import Route

from .hub import AgentInfo, Hub, HubError

# ---------------------------------------------------------------------------
# Agent context — set by the auth middleware, read by tool functions.
# ---------------------------------------------------------------------------

_AGENT_CTX: contextvars.ContextVar[AgentInfo] = contextvars.ContextVar("dibs_mcp_agent")


def _require_agent() -> AgentInfo:
    try:
        return _AGENT_CTX.get()
    except LookupError as exc:  # pragma: no cover - auth middleware always sets this first
        raise ToolError("unauthorized: no authenticated agent for this call") from exc


# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

COMPUTER_DESCRIPTION = """Control the shared Windows desktop: screenshots, mouse, keyboard, \
windows, clipboard. One tool, `action` selects the behaviour — mirrors Anthropic's \
computer_toolset_20260801 action set plus a few dibs extras.

All `coordinate` / `start_coordinate` / `region` values are ints in SCREENSHOT-SPACE pixels of \
the target screen (i.e. the coordinate system of the image you last saw from `screenshot` or \
`zoom` on that screen, not real desktop pixels — dibs maps the scaling for you). `screen?` \
selects a monitor by index (default: the server's configured primary).

Observation actions (they do NOT change the screen, but they still need dibs: the human decides who may look at their screen):
  screenshot(screen?) -> image
  zoom(region=[x0,y0,x1,y1], screen?) -> image of that region at full native resolution
  cursor_position(screen?) -> text "X=..,Y=.." (+ json)
  list_windows() -> json array of {hwnd,title,process,rect,visible,foreground} (+ text table)
  get_clipboard() -> text
  wait(duration<=300s) -> "OK"

Input actions (auto-acquire the exclusive desk lease; may wait/fail if another agent holds it):
  left_click / right_click / middle_click / double_click / triple_click(coordinate?, text?, screen?)
    -- coordinate omitted = click at current cursor position; text = modifier keys held during
    the click, e.g. "shift", "ctrl", "alt", "super", or combos like "ctrl+shift"
  left_click_drag(start_coordinate, coordinate, text?) -- press at start, drag, release at end
  mouse_move(coordinate)
  left_mouse_down() / left_mouse_up()
  scroll(scroll_direction="up"|"down"|"left"|"right", scroll_amount=1..50, coordinate?, text?)
  type(text<=10000 chars) -- types literal text (non-ASCII supported)
  key(text, repeat?=1..100) -- xdotool-style key or combo, e.g. "Return", "ctrl+s", "F5"
  hold_key(text, duration<=300s)
  focus_window(title? substring case-insensitive, or hwnd) -> json of the focused window
  get_clipboard() / set_clipboard(text) -- set_clipboard is an input action
  launch(command) -- only if the server has allow_launch enabled, else errors launch_disabled

Key names: Return, Enter, Tab, Escape/Esc, BackSpace, Delete, Insert, Home, End, Page_Up, \
Page_Down, Up/Down/Left/Right, F1..F24, space, minus, plus, equal, comma, period, slash, \
backslash, semicolon, apostrophe, grave, bracketleft, bracketright, KP_0..KP_9, KP_Enter, \
KP_Add, Print, Scroll_Lock, Pause, Caps_Lock, Num_Lock, Menu, super/Super_L/win/cmd, \
ctrl/Control_L/control, alt/Alt_L, shift/Shift_L, or any single character; combine with "+".

A human may be sitting at this machine. Input actions may need the human's consent before the \
desk is granted (screenshots included: the human decides who may look at their screen) — if so this tool's error \
names the pending consent request and how long it has left; call acquire_desk(wait_s=...) to \
wait for a decision. If the human takes the mouse or \
keyboard back while you hold the desk, your next input action fails until you re-acquire. \
While you hold the desk, the human sees a coloured cursor halo and a banner naming you and \
your purpose, so they know what you're doing and can pause or take over at any time.

If the desk is busy, awaiting consent, denied, or the server is paused, this tool raises an \
error describing why (who holds it, the pending consent request, the denial reason, or the \
pause reason) and what to do next — call acquire_desk(wait_s=...) to wait, \
or back off for the given retry_after_s, per the error text."""

DESK_STATUS_DESCRIPTION = (
    "Current dibs state as JSON: pause status + reason, mode (ask/hands_off/locked), human "
    "presence, pending/recent consent decisions, who holds/queues the exclusive desk lease, "
    "registered agents, display/screen layout, and action stats. No dibs needed (reveals nothing on screen)."
)
ACQUIRE_DESK_DESCRIPTION = (
    "Try to acquire the exclusive desk (input) lease. ttl_s: how long to hold it once granted "
    "(server default/max apply). wait_s: how long to long-poll if someone else holds it, or if "
    "human consent is pending — 0 (default) returns immediately (with your queue position, or "
    "the pending consent request) instead of waiting. Returns the hub's JSON verbatim: "
    "{status:'granted'|'queued'|'awaiting_consent', ...} on success/pending; raises a tool error "
    "for {status:'denied', reason, retry_after_s} (reason is one of human_denied, timeout, "
    "locked, paused). You usually don't need this: the `computer` tool auto-acquires the lease "
    "for input actions."
)
RELEASE_DESK_DESCRIPTION = "Release the exclusive desk lease, if you currently hold it."
LIST_WINDOWS_DESCRIPTION = (
    "List top-level visible windows: hwnd, title, process, rect [left,top,right,bottom] in "
    "screenshot-space pixels, visible, foreground. Needs dibs (it reveals what is on the human's screen). Thin wrapper "
    "over computer(action='list_windows')."
)
FOCUS_WINDOW_DESCRIPTION = (
    "Bring a window to the foreground, by hwnd or a case-insensitive substring of its title. "
    "Requires the desk lease (auto-acquired). Thin wrapper over computer(action='focus_window')."
)


# ---------------------------------------------------------------------------
# Hub error -> tool-facing message
# ---------------------------------------------------------------------------


def _seconds_until(iso_ts: Any) -> int | None:
    """Best-effort `iso_ts - now`, rounded to whole seconds, or None if unparseable."""
    if not isinstance(iso_ts, str) or not iso_ts:
        return None
    try:
        ts = iso_ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        remaining = (dt - datetime.now(timezone.utc)).total_seconds()
    except ValueError:
        return None
    return max(0, round(remaining))


def _format_hub_error(exc: HubError) -> str:
    payload = exc.payload or {}
    detail = (exc.detail or "").lower()

    if exc.code == "lease_required":
        # v0.2: a consent request is pending for this (or the head-of-queue) agent, or the
        # human just took the desk back -- both come through as lease_required so `computer`'s
        # auto-lease path and a bare input action see the same error shape either way.
        if "consent" in detail:
            request_id = payload.get("request_id")
            remaining = _seconds_until(payload.get("expires_at"))
            expiry_txt = f"expires in {remaining}s" if remaining is not None else "expires soon"
            id_txt = f" ({request_id})" if request_id else ""
            return (
                f"desk needs human consent — request pending{id_txt}, {expiry_txt}; call "
                "acquire_desk(wait_s=...) to wait, or wait"
            )
        if "taken by" in detail or payload.get("human_active"):
            resume_after = payload.get("resume_after_s", 20)
            return (
                "desk taken by the human — paused; retry after it auto-resumes "
                f"(~{resume_after}s idle) or ask again"
            )
        holder = payload.get("holder")
        queue_position = payload.get("queue_position")
        if holder:
            return (
                f"desk busy: held by {holder.get('name')} ({holder.get('agent_id')}) "
                f"until {holder.get('expires_at')}; you are queued at position {queue_position} "
                "— retry or call acquire_desk(wait_s=...)"
            )
        return "desk busy: lease required — call acquire_desk(wait_s=...) or retry"

    if exc.code == "denied":
        # v0.2: consent was refused, timed out, or the desk is locked/paused for agents.
        reason = payload.get("reason") or exc.detail or "denied"
        retry_after_s = payload.get("retry_after_s")
        retry_txt = f" (retry after {retry_after_s}s)" if retry_after_s is not None else ""
        if reason == "human_denied":
            return f"desk denied by the human{retry_txt}"
        if reason == "timeout":
            return f"desk denied: no human decision in time{retry_txt}"
        if reason == "locked":
            return f"desk denied: locked by the operator, no agent may take the desk{retry_txt}"
        if reason == "paused":
            return f"desk denied: the server is paused{retry_txt}"
        return f"desk denied: {reason}{retry_txt}"

    if exc.code == "paused":
        reason = payload.get("reason") or exc.detail
        return f"paused: {reason}"
    if exc.code == "launch_disabled":
        return "launch is disabled on this dibs server (config allow_launch=false)"
    if exc.code == "not_holder":
        return "you don't currently hold the desk — call acquire_desk first"
    return f"{exc.code}: {exc.detail}" if exc.detail else exc.code


def _result_to_content(result: Any) -> list[Any]:
    """ActionResult -> FastMCP content list (Image / str; FastMCP normalises both)."""
    parts: list[Any] = []
    if result.image is not None:
        parts.append(Image(data=result.image.png, format="png"))
        if result.text:
            parts.append(result.text)
        if result.data:
            parts.append(json.dumps(result.data, separators=(",", ":")))
        return parts

    text = result.text or ""
    if result.data:
        extra = json.dumps(result.data, separators=(",", ":"))
        text = f"{text}\n{extra}" if text else extra
    parts.append(text)
    return parts


async def _run_action(hub: Hub, action: dict[str, Any]) -> list[Any]:
    agent = _require_agent()
    try:
        result = await hub.run(
            agent, action, auto_lease=True, wait_s=hub.settings.auto_lease_wait_s
        )
    except HubError as exc:
        raise ToolError(_format_hub_error(exc)) from exc
    return _result_to_content(result)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def _register_tools(mcp: FastMCP, hub: Hub) -> None:
    # structured_output=False on the list[Any]-returning tools below: their content mixes
    # FastMCP `Image` objects with plain strings, which is exactly what MCP's unstructured
    # content-block conversion (`_convert_to_content`) is for. Left at the default (auto-detect
    # from the `list[Any]` return annotation), FastMCP *also* tries to build a structured-output
    # JSON schema/model for the same return value and fails serializing the raw `Image` object
    # inside it ("Unable to serialize unknown type Image"), turning every screenshot/zoom call
    # into a tool error. We only ever want the unstructured content list here.
    @mcp.tool(description=COMPUTER_DESCRIPTION, structured_output=False)
    async def computer(
        action: str,
        coordinate: list[int] | None = None,
        start_coordinate: list[int] | None = None,
        text: str | None = None,
        scroll_direction: str | None = None,
        scroll_amount: int | None = None,
        duration: float | None = None,
        repeat: int | None = None,
        region: list[int] | None = None,
        screen: int | None = None,
        title: str | None = None,
        hwnd: int | None = None,
        command: str | None = None,
    ) -> list[Any]:
        action_dict: dict[str, Any] = {"action": action}
        for key, value in (
            ("coordinate", coordinate),
            ("start_coordinate", start_coordinate),
            ("text", text),
            ("scroll_direction", scroll_direction),
            ("scroll_amount", scroll_amount),
            ("duration", duration),
            ("repeat", repeat),
            ("region", region),
            ("screen", screen),
            ("title", title),
            ("hwnd", hwnd),
            ("command", command),
        ):
            if value is not None:
                action_dict[key] = value
        return await _run_action(hub, action_dict)

    @mcp.tool(description=DESK_STATUS_DESCRIPTION)
    async def desk_status() -> str:
        return json.dumps(hub.state())

    @mcp.tool(description=ACQUIRE_DESK_DESCRIPTION)
    async def acquire_desk(ttl_s: int | None = None, wait_s: int = 0) -> str:
        agent = _require_agent()
        try:
            result = await hub.acquire(agent, ttl_s=ttl_s, wait_s=wait_s)
        except HubError as exc:
            raise ToolError(_format_hub_error(exc)) from exc
        return json.dumps(result)

    @mcp.tool(description=RELEASE_DESK_DESCRIPTION)
    async def release_desk() -> str:
        agent = _require_agent()
        try:
            hub.release(agent)
        except HubError as exc:
            raise ToolError(_format_hub_error(exc)) from exc
        return "OK"

    @mcp.tool(description=LIST_WINDOWS_DESCRIPTION, structured_output=False)
    async def list_windows() -> list[Any]:
        return await _run_action(hub, {"action": "list_windows"})

    @mcp.tool(description=FOCUS_WINDOW_DESCRIPTION, structured_output=False)
    async def focus_window(title: str | None = None, hwnd: int | None = None) -> list[Any]:
        action_dict: dict[str, Any] = {"action": "focus_window"}
        if title is not None:
            action_dict["title"] = title
        if hwnd is not None:
            action_dict["hwnd"] = hwnd
        return await _run_action(hub, action_dict)


# ---------------------------------------------------------------------------
# ASGI plumbing: self-starting session manager + bearer auth
# ---------------------------------------------------------------------------


class _RawStreamableHTTPApp:
    """Dispatches every http scope straight to the FastMCP session manager, bypassing
    Starlette's own route matching entirely.

    FastMCP.streamable_http_app() returns a Starlette app with a Route registered at an exact
    path (`streamable_http_path`). When mounted under a parent app via `Mount("/mcp", ...)`,
    Starlette's Mount only forwards requests whose remaining path is "/..." — a bare "/mcp" (no
    trailing slash) doesn't match and 404s unless the parent's redirect-slash middleware kicks in.
    `StreamableHTTPSessionManager.handle_request()` itself does no path-based routing (it
    dispatches purely by HTTP method + session header), so calling it directly for *any* http
    scope that reaches this object sidesteps the mount-path gotcha completely: "/mcp", "/mcp/",
    "/mcp/anything" all work identically, no redirect required.
    """

    def __init__(self, mcp: FastMCP):
        self._mcp = mcp
        # Calling streamable_http_app() is what lazily constructs mcp._session_manager; we don't
        # use the Starlette app it returns, only that side effect.
        mcp.streamable_http_app()
        self._session_manager = mcp.session_manager
        self._run_cm: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_started(self) -> None:
        if self._run_cm is not None:
            return
        async with self._get_lock():
            if self._run_cm is not None:
                return
            cm = self._session_manager.run()
            await cm.__aenter__()
            self._run_cm = cm

    async def _ensure_stopped(self) -> None:
        async with self._get_lock():
            if self._run_cm is None:
                return
            cm, self._run_cm = self._run_cm, None
            await cm.__aexit__(None, None, None)

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Any = None):
        """Optional: tie the session manager's lifetime to a parent app's lifespan."""
        await self._ensure_started()
        try:
            yield
        finally:
            await self._ensure_stopped()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._run_lifespan_protocol(receive, send)
            return
        if scope["type"] == "http":
            await self._ensure_started()
            await self._session_manager.handle_request(scope, receive, send)
            return
        # No other scope types are meaningful for streamable HTTP (no websockets).
        return

    async def _run_lifespan_protocol(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self._ensure_started()
                except Exception as exc:  # pragma: no cover - defensive
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self._ensure_stopped()
                await send({"type": "lifespan.shutdown.complete"})
                return


def _bearer_token(scope: dict) -> str | None:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            raw = value.decode("latin-1")
            if raw.lower().startswith("bearer "):
                return raw[7:].strip()
            return None
    return None


async def _send_json(send: Any, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _AuthedASGIApp:
    """Bearer-token auth wrapper around the MCP app. On success, stashes the authenticated
    AgentInfo in `_AGENT_CTX` for the duration of the request (tool functions read it back);
    on failure, short-circuits with a plain 401 JSON body — never reaches MCP protocol handling."""

    def __init__(self, inner: Any, hub: Hub):
        self._inner = inner
        self._hub = hub

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return

        token = _bearer_token(scope)
        try:
            agent = self._hub.authenticate(token)
        except HubError as exc:
            payload = {"ok": False, "error": exc.code, "detail": exc.detail, **exc.payload}
            await _send_json(send, exc.status, payload)
            return

        reset_token = _AGENT_CTX.set(agent)
        try:
            await self._inner(scope, receive, send)
        finally:
            _AGENT_CTX.reset(reset_token)


def build_mcp_app(hub: Hub) -> Any:
    """Build the ASGI app for dibs's MCP (streamable-HTTP) endpoint. See module docstring for
    the mounting contract and lifespan handling. Prefer `mount_mcp_app` unless you have a
    specific reason to mount the raw app yourself."""
    mcp = FastMCP(
        name="dibs",
        instructions=(
            "dibs controls a shared Windows desktop that a human may also be using. Call "
            "desk_status() first if unsure whether the desk is free, paused, or awaiting human "
            "consent. The `computer` tool auto-acquires the exclusive input lease as needed "
            "(which may require the human to allow it) and shows the human a cursor halo + "
            "banner while you hold it; screenshots and other read-only actions never need the "
            "lease or consent."
        ),
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
    )
    _register_tools(mcp, hub)

    raw_app = _RawStreamableHTTPApp(mcp)
    authed_app = _AuthedASGIApp(raw_app, hub)
    authed_app.dibs_lifespan = raw_app.lifespan
    return authed_app


def mount_mcp_app(app: Any, hub: Hub, path: str = "/mcp") -> Any:
    """Mount `build_mcp_app(hub)` on `app` (a FastAPI/Starlette app) at `path` such that BOTH
    the bare `path` (no trailing slash, e.g. "/mcp") and `path + "/"` / any deeper sub-path
    reach it directly, with no 307 redirect in between. See the module docstring for why a
    plain `app.mount(path, build_mcp_app(hub))` alone doesn't cover the bare path. Returns the
    mounted app (the same object `build_mcp_app` would have returned), in case the caller wants
    `.dibs_lifespan`.
    """
    mcp_app = build_mcp_app(hub)
    # Exact match for `path` itself -- disjoint from the Mount below (which only matches
    # `path + "/..."`), so registration order between the two doesn't matter.
    app.router.routes.insert(0, Route(path, endpoint=mcp_app, include_in_schema=False))
    app.mount(path, mcp_app)
    return mcp_app
