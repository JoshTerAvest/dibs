"""Tiny FastAPI app that serves canned JSON per docs/SPEC.md shapes, for testing the
dashboard UI while the real dibs server is being built by other agents.

Serves /v1/state, /v1/display, /v1/audit, a generated PNG for /v1/screenshot.png and
/v1/shots/{id}.png, accepts the admin POST/DELETE routes (mutating in-memory canned
state so the dashboard's optimistic re-fetches show real effects), and mounts the
dashboard directory (dibs/dashboard/) at "/".

Run: `uv run python tests/dashboard_mock_server.py --port 7475`
Import: `from dashboard_mock_server import app` (used by test_dashboard_static.py).
"""
from __future__ import annotations

import argparse
import io
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dibs" / "dashboard"

START_TIME = time.time()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


_now_at_start = _now()

# ---------------------------------------------------------------------------
# Canned, mutable in-memory state
# ---------------------------------------------------------------------------

AGENTS: list[dict[str, Any]] = [
    {
        "agent_id": "claude-code-7f3a",
        "name": "claude-code",
        "purpose": "desktop automation for dibs itself",
        "created_at": _iso(_now_at_start - timedelta(days=2)),
        "last_seen": _iso(_now_at_start - timedelta(seconds=2)),
        "action_count": 142,
        "revoked": False,
    },
    {
        "agent_id": "night-queue-9c2d",
        "name": "night-queue",
        "purpose": "scheduled task runner on a remote host",
        "created_at": _iso(_now_at_start - timedelta(days=5)),
        "last_seen": _iso(_now_at_start - timedelta(seconds=31)),
        "action_count": 58,
        "revoked": False,
    },
    {
        "agent_id": "gemini-explorer-1a2b",
        "name": "gemini-explorer",
        "purpose": "research browsing",
        "created_at": _iso(_now_at_start - timedelta(days=1)),
        "last_seen": _iso(_now_at_start - timedelta(minutes=6, seconds=40)),
        "action_count": 12,
        "revoked": False,
    },
    {
        "agent_id": "old-bot-55zz",
        "name": "old-bot",
        "purpose": "deprecated crawler",
        "created_at": _iso(_now_at_start - timedelta(days=30)),
        "last_seen": _iso(_now_at_start - timedelta(days=1, hours=1)),
        "action_count": 300,
        "revoked": True,
    },
]

LEASE: dict[str, Any] = {
    "holder": {
        "agent_id": "claude-code-7f3a",
        "name": "claude-code",
        "lease_id": "lease-abc123",
        "acquired_at": _iso(_now_at_start - timedelta(seconds=5)),
        "expires_at": _iso(_now_at_start + timedelta(seconds=55)),
    },
    "queue": [
        {
            "agent_id": "night-queue-9c2d",
            "name": "night-queue",
            "since": _iso(_now_at_start - timedelta(seconds=20)),
        }
    ],
}

PAUSED: dict[str, Any] = {"paused": False, "reason": None, "paused_at": None}

MODE: dict[str, Any] = {"mode": "ask"}

# Human presence toggles automatically over a 40s cycle (30s "active", 10s "idle") so the
# dashboard's presence chip visibly changes state during manual/browser testing without
# needing a debug control. idle_after_s matches the default in docs/SPEC-v0.2-human.md.
_HUMAN_IDLE_AFTER_S = 30
_HUMAN_CYCLE_S = 40.0


def _human_state() -> dict[str, Any]:
    elapsed = (time.time() - START_TIME) % _HUMAN_CYCLE_S
    return {
        "active": elapsed < _HUMAN_IDLE_AFTER_S,
        "last_input_ago_s": round(elapsed, 1),
        "idle_after_s": _HUMAN_IDLE_AFTER_S,
    }


CONSENT_PENDING: dict[str, Any] | None = {
    "request_id": "req-9f21",
    "agent_id": "gemini-explorer-1a2b",
    "name": "gemini-explorer",
    "purpose": "research browsing -- needs to click a link",
    "requested_at": _iso(_now_at_start - timedelta(seconds=5)),
    "expires_at": _iso(_now_at_start + timedelta(seconds=55)),
}

CONSENT_WINDOWS: list[dict[str, Any]] = [
    {"agent_id": "claude-code-7f3a", "consent_until": _iso(_now_at_start + timedelta(minutes=3))},
]

CONSENT_RECENT: list[dict[str, Any]] = [
    {"request_id": "req-7a10", "agent_id": "night-queue-9c2d", "decision": "allow", "at": _iso(_now_at_start - timedelta(minutes=4))},
    {"request_id": "req-5b02", "agent_id": "old-bot-55zz", "decision": "deny", "at": _iso(_now_at_start - timedelta(minutes=12))},
]


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _resolve_expired_consent() -> None:
    """Mirrors the real hub's consent_timeout_s behaviour: an unanswered pending request
    ages out on its own and lands in `recent` with decision "timeout"."""
    global CONSENT_PENDING
    if CONSENT_PENDING and _parse_iso(CONSENT_PENDING["expires_at"]) <= _now():
        CONSENT_RECENT.insert(0, {
            "request_id": CONSENT_PENDING["request_id"],
            "agent_id": CONSENT_PENDING["agent_id"],
            "decision": "timeout",
            "at": _iso(_now()),
        })
        CONSENT_PENDING = None


def _prune_expired_windows() -> None:
    now = _now()
    CONSENT_WINDOWS[:] = [w for w in CONSENT_WINDOWS if _parse_iso(w["consent_until"]) > now]


# ---------------------------------------------------------------------------
# Demo cycle: rotates the lease/consent/pause fields through all five (six, counting
# "locked") pill states on a timer, so the friendlier dashboard v0.3 skin can be watched
# or screenshotted cycling without scripting every click. Any manual admin action
# (pause/resume/mode/consent/release/revoke) suspends the cycle for _DEMO_IDLE_GATE_S
# seconds so a directed manual QA pass (click Allow, Pause, switch mode, ...) isn't
# fought by the clock -- and it keeps the whole thing dormant for the length of a normal
# pytest run, which only ever calls these endpoints directly.
# ---------------------------------------------------------------------------

_LAST_MANUAL_CHANGE = time.time()
_DEMO_IDLE_GATE_S = 12.0
_DEMO_STEP_S = 10.0
_DEMO_STEPS = ["consent", "agent", "human", "paused", "idle", "locked"]


def _touch_manual() -> None:
    global _LAST_MANUAL_CHANGE
    _LAST_MANUAL_CHANGE = time.time()


def _apply_demo_cycle() -> None:
    global CONSENT_PENDING
    idle_for = time.time() - _LAST_MANUAL_CHANGE
    if idle_for < _DEMO_IDLE_GATE_S:
        return
    step = _DEMO_STEPS[int((idle_for - _DEMO_IDLE_GATE_S) // _DEMO_STEP_S) % len(_DEMO_STEPS)]
    now = _now()
    agent_holder = {
        "agent_id": "claude-code-7f3a", "name": "claude-code", "lease_id": "lease-demo",
        "acquired_at": _iso(now - timedelta(seconds=5)), "expires_at": _iso(now + timedelta(seconds=55)),
    }
    if step == "consent":
        PAUSED.update(paused=False, reason=None, paused_at=None)
        LEASE["holder"] = agent_holder
        MODE["mode"] = "ask"
        if not CONSENT_PENDING:
            CONSENT_PENDING = {
                "request_id": "req-demo", "agent_id": "gemini-explorer-1a2b", "name": "gemini-explorer",
                "purpose": "research browsing -- needs to click a link",
                "requested_at": _iso(now - timedelta(seconds=2)), "expires_at": _iso(now + timedelta(seconds=55)),
            }
    elif step == "agent":
        CONSENT_PENDING = None
        PAUSED.update(paused=False, reason=None, paused_at=None)
        LEASE["holder"] = agent_holder
        MODE["mode"] = "ask"
    elif step == "human":
        CONSENT_PENDING = None
        LEASE["holder"] = None
        PAUSED.update(paused=True, reason="human_took_the_mouse", paused_at=_iso(now))
    elif step == "paused":
        CONSENT_PENDING = None
        LEASE["holder"] = None
        PAUSED.update(paused=True, reason="dashboard-demo", paused_at=_iso(now))
    elif step == "idle":
        CONSENT_PENDING = None
        LEASE["holder"] = None
        PAUSED.update(paused=False, reason=None, paused_at=None)
        MODE["mode"] = "ask"
    elif step == "locked":
        CONSENT_PENDING = None
        LEASE["holder"] = None
        PAUSED.update(paused=False, reason=None, paused_at=None)
        MODE["mode"] = "locked"


SCREENS = [
    {"index": 0, "x": 0, "y": 0, "width": 2560, "height": 1440, "primary": True},
    {"index": 1, "x": -1920, "y": 0, "width": 1920, "height": 1080, "primary": False},
]

SCREEN_COLORS = {0: (26, 42, 74), 1: (58, 34, 74)}

DISPLAY = {
    "screens": SCREENS,
    "default_screen": 0,
    "screenshot": {"width": 1430, "height": 804, "scale": 0.5586, "max_long_edge": 1568, "max_pixels": 1150000},
}

_ACTION_TEMPLATES: list[dict[str, Any]] = [
    {"action": "left_click", "input": {"coordinate": [715, 402]}, "ok": True, "duration_ms": 45, "shot": False},
    {"action": "type", "input": {"text": "Hello from the dibs dashboard mock, this line is long"}, "ok": True, "duration_ms": 120, "shot": False},
    {"action": "screenshot", "input": {}, "ok": True, "duration_ms": 80, "shot": True},
    {"action": "key", "input": {"text": "ctrl+alt+shift+p", "repeat": 1}, "ok": True, "duration_ms": 5, "shot": False},
    {"action": "scroll", "input": {"scroll_direction": "down", "scroll_amount": 3, "coordinate": [400, 300]}, "ok": True, "duration_ms": 10, "shot": False},
    {"action": "left_click_drag", "input": {"start_coordinate": [100, 100], "coordinate": [300, 300]}, "ok": True, "duration_ms": 310, "shot": False},
    {"action": "zoom", "input": {"region": [0, 0, 500, 500]}, "ok": True, "duration_ms": 60, "shot": True},
    {"action": "left_click", "input": {"coordinate": [200, 88]}, "ok": False, "error": "lease_required", "duration_ms": 2, "shot": False},
    {"action": "focus_window", "input": {"title": "Notepad"}, "ok": True, "duration_ms": 15, "shot": False},
    {"action": "get_clipboard", "input": {}, "ok": True, "duration_ms": 3, "shot": False},
    {"action": "wait", "input": {"duration": 2}, "ok": True, "duration_ms": 2001, "shot": False},
    {"action": "list_windows", "input": {}, "ok": True, "duration_ms": 22, "shot": False},
    {"action": "launch", "input": {"command": "notepad.exe"}, "ok": False, "error": "launch_disabled", "duration_ms": 1, "shot": False},
    {"action": "double_click", "input": {"coordinate": [960, 540]}, "ok": True, "duration_ms": 38, "shot": False},
    {"action": "mouse_move", "input": {"coordinate": [512, 256]}, "ok": True, "duration_ms": 8, "shot": False},
    {"action": "set_clipboard", "input": {"text": "https://example.com/some/very/long/url/for/truncation/testing"}, "ok": True, "duration_ms": 4, "shot": False},
]

_agent_cycle = [AGENTS[0], AGENTS[1], AGENTS[0], AGENTS[2], AGENTS[0]]

AUDIT: list[dict[str, Any]] = []
_shot_counter = 0
for _i, _tpl in enumerate(_ACTION_TEMPLATES):
    _agent = _agent_cycle[_i % len(_agent_cycle)]
    _row: dict[str, Any] = {
        "id": _i + 1,
        "ts": _iso(_now_at_start - timedelta(seconds=(len(_ACTION_TEMPLATES) - _i) * 7)),
        "agent_id": _agent["agent_id"],
        "agent_name": _agent["name"],
        "action": _tpl["action"],
        "input": _tpl["input"],
        "ok": _tpl["ok"],
        "error": _tpl.get("error"),
        "duration_ms": _tpl["duration_ms"],
        "screenshot_url": None,
    }
    if _tpl["shot"]:
        _shot_counter += 1
        _row["screenshot_url"] = f"/v1/shots/{_shot_counter}.png"
    AUDIT.append(_row)
AUDIT.reverse()  # newest first, matching the real /v1/audit contract

STATS = {"actions_total": 4213, "actions_failed": 13, "actions_last_5m": 9}

OVERLAY_ENABLED = True



def _config() -> dict[str, Any]:
    return {
        "host": "127.0.0.1",
        "port": 7475,
        "allow_launch": False,
        "mode": MODE["mode"],
        "overlay": OVERLAY_ENABLED,
    }

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="dibs dashboard mock")


@app.get("/v1/state")
def get_state() -> dict[str, Any]:
    _apply_demo_cycle()
    _resolve_expired_consent()
    _prune_expired_windows()
    return {
        "version": "0.2.0-mock",
        "uptime_s": int(time.time() - START_TIME),
        "paused": PAUSED["paused"],
        "pause_reason": PAUSED["reason"],
        "paused_at": PAUSED["paused_at"],
        "mode": MODE["mode"],
        "human": _human_state(),
        "consent": {
            "pending": CONSENT_PENDING,
            "windows": CONSENT_WINDOWS,
            "recent": CONSENT_RECENT[:10],
        },
        "lease": LEASE,
        "agents": [{**a, "holding": (LEASE["holder"] or {}).get("agent_id") == a["agent_id"]} for a in AGENTS],
        "display": DISPLAY,
        "stats": STATS,
        "config": _config(),
    }


@app.get("/v1/display")
def get_display() -> dict[str, Any]:
    return DISPLAY


@app.get("/v1/audit")
def get_audit(limit: int = Query(50), agent_id: str | None = Query(None)) -> list[dict[str, Any]]:
    rows = AUDIT
    if agent_id:
        rows = [r for r in rows if r["agent_id"] == agent_id]
    return rows[:limit]


def _make_png(width: int, height: int, color: tuple[int, int, int], label: str) -> bytes:
    width, height = max(40, width), max(24, height)
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), f"{label}\n{datetime.now().strftime('%H:%M:%S')}", fill=(235, 235, 235))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.get("/v1/screenshot.png")
def get_screenshot(screen: int = Query(0), scale: float = Query(1.0)) -> Response:
    base = DISPLAY["screenshot"]
    width, height = int(base["width"] * scale), int(base["height"] * scale)
    color = SCREEN_COLORS.get(screen, (40, 40, 40))
    png = _make_png(width, height, color, f"screen {screen}")
    return Response(content=png, media_type="image/png")


@app.get("/v1/shots/{shot_id}.png")
def get_shot(shot_id: int) -> Response:
    palette = [(74, 26, 42), (26, 74, 58), (74, 60, 26), (34, 42, 74)]
    color = palette[shot_id % len(palette)]
    png = _make_png(320, 180, color, f"shot {shot_id}")
    return Response(content=png, media_type="image/png")


@app.post("/v1/admin/pause")
async def admin_pause(request: Request) -> dict[str, Any]:
    _touch_manual()
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    PAUSED["paused"] = True
    PAUSED["reason"] = (body or {}).get("reason") or "manual"
    PAUSED["paused_at"] = _iso(_now())
    return {"paused": True, "reason": PAUSED["reason"]}


@app.post("/v1/admin/resume")
def admin_resume() -> dict[str, Any]:
    _touch_manual()
    PAUSED["paused"] = False
    PAUSED["reason"] = None
    PAUSED["paused_at"] = None
    return {"paused": False}


@app.post("/v1/admin/mode")
async def admin_mode(request: Request) -> JSONResponse:
    _touch_manual()
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    mode = (body or {}).get("mode")
    if mode not in ("ask", "hands_off", "locked"):
        return JSONResponse({"ok": False, "error": "invalid_mode"}, status_code=400)
    MODE["mode"] = mode
    return JSONResponse({"mode": mode})


@app.post("/v1/admin/consent/{request_id}")
async def admin_consent(request_id: str, request: Request) -> JSONResponse:
    global CONSENT_PENDING
    _touch_manual()
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    decision = (body or {}).get("decision")
    if decision not in ("allow", "deny"):
        return JSONResponse({"ok": False, "error": "invalid_decision"}, status_code=400)
    if not CONSENT_PENDING or CONSENT_PENDING["request_id"] != request_id:
        return JSONResponse({"error": "no_pending_request"}, status_code=404)

    pending = CONSENT_PENDING
    CONSENT_PENDING = None
    CONSENT_RECENT.insert(0, {
        "request_id": pending["request_id"],
        "agent_id": pending["agent_id"],
        "decision": decision,
        "at": _iso(_now()),
    })
    if decision == "allow":
        CONSENT_WINDOWS.append({
            "agent_id": pending["agent_id"],
            "consent_until": _iso(_now() + timedelta(seconds=300)),
        })
    return JSONResponse({"ok": True, "decision": decision})


@app.post("/v1/admin/release")
def admin_release() -> dict[str, Any]:
    """Human takeover from the dashboard: revoke the lease holder (queue kept) and pause
    with reason human_took_the_mouse, per docs/SPEC-v0.2-human.md §2.3."""
    _touch_manual()
    LEASE["holder"] = None
    PAUSED["paused"] = True
    PAUSED["reason"] = "human_took_the_mouse"
    PAUSED["paused_at"] = _iso(_now())
    return {"paused": True, "reason": PAUSED["reason"]}


@app.delete("/v1/agents/{agent_id}")
def revoke_agent(agent_id: str) -> JSONResponse:
    _touch_manual()
    for a in AGENTS:
        if a["agent_id"] == agent_id:
            a["revoked"] = True
            if (LEASE["holder"] or {}).get("agent_id") == agent_id:
                LEASE["holder"] = None
            return JSONResponse({"ok": True, "agent_id": agent_id})
    return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)


@app.delete("/v1/lease")
def release_lease(force: bool = Query(False)) -> Response:
    _touch_manual()
    if not force:
        return JSONResponse({"ok": False, "error": "not_holder"}, status_code=409)
    LEASE["holder"] = None
    return Response(status_code=204)


# Mount the dashboard static files at "/" LAST so the explicit /v1/* routes above
# are matched first and this only catches everything else (index.html, app.js, style.css).
app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7475)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
