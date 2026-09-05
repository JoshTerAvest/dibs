"""CLI: `python -m dibs` / `dibs` (see pyproject.toml `[project.scripts]`). Owner: hub agent.

`serve` runs the server in-process. Every other subcommand is a thin REST client against a
running server (default `http://127.0.0.1:7474`), authenticating with the admin token read
from `<data_dir>/secrets.json` unless `--token` is given.
"""

from __future__ import annotations

import argparse
import time
import subprocess
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, load_settings
from .registry import Registry

DEFAULT_URL = "http://127.0.0.1:7474"


# ---------------------------------------------------------------------------
# helpers shared by the REST client subcommands
# ---------------------------------------------------------------------------


def _resolve_token(args: argparse.Namespace) -> str | None:
    if getattr(args, "token", None):
        return args.token
    settings = load_settings(getattr(args, "config", None))
    secrets_path = Path(settings.data_dir) / "secrets.json"
    if secrets_path.is_file():
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        token = data.get("admin_token")
        if token:
            return token
    return None


def _auth_headers(args: argparse.Namespace) -> dict[str, str]:
    token = _resolve_token(args)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _check(resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        try:
            body: Any = resp.json()
        except Exception:
            body = resp.text
        print(f"error {resp.status_code}: {body}", file=sys.stderr)
        raise SystemExit(1)
    return resp


def _client(args: argparse.Namespace) -> httpx.Client:
    return httpx.Client(base_url=args.url, headers=_auth_headers(args), timeout=30.0)


def _add_client_args(p: argparse.ArgumentParser, *, needs_config: bool = True) -> None:
    p.add_argument("--url", default=DEFAULT_URL, help=f"dibs server URL (default {DEFAULT_URL})")
    p.add_argument(
        "--token", default=None, help="bearer token (default: admin token from secrets.json)"
    )
    if needs_config:
        p.add_argument("--config", default=None, help="config.yaml path (to locate data_dir)")


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app

    settings = load_settings(args.config)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if getattr(args, "data_dir", None):
        settings.data_dir = args.data_dir

    # Touch the registry now (creates data_dir/secrets.json/agents.json on first run) so we
    # can print the admin token and dashboard URL before uvicorn takes over the process.
    registry = Registry(settings.data_dir)
    admin_token = registry.admin_token()
    url = f"http://{settings.host}:{settings.port}/"
    print(f"dibs {url}")
    print(f"admin token: {admin_token}")
    print(f"  (also in {Path(settings.data_dir) / 'secrets.json'})")

    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    )
    hub = getattr(app.state, "hub", None)
    if hub is not None:
        hub.request_shutdown = lambda: setattr(server, "should_exit", True)  # tray "Quit dibs"
    server.run()


def cmd_token(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    registry = Registry(settings.data_dir)
    print(registry.admin_token())


def cmd_register(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = _check(client.post("/v1/agents", json={"name": args.name, "purpose": args.purpose}))
    data = resp.json()
    print(f"agent_id: {data['agent_id']}")
    print(f"name:     {data['name']}")
    print(f"token:    {data['token']}")
    print("(token is shown once -- store it now)")


def cmd_agents(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = _check(client.get("/v1/state"))
    state = resp.json()
    for a in state.get("agents", []):
        flags = []
        if a.get("holding"):
            flags.append("holding")
        if a.get("revoked"):
            flags.append("revoked")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"{a['agent_id']}\t{a['name']!r}\tactions={a['action_count']}\t"
            f"last_seen={a['last_seen']}{flag_str}"
        )
    if not state.get("agents"):
        print("(no agents registered)")


def cmd_status(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = _check(client.get("/v1/state"))
    state = resp.json()
    lease = state.get("lease", {})
    holder = lease.get("holder")
    print(f"version:      {state.get('version')}")
    print(f"uptime_s:     {state.get('uptime_s')}")
    print(f"mode:         {state.get('mode')}")
    print(
        f"paused:       {state.get('paused')}"
        + (f" (reason={state.get('pause_reason')})" if state.get("paused") else "")
    )
    print(
        f"has dibs:     {holder['name'] + ' (' + holder['agent_id'] + ')' if holder else '(none)'}"
    )
    print(f"queue:        {len(lease.get('queue', []))}")
    human = state.get("human", {})
    print(
        f"human:        {'active' if human.get('active') else 'idle'}"
        + (
            f" ({human['last_input_ago_s']:.0f}s ago)"
            if human.get("last_input_ago_s") is not None
            else ""
        )
    )
    pending = state.get("consent", {}).get("pending")
    if pending:
        print(
            f"consent:      pending from {pending['name']!r} ({pending['request_id']})"
            f" -- purpose: {pending['purpose']!r}"
        )
    print(f"agents:       {len(state.get('agents', []))}")
    stats = state.get("stats", {})
    print(
        f"actions:      total={stats.get('actions_total')} "
        f"failed={stats.get('actions_failed')} last_5m={stats.get('actions_last_5m')}"
    )


def cmd_pause(args: argparse.Namespace) -> None:
    with _client(args) as client:
        _check(client.post("/v1/admin/pause", json={"reason": args.reason}))
    print(f"paused (reason={args.reason!r})")


def cmd_resume(args: argparse.Namespace) -> None:
    with _client(args) as client:
        _check(client.post("/v1/admin/resume"))
    print("resumed")


def cmd_mode(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = _check(client.post("/v1/admin/mode", json={"mode": args.mode}))
    print(f"mode: {resp.json()['mode']}")


def _pending_request_id(client: httpx.Client) -> str:
    resp = _check(client.get("/v1/state"))
    pending = resp.json().get("consent", {}).get("pending")
    if not pending:
        print("no pending consent request", file=sys.stderr)
        raise SystemExit(1)
    return pending["request_id"]


def cmd_allow(args: argparse.Namespace) -> None:
    with _client(args) as client:
        request_id = _pending_request_id(client)
        _check(client.post(f"/v1/admin/consent/{request_id}", json={"decision": "allow"}))
    print(f"allowed {request_id}")


def cmd_deny(args: argparse.Namespace) -> None:
    with _client(args) as client:
        request_id = _pending_request_id(client)
        _check(client.post(f"/v1/admin/consent/{request_id}", json={"decision": "deny"}))
    print(f"denied {request_id}")


def cmd_release(args: argparse.Namespace) -> None:
    with _client(args) as client:
        _check(client.post("/v1/admin/release"))
    print("released -- agents paused until the human is idle again")


def cmd_stop(args: argparse.Namespace) -> None:
    with _client(args) as client:
        _check(client.post("/v1/admin/shutdown"))
    print("dibs: stopping")


def _wait_port(url: str, *, up: bool, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            httpx.get(url.rstrip("/") + "/", timeout=1.0)
            if up:
                return True
        except Exception:  # noqa: BLE001
            if not up:
                return True
        time.sleep(0.5)
    return False


def _task_state() -> str | None:
    """State of the `dibs` Scheduled Task (Windows), or None when it isn't installed."""
    r = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "(Get-ScheduledTask -TaskName dibs -ErrorAction Stop).State",
        ],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def cmd_restart(args: argparse.Namespace) -> None:
    """Stop the running server, then start it again: via the `dibs` Scheduled Task when it is
    installed (so the task keeps owning the process), else as a detached `dibs serve`."""
    try:
        cmd_stop(args)
    except Exception as e:  # noqa: BLE001
        print(f"dibs: not running ({type(e).__name__}); starting")
    if not _wait_port(args.url, up=False, timeout_s=15):
        sys.exit("dibs: server did not stop in time")
    via_task = False
    if sys.platform == "win32":
        # The port closes before the task's process fully exits; Start-ScheduledTask on a task
        # that is still 'Running' is a silent no-op, so wait for it to settle first.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _task_state() == "Running":
            time.sleep(0.5)
        if _task_state() == "Running":
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", "Stop-ScheduledTask -TaskName dibs"],
                capture_output=True,
                text=True,
            )
            time.sleep(1)
        r = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Start-ScheduledTask -TaskName dibs -ErrorAction Stop",
            ],
            capture_output=True,
            text=True,
        )
        via_task = r.returncode == 0
    if not via_task:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        subprocess.Popen(
            [sys.executable, "-m", "dibs", "serve"], creationflags=flags, close_fds=True
        )
    if _wait_port(args.url, up=True, timeout_s=25):
        print(
            f"dibs: running at {args.url} via "
            + ("the scheduled task" if via_task else "a detached process")
        )
    else:
        sys.exit("dibs: did not come back up; check data/dibs.log")


def cmd_shot(args: argparse.Namespace) -> None:
    params: dict[str, Any] = {}
    if args.screen is not None:
        params["screen"] = args.screen
    with _client(args) as client:
        resp = _check(client.get("/v1/screenshot.png", params=params))
    out_path = Path(args.out)
    out_path.write_bytes(resp.content)
    print(f"wrote {out_path} ({len(resp.content)} bytes)")


# ---------------------------------------------------------------------------
# argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dibs", description="Windows computer-use hub.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the dibs server")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--config", default=None, help="config.yaml path")
    p_serve.add_argument("--data-dir", default=None, help="override data_dir (also DIBS_DATA_DIR)")
    p_serve.set_defaults(func=cmd_serve)

    p_token = sub.add_parser("token", help="print the admin token")
    p_token.add_argument("--config", default=None, help="config.yaml path (to locate data_dir)")
    p_token.set_defaults(func=cmd_token)

    p_register = sub.add_parser("register", help="register a new agent")
    p_register.add_argument("--name", required=True)
    p_register.add_argument("--purpose", default="")
    _add_client_args(p_register)
    p_register.set_defaults(func=cmd_register)

    p_agents = sub.add_parser("agents", help="list registered agents")
    _add_client_args(p_agents)
    p_agents.set_defaults(func=cmd_agents)

    p_status = sub.add_parser("status", help="show hub state")
    _add_client_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_pause = sub.add_parser("pause", help="pause (kill switch)")
    p_pause.add_argument("--reason", default="manual")
    _add_client_args(p_pause)
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", help="resume from pause")
    _add_client_args(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_mode = sub.add_parser("mode", help="set the operating mode")
    p_mode.add_argument("mode", choices=["ask", "hands_off", "locked"])
    _add_client_args(p_mode)
    p_mode.set_defaults(func=cmd_mode)

    p_allow = sub.add_parser("allow", help="allow the pending consent request")
    _add_client_args(p_allow)
    p_allow.set_defaults(func=cmd_allow)

    p_deny = sub.add_parser("deny", help="deny the pending consent request")
    _add_client_args(p_deny)
    p_deny.set_defaults(func=cmd_deny)

    p_release = sub.add_parser("release", help="take the desk back (human takeover)")
    _add_client_args(p_release)
    p_release.set_defaults(func=cmd_release)

    p_stop = sub.add_parser("stop", help="stop the running server gracefully")
    _add_client_args(p_stop)
    p_stop.set_defaults(func=cmd_stop)

    p_restart = sub.add_parser(
        "restart", help="stop, then start again (scheduled task if installed)"
    )
    _add_client_args(p_restart)
    p_restart.set_defaults(func=cmd_restart)

    p_shot = sub.add_parser("shot", help="save a screenshot")
    p_shot.add_argument("out", help="output PNG path")
    p_shot.add_argument("--screen", type=int, default=None)
    _add_client_args(p_shot)
    p_shot.set_defaults(func=cmd_shot)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
