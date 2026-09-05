"""Claude computer-use loop that forwards computer_toolset_20260801 actions to a dibs hub.
Owner: mcp agent.

Usage:
    uv run python examples/claude_agent.py --task "..." --url http://127.0.0.1:7474

Registers itself as agent "claude-agent" against the dibs server (relying on
`allow_local_open_registration` for a token-less registration against localhost, or pass
--token for an already-registered agent). Runs Claude Opus 5 with the
`computer_toolset_20260801` tool, executing each action via
`dibs_client.computer_tool_handler`, until Claude stops requesting tool calls or
--max-turns is hit. Only makes a live API call if Anthropic credentials are available
(ANTHROPIC_API_KEY, or an active `ant auth login` profile) — otherwise it just proves the
module imports and argument parsing work.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients" / "python"))

from dibs_client import DibsClient, DibsError, computer_tool_handler  # noqa: E402

DEFAULT_TASK = (
    "Take a screenshot and tell me which application is in the foreground. Do not click anything."
)
NOT_EXECUTED_TEXT = "Not executed: an earlier computer action in this turn failed."
MODEL = "claude-opus-5"

# The computer_toolset_20260801 member action names (what a tool_use block's `name` will be).
_COMPUTER_ACTIONS = frozenset(
    {
        "screenshot",
        "zoom",
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "triple_click",
        "left_click_drag",
        "mouse_move",
        "left_mouse_down",
        "left_mouse_up",
        "cursor_position",
        "scroll",
        "type",
        "key",
        "hold_key",
        "wait",
    }
)


def _has_anthropic_credentials() -> bool:
    """True if a live API call would have credentials to use: ANTHROPIC_API_KEY is set, or
    `ant auth status` reports an active profile."""
    import os

    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        result = subprocess.run(
            ["ant", "auth", "status"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    output = result.stdout.lower()
    return "active" in output and "no active" not in output


def _is_computer_tool_use(block: Any) -> bool:
    if getattr(block, "type", None) != "tool_use":
        return False
    if getattr(block, "toolset_name", None) == "computer":
        return True
    return getattr(block, "name", None) in _COMPUTER_ACTIONS


def run_agent(task: str, *, url: str, token: str | None, max_turns: int) -> str:
    import anthropic

    dibs = DibsClient(base_url=url, token=token)
    if not dibs.token:
        dibs.register("claude-agent", "examples/claude_agent.py computer-use loop")

    handle_tool = computer_tool_handler(dibs)
    client = anthropic.Anthropic()

    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    tools = [{"type": "computer_toolset_20260801"}]

    for _ in range(max_turns):
        with client.messages.stream(
            model=MODEL,
            max_tokens=32000,
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [b for b in response.content if _is_computer_tool_use(b)]
        if not tool_use_blocks:
            return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

        tool_results: list[dict[str, Any]] = []
        failed = False
        for block in tool_use_blocks:
            if failed:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "toolset_name": "computer",
                        "is_error": True,
                        "content": NOT_EXECUTED_TEXT,
                    }
                )
                continue
            try:
                content = handle_tool(block)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "toolset_name": "computer",
                        "content": content,
                    }
                )
            except DibsError as exc:
                failed = True
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "toolset_name": "computer",
                        "is_error": True,
                        "content": str(exc),
                    }
                )

        messages.append({"role": "user", "content": tool_results})

    return "(max turns reached without a final text response)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--url", default="http://127.0.0.1:7474")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    try:
        import anthropic  # noqa: F401
    except ImportError:
        print(
            "anthropic package not installed. Import + argument parsing checked OK; skipping the live call."
        )
        return

    if not _has_anthropic_credentials():
        print(
            "No Anthropic credentials found (ANTHROPIC_API_KEY unset and `ant auth status` "
            "reports no active profile). Import + argument parsing checked OK; skipping the live call."
        )
        return

    result = run_agent(args.task, url=args.url, token=args.token, max_turns=args.max_turns)
    print(result)


if __name__ == "__main__":
    main()
