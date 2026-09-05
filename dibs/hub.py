"""Hub facade. Owner: hub agent.

Everything above the desk goes through here: auth, pause, lease gating, modes/consent, human
presence + takeover, audit. server.py (REST) and mcp_server.py (MCP) are thin layers over this
class, so keep it transport-free. See docs/SPEC.md and docs/SPEC-v0.2-human.md (mode / consent /
takeover / overlay hookups supersede the v0.1 "human override" section).
"""
from __future__ import annotations

import asyncio
import os
import concurrent.futures
import logging
import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from . import actions, desk, overlay, presence, tray
from . import __version__
from .actions import ActionResult
from .audit import AuditLog
from .config import Settings
from .lease import LeaseManager
from .registry import Registry

logger = logging.getLogger(__name__)

_CONSENT_POLL_INTERVAL_S = 0.2
_DEFAULT_RETRY_AFTER_S = 30
_VALID_MODES = ("ask", "hands_off", "locked")

# Actions that visibly type/press keys -- the overlay shows a "typing..." bubble around these.
_TYPING_ACTIONS = frozenset({"type", "key", "hold_key"})

# Click-shaped actions -> the button name the overlay's flash_click wants.
_CLICK_FLASH_BUTTON = {
    "left_click": "left", "double_click": "left", "triple_click": "left",
    "right_click": "right", "middle_click": "middle",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(ts: float) -> str:
    """time.time()-style epoch seconds -> ISO 8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _estimate_action_duration(action: dict[str, Any]) -> float:
    """Best-effort guess at how long an input action will keep moving the mouse/keyboard, so
    `presence.agent_input_until()` can be given a deadline that covers it (SPEC-v0.2 §1)."""
    name = action.get("action")
    motion_s = 0.0
    
    if name in ("mouse_move", "left_click", "right_click", "middle_click", "double_click", "triple_click", "left_click_drag", "scroll"):
        to_coord = action.get("coordinate")
        if to_coord and isinstance(to_coord, list) and len(to_coord) == 2:
            from_coord = action.get("start_coordinate")
            if from_coord and isinstance(from_coord, list) and len(from_coord) == 2:
                from_xy = (float(from_coord[0]), float(from_coord[1]))
            else:
                try:
                    from_xy = desk.cursor_position()
                except Exception:
                    from_xy = (0.0, 0.0)
            to_xy = (float(to_coord[0]), float(to_coord[1]))
            # Note: to_xy is usually scaled, from_xy is absolute. The distance might be slightly off
            # but it is good enough for a presence deadline estimate.
            motion_s = desk.estimate_motion_s(from_xy, to_xy)

    if name == "hold_key":
        try:
            return float(action.get("duration", 0)) + motion_s
        except (TypeError, ValueError):
            return 0.5 + motion_s
    if name == "left_click_drag":
        return 0.4 + motion_s # actions.py drags over ~0.3s
    if name == "type":
        text = action.get("text") or ""
        return min(10.0, 0.02 * len(text) + 0.2) + motion_s
    if name == "key":
        try:
            repeat = int(action.get("repeat", 1))
        except (TypeError, ValueError):
            repeat = 1
        return 0.05 * repeat + 0.2 + motion_s
    return 0.3 + motion_s


@dataclass
class AgentInfo:
    agent_id: str
    name: str
    purpose: str
    is_admin: bool = False


class HubError(Exception):
    """status = HTTP status to use; code = machine string; payload = extra json fields."""

    def __init__(self, status: int, code: str, detail: str = "", payload: dict[str, Any] | None = None):
        super().__init__(detail or code)
        self.status, self.code, self.detail, self.payload = status, code, detail or code, payload or {}


@dataclass
class ConsentRequest:
    """A single pending "may I take the desk?" prompt. Only one is ever pending at a time
    (SPEC-v0.2 §2.1) -- a second agent asking just waits for this one to resolve."""

    request_id: str
    agent_id: str
    name: str
    purpose: str
    requested_at: float  # time.time()
    expires_at: float    # time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "purpose": self.purpose,
            "requested_at": _iso(self.requested_at),
            "expires_at": _iso(self.expires_at),
        }


class _TrayActions:
    """Tray -> hub bridge. The tray calls these from its own threads; everything is marshalled onto
    the hub's event loop so lease/consent state is only ever touched from one thread."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    def _on_loop(self, fn):
        loop = self.hub._loop
        if loop is None or loop.is_closed():
            return fn()
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)

        loop.call_soon_threadsafe(run)
        return fut.result(timeout=5)

    def get_state(self) -> dict[str, Any]:
        return self._on_loop(self.hub.state)

    def pause(self) -> None:
        self._on_loop(lambda: self.hub.pause("tray"))

    def resume(self) -> None:
        self._on_loop(self.hub.resume)

    def release(self) -> None:
        self._on_loop(self.hub.human_release)

    def set_mode(self, mode: str) -> None:
        self._on_loop(lambda: self.hub.set_mode(mode))

    def allow(self, request_id: str) -> None:
        self._on_loop(lambda: self.hub.decide_consent(request_id, "allow"))

    def deny(self, request_id: str) -> None:
        self._on_loop(lambda: self.hub.decide_consent(request_id, "deny"))

    def quit(self) -> None:
        fn = self.hub.request_shutdown
        if fn is not None:
            fn()
        else:
            os._exit(0)


class Hub:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.registry = Registry(self.data_dir)
        self._audit = AuditLog(self.data_dir)
        self._lease = LeaseManager(settings.lease_default_ttl_s, settings.lease_max_ttl_s)
        self._presence = presence.Presence(
            idle_after_s=settings.presence.idle_after_s, on_human_input=self._on_human_input,
        )
        self.overlay = overlay.create(settings)
        self.request_shutdown: Callable[[], None] | None = None   # set by `dibs serve`
        self.tray = tray.create(_TrayActions(self), f"http://127.0.0.1:{settings.port}",
                                enabled=settings.tray.enabled)

        self._paused = False
        self._pause_reason: str | None = None
        self._pause_manual = True
        self._paused_at: str | None = None

        self._action_lock = asyncio.Lock()

        # consent / mode state (SPEC-v0.2 §2)
        self._pending_consent: ConsentRequest | None = None
        self._consent_windows: dict[str, float] = {}   # agent_id -> consent_until (time.time())
        self._deny_cooldowns: dict[str, float] = {}     # agent_id -> cooldown_until (time.time())
        self._consent_recent: list[dict[str, Any]] = []
        self._consent_recent_max = 20

        self._last_overlay_holder_id: str | None = None

        self._start_time: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lease_task: asyncio.Task | None = None
        self._presence_task: asyncio.Task | None = None
        self._hotkey_listener: Any = None

    # ---- lifecycle ----

    async def start(self) -> None:
        desk.set_dpi_aware()
        desk.configure_motion(enabled=self.settings.motion.human_like, speed=self.settings.motion.speed)
        self._audit.open(keep_screenshots=self.settings.keep_screenshots)
        self._start_time = time.monotonic()
        self._loop = asyncio.get_running_loop()

        try:
            self.overlay.start()
        except Exception:
            logger.exception("failed to start overlay; continuing without it")
        try:
            self.tray.start()
        except Exception:
            logger.exception("failed to start tray; continuing without it")

        if self.settings.presence.enabled:
            try:
                self._presence.start()
            except Exception:
                logger.exception("failed to start presence listeners; continuing without them")

        self._lease_task = asyncio.create_task(self._lease_sweep_loop())
        self._presence_task = asyncio.create_task(self._presence_sweep_loop())
        self._start_hotkey_listener()

    async def stop(self) -> None:
        for task in (self._lease_task, self._presence_task):
            if task is not None:
                task.cancel()
        for task in (self._lease_task, self._presence_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._lease_task = None
        self._presence_task = None

        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                logger.exception("failed to stop hotkey listener")
            self._hotkey_listener = None

        try:
            self._presence.stop()
        except Exception:
            logger.exception("failed to stop presence listeners")
        try:
            self.overlay.stop()
        except Exception:
            logger.exception("failed to stop overlay")
        try:
            self.tray.stop()
        except Exception:
            logger.exception("failed to stop tray")

        self._audit.close()

    # ---- background loops ----

    async def _lease_sweep_loop(self) -> None:
        while True:
            try:
                self._lease.sweep()
                self._update_overlay_holder()
            except Exception:
                logger.exception("lease sweep failed")
            await asyncio.sleep(1)

    async def _presence_sweep_loop(self) -> None:
        while True:
            try:
                self._presence_tick()
            except Exception:
                logger.exception("presence sweep tick failed")
            await asyncio.sleep(0.5)

    def _presence_tick(self) -> None:
        self._maybe_auto_allow_on_idle()
        self._resolve_pending_consent_if_due()
        if self._paused and not self._pause_manual and self._pause_reason == "human_took_the_mouse":
            seconds = self._presence.seconds_since_human()
            if seconds is not None and seconds >= self.settings.presence.resume_after_s:
                self.resume()

    # ---- hotkeys (P pause, Y allow, N deny, R release) ----

    def _start_hotkey_listener(self) -> None:
        try:
            from pynput import keyboard

            hk = self.settings.hotkeys
            loop = asyncio.get_running_loop()

            def wrap(callback):
                def _on_activate() -> None:
                    loop.call_soon_threadsafe(callback)
                return _on_activate

            combos = {
                self._parse_hotkey(hk.pause): wrap(self._hotkey_pause),
                self._parse_hotkey(hk.allow): wrap(self._hotkey_allow),
                self._parse_hotkey(hk.deny): wrap(self._hotkey_deny),
                self._parse_hotkey(hk.release): wrap(self._hotkey_release),
            }
            listener = keyboard.GlobalHotKeys(combos)
            listener.start()
            self._hotkey_listener = listener
        except Exception:
            logger.exception("failed to register global hotkey listeners; continuing without them")
            self._hotkey_listener = None

    def _note_hotkey_fired(self) -> None:
        # Belt-and-suspenders on top of Presence's own chord filtering: the trailing key-up
        # events for this chord shouldn't read as "the human wants the desk" either.
        self._presence.agent_input_until(time.monotonic() + 0.3)

    def _hotkey_pause(self) -> None:
        self._note_hotkey_fired()
        self._toggle_pause()

    def _hotkey_allow(self) -> None:
        self._note_hotkey_fired()
        p = self._pending_consent
        if p is not None:
            self._finish_consent(p, "allow")

    def _hotkey_deny(self) -> None:
        self._note_hotkey_fired()
        p = self._pending_consent
        if p is not None:
            self._finish_consent(p, "deny")

    def _hotkey_release(self) -> None:
        self._note_hotkey_fired()
        self.human_release()

    def _toggle_pause(self) -> None:
        if self._paused:
            self.resume()
        else:
            self.pause("manual", manual=True)

    @staticmethod
    def _parse_hotkey(hotkey: str) -> str:
        """'ctrl+alt+shift+p' -> '<ctrl>+<alt>+<shift>+p' (pynput GlobalHotKeys syntax)."""
        mods = {"ctrl", "alt", "shift", "cmd"}
        alias = {"super": "cmd", "win": "cmd", "control": "ctrl"}
        parts = []
        for raw in hotkey.split("+"):
            token = alias.get(raw.strip().lower(), raw.strip().lower())
            parts.append(f"<{token}>" if token in mods else token)
        return "+".join(parts)

    # ---- auth ----

    def admin_token(self) -> str:
        return self.registry.admin_token()

    def authenticate(self, token: str | None) -> AgentInfo:
        if not token:
            raise HubError(401, "unauthorized", detail="missing bearer token")
        if token == self.registry.admin_token():
            return AgentInfo(agent_id="admin", name="admin", purpose="admin", is_admin=True)
        agent = self.registry.by_token(token)
        if agent is None:
            raise HubError(401, "unauthorized", detail="invalid or revoked token")
        return AgentInfo(agent_id=agent.agent_id, name=agent.name, purpose=agent.purpose, is_admin=False)

    def register(self, name: str, purpose: str) -> dict[str, Any]:
        agent = self.registry.register(name, purpose)
        return {"agent_id": agent.agent_id, "name": agent.name, "purpose": agent.purpose,
                "token": agent.token, "created_at": agent.created_at}

    def revoke(self, agent_id: str) -> None:
        self.registry.revoke(agent_id)

    # ---- mode ----

    def set_mode(self, mode: str) -> None:
        if mode not in _VALID_MODES:
            raise HubError(400, "invalid_mode", detail=f"mode must be one of {_VALID_MODES}")
        self.settings.mode = mode  # type: ignore[assignment]

    # ---- lease / consent (SPEC-v0.2 §2.1) ----

    async def acquire(self, agent: AgentInfo, ttl_s: int | None = None, wait_s: int = 0) -> dict[str, Any]:
        wait_s = max(0, wait_s)
        deadline = time.monotonic() + wait_s
        while True:
            result = await self._acquire_step(agent, ttl_s)
            if result is not None:
                self._update_overlay_holder()
                return result
            if time.monotonic() >= deadline:
                return self._pending_snapshot(agent)
            await asyncio.sleep(min(_CONSENT_POLL_INTERVAL_S, max(0.01, deadline - time.monotonic())))

    async def _acquire_step(self, agent: AgentInfo, ttl_s: int | None) -> dict[str, Any] | None:
        resolved = self._maybe_auto_allow_on_idle() or self._resolve_pending_consent_if_due()
        if resolved is not None:
            decision, req = resolved
            if req.agent_id == agent.agent_id and decision == "timeout":
                raise HubError(403, "denied", payload={
                    "status": "denied", "reason": "timeout", "retry_after_s": 60,
                })
            if req.agent_id == agent.agent_id and decision == "deny":
                raise HubError(403, "denied", payload={
                    "status": "denied", "reason": "human_denied",
                    "retry_after_s": int(self.settings.presence.deny_cooldown_s),
                })

        cooldown_until = self._deny_cooldowns.get(agent.agent_id)
        if cooldown_until is not None:
            if time.time() < cooldown_until:
                raise HubError(403, "denied", payload={
                    "status": "denied", "reason": "human_denied",
                    "retry_after_s": int(math.ceil(cooldown_until - time.time())),
                })
            self._deny_cooldowns.pop(agent.agent_id, None)

        if self._paused and self._pause_manual:
            raise HubError(403, "denied", payload={
                "status": "denied", "reason": "paused",
                "retry_after_s": _DEFAULT_RETRY_AFTER_S,
            })
        if self.settings.mode == "locked":
            raise HubError(403, "denied", payload={
                "status": "denied", "reason": "locked", "retry_after_s": _DEFAULT_RETRY_AFTER_S,
            })

        holder_id = self._lease.holder_agent_id()
        if holder_id is not None and holder_id != agent.agent_id:
            # Held by someone else -- queue as in v0.1. Consent is (re-)checked once we reach
            # the head of the queue, i.e. on a later iteration once holder_id is None/ours.
            await self._lease.acquire(agent.agent_id, agent.name, ttl_s=ttl_s, wait_s=0)
            return None

        if self._agent_may_take_desk(agent):
            return await self._lease.acquire(agent.agent_id, agent.name, ttl_s=ttl_s, wait_s=0)

        if self._pending_consent is None:
            self._create_consent_request(agent)
        return None

    def _agent_may_take_desk(self, agent: AgentInfo) -> bool:
        """Ask mode: only an explicit human decision (or a still-valid consent window) grants dibs.
        The human being idle is NOT permission - agents may not look at the screen unasked."""
        if self.settings.mode == "hands_off":
            return True
        consent_until = self._consent_windows.get(agent.agent_id)
        return consent_until is not None and time.time() < consent_until

    def _pending_snapshot(self, agent: AgentInfo) -> dict[str, Any]:
        holder_id = self._lease.holder_agent_id()
        if holder_id is not None and holder_id != agent.agent_id:
            snap = self._lease.snapshot()
            return {"status": "queued", "position": self._lease.queue_position(agent.agent_id) or 0,
                    "holder": snap["holder"]}
        p = self._pending_consent
        if p is not None:
            return {"status": "awaiting_consent", "request_id": p.request_id,
                    "expires_at": _iso(p.expires_at), "human": self._presence.snapshot()}
        # Resolved in the instant between the last _acquire_step and this snapshot -- tell the
        # caller to just ask again right away.
        return {"status": "queued", "position": 0, "holder": None}

    # ---- consent request lifecycle ----

    def _create_consent_request(self, agent: AgentInfo) -> None:
        now = time.time()
        timeout_s = self.settings.presence.consent_timeout_s
        req = ConsentRequest(
            request_id=secrets.token_urlsafe(8), agent_id=agent.agent_id, name=agent.name,
            purpose=agent.purpose, requested_at=now, expires_at=now + timeout_s,
        )
        self._pending_consent = req
        try:
            self.overlay.prompt_consent(req.request_id, agent.name, agent.purpose, timeout_s,
                                         self._on_overlay_consent_decision)
        except Exception:
            logger.exception("overlay.prompt_consent failed")

    def _resolve_pending_consent_if_due(self) -> tuple[str, ConsentRequest] | None:
        p = self._pending_consent
        if p is None or time.time() < p.expires_at:
            return None
        self._finish_consent(p, "timeout")
        return "timeout", p

    def _maybe_auto_allow_on_idle(self) -> tuple[str, ConsentRequest] | None:
        """Disabled on purpose (9/4): a pending request never resolves itself because the
        human walked away. It waits for a decision or times out to deny."""
        return None

    def _finish_consent(self, p: ConsentRequest, decision: str) -> None:
        if self._pending_consent is p:
            self._pending_consent = None
        now = time.time()
        if decision in ("allow", "human_idle"):
            self._consent_windows[p.agent_id] = now + self.settings.presence.consent_grant_s
        elif decision == "deny":
            self._deny_cooldowns[p.agent_id] = now + self.settings.presence.deny_cooldown_s
        self._consent_recent.append({
            "request_id": p.request_id, "agent_id": p.agent_id, "decision": decision, "at": _now_iso(),
        })
        del self._consent_recent[:-self._consent_recent_max]
        try:
            self.overlay.dismiss_consent(p.request_id)
        except Exception:
            logger.exception("overlay.dismiss_consent failed")

    def _on_overlay_consent_decision(self, request_id: str, allowed: bool) -> None:
        """Fires on the overlay's own thread (Tk) -- marshal onto the event loop."""
        decision = "allow" if allowed else "deny"
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._apply_consent_decision, request_id, decision)
        else:
            self._apply_consent_decision(request_id, decision)

    def _apply_consent_decision(self, request_id: str, decision: str) -> bool:
        p = self._pending_consent
        if p is None or p.request_id != request_id:
            return False
        self._finish_consent(p, decision)
        return True

    def decide_consent(self, request_id: str, decision: str) -> None:
        """POST /v1/admin/consent/{request_id} {decision}."""
        if decision not in ("allow", "deny"):
            raise HubError(400, "invalid_decision", detail="decision must be 'allow' or 'deny'")
        if not self._apply_consent_decision(request_id, decision):
            raise HubError(404, "no_pending_request", detail=f"no pending consent request {request_id!r}")

    # ---- lease: renew / release ----

    def renew(self, agent: AgentInfo, ttl_s: int | None = None) -> dict[str, Any]:
        result = self._lease.renew(agent.agent_id, ttl_s)
        if result is None:
            raise HubError(409, "not_holder")
        self._update_overlay_holder()
        return result

    def release(self, agent: AgentInfo, *, force: bool = False) -> None:
        if force and not agent.is_admin:
            raise HubError(403, "admin_required", detail="force release requires the admin token")
        holder_before = self._lease.holder_agent_id()
        self._lease.release(agent.agent_id, force=force)
        self._update_overlay_holder()
        if force and holder_before is not None and holder_before != agent.agent_id:
            try:
                self.overlay.notify(f"{holder_before}'s dibs were revoked")
            except Exception:
                logger.exception("overlay.notify failed")

    def _update_overlay_holder(self) -> None:
        holder = self._lease.snapshot()["holder"]
        holder_id = holder["agent_id"] if holder else None
        if holder_id == self._last_overlay_holder_id:
            return
        self._last_overlay_holder_id = holder_id
        try:
            if holder is None:
                self.overlay.set_holder(None)
            else:
                rec = self.registry.get(holder_id)
                purpose = rec.purpose if rec is not None else ""
                self.overlay.set_holder(holder["name"], purpose, holder["expires_at"])
        except Exception:
            logger.exception("overlay.set_holder failed")

    # ---- pause ----

    def pause(self, reason: str = "manual", *, manual: bool = True) -> None:
        self._paused = True
        self._pause_reason = reason
        self._pause_manual = manual
        self._paused_at = _now_iso()
        try:
            self.overlay.set_paused(reason)
        except Exception:
            logger.exception("overlay.set_paused failed")

    def resume(self) -> None:
        self._paused = False
        self._pause_reason = None
        self._pause_manual = True
        self._paused_at = None
        try:
            self.overlay.set_paused(None)
        except Exception:
            logger.exception("overlay.set_paused failed")

    # ---- human presence / takeover (SPEC-v0.2 §2.3) ----

    def _on_human_input(self) -> None:
        """Presence callback -- invoked from the pynput listener thread."""
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._handle_human_input_on_loop)
        else:
            self._handle_human_input_on_loop()

    def _handle_human_input_on_loop(self) -> None:
        if self._lease.holder_agent_id() is not None:
            self._human_takeover()

    def human_release(self) -> None:
        """Explicit human release: hotkey R or POST /v1/admin/release. Always pauses, even with
        nobody holding the desk right now (SPEC-v0.2 §2.3)."""
        self._human_takeover(force_pause=True)

    def _human_takeover(self, *, force_pause: bool = False) -> None:
        holder_id = self._lease.holder_agent_id()
        revoked = holder_id is not None
        if revoked:
            self._lease.release(holder_id, force=True)
            self._update_overlay_holder()
        if revoked or force_pause:
            self.pause("human_took_the_mouse", manual=False)
            self._audit.record(agent_id=holder_id or "human", action="human_takeover",
                                input_data={"holder": holder_id}, ok=True, error=None, duration_ms=0)
            try:
                self.overlay.show_human()
            except Exception:
                logger.exception("overlay.show_human failed")

    # ---- actions ----

    async def run(self, agent: AgentInfo, action: dict[str, Any], *, auto_lease: bool = False,
                  wait_s: int | None = None) -> ActionResult:
        action_name = action.get("action")
        start = time.monotonic()

        def record(ok: bool, error: str | None, result: ActionResult | None = None) -> None:
            duration_ms = int((time.monotonic() - start) * 1000)
            png = result.image.png if (result is not None and result.image is not None) else None
            self._audit.record(agent_id=agent.agent_id, action=str(action_name), input_data=action,
                               ok=ok, error=error, duration_ms=duration_ms, png=png)
            self.registry.touch(agent.agent_id)

        try:
            if action_name not in actions.ALL_ACTIONS:
                raise HubError(400, "unknown_action", detail=f"unknown action: {action_name!r}")
            read_only = actions.is_read_only(action_name)
            gated = action_name not in actions.FREE_ACTIONS   # everything but `wait` needs dibs

            if gated:
                if action_name == "launch" and not self.settings.allow_launch:
                    raise HubError(403, "launch_disabled")

                holder = self._lease.holder_agent_id()
                if holder != agent.agent_id:
                    if auto_lease:
                        wait = self.settings.auto_lease_wait_s if wait_s is None else wait_s
                        lease_result = await self.acquire(agent, wait_s=wait)
                        if lease_result["status"] == "awaiting_consent":
                            raise HubError(409, "lease_required", detail="awaiting human consent", payload={
                                "status": "awaiting_consent",
                                "request_id": lease_result["request_id"],
                                "expires_at": lease_result["expires_at"],
                                "human": lease_result.get("human"),
                            })
                        if lease_result["status"] != "granted":
                            raise HubError(409, "lease_required", payload={
                                "holder": lease_result.get("holder"),
                                "queue_position": lease_result.get("position"),
                            })
                    else:
                        detail = "lease_required"
                        payload: dict[str, Any] = {
                            "holder": self._lease.snapshot()["holder"],
                            "queue_position": self._lease.queue_position(agent.agent_id),
                        }
                        if (self._paused and self._pause_reason == "human_took_the_mouse"
                                and not self._pause_manual):
                            detail = "desk taken by human"
                            payload["human_active"] = self._presence.human_active()
                        raise HubError(409, "lease_required", detail=detail, payload=payload)

                # We hold the lease now (already did, or just acquired it via consent) -- but
                # the physical desk may still be paused (failsafe, a manual pause, or a
                # takeover pause that hasn't auto-resumed yet).
                if self._paused:
                    raise HubError(423, "paused", detail=self._pause_reason or "paused",
                                    payload={"reason": self._pause_reason})

            show_typing = not read_only and action_name in _TYPING_ACTIONS
            if not read_only:
                deadline = time.monotonic() + _estimate_action_duration(action) + 0.25
                self._presence.agent_input_until(deadline)
            if show_typing:
                try:
                    self.overlay.show_typing(True)
                except Exception:
                    logger.exception("overlay.show_typing failed")

            try:
                async with self._action_lock:
                    result = await asyncio.to_thread(
                        actions.run_action, action,
                        screen_index=self.settings.screen_index,
                        max_long_edge=self.settings.max_long_edge,
                        max_pixels=self.settings.max_pixels,
                        allow_launch=self.settings.allow_launch,
                    )
            except actions.ActionError as e:
                raise HubError(400, e.code, detail=e.detail) from e
            except desk.FailsafeTriggered as e:
                self.pause("failsafe", manual=False)
                raise HubError(423, "paused", detail="failsafe", payload={"reason": "failsafe"}) from e
            except desk.DeskError as e:
                raise HubError(500, "desk_error", detail=str(e)) from e
            finally:
                if not read_only:
                    self._presence.agent_input_until(time.monotonic() + 0.25)
                if show_typing:
                    try:
                        self.overlay.show_typing(False)
                    except Exception:
                        logger.exception("overlay.show_typing failed")

            if gated:
                self._lease.touch(agent.agent_id)
                if action_name in _CLICK_FLASH_BUTTON and result.data and "absolute" in result.data:
                    ax, ay = result.data["absolute"]
                    try:
                        self.overlay.flash_click(ax, ay, _CLICK_FLASH_BUTTON[action_name])
                    except Exception:
                        logger.exception("overlay.flash_click failed")

            record(True, None, result)
            return result
        except HubError as e:
            record(False, e.code)
            raise

    async def run_batch(self, agent: AgentInfo, actions: list[dict[str, Any]], *, auto_lease: bool = False,
                        wait_s: int | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        failed = False
        for act in actions:
            if failed:
                results.append({"ok": False, "error": "not_executed",
                                 "detail": "an earlier action in this batch failed"})
                continue
            try:
                result = await self.run(agent, act, auto_lease=auto_lease, wait_s=wait_s)
                results.append(result.to_dict())
            except HubError as e:
                failed = True
                entry = {"ok": False, "error": e.code, "detail": e.detail}
                entry.update(e.payload)
                results.append(entry)
        return results

    # ---- introspection ----

    def state(self) -> dict[str, Any]:
        lease_snap = self._lease.snapshot()
        holder_id = lease_snap["holder"]["agent_id"] if lease_snap["holder"] else None
        agents_list = []
        for a in self.registry.list():
            agents_list.append({
                "agent_id": a.agent_id, "name": a.name, "purpose": a.purpose,
                "created_at": a.created_at, "last_seen": a.last_seen,
                "action_count": a.action_count, "revoked": a.revoked,
                "holding": a.agent_id == holder_id,
            })
        uptime = time.monotonic() - self._start_time if self._start_time is not None else 0.0
        now = time.time()
        return {
            "version": __version__,
            "uptime_s": int(uptime),
            "paused": self._paused,
            "pause_reason": self._pause_reason,
            "paused_at": self._paused_at,
            "lease": lease_snap,
            "agents": agents_list,
            "display": self.display(),
            "stats": self._audit.stats(),
            "mode": self.settings.mode,
            "human": self._presence.snapshot(),
            "consent": {
                "pending": self._pending_consent.to_dict() if self._pending_consent else None,
                "windows": [
                    {"agent_id": aid, "consent_until": _iso(until)}
                    for aid, until in self._consent_windows.items() if until > now
                ],
                "recent": list(self._consent_recent),
            },
            "config": {
                "host": self.settings.host,
                "port": self.settings.port,
                "allow_launch": self.settings.allow_launch,
                "mode": self.settings.mode,
                "overlay": self.settings.overlay.enabled,
            },
        }

    def display(self) -> dict[str, Any]:
        screens = desk.list_screens()
        default_index = self.settings.screen_index
        if default_index is None:
            default_index = next((s.index for s in screens if s.primary), 0)
        default_screen = next((s for s in screens if s.index == default_index),
                               screens[0] if screens else None)
        screenshot_info: dict[str, Any] = {
            "width": 0, "height": 0, "scale": 1.0,
            "max_long_edge": self.settings.max_long_edge, "max_pixels": self.settings.max_pixels,
        }
        if default_screen is not None:
            scale = actions.scale_for(default_screen, self.settings.max_long_edge, self.settings.max_pixels)
            screenshot_info["width"] = round(default_screen.width * scale)
            screenshot_info["height"] = round(default_screen.height * scale)
            screenshot_info["scale"] = scale
        return {
            "screens": [s.to_dict() for s in screens],
            "default_screen": default_index,
            "screenshot": screenshot_info,
        }

    def audit(self, limit: int = 50, agent_id: str | None = None) -> list[dict[str, Any]]:
        rows = self._audit.recent(limit=limit, agent_id=agent_id)
        for row in rows:
            if row["agent_id"] == "admin":
                row["agent_name"] = "admin"
            else:
                a = self.registry.get(row["agent_id"])
                row["agent_name"] = a.name if a is not None else row["agent_id"]
        return rows

    def screenshot_path(self, shot_id: int) -> str | None:
        return self._audit.screenshot_path(shot_id)
