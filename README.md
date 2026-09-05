# dibs

**A local hub for AI Agent Computer Use. Let your AI agents call dibs on using your desktop.**

It lets multiple agents share one Windows desktop by exposing screenshot,
mouse, keyboard, and window control over HTTP and MCP, so a Claude Code session, a
scheduled automation task, and a browsing agent can all drive the same machine without
fighting each other — or without shoving the user out of their own chair.

The mental model: agents register, one has dibs on "the desk" at a time (an exclusive lease on
the mouse/keyboard), the human always wins, every action is logged, there's one big pause
button, and there's an on-screen overlay so anyone glancing at the monitor can see which
agent is doing what.

## Install

```powershell
uv venv --python 3.12
uv sync
```

That creates `.venv` and installs everything (FastAPI, uvicorn, mss, pyautogui, pywin32,
pynput, the mcp SDK, httpx). No extra dependencies to add yourself.

## Run

```powershell
uv run dibs serve
```

Prints the dashboard URL and the admin token, then serves REST (`/v1/*`), MCP (`/mcp`),
and the dashboard (`/`) all on one port (7474 by default).

For it to start automatically at Windows logon:

```powershell
.\scripts\install-task.ps1
```

> On this machine `Register-ScheduledTask` needs an elevated shell (UAC-filtered admin token): open Windows Terminal **as administrator** and run the installer from there. The task itself runs unelevated as your interactive user. The installer stops any hand-started `dibs serve` first so the task can take port 7474.

This registers a Scheduled Task named `dibs`, not a Windows service — a service runs in
session 0, which can't see the desktop or send input at all. The task runs in your
interactive logon session with a hidden window instead. `.\scripts\uninstall-task.ps1`
removes it.

## First run

`uv run dibs serve` prints an admin token the first time it runs (stored once in
`data/secrets.json`). You'll need it for anything that isn't loopback-open. Grab it again
anytime: `uv run dibs token`.

## Dashboard

`http://127.0.0.1:7474` — a single-page, no-build-step dashboard:

- Live screenshot (polls every second) with a screen picker for multi-monitor setups.
- Mode selector (Ask me / Hands-off / Locked) and a human-presence chip.
- A consent card that appears whenever an agent is waiting on you, with a countdown and
  Allow / Deny buttons.
- The desk card: who holds it, the wait queue, recent consent decisions, force-release and
  take-the-desk-back buttons.
- Agents table (revoke a token), stats, and an audit tail with screenshot thumbnails.

It asks for the admin token the first time it hits a 401 and remembers it in the browser.
Looks fine on a phone.

## Register an agent

```powershell
uv run dibs register --name my-agent --purpose "testing things"
```

Prints the new agent's token once — store it. Equivalent over curl (works without a token
from loopback if `allow_local_open_registration` is true, the default):

```bash
curl -s -X POST http://127.0.0.1:7474/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"my-agent","purpose":"testing things"}'
```

## Use it — REST

Every action except `wait` needs dibs first, screenshots included: nobody looks at your screen without you saying yes.

```bash
TOKEN=<agent token>

# acquire the desk (long-polls up to wait_s if someone else holds it)
curl -s -X POST http://127.0.0.1:7474/v1/lease \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ttl_s":60,"wait_s":10}'

# screenshot (needs dibs like everything else)
curl -s -o shot.png "http://127.0.0.1:7474/v1/screenshot.png?screen=0" \
  -H "Authorization: Bearer $TOKEN"

# click and type
curl -s -X POST http://127.0.0.1:7474/v1/actions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"left_click","coordinate":[400,300]}'
curl -s -X POST http://127.0.0.1:7474/v1/actions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"type","text":"hello"}'

# batch (stops at first failure)
curl -s -X POST http://127.0.0.1:7474/v1/actions/batch \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"actions":[{"action":"left_click","coordinate":[400,300]},{"action":"type","text":"hi"}],"auto_lease":true}'
```

`POST /v1/lease` responds one of four ways depending on mode and who's around:
- `granted` (200) — you hold it.
- `queued` (202) — someone else holds it; `wait_s` ran out, keep polling.
- `awaiting_consent` (202) — mode is `ask` and a human is active; a consent prompt is up
  (overlay + dashboard + hotkeys), keep polling with the same request.
- `denied` (403) — `locked` mode, human said no, or the request timed out; check `reason`
  and `retry_after_s`.

## Use it — MCP from Claude Code

```bash
claude mcp add --transport http dibs http://127.0.0.1:7474/mcp \
  --header "Authorization: Bearer <token>"
```

Exposes six tools: `computer` (one tool, `action` param selects the behavior — mirrors
Anthropic's `computer_toolset_20260801`), `desk_status`, `acquire_desk`, `release_desk`,
`list_windows`, `focus_window`. `computer` always auto-leases, and a denial comes back as
a tool error naming the holder, the consent countdown, or when it'll auto-resume — so the
model can decide whether to wait or back off.

## Use it — Python client

`clients/python/dibs_client.py` is a small sync `httpx` wrapper, no dependency on the
`dibs` package itself:

```python
from dibs_client import DibsClient

client = DibsClient("http://127.0.0.1:7474", token="<agent token>")
client.acquire(ttl_s=60)
client.click(400, 300)
png, w, h, scale = client.screenshot()
client.release()
```

- `examples/claude_agent.py` — a real Claude computer-use loop (`computer_toolset_20260801`)
  that forwards tool calls straight to dibs. Only makes a live API call if you have
  Anthropic credentials; otherwise it just proves the wiring.
- `examples/two_agents_demo.py` — two agents contend for the desk against a running
  server, printing the FIFO queueing timeline (acquire, queued, long-poll, granted).

## Living with a human

Three modes (`dibs mode ask|hands_off|locked`, or the dashboard selector):

| mode | agents get the desk… |
|---|---|
| `ask` (default) | only after you say yes to a consent prompt (being idle is not a yes) |
| `hands_off` | any time — you can still take it back |
| `locked` | never |

In `ask` mode, if you're active at the keyboard an agent has to ask first: a prompt shows
up on screen (bottom-right, doesn't steal focus), on the dashboard as a consent card, with
a countdown. Answer with the buttons, or the hotkeys **Ctrl+Alt+Shift+Y** (allow) /
**Ctrl+Alt+Shift+N** (deny). Saying yes grants a 5-minute consent window so the same agent
doesn't have to ask again right away.

Takeover always wins: touch the mouse or keyboard while an agent holds the desk, and
everything pauses immediately, the agent loses its dibs, and its next action gets a clear
"a human took the desk" error. It resumes on its own after you've been idle for 20 seconds.
**Ctrl+Alt+Shift+R** does the same thing explicitly (take the desk back right now, no
agent input required). **Ctrl+Alt+Shift+P** is the separate manual pause/resume toggle —
manual pauses never auto-resume, you have to un-pause them yourself.

## What you see on screen

An always-on-top, click-through overlay makes agent activity visible to anyone looking at
the monitor:

- Cyan halo (color configurable) around the cursor with the current agent's name, whenever
  an agent holds the desk. Hidden otherwise.
- Top banner: which agent, its purpose, countdown to when its dibs run out. Green the whole
  time you have the desk (after a takeover or Ctrl+Alt+Shift+R); red only for a manual pause.
- Quick flash ring on every click, small "typing…" tag while text goes in.
- The consent prompt described above, bottom-right.

Turn it off with `overlay.enabled: false` — the hub never depends on it (no display, or Tk
failing to start, just logs a warning and carries on).

## Safety

- **Pause**: dashboard button, `Ctrl+Alt+Shift+P`, or `dibs pause` — stops all input
  actions immediately (423 to callers). Only `wait` is exempt: nothing looks at or touches the screen while paused.) still work.
- **Failsafe**: fling the physical mouse to the top-left screen corner during an action
  (the pyautogui convention) and it aborts and pauses.
- **`allow_launch`** is off by default — agents can't start arbitrary processes unless you
  turn it on.
- **Audit log**: every action, successful or not, read-only or not, is written to
  `data/dibs.db` (SQLite). Screenshots and zooms are saved to `data/shots/` (rolling,
  `keep_screenshots` deep, default 200) and linked from each audit row.

## Exposing to other machines

By default dibs only listens on `127.0.0.1` — nothing outside this machine can reach
it. To let other boxes on your Tailscale (e.g. a remote automation runner) drive it, set
`host: 0.0.0.0` in `config.yaml` (copy `config.example.yaml` to start). The moment you do
that, loopback's free pass goes away: every route needs a bearer token, including
registration and the dashboard.

## Config reference

`config.yaml` in the working directory, or `DIBS_CONFIG=<path>`. Every key is also
settable via env var `DIBS_<KEY>` (`__` for nesting, e.g.
`DIBS_PRESENCE__IDLE_AFTER_S=10`). See `config.example.yaml` for a starting copy.

| key | default | what it does |
|---|---|---|
| `host` | `127.0.0.1` | bind address; `0.0.0.0` to expose on Tailscale |
| `port` | `7474` | one port for REST, MCP, and the dashboard |
| `data_dir` | `./data` | secrets, agent registry, audit db, screenshots |
| `screen_index` | `null` | force a default monitor; `null` = primary |
| `max_long_edge` | `1568` | screenshot scaling cap (2576 for larger-image models) |
| `max_pixels` | `1150000` | screenshot scaling cap (3750000 for larger-image models) |
| `lease_default_ttl_s` | `60` | desk lease length if not specified |
| `lease_max_ttl_s` | `600` | hard cap on requested lease length |
| `auto_lease_wait_s` | `30` | how long `auto_lease` waits to acquire before giving up |
| `allow_local_open_registration` | `true` | loopback callers can `POST /v1/agents` without a token |
| `dashboard_open_on_loopback` | `true` | dashboard works without a token from 127.0.0.1 |
| `allow_launch` | `false` | let agents start processes via the `launch` action |
| `keep_screenshots` | `200` | rolling cap on files in `data/shots/` |
| `mode` | `ask` | `ask` / `hands_off` / `locked` — see Living with a human |
| `presence.enabled` | `true` | run the pynput human-presence watcher |
| `presence.idle_after_s` | `30` | how long with no input before you count as "idle" |
| `presence.resume_after_s` | `20` | idle time after a takeover before agents can resume |
| `presence.consent_timeout_s` | `60` | how long an unanswered consent request stays pending |
| `presence.consent_grant_s` | `300` | how long a granted consent window lasts before asking again |
| `presence.deny_cooldown_s` | `120` | how long a denied agent gets an automatic no |
| `overlay.enabled` | `true` | show the cursor halo / banner / consent prompt |
| `overlay.halo_color` | `#00e5ff` | cursor halo color |
| `overlay.banner` | `true` | show the top banner strip |
| `hotkeys.pause` | `ctrl+alt+shift+p` | manual pause/resume toggle |
| `hotkeys.allow` | `ctrl+alt+shift+y` | allow the pending consent request |
| `hotkeys.deny` | `ctrl+alt+shift+n` | deny the pending consent request |
| `hotkeys.release` | `ctrl+alt+shift+r` | take the desk back right now |

## Layout

| path | what's there |
|---|---|
| `dibs/desk.py`, `keymap.py`, `actions.py` | Windows primitives, key names, action dispatch |
| `dibs/config.py` | settings: YAML + env overrides |
| `dibs/registry.py`, `lease.py`, `presence.py` | agents/tokens, the desk lease, human-presence detection |
| `dibs/audit.py` | SQLite audit log + rolling screenshots |
| `dibs/hub.py` | the `Hub` facade — auth, mode/consent, lease gating, runs actions, audit |
| `dibs/server.py` | FastAPI app, REST routes, mounts MCP + dashboard |
| `dibs/mcp_server.py` | MCP streamable-HTTP server at `/mcp` |
| `dibs/overlay.py` | the on-screen Tk overlay |
| `dibs/__main__.py` | CLI: `serve`, `register`, `agents`, `status`, `pause`/`resume`, `mode`, `allow`/`deny`/`release`, `shot`, `token` |
| `dibs/dashboard/` | the web dashboard (`index.html`, `app.js`, `style.css`) |
| `clients/python/dibs_client.py` | small sync REST client |
| `examples/` | `claude_agent.py`, `two_agents_demo.py`, `overlay_demo.py` |
| `scripts/*.ps1` | `run.ps1`, `install-task.ps1`, `uninstall-task.ps1` |

## Dev

```powershell
uv run pytest                 # everything that doesn't need a real display
uv run pytest -m display      # exercises the real screen/mouse/keyboard/overlay on this box
uv run ruff check .           # lint python code
uv run ruff format .          # format python code
uv run mypy .                 # typecheck python code

npm install                   # install JS dependencies
npm run lint                  # lint dashboard JS
npm run format                # format dashboard files
```

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

Ensure you run the test suite (`uv run pytest`) before submitting.
