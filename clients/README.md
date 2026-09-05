# dibs Python client

`clients/python/dibs_client.py` is a small, dependency-light (just `httpx`) synchronous
client for a running dibs hub's REST API (see `../docs/SPEC.md`). It's a single file with
no `dibs` package dependency, so you can copy it into another project or add
`clients/python` to your `PYTHONPATH` / `sys.path`.

```python
import sys
sys.path.insert(0, "/path/to/dibs/clients/python")

from dibs_client import DibsClient, DibsError

client = DibsClient("http://127.0.0.1:7474")
client.register("my-agent", "testing things")   # sets client.token
client.acquire(ttl_s=60)                         # hold the desk lease

client.click(400, 300)
client.type("hello, world")
client.key("ctrl+s")
client.scroll("down", 3)

png, width, height, scale = client.screenshot()
with open("shot.png", "wb") as f:
    f.write(png)

client.release()
```

## Reference

- `DibsClient(base_url="http://127.0.0.1:7474", token=None, *, timeout=30.0, transport=None)`
  — `transport` is an optional *synchronous* `httpx.BaseTransport` override, mainly for tests
  (e.g. `httpx.MockTransport`). Note this client uses `httpx.Client`, not `AsyncClient`, so
  `httpx.ASGITransport` doesn't work here (it's async-only — `handle_async_request`, no
  `handle_request`); to test against a FastAPI/Starlette app synchronously, run it for real
  (uvicorn in a background thread on a free port) and point `base_url` at that instead. See
  `tests/test_client.py` for the pattern.
- `register(name, purpose, admin_token=None) -> dict` — `POST /v1/agents`; sets `self.token`.
  Only pass `admin_token` if the server requires it (non-loopback, or
  `allow_local_open_registration` disabled).
- `state() -> dict`, `display() -> dict`, `audit(limit=50, agent_id=None) -> list[dict]`
- `acquire(ttl_s=None, wait_s=0) -> dict` — `{status: "granted"|"queued", ...}` (200/202), or —
  since v0.2 (`docs/SPEC-v0.2-human.md` §2.1) — `{status: "awaiting_consent", request_id,
  expires_at, human}` (202, a human decision on whether to let you take the desk is pending;
  call again, or pass a larger `wait_s` to long-poll for the decision instead of polling
  yourself). Never raises for any of those. Raises `DibsError(403, "denied", ...)` when the
  human refused, the request timed out waiting for a decision, or the desk is locked/paused for
  agents right now — check `.reason` (one of `"human_denied"`, `"timeout"`, `"locked"`,
  `"paused"`) and `.retry_after_s` on the exception.
- `renew(ttl_s=None) -> dict`, `release(*, force=False) -> None`
- `action(**kwargs) -> dict` — raw `POST /v1/actions`, e.g.
  `client.action(action="left_click", coordinate=[1, 2], auto_lease=True)`
- `batch(actions, auto_lease=False) -> list[dict]`
- `screenshot(screen=None) -> (png_bytes, width, height, scale)`
- Convenience wrappers over `action()`: `click(x, y, button="left", modifiers=None)`,
  `type(text)`, `key(combo, repeat=1)`, `scroll(direction, amount, x=None, y=None)`.
- Every error (`{ok: false, ...}` response, or a non-2xx status) raises `DibsError(status,
  code, detail, payload)` — `code` is the machine string (`"lease_required"`,
  `"unauthorized"`, `"paused"`, and, since v0.2, `"denied"`), `payload` carries any extra
  fields the server sent (e.g. `holder`, `queue_position`, and, since v0.2, `reason`,
  `retry_after_s`, `request_id`). `.reason` and `.retry_after_s` are convenience shortcuts onto
  `payload` (both `None` if the server didn't send them); `str(err)` includes them when set.

### `computer_tool_handler(client)`

Bridges Claude's `computer_toolset_20260801` tool calls straight to dibs. That toolset
issues one `tool_use` block per action — the block's `name` *is* the action (`"left_click"`,
`"screenshot"`, ...) and `toolset_name` is `"computer"`; there's no `action` field inside
`input`. `computer_tool_handler(client)` returns a function that takes such a block (an SDK
object with `.name`/`.input`, or an equivalent `{"name": ..., "input": ...}` mapping),
executes it against dibs (auto-acquiring the desk lease), and returns the content list to
put in the matching `tool_result` block — an `image` block for `screenshot`/`zoom`, a `text`
block otherwise. It raises `DibsError` on failure so the caller can set `is_error: True`.
See `../examples/claude_agent.py` for the full agentic loop, including the batch-failure rule
(everything after the first failed action in a turn gets `is_error: True` with content
`"Not executed: an earlier computer action in this turn failed."`).

## Examples

- `../examples/claude_agent.py` — a full Claude Opus 5 computer-use loop against dibs.
- `../examples/two_agents_demo.py` — two clients contending for the desk lease, showing the
  FIFO queue.
