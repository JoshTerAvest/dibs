# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.


## Build & Test

```bash
uv sync --extra dev
uv run pytest -q -m "not display"   # full suite, safe anywhere
uv run pytest -m display            # desk/overlay/tray tests — real desktop only
uv run dibs serve                   # run the hub (http://127.0.0.1:7474)
```

## Architecture Overview

A local hub that lets many AI agents share one Windows desktop. It exposes screenshot,
mouse, keyboard, and window control over REST (`/v1/*`) and MCP (`/mcp`), with agent
registration, an exclusive input lease ("the desk") with a FIFO queue, a pause/kill
switch, human-override when the user moves the mouse, an audit log, and a web dashboard.

## Conventions & Patterns

- Follow `docs/DESIGN-PRINCIPLES.md` for anything human-facing (dashboard, overlay, tray,
  prompts, CLI copy, README).
- Display tests (`-m display`) must never touch the real user's apps — drive a throwaway
  window (Calculator, a solid-colour test window) or the mock dashboard server
  (`tests/dashboard_mock_server.py`), never Notepad/browser/real session windows.

## Design standard
All human-facing UI (dashboard, overlay, tray, prompts, CLI copy, README) follows `docs/DESIGN-PRINCIPLES.md`. Read it before touching any of those.
