"""Two agents contend for the dibs desk lease — demonstrates FIFO queueing. Owner: mcp agent.

Usage:
    uv run python examples/two_agents_demo.py --url http://127.0.0.1:7474

Registers agent-a and agent-b (pass --admin-token if the server has
allow_local_open_registration disabled). Sequence:
  1. agent-a acquires the desk (ttl_s=10).
  2. agent-b tries acquire(wait_s=0) -> queued, prints its queue position.
  3. agent-b starts a background acquire(wait_s=15) (long-polls).
  4. agent-a reads cursor_position, waits ~3s, then releases.
  5. agent-b's long-poll returns granted.
Prints a timeline of what happened and when.

v0.2 (SPEC-v0.2-human.md §2.1): if the server is in `ask` mode and a human is active at the
keyboard, an `acquire()` can also come back `awaiting_consent` (a consent prompt is pending --
this demo just polls and reports it, it doesn't answer the prompt itself) or raise a
`DibsError(403, "denied", ...)` (the human said no, the request timed out, or the desk is
locked/paused for agents). Every `acquire()` call below goes through the helpers below so those
states get logged into the timeline like everything else, whichever mode the server is in.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "python"))

from dibs_client import DibsClient, DibsError  # noqa: E402


def _describe_acquire_result(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "queued":
        return f"queued (position {result.get('position')})"
    if status == "awaiting_consent":
        human = result.get("human") or {}
        return (
            f"awaiting_consent (request_id={result.get('request_id')}, "
            f"expires_at={result.get('expires_at')}, human_active={human.get('active')})"
        )
    return str(status)


def _describe_denied(exc: DibsError) -> str:
    return f"denied (reason={exc.reason}, retry_after_s={exc.retry_after_s})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7474")
    parser.add_argument("--admin-token", default=None)
    args = parser.parse_args()

    start = time.monotonic()
    timeline: list[str] = []

    def log(msg: str) -> None:
        line = f"[t={time.monotonic() - start:5.2f}s] {msg}"
        timeline.append(line)
        print(line, flush=True)

    client_a = DibsClient(base_url=args.url)
    client_b = DibsClient(base_url=args.url)

    client_a.register("agent-a", "two_agents_demo.py contender A", admin_token=args.admin_token)
    log("agent-a registered")
    client_b.register("agent-b", "two_agents_demo.py contender B", admin_token=args.admin_token)
    log("agent-b registered")

    try:
        granted = client_a.acquire(ttl_s=10, wait_s=0)
        log(f"agent-a acquires the desk -> {granted.get('status')}")
    except DibsError as exc:
        log(f"agent-a acquires the desk -> {_describe_denied(exc)}")
        granted = {}

    try:
        queued = client_b.acquire(wait_s=0)
        log(f"agent-b tries to acquire -> {_describe_acquire_result(queued)}")
    except DibsError as exc:
        log(f"agent-b tries to acquire -> {_describe_denied(exc)}")

    result_holder: dict[str, Any] = {}

    def wait_for_desk() -> None:
        try:
            result_holder["result"] = client_b.acquire(wait_s=15)
            log(
                f"agent-b's long-poll returns -> {_describe_acquire_result(result_holder['result'])}"
            )
        except DibsError as exc:
            result_holder["error"] = exc
            log(f"agent-b's long-poll returns -> {_describe_denied(exc)}")

    waiter = threading.Thread(target=wait_for_desk, daemon=True)
    waiter.start()
    log("agent-b starts long-polling acquire(wait_s=15) in the background")

    time.sleep(0.5)
    pos = client_a.action(action="cursor_position")
    log(f"agent-a reads cursor_position -> {pos.get('result')}")

    time.sleep(2.5)
    client_a.release()
    log("agent-a releases the desk")

    waiter.join(timeout=15)
    if "result" not in result_holder and "error" not in result_holder:
        log("agent-b's long-poll did not return within 15s")

    print("\n--- timeline ---")
    for line in timeline:
        print(line)


if __name__ == "__main__":
    main()
