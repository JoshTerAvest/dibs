"""FastAPI app: REST routes, auth dependency, MCP + dashboard mounts. Owner: hub agent."""

from __future__ import annotations

import asyncio

from . import actions

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .hub import AgentInfo, Hub, HubError

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


def _is_loopback(request: Request) -> bool:
    client = request.client
    return client is not None and client.host in LOOPBACK_HOSTS


def _hub_error_response(e: HubError) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": e.code, "detail": e.detail}
    body.update(e.payload)
    return JSONResponse(status_code=e.status, content=body)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class RegisterBody(BaseModel):
    name: str
    purpose: str = ""


class LeaseAcquireBody(BaseModel):
    ttl_s: int | None = None
    wait_s: int = Field(0, ge=0, le=120)


class LeaseRenewBody(BaseModel):
    ttl_s: int | None = None


class ActionBody(BaseModel):
    model_config = {"extra": "allow"}

    action: str
    auto_lease: bool = False
    wait_s: int | None = None


class BatchBody(BaseModel):
    actions: list[dict[str, Any]]
    auto_lease: bool = False
    wait_s: int | None = None


class PauseBody(BaseModel):
    reason: str = "manual"


class ModeBody(BaseModel):
    mode: str


class ConsentDecisionBody(BaseModel):
    decision: str


def _strip_dispatch_fields(body: ActionBody) -> dict[str, Any]:
    """The action dict actually dispatched: the raw body minus auto_lease/wait_s."""
    data = body.model_dump()
    data.pop("auto_lease", None)
    data.pop("wait_s", None)
    return data


def create_app(settings: Settings) -> FastAPI:
    # Constructed eagerly (not inside lifespan) so it exists in time to hand to
    # mcp_server.build_mcp_app(hub) below, before the app object is returned.
    # Hub.__init__ only does cheap sync setup (registry/lease/audit-path wiring);
    # the real startup work (opening the db, starting background tasks, DPI
    # awareness, the hotkey listener) happens in hub.start() from the lifespan.
    hub = Hub(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(title="dibs", version="0.1.0", lifespan=lifespan)
    app.state.hub = hub

    def get_hub(request: Request) -> Hub:
        return request.app.state.hub

    # ---- auth dependencies ----

    def _bearer_token(authorization: str | None) -> str | None:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return None

    # Synthetic identities for unauthenticated-but-loopback-exempt callers (the local
    # dashboard, hitting the server without a token from 127.0.0.1/::1). They are never
    # persisted in the registry; registry.touch()/get() simply no-op on unknown ids.
    LOCAL_DASHBOARD = AgentInfo(
        agent_id="dashboard", name="dashboard", purpose="local dashboard (loopback)", is_admin=False
    )
    LOCAL_DASHBOARD_ADMIN = AgentInfo(
        agent_id="dashboard", name="dashboard", purpose="local dashboard (loopback)", is_admin=True
    )

    # The dashboard page is the human's door. Loading `/` from loopback sets this per-process
    # cookie; the unauthenticated loopback exemption below requires it, so a local script can't
    # use /v1/screenshot.png (or the admin routes) as a side door around consent.
    DASH_COOKIE = "dibs_dash"
    dash_secret = secrets.token_urlsafe(24)

    def _dash_ok(request: Request) -> bool:
        return (
            settings.dashboard_open_on_loopback
            and _is_loopback(request)
            and request.cookies.get(DASH_COOKIE) == dash_secret
        )

    def require_auth(
        request: Request, authorization: str | None = Header(default=None)
    ) -> AgentInfo:
        hub: Hub = get_hub(request)
        return hub.authenticate(_bearer_token(authorization))

    def register_auth(
        request: Request, authorization: str | None = Header(default=None)
    ) -> AgentInfo | None:
        """POST /v1/agents: open (no auth) from loopback when allow_local_open_registration,
        else the admin token is required."""
        hub: Hub = get_hub(request)
        if settings.allow_local_open_registration and _is_loopback(request):
            return None
        agent = hub.authenticate(_bearer_token(authorization))
        if not agent.is_admin:
            raise HubError(403, "admin_required", detail="admin token required")
        return agent

    def dashboard_auth(
        request: Request, authorization: str | None = Header(default=None)
    ) -> AgentInfo:
        """GET /v1/state, /v1/display, /v1/audit, /v1/screenshot.png, /v1/shots/* :
        authenticated normally, OR unauthenticated from loopback when dashboard_open_on_loopback."""
        hub: Hub = get_hub(request)
        token = _bearer_token(authorization)
        if token is None and _dash_ok(request):
            return LOCAL_DASHBOARD
        return hub.authenticate(token)

    def dashboard_admin(
        request: Request, authorization: str | None = Header(default=None)
    ) -> AgentInfo:
        """Admin pause/resume/force-release/revoke: admin token normally, OR unauthenticated
        from loopback when dashboard_open_on_loopback (so the local dashboard buttons work)."""
        hub: Hub = get_hub(request)
        token = _bearer_token(authorization)
        if token is None and _dash_ok(request):
            return LOCAL_DASHBOARD_ADMIN
        agent = hub.authenticate(token)
        if not agent.is_admin:
            raise HubError(403, "admin_required", detail="admin token required")
        return agent

    def lease_release_auth(
        request: Request,
        force: bool = Query(False),
        authorization: str | None = Header(default=None),
    ) -> AgentInfo:
        """DELETE /v1/lease: any authenticated agent may release (a no-op if they don't hold
        it). ?force=true additionally requires admin -- with the same loopback exemption as
        the other admin routes."""
        if force:
            return dashboard_admin(request, authorization)
        return require_auth(request, authorization)

    # ---- error handling ----

    @app.exception_handler(HubError)
    async def hub_error_handler(_request: Request, exc: HubError) -> JSONResponse:
        return _hub_error_response(exc)

    # ---- agents ----

    @app.post("/v1/agents")
    async def post_agents(
        request: Request, body: RegisterBody, _agent: AgentInfo | None = Depends(register_auth)
    ):
        hub: Hub = get_hub(request)
        return hub.register(body.name, body.purpose)

    @app.delete("/v1/agents/{agent_id}")
    async def delete_agent(
        agent_id: str, request: Request, _admin: AgentInfo = Depends(dashboard_admin)
    ):
        hub: Hub = get_hub(request)
        hub.revoke(agent_id)
        return Response(status_code=204)

    # ---- state / display ----

    @app.get("/v1/state")
    async def get_state(request: Request, _agent: AgentInfo = Depends(dashboard_auth)):
        hub: Hub = get_hub(request)
        return hub.state()

    @app.get("/v1/display")
    async def get_display(request: Request, _agent: AgentInfo = Depends(dashboard_auth)):
        hub: Hub = get_hub(request)
        return hub.display()

    # ---- lease ----

    @app.post("/v1/lease")
    async def post_lease(
        request: Request, body: LeaseAcquireBody, agent: AgentInfo = Depends(require_auth)
    ):
        hub: Hub = get_hub(request)
        result = await hub.acquire(agent, ttl_s=body.ttl_s, wait_s=body.wait_s)
        status_code = 200 if result["status"] == "granted" else 202
        return JSONResponse(status_code=status_code, content=result)

    @app.post("/v1/lease/renew")
    async def post_lease_renew(
        request: Request, body: LeaseRenewBody, agent: AgentInfo = Depends(require_auth)
    ):
        hub: Hub = get_hub(request)
        return hub.renew(agent, ttl_s=body.ttl_s)

    @app.delete("/v1/lease")
    async def delete_lease(
        request: Request, force: bool = Query(False), agent: AgentInfo = Depends(lease_release_auth)
    ):
        hub: Hub = get_hub(request)
        hub.release(agent, force=force)
        return Response(status_code=204)

    # ---- actions ----

    @app.post("/v1/actions")
    async def post_actions(
        request: Request, body: ActionBody, agent: AgentInfo = Depends(require_auth)
    ):
        hub: Hub = get_hub(request)
        action = _strip_dispatch_fields(body)
        result = await hub.run(agent, action, auto_lease=body.auto_lease, wait_s=body.wait_s)
        return result.to_dict()

    @app.post("/v1/actions/batch")
    async def post_actions_batch(
        request: Request, body: BatchBody, agent: AgentInfo = Depends(require_auth)
    ):
        hub: Hub = get_hub(request)
        results = await hub.run_batch(
            agent, body.actions, auto_lease=body.auto_lease, wait_s=body.wait_s
        )
        return {"results": results}

    # ---- screenshot / audit ----

    @app.get("/v1/screenshot.png")
    async def get_screenshot(
        request: Request,
        screen: int | None = Query(None),
        scale: float | None = Query(None),
        agent: AgentInfo = Depends(dashboard_auth),
    ):
        hub: Hub = get_hub(request)
        action: dict[str, Any] = {"action": "screenshot"}
        if screen is not None:
            action["screen"] = screen
        if agent.agent_id == LOCAL_DASHBOARD.agent_id:
            # The dashboard's live view polls every second; that's not an agent action, so it
            # bypasses the hub (no lease/pause gating, no audit row, no stats) - read-only capture.
            result = await asyncio.to_thread(
                actions.run_action,
                action,
                screen_index=settings.screen_index,
                max_long_edge=settings.max_long_edge,
                max_pixels=settings.max_pixels,
            )
        else:
            result = await hub.run(agent, action)
        png = result.image.png if result.image is not None else b""
        if scale is not None and scale > 0 and scale < 1.0 and result.image is not None:
            try:
                import io as _io

                from PIL import Image as _Image

                img = _Image.open(_io.BytesIO(png))
                new_w = max(1, round(img.width * scale))
                new_h = max(1, round(img.height * scale))
                img = img.resize((new_w, new_h), _Image.LANCZOS)
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                png = buf.getvalue()
            except Exception:
                logger.exception("failed to apply extra scale to screenshot")
        return Response(content=png, media_type="image/png")

    @app.get("/v1/audit")
    async def get_audit(
        request: Request,
        limit: int = Query(50, ge=1, le=1000),
        agent_id: str | None = Query(None),
        _agent: AgentInfo = Depends(dashboard_auth),
    ):
        hub: Hub = get_hub(request)
        return hub.audit(limit=limit, agent_id=agent_id)

    @app.get("/v1/shots/{shot_id}.png")
    async def get_shot(shot_id: int, request: Request, _agent: AgentInfo = Depends(dashboard_auth)):
        hub: Hub = get_hub(request)
        path = hub.screenshot_path(shot_id)
        if path is None or not Path(path).is_file():
            raise HubError(404, "not_found", detail=f"no screenshot for id {shot_id}")
        return Response(content=Path(path).read_bytes(), media_type="image/png")

    # ---- admin ----

    @app.post("/v1/admin/shutdown")
    async def post_admin_shutdown(request: Request, _agent: AgentInfo = Depends(dashboard_admin)):
        """Graceful stop: sets uvicorn's should_exit via the hook `dibs serve` installs."""
        hub: Hub = get_hub(request)
        fn = hub.request_shutdown
        if fn is None:
            raise HubError(409, "not_serving", detail="no shutdown hook (start with `dibs serve`)")
        asyncio.get_running_loop().call_later(0.3, fn)  # let this response go out first
        return {"ok": True, "stopping": True}

    @app.post("/v1/admin/pause")
    async def post_admin_pause(
        request: Request, body: PauseBody, _agent: AgentInfo = Depends(dashboard_admin)
    ):
        hub: Hub = get_hub(request)
        hub.pause(body.reason, manual=True)
        return {"ok": True}

    @app.post("/v1/admin/resume")
    async def post_admin_resume(request: Request, _agent: AgentInfo = Depends(dashboard_admin)):
        hub: Hub = get_hub(request)
        hub.resume()
        return {"ok": True}

    @app.post("/v1/admin/mode")
    async def post_admin_mode(
        request: Request, body: ModeBody, _agent: AgentInfo = Depends(dashboard_admin)
    ):
        hub: Hub = get_hub(request)
        hub.set_mode(body.mode)
        return {"ok": True, "mode": hub.settings.mode}

    @app.post("/v1/admin/consent/{request_id}")
    async def post_admin_consent(
        request_id: str,
        request: Request,
        body: ConsentDecisionBody,
        _agent: AgentInfo = Depends(dashboard_admin),
    ):
        hub: Hub = get_hub(request)
        hub.decide_consent(request_id, body.decision)
        return {"ok": True, "request_id": request_id, "decision": body.decision}

    @app.post("/v1/admin/release")
    async def post_admin_release(request: Request, _agent: AgentInfo = Depends(dashboard_admin)):
        hub: Hub = get_hub(request)
        hub.human_release()
        return {"ok": True}

    # ---- MCP mount (optional; the mcp agent is writing this in parallel) ----

    try:
        from . import mcp_server  # type: ignore[attr-defined]
    except ImportError:
        logger.info("dibs.mcp_server not available yet; skipping /mcp mount")
    else:
        try:
            mcp_server.mount_mcp_app(app, hub, path="/mcp")
        except Exception:
            logger.exception("failed to build MCP app; skipping /mcp mount")

    # ---- dashboard / static (mounted LAST so it doesn't shadow /v1 or /mcp) ----

    pkg_dir = Path(__file__).resolve().parent
    dashboard_dir = pkg_dir / "dashboard"
    index_file = dashboard_dir / "index.html"

    def _with_dash_cookie(request: Request, resp: Response) -> Response:
        if settings.dashboard_open_on_loopback and _is_loopback(request):
            resp.set_cookie(DASH_COOKIE, dash_secret, httponly=True, samesite="strict")
        return resp

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def dashboard_index(request: Request) -> Response:
        # An explicit route, not middleware: Starlette's BaseHTTPMiddleware breaks the MCP
        # streamable-HTTP transport mounted at /mcp (ClosedResourceError on every POST).
        if index_file.is_file():
            return _with_dash_cookie(request, FileResponse(str(index_file), media_type="text/html"))
        return _with_dash_cookie(
            request,
            HTMLResponse(
                "<html><head><title>dibs</title></head><body><h1>dibs is running</h1>"
                '<p><a href="/v1/state">/v1/state</a> &middot; <a href="/docs">/docs</a></p>'
                "</body></html>"
            ),
        )

    if dashboard_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(dashboard_dir), html=False), name="dashboard")

    return app
