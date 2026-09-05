"""Tests for dibs.mcp_server. Owner: mcp agent.

Builds a FakeHub (no real desk/registry/lease code — those belong to the desk/hub agents),
mounts it under a FastAPI app at /mcp via `mount_mcp_app` (so both "/mcp" and "/mcp/" are
covered -- see the redirect-vs-401 tests below), serves it with uvicorn in a background thread
on a free port, and drives it with the real `mcp` python client over streamable HTTP.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from PIL import Image as PILImage

from dibs.actions import ActionResult
from dibs.desk import Screen, Shot
from dibs.hub import AgentInfo, HubError
from dibs.mcp_server import mount_mcp_app


@dataclass
class _FakeSettings:
    auto_lease_wait_s: int = 30


class FakeHub:
    """Implements just enough of the Hub surface for mcp_server.py: authenticate, run,
    acquire, release, state, settings.auto_lease_wait_s.

    The `fail_with_*` flags simulate the v0.2 desk-access outcomes described in
    docs/SPEC-v0.2-human.md §6, in the shapes `Hub.run()` raises them in per the mcp round-2
    brief: lease_required with detail "awaiting human consent" (a consent request is pending),
    lease_required with detail "desk taken by human" (human takeover revoked the lease
    mid-session), and a plain 403 "denied" (human refused / timed out / mode is locked / server
    paused). Only one should be set at a time; `run()` checks them in that order.
    """

    def __init__(self) -> None:
        self.settings = _FakeSettings()
        self.fail_with_lease_required = False
        self.fail_with_awaiting_consent = False
        self.fail_with_human_takeover = False
        self.fail_with_denied: dict[str, Any] | None = (
            None  # e.g. {"reason": "human_denied", "retry_after_s": 120}
        )
        self.acquire_result: dict[str, Any] | None = None
        self.acquire_raises: HubError | None = None
        self._agents = {
            "t": AgentInfo(agent_id="agent-1", name="tester", purpose="mcp tests", is_admin=False),
        }
        self.calls: list[dict[str, Any]] = []

    def authenticate(self, token: str | None) -> AgentInfo:
        agent = self._agents.get(token or "")
        if agent is None:
            raise HubError(401, "unauthorized", "invalid or missing bearer token")
        return agent

    async def run(
        self,
        agent: AgentInfo,
        action: dict[str, Any],
        *,
        auto_lease: bool = False,
        wait_s: int | None = None,
    ) -> ActionResult:
        self.calls.append(action)
        if self.fail_with_lease_required:
            raise HubError(
                409,
                "lease_required",
                "the desk is held by someone else",
                payload={
                    "holder": {
                        "agent_id": "agent-2",
                        "name": "other-agent",
                        "expires_at": "2026-01-01T00:00:00Z",
                    },
                    "queue_position": 1,
                },
            )
        if self.fail_with_awaiting_consent:
            raise HubError(
                409,
                "lease_required",
                "awaiting human consent",
                payload={
                    "request_id": "req-42",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "human": {"active": True, "last_input_ago_s": 1.0},
                },
            )
        if self.fail_with_human_takeover:
            raise HubError(
                409,
                "lease_required",
                "desk taken by human",
                payload={"human_active": True, "resume_after_s": 20},
            )
        if self.fail_with_denied is not None:
            payload = {"reason": "human_denied", "retry_after_s": 120}
            payload.update(self.fail_with_denied)
            raise HubError(403, "denied", payload.get("reason", "denied"), payload=payload)

        if action.get("action") in ("screenshot", "zoom"):
            screen = Screen(index=0, x=0, y=0, width=4, height=3, primary=True)
            img = PILImage.new("RGB", (4, 3), (200, 30, 30))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            shot = Shot(png=buf.getvalue(), width=4, height=3, scale=1.0, screen=screen)
            return ActionResult(image=shot)

        if action.get("action") == "list_windows":
            return ActionResult(
                text="hwnd  title\n1     Notepad",
                data={"windows": [{"hwnd": 1, "title": "Notepad"}]},
            )

        return ActionResult(text="OK")

    async def acquire(
        self, agent: AgentInfo, ttl_s: int | None = None, wait_s: int = 0
    ) -> dict[str, Any]:
        if self.acquire_raises is not None:
            raise self.acquire_raises
        if self.acquire_result is not None:
            return self.acquire_result
        return {
            "status": "granted",
            "lease_id": "lease-1",
            "agent_id": agent.agent_id,
            "expires_at": "2026-01-01T00:00:00Z",
        }

    def release(self, agent: AgentInfo, *, force: bool = False) -> None:
        return None

    def state(self) -> dict[str, Any]:
        return {"version": "0.1.0", "paused": False, "agents": []}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def running_server():
    fake = FakeHub()
    app = FastAPI()
    mount_mcp_app(app, fake, path="/mcp")

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn server did not start in time"

    try:
        yield f"http://127.0.0.1:{port}", fake
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def test_list_tools_screenshot_and_click(running_server):
    base_url, _fake = running_server
    url = f"{base_url}/mcp"

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "computer" in names
            assert {
                "desk_status",
                "acquire_desk",
                "release_desk",
                "list_windows",
                "focus_window",
            } <= names

            computer_tool = next(t for t in tools.tools if t.name == "computer")
            assert (
                "screenshot pixel space" in computer_tool.description.lower()
                or "screenshot-space" in computer_tool.description.lower()
            )
            for action_name in (
                "screenshot",
                "left_click",
                "scroll",
                "type",
                "key",
                "wait",
                "launch",
            ):
                assert action_name in computer_tool.description

            shot_result = await session.call_tool("computer", {"action": "screenshot"})
            assert not shot_result.isError
            image_blocks = [b for b in shot_result.content if b.type == "image"]
            assert len(image_blocks) == 1
            assert image_blocks[0].mimeType == "image/png"
            assert image_blocks[0].data  # base64 payload present

            click_result = await session.call_tool(
                "computer", {"action": "left_click", "coordinate": [1, 2]}
            )
            assert not click_result.isError
            text = "".join(b.text for b in click_result.content if b.type == "text")
            assert "OK" in text


async def test_lease_required_error_names_holder(running_server):
    base_url, fake = running_server
    url = f"{base_url}/mcp"
    fake.fail_with_lease_required = True

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "computer", {"action": "left_click", "coordinate": [1, 2]}
            )
            assert result.isError
            text = "".join(b.text for b in result.content if b.type == "text")
            assert "held by" in text
            assert "other-agent" in text
            assert "queued at position 1" in text


async def test_desk_status_and_windows(running_server):
    base_url, _fake = running_server
    url = f"{base_url}/mcp"

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()

            status = await session.call_tool("desk_status", {})
            assert not status.isError
            status_text = "".join(b.text for b in status.content if b.type == "text")
            assert '"version"' in status_text

            windows = await session.call_tool("list_windows", {})
            assert not windows.isError
            windows_text = "".join(b.text for b in windows.content if b.type == "text")
            assert "Notepad" in windows_text


def test_missing_auth_header_returns_401(running_server):
    base_url, _fake = running_server
    response = httpx.post(
        f"{base_url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "unauthorized"


def test_bad_token_returns_401(running_server):
    base_url, _fake = running_server
    response = httpx.post(
        f"{base_url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401


def test_bad_token_returns_401_with_trailing_slash_too(running_server):
    """Both mount points (the exact Route and the Mount) dispatch to the same authed app, so a
    bad token is a 401 on "/mcp" (tested above, no trailing slash -- the case that used to
    307-redirect) and on "/mcp/" (below) alike; neither ever produces a redirect."""
    base_url, _fake = running_server
    response = httpx.post(
        f"{base_url}/mcp/",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401


async def test_consent_pending_error_mentions_consent(running_server):
    base_url, fake = running_server
    url = f"{base_url}/mcp"
    fake.fail_with_awaiting_consent = True

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "computer", {"action": "left_click", "coordinate": [1, 2]}
            )
            assert result.isError
            text = "".join(b.text for b in result.content if b.type == "text")
            assert "consent" in text
            assert "acquire_desk" in text


async def test_denied_error_mentions_denied(running_server):
    base_url, fake = running_server
    url = f"{base_url}/mcp"
    fake.fail_with_denied = {"reason": "human_denied", "retry_after_s": 120}

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "computer", {"action": "left_click", "coordinate": [1, 2]}
            )
            assert result.isError
            text = "".join(b.text for b in result.content if b.type == "text")
            assert "denied" in text
            assert "120" in text


async def test_human_takeover_error_mentions_taken_by_the_human(running_server):
    base_url, fake = running_server
    url = f"{base_url}/mcp"
    fake.fail_with_human_takeover = True

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "computer", {"action": "left_click", "coordinate": [1, 2]}
            )
            assert result.isError
            text = "".join(b.text for b in result.content if b.type == "text")
            assert "taken by the human" in text


async def test_acquire_desk_returns_awaiting_consent_json_verbatim(running_server):
    base_url, fake = running_server
    url = f"{base_url}/mcp"
    fake.acquire_result = {
        "status": "awaiting_consent",
        "request_id": "req-9",
        "expires_at": "2099-01-01T00:00:00Z",
        "human": {"active": True},
    }

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("acquire_desk", {"wait_s": 0})
            assert not result.isError
            text = "".join(b.text for b in result.content if b.type == "text")
            data = json.loads(text)
            assert data == fake.acquire_result


async def test_acquire_desk_denied_raises_tool_error(running_server):
    base_url, fake = running_server
    url = f"{base_url}/mcp"
    fake.acquire_raises = HubError(
        403, "denied", "human_denied", payload={"reason": "human_denied", "retry_after_s": 120}
    )

    async with streamablehttp_client(url, headers={"Authorization": "Bearer t"}) as (
        read,
        write,
        _get_id,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("acquire_desk", {"wait_s": 0})
            assert result.isError
            text = "".join(b.text for b in result.content if b.type == "text")
            assert "denied" in text
