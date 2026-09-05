"""Tiny synchronous Python client for a dibs hub. Owner: mcp agent.

No dependency on the `dibs` package itself — this talks to the REST API
(`docs/SPEC.md`) over plain HTTP via `httpx`, so it works from any machine/venv that can
reach the server.

    from dibs_client import DibsClient

    client = DibsClient("http://127.0.0.1:7474")
    client.register("my-agent", "testing things")
    client.acquire(ttl_s=60)
    client.click(400, 300)
    png, w, h, scale = client.screenshot()
    client.release()
"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Iterable

import httpx


class DibsError(Exception):
    """Raised for any `{ok: false}` REST response or non-2xx status.

    `status` is the HTTP status code, `code` the machine-readable error string (e.g.
    "lease_required", "unauthorized", "paused", and (v0.2) "denied"), `detail` a
    human-readable message, and `payload` any extra JSON fields the server sent alongside the
    error (e.g. `holder`, `queue_position`, and (v0.2) `reason`, `retry_after_s`, `request_id`).

    `reason` and `retry_after_s` are convenience shortcuts onto `payload` -- both are `None`
    when the server didn't send them (only the v0.2 `denied` shape, `{status:"denied", reason,
    retry_after_s}`, sets both; `reason` is one of `human_denied`, `timeout`, `locked`,
    `paused`).
    """

    def __init__(self, status: int, code: str, detail: str = "", payload: dict[str, Any] | None = None):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail or code
        self.payload = payload or {}
        self.reason: str | None = self.payload.get("reason")
        self.retry_after_s: float | None = self.payload.get("retry_after_s")

    def __str__(self) -> str:
        extra = ""
        if self.reason is not None and self.reason != self.code:
            extra += f" reason={self.reason}"
        if self.retry_after_s is not None:
            extra += f" retry_after_s={self.retry_after_s}"
        return f"DibsError({self.status} {self.code}): {self.detail}{extra}"


class DibsClient:
    """Synchronous REST client for a dibs hub."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7474",
        token: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        """`transport` overrides the underlying `httpx.Client`'s transport, mainly for tests
        against a fake app -- it must be a *synchronous* `httpx.BaseTransport` (e.g.
        `httpx.MockTransport`), since this client uses `httpx.Client`, not `AsyncClient`.
        `httpx.ASGITransport` does NOT work here even though it takes an ASGI `app=...`: it
        only implements `handle_async_request`, so a sync `httpx.Client` calling it raises
        `AttributeError: 'ASGITransport' object has no attribute 'handle_request'`. To test
        against an ASGI app (FastAPI/Starlette) synchronously, run it for real (e.g. uvicorn in
        a background thread on a free port, as `tests/test_client.py` and `tests/test_mcp.py`
        do) and point `base_url` at it instead of passing `transport=`."""
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DibsClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self._client.request(
            method, path, json=json_body, params=params, headers=self._headers(headers)
        )

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        code = payload.get("error", f"http_{response.status_code}")
        detail = payload.get("detail", response.text)
        extra = {k: v for k, v in payload.items() if k not in ("ok", "error", "detail")}
        raise DibsError(response.status_code, code, detail, extra)

    def _json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = self._request(method, path, json_body=json_body, params=params, headers=headers)
        self._raise_for_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- registration ---------------------------------------------------------

    def register(self, name: str, purpose: str, admin_token: str | None = None) -> dict[str, Any]:
        """POST /v1/agents. On success, sets self.token to the new agent's token and returns
        the full response ({agent_id, name, purpose, token, created_at})."""
        headers = {"Authorization": f"Bearer {admin_token}"} if admin_token else None
        result = self._json("POST", "/v1/agents", json_body={"name": name, "purpose": purpose}, headers=headers)
        if isinstance(result, dict) and result.get("token"):
            self.token = result["token"]
        return result

    # -- introspection ---------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return self._json("GET", "/v1/state")

    def display(self) -> dict[str, Any]:
        return self._json("GET", "/v1/display")

    def audit(self, limit: int = 50, agent_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        return self._json("GET", "/v1/audit", params=params)

    # -- lease ---------------------------------------------------------

    def acquire(self, ttl_s: int | None = None, wait_s: int = 0) -> dict[str, Any]:
        """POST /v1/lease. Returns the response dict for any 2xx: `{status:"granted", ...}`
        (200), `{status:"queued", position, holder}` (202), or -- v0.2 -- `{status:
        "awaiting_consent", request_id, expires_at, human}` (202, a human decision is pending;
        call again with the same wait_s to keep polling, or a larger wait_s to long-poll for
        the decision). Never raises for those. Raises `DibsError(403, "denied", ...)` -- v0.2
        -- when the human refused, the request timed out, or the desk is locked/paused for
        agents; check `.reason` (`human_denied`|`timeout`|`locked`|`paused`) and
        `.retry_after_s` on the exception."""
        body: dict[str, Any] = {"wait_s": wait_s}
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        return self._json("POST", "/v1/lease", json_body=body)

    def renew(self, ttl_s: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"ttl_s": ttl_s} if ttl_s is not None else {}
        return self._json("POST", "/v1/lease/renew", json_body=body)

    def release(self, *, force: bool = False) -> None:
        params = {"force": "true"} if force else None
        self._json("DELETE", "/v1/lease", params=params)

    # -- actions ---------------------------------------------------------

    def action(self, **kwargs: Any) -> dict[str, Any]:
        """POST /v1/actions with the given action fields, e.g.
        `client.action(action="left_click", coordinate=[1, 2])`. Pass `auto_lease=True` to have
        the server acquire the desk lease for you if you don't already hold it."""
        return self._json("POST", "/v1/actions", json_body=kwargs)

    def batch(self, actions: Iterable[dict[str, Any]], auto_lease: bool = False) -> list[dict[str, Any]]:
        body = {"actions": list(actions), "auto_lease": auto_lease}
        result = self._json("POST", "/v1/actions/batch", json_body=body)
        if isinstance(result, dict):
            return result.get("results", [])
        return result

    def screenshot(self, screen: int | None = None) -> tuple[bytes, int, int, float]:
        """GET /v1/screenshot.png. Returns (png_bytes, width, height, scale). width/height come
        from decoding the PNG; scale is derived by comparing that width against the target
        screen's native width from /v1/display (1.0 if it can't be resolved)."""
        params: dict[str, Any] = {}
        if screen is not None:
            params["screen"] = screen
        response = self._request("GET", "/v1/screenshot.png", params=params)
        self._raise_for_error(response)
        png = response.content

        from PIL import Image as PILImage

        with PILImage.open(BytesIO(png)) as img:
            width, height = img.size

        scale = 1.0
        try:
            display = self.display()
            screens = display.get("screens", [])
            target_index = screen if screen is not None else display.get("default_screen", 0)
            native = next((s for s in screens if s.get("index") == target_index), None)
            if native and native.get("width"):
                scale = width / native["width"]
        except Exception:
            pass

        return png, width, height, scale

    # -- convenience wrappers ---------------------------------------------------------

    def click(self, x: int, y: int, button: str = "left", modifiers: list[str] | str | None = None) -> dict[str, Any]:
        action_name = {"left": "left_click", "right": "right_click", "middle": "middle_click"}[button]
        kwargs: dict[str, Any] = {"action": action_name, "coordinate": [x, y]}
        if modifiers:
            kwargs["text"] = "+".join(modifiers) if isinstance(modifiers, (list, tuple)) else modifiers
        return self.action(**kwargs)

    def type(self, text: str) -> dict[str, Any]:
        return self.action(action="type", text=text)

    def key(self, combo: str, repeat: int = 1) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"action": "key", "text": combo}
        if repeat != 1:
            kwargs["repeat"] = repeat
        return self.action(**kwargs)

    def scroll(self, direction: str, amount: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"action": "scroll", "scroll_direction": direction, "scroll_amount": amount}
        if x is not None and y is not None:
            kwargs["coordinate"] = [x, y]
        return self.action(**kwargs)


def computer_tool_handler(client: DibsClient):
    """Return a handler for Claude `computer_toolset_20260801` tool_use blocks.

    That toolset issues one `tool_use` block per action, e.g.
    `{"type": "tool_use", "id": "...", "name": "left_click", "toolset_name": "computer",
      "input": {"coordinate": [512, 742]}}` — the member action name IS the tool_use `name`
    (there's no `action` field inside `input`).

    The returned callable takes such a block (an SDK object with `.name`/`.input`, or an
    equivalent dict/mapping) and executes it against dibs, returning the `tool_result`
    block's `content` list: an `image` content block for screenshot/zoom, a `text` block
    ("OK", or the action's text result) for everything else. Raises `DibsError` on failure —
    callers building a tool_result should catch that and set `is_error: True`.
    """

    def handle(tool_use_block: Any) -> list[dict[str, Any]]:
        if hasattr(tool_use_block, "name"):
            name = tool_use_block.name
            raw_input = getattr(tool_use_block, "input", None) or {}
        else:
            name = tool_use_block["name"]
            raw_input = tool_use_block.get("input") or {}

        kwargs = dict(raw_input)
        kwargs["action"] = name
        kwargs.setdefault("auto_lease", True)

        result = client.action(**kwargs)

        image = result.get("image")
        if image:
            return [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image["png_base64"],
                },
            }]

        text = result.get("result")
        if text is None:
            text = "OK"
        return [{"type": "text", "text": str(text)}]

    return handle
