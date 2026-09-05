"""Tests for clients/python/dibs_client.py. Owner: mcp agent.

Exercises DibsClient against a tiny FastAPI app that serves canned responses matching the
REST shapes in docs/SPEC.md and docs/SPEC-v0.2-human.md. Not the real dibs/server.py (owned
by the hub agent) — just enough surface to prove the client builds correct requests and maps
responses/errors correctly.

DibsClient is deliberately synchronous (`httpx.Client`), and httpx 0.28's `ASGITransport`
only implements `handle_async_request` -- it was never usable with a sync `httpx.Client` (see
the note on `DibsClient.__init__`). So instead of a transport override, the `client` fixture
below runs the fake app for real with uvicorn in a background thread on a free port (the same
approach `tests/test_mcp.py` already uses) and points a real sync `DibsClient` at it over a
loopback socket.
"""
from __future__ import annotations

import io
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "python"))

from dibs_client import DibsClient, DibsError, computer_tool_handler  # noqa: E402

ADMIN_TOKEN = "admin-secret"
AGENT_TOKEN = "agent-token-abc"


def _make_png(width: int, height: int) -> bytes:
    img = PILImage.new("RGB", (width, height), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_fake_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _flat_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        # FastAPI's default handler wraps `detail` under {"detail": ...}; the real dibs REST
        # API returns the error body flat (docs/SPEC.md: {ok:false, error, detail}), so unwrap it
        # here to match.
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    state: dict[str, Any] = {
        "lease_holder": None,  # agent_id or None
        "fail_next_action": None,  # set to an error dict to force POST /v1/actions to fail once
        "force_lease_result": None,  # set to (status_code, body) to force the next POST /v1/lease reply
    }

    def _authed(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise _err(401, "unauthorized", "missing bearer token")
        token = auth[7:]
        if token not in (ADMIN_TOKEN, AGENT_TOKEN):
            raise _err(401, "unauthorized", "unknown token")
        return token

    def _err(status: int, error: str, detail: str, **extra: Any) -> Exception:
        return HTTPException(status_code=status, detail={"ok": False, "error": error, "detail": detail, **extra})

    @app.post("/v1/agents")
    async def register(request: Request) -> dict[str, Any]:
        body = await request.json()
        state["last_register_auth_header"] = request.headers.get("authorization")
        return {
            "agent_id": f"{body['name']}-abcd",
            "name": body["name"],
            "purpose": body["purpose"],
            "token": AGENT_TOKEN,
            "created_at": "2026-01-01T00:00:00Z",
        }

    @app.get("/v1/state")
    def get_state(request: Request) -> dict[str, Any]:
        _authed(request)
        return {"version": "0.1.0", "paused": False, "lease": {"holder": None, "queue": []}, "agents": []}

    @app.get("/v1/display")
    def get_display(request: Request) -> dict[str, Any]:
        _authed(request)
        return {
            "screens": [{"index": 0, "x": 0, "y": 0, "width": 2560, "height": 1440, "primary": True}],
            "default_screen": 0,
            "screenshot": {"width": 1280, "height": 720, "scale": 0.5, "max_long_edge": 1568, "max_pixels": 1150000},
        }

    @app.post("/v1/lease")
    async def acquire_lease(request: Request) -> dict[str, Any]:
        token = _authed(request)
        await request.json()
        if state["force_lease_result"] is not None:
            status_code, body = state["force_lease_result"]
            state["force_lease_result"] = None
            if status_code >= 400:
                raise HTTPException(status_code=status_code, detail=body)
            return JSONResponse(status_code=status_code, content=body)
        if state["lease_holder"] not in (None, token):
            return JSONResponse(
                status_code=202,
                content={"status": "queued", "position": 1, "holder": {"agent_id": "someone-else", "name": "someone-else", "expires_at": "later"}},
            )
        state["lease_holder"] = token
        return {"status": "granted", "lease_id": "lease-1", "agent_id": token, "expires_at": "later"}

    @app.post("/v1/lease/renew")
    async def renew_lease(request: Request) -> dict[str, Any]:
        token = _authed(request)
        if state["lease_holder"] != token:
            raise _err(409, "not_holder", "you do not hold the lease")
        return {"status": "granted", "lease_id": "lease-1", "agent_id": token, "expires_at": "later"}

    @app.delete("/v1/lease")
    def release_lease(request: Request, force: bool = Query(False)) -> Response:
        token = _authed(request)
        if force or state["lease_holder"] == token:
            state["lease_holder"] = None
        return Response(status_code=204)

    @app.post("/v1/actions")
    async def post_action(request: Request) -> dict[str, Any]:
        _authed(request)
        body = await request.json()
        if state["fail_next_action"] is not None:
            err = state["fail_next_action"]
            state["fail_next_action"] = None
            raise _err(err["status"], err["error"], err["detail"], **err.get("extra", {}))

        action_name = body.get("action")
        if action_name in ("screenshot", "zoom"):
            png = _make_png(4, 3)
            import base64

            return {"ok": True, "image": {"png_base64": base64.b64encode(png).decode("ascii"), "width": 4, "height": 3, "scale": 1.0, "screen": 0}}
        if action_name == "cursor_position":
            return {"ok": True, "result": "X=10,Y=20", "data": {"x": 10, "y": 20, "screen": 0}}
        return {"ok": True, "result": "OK"}

    @app.post("/v1/actions/batch")
    async def post_batch(request: Request) -> dict[str, Any]:
        _authed(request)
        body = await request.json()
        results = [{"ok": True, "result": "OK"} for _ in body.get("actions", [])]
        return {"results": results}

    @app.get("/v1/screenshot.png")
    def get_screenshot_png(request: Request, screen: int = Query(0)) -> Response:
        _authed(request)
        return Response(content=_make_png(4, 3), media_type="image/png")

    app.state.internal = state
    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def client() -> DibsClient:
    app = build_fake_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn server did not start in time"

    base_url = f"http://127.0.0.1:{port}"
    c = DibsClient(base_url=base_url)
    c.test_app = app  # test-only handle so other tests can point a second client at the same app
    c.test_base_url = base_url  # test-only handle for standing up a second DibsClient
    try:
        yield c
    finally:
        c.close()
        server.should_exit = True
        thread.join(timeout=5)


def test_register_sets_token(client: DibsClient):
    result = client.register("my-agent", "testing", admin_token=ADMIN_TOKEN)
    assert result["token"] == AGENT_TOKEN
    assert client.token == AGENT_TOKEN
    assert client.test_app.state.internal["last_register_auth_header"] == f"Bearer {ADMIN_TOKEN}"


def test_state_and_display_require_auth(client: DibsClient):
    with pytest.raises(DibsError) as exc_info:
        client.state()
    assert exc_info.value.status == 401
    assert exc_info.value.code == "unauthorized"

    client.token = AGENT_TOKEN
    state = client.state()
    assert state["version"] == "0.1.0"
    display = client.display()
    assert display["screens"][0]["width"] == 2560


def test_lease_acquire_renew_release(client: DibsClient):
    client.token = AGENT_TOKEN
    granted = client.acquire(ttl_s=30)
    assert granted["status"] == "granted"
    renewed = client.renew(ttl_s=60)
    assert renewed["status"] == "granted"
    client.release()  # should not raise


def test_lease_queued_when_held(client: DibsClient):
    client.token = AGENT_TOKEN
    client.acquire(ttl_s=30)

    other = DibsClient(base_url=client.test_base_url, token=ADMIN_TOKEN)
    queued = other.acquire(wait_s=0)
    assert queued["status"] == "queued"
    assert queued["position"] == 1
    other.close()


def test_lease_awaiting_consent_returns_dict_not_raise(client: DibsClient):
    """v0.2: acquire() never raises for a 202, whether it's the v0.1 'queued' shape or the
    v0.2 'awaiting_consent' shape (SPEC-v0.2-human.md §2.1) -- both just come back as dicts."""
    client.token = AGENT_TOKEN
    client.test_app.state.internal["force_lease_result"] = (
        202,
        {
            "status": "awaiting_consent",
            "request_id": "req-1",
            "expires_at": "2099-01-01T00:00:00Z",
            "human": {"active": True, "last_input_ago_s": 2.0},
        },
    )

    result = client.acquire(wait_s=0)
    assert result["status"] == "awaiting_consent"
    assert result["request_id"] == "req-1"


def test_lease_denied_raises_with_reason_and_retry_after(client: DibsClient):
    """v0.2: a 403 {status:'denied', reason, retry_after_s} raises DibsError with .reason and
    .retry_after_s populated from the payload (SPEC-v0.2-human.md §2.1 / §6)."""
    client.token = AGENT_TOKEN
    client.test_app.state.internal["force_lease_result"] = (
        403,
        {"ok": False, "error": "denied", "detail": "human_denied", "reason": "human_denied", "retry_after_s": 120},
    )

    with pytest.raises(DibsError) as exc_info:
        client.acquire(wait_s=0)
    assert exc_info.value.status == 403
    assert exc_info.value.code == "denied"
    assert exc_info.value.reason == "human_denied"
    assert exc_info.value.retry_after_s == 120
    assert "reason=human_denied" in str(exc_info.value)
    assert "retry_after_s=120" in str(exc_info.value)


def test_action_and_click_convenience(client: DibsClient):
    client.token = AGENT_TOKEN
    result = client.click(10, 20)
    assert result["result"] == "OK"

    result2 = client.type("hello")
    assert result2["result"] == "OK"

    result3 = client.key("ctrl+s")
    assert result3["result"] == "OK"

    result4 = client.scroll("down", 3, x=5, y=6)
    assert result4["result"] == "OK"


def test_action_error_maps_to_dibs_error(client: DibsClient):
    client.token = AGENT_TOKEN
    # Force the next action to fail with a lease_required-shaped error.
    client.test_app.state.internal["fail_next_action"] = {
        "status": 409,
        "error": "lease_required",
        "detail": "the desk is held by someone else",
        "extra": {"holder": {"agent_id": "other", "name": "other-agent"}, "queue_position": 2},
    }

    with pytest.raises(DibsError) as exc_info:
        client.action(action="left_click", coordinate=[1, 2])
    assert exc_info.value.status == 409
    assert exc_info.value.code == "lease_required"
    assert exc_info.value.payload["queue_position"] == 2


def test_screenshot_returns_png_dimensions_and_scale(client: DibsClient):
    client.token = AGENT_TOKEN
    png, width, height, scale = client.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (width, height) == (4, 3)
    assert scale == pytest.approx(4 / 2560)


def test_batch(client: DibsClient):
    client.token = AGENT_TOKEN
    results = client.batch([{"action": "left_click", "coordinate": [1, 1]}, {"action": "wait", "duration": 0.1}])
    assert len(results) == 2
    assert all(r["ok"] for r in results)


def test_computer_tool_handler_text_and_image(client: DibsClient):
    client.token = AGENT_TOKEN
    handle = computer_tool_handler(client)

    class ToolUseBlock:
        def __init__(self, name: str, input: dict[str, Any]):
            self.name = name
            self.input = input
            self.id = "toolu_1"

    text_content = handle(ToolUseBlock("left_click", {"coordinate": [1, 2]}))
    assert text_content == [{"type": "text", "text": "OK"}]

    image_content = handle(ToolUseBlock("screenshot", {}))
    assert len(image_content) == 1
    assert image_content[0]["type"] == "image"
    assert image_content[0]["source"]["media_type"] == "image/png"
    assert image_content[0]["source"]["data"]  # base64 payload present


def test_computer_tool_handler_accepts_dict_block(client: DibsClient):
    client.token = AGENT_TOKEN
    handle = computer_tool_handler(client)
    result = handle({"name": "wait", "input": {"duration": 0.1}})
    assert result == [{"type": "text", "text": "OK"}]
