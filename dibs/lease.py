"""Exclusive input lease: single holder, FIFO queue, TTL, sliding renewal. Owner: hub agent.

Pure asyncio, no threads. asyncio is single-threaded and cooperative: a coroutine only yields
control at an `await`, so every method here that doesn't `await` (renew/release/touch/sweep, and
the fast paths of acquire) mutates `_holder`/`_queue` atomically with respect to every other
coroutine on the loop -- no `asyncio.Lock` needed. `acquire()` is `async` only because a queued
caller with `wait_s > 0` needs to block on an `asyncio.Event` until it's granted or times out;
the state is always re-validated synchronously after that single await point.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

QUEUE_POLL_TIMEOUT_S = 30.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class Holder:
    agent_id: str
    name: str
    lease_id: str
    acquired_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "lease_id": self.lease_id,
            "acquired_at": _iso(self.acquired_at),
            "expires_at": _iso(self.expires_at),
        }


@dataclass
class QueueEntry:
    agent_id: str
    name: str
    enqueued_at: float
    since_poll: float
    ttl_requested: float
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "name": self.name, "since": _iso(self.enqueued_at)}


class LeaseManager:
    def __init__(self, default_ttl_s: int, max_ttl_s: int):
        self.default_ttl_s = default_ttl_s
        self.max_ttl_s = max_ttl_s
        self._holder: Holder | None = None
        self._queue: list[QueueEntry] = []

    # ---- helpers ----

    def _clamp_ttl(self, ttl_s: int | None) -> float:
        ttl = self.default_ttl_s if ttl_s is None else ttl_s
        return float(max(1, min(ttl, self.max_ttl_s)))

    def _is_expired(self, holder: Holder, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return holder.expires_at <= now

    def _expire_holder_if_stale(self) -> None:
        if self._holder is not None and self._is_expired(self._holder):
            self._holder = None

    def _find_entry(self, agent_id: str) -> QueueEntry | None:
        for entry in self._queue:
            if entry.agent_id == agent_id:
                return entry
        return None

    def _position(self, agent_id: str) -> int:
        for i, entry in enumerate(self._queue):
            if entry.agent_id == agent_id:
                return i + 1
        return len(self._queue)

    def _grant(self, agent_id: str, name: str, ttl: float) -> Holder:
        now = time.time()
        holder = Holder(
            agent_id=agent_id,
            name=name,
            lease_id=secrets.token_urlsafe(8),
            acquired_at=now,
            expires_at=now + ttl,
        )
        self._holder = holder
        entry = self._find_entry(agent_id)
        if entry is not None:
            self._queue.remove(entry)
            entry.event.set()
        return holder

    @staticmethod
    def _granted_dict(holder: Holder) -> dict[str, Any]:
        return {
            "status": "granted",
            "lease_id": holder.lease_id,
            "agent_id": holder.agent_id,
            "expires_at": _iso(holder.expires_at),
        }

    def _queued_dict(self, agent_id: str) -> dict[str, Any]:
        holder = self._holder
        return {
            "status": "queued",
            "position": self._position(agent_id),
            "holder": holder.to_dict() if holder else None,
        }

    def _promote_head(self) -> None:
        """Holder is empty: grant to the first non-stale queue entry (dropping stale ones)."""
        now = time.time()
        while self._queue:
            entry = self._queue[0]
            if now - entry.since_poll > QUEUE_POLL_TIMEOUT_S:
                self._queue.pop(0)
                continue
            self._queue.pop(0)
            self._grant(entry.agent_id, entry.name, entry.ttl_requested)
            return

    # ---- public API ----

    async def acquire(self, agent_id: str, name: str, ttl_s: int | None = None,
                       wait_s: float = 0) -> dict[str, Any]:
        ttl = self._clamp_ttl(ttl_s)
        wait_s = max(0.0, float(wait_s))

        self._expire_holder_if_stale()
        if self._holder is None:
            return self._granted_dict(self._grant(agent_id, name, ttl))
        if self._holder.agent_id == agent_id:
            return self._granted_dict(self._grant(agent_id, name, ttl))

        now = time.time()
        entry = self._find_entry(agent_id)
        if entry is None:
            entry = QueueEntry(agent_id=agent_id, name=name, enqueued_at=now,
                                since_poll=now, ttl_requested=ttl)
            self._queue.append(entry)
        else:
            entry.since_poll = now
            entry.ttl_requested = ttl
        if wait_s <= 0:
            return self._queued_dict(agent_id)

        event = entry.event
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            pass

        if self._holder is not None and self._holder.agent_id == agent_id and not self._is_expired(self._holder):
            return self._granted_dict(self._holder)
        # still queued (or timed out) -- refresh since_poll so we don't get swept for
        # having "not polled", and re-enqueue if we were dropped while waiting.
        now = time.time()
        entry = self._find_entry(agent_id)
        if entry is None:
            entry = QueueEntry(agent_id=agent_id, name=name, enqueued_at=now,
                                since_poll=now, ttl_requested=ttl)
            self._queue.append(entry)
        else:
            entry.since_poll = now
        return self._queued_dict(agent_id)

    def renew(self, agent_id: str, ttl_s: int | None = None) -> dict[str, Any] | None:
        ttl = self._clamp_ttl(ttl_s)
        self._expire_holder_if_stale()
        if self._holder is None or self._holder.agent_id != agent_id:
            return None
        return self._granted_dict(self._grant(agent_id, self._holder.name, ttl))

    def touch(self, agent_id: str, ttl_s: int | None = None) -> None:
        """Sliding renewal after a successful input action; silently a no-op if not holder."""
        ttl = self._clamp_ttl(ttl_s)
        self._expire_holder_if_stale()
        if self._holder is not None and self._holder.agent_id == agent_id:
            self._holder.expires_at = time.time() + ttl

    def release(self, agent_id: str, *, force: bool = False) -> None:
        if self._holder is None:
            return
        if force or self._holder.agent_id == agent_id:
            self._holder = None
            self._promote_head()

    def sweep(self) -> None:
        self._expire_holder_if_stale()
        if self._holder is None:
            self._promote_head()
        now = time.time()
        stale = [e for e in self._queue if now - e.since_poll > QUEUE_POLL_TIMEOUT_S]
        for entry in stale:
            self._queue.remove(entry)

    def holder_agent_id(self) -> str | None:
        self._expire_holder_if_stale()
        return self._holder.agent_id if self._holder else None

    def queue_position(self, agent_id: str) -> int | None:
        entry = self._find_entry(agent_id)
        return self._position(agent_id) if entry else None

    def snapshot(self) -> dict[str, Any]:
        self._expire_holder_if_stale()
        return {
            "holder": self._holder.to_dict() if self._holder else None,
            "queue": [e.to_dict() for e in self._queue],
        }
