"""Tests for dibs/lease.py: single holder, FIFO queue, TTL, sweeper.

Pytest-asyncio runs in `auto` mode (see pyproject.toml), so plain `async def test_...`
functions are collected and run as coroutines without an extra decorator.
"""

from __future__ import annotations

import asyncio

import dibs.lease as lease_mod
from dibs.lease import LeaseManager


async def test_acquire_grants_when_free():
    lm = LeaseManager(default_ttl_s=60, max_ttl_s=600)
    result = await lm.acquire("agent-a", "Agent A")
    assert result["status"] == "granted"
    assert result["agent_id"] == "agent-a"
    assert result["lease_id"]
    assert result["expires_at"]
    assert lm.holder_agent_id() == "agent-a"


async def test_acquire_by_current_holder_renews():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A", ttl_s=5)
    second = await lm.acquire("agent-a", "Agent A", ttl_s=5)
    assert second["status"] == "granted"
    assert second["agent_id"] == "agent-a"


async def test_second_agent_queued_when_held():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")
    result = await lm.acquire("agent-b", "Agent B", wait_s=0)
    assert result["status"] == "queued"
    assert result["position"] == 1
    assert result["holder"]["agent_id"] == "agent-a"
    assert result["holder"]["name"] == "Agent A"


async def test_queue_position_increments():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")
    r_b = await lm.acquire("agent-b", "Agent B", wait_s=0)
    r_c = await lm.acquire("agent-c", "Agent C", wait_s=0)
    assert r_b["position"] == 1
    assert r_c["position"] == 2


async def test_renew_requires_holder():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")

    ok = lm.renew("agent-a", ttl_s=10)
    assert ok is not None
    assert ok["status"] == "granted"

    not_holder = lm.renew("agent-b", ttl_s=10)
    assert not_holder is None


async def test_release_promotes_queue_head():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")
    await lm.acquire("agent-b", "Agent B", wait_s=0)
    assert lm.holder_agent_id() == "agent-a"

    lm.release("agent-a")

    assert lm.holder_agent_id() == "agent-b"
    assert lm.queue_position("agent-b") is None


async def test_release_by_non_holder_is_noop():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")
    lm.release("agent-b")  # not holder, not force
    assert lm.holder_agent_id() == "agent-a"


async def test_force_release_evicts_any_holder():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")
    lm.release("admin", force=True)
    assert lm.holder_agent_id() is None


async def test_release_with_no_holder_is_noop():
    lm = LeaseManager(60, 600)
    lm.release("nobody")  # must not raise


async def test_holder_expires_and_sweep_promotes_queue():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A", ttl_s=1)
    await lm.acquire("agent-b", "Agent B", wait_s=0)

    await asyncio.sleep(1.1)
    lm.sweep()

    assert lm.holder_agent_id() == "agent-b"


async def test_holder_expires_without_queue():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A", ttl_s=1)
    await asyncio.sleep(1.1)
    # holder_agent_id() itself lazily checks expiry -- no sweep() call needed
    assert lm.holder_agent_id() is None


async def test_touch_slides_expiry_only_for_holder():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A", ttl_s=1)
    lm.touch("agent-a", ttl_s=5)
    await asyncio.sleep(1.1)
    # would have expired at the original 1s ttl; touch() should have slid it to 5s
    assert lm.holder_agent_id() == "agent-a"

    lm.touch("agent-b")  # not the holder -- must be a silent no-op
    assert lm.holder_agent_id() == "agent-a"


async def test_stale_queue_entry_dropped_by_sweep(monkeypatch):
    monkeypatch.setattr(lease_mod, "QUEUE_POLL_TIMEOUT_S", 0.05)
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")
    await lm.acquire("agent-b", "Agent B", wait_s=0)

    await asyncio.sleep(0.1)
    lm.sweep()

    assert lm.queue_position("agent-b") is None


async def test_wait_s_grants_when_released_before_timeout():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")

    async def release_soon():
        await asyncio.sleep(0.1)
        lm.release("agent-a")

    release_task = asyncio.create_task(release_soon())
    result = await lm.acquire("agent-b", "Agent B", wait_s=2)
    await release_task

    assert result["status"] == "granted"
    assert result["agent_id"] == "agent-b"


async def test_wait_s_times_out_when_still_held():
    lm = LeaseManager(60, 600)
    await lm.acquire("agent-a", "Agent A")
    result = await lm.acquire("agent-b", "Agent B", wait_s=0.2)
    assert result["status"] == "queued"
    assert result["holder"]["agent_id"] == "agent-a"


async def test_ttl_clamped_to_max():
    lm = LeaseManager(default_ttl_s=60, max_ttl_s=120)
    result = await lm.acquire("agent-a", "Agent A", ttl_s=99999)
    # expires_at should be close to now + max_ttl_s (120s), not 99999s
    from datetime import datetime, timezone

    expires = datetime.fromisoformat(result["expires_at"])
    delta = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 100 < delta <= 121


async def test_snapshot_shape():
    lm = LeaseManager(60, 600)
    assert lm.snapshot() == {"holder": None, "queue": []}

    await lm.acquire("agent-a", "Agent A")
    snap = lm.snapshot()
    assert snap["holder"]["agent_id"] == "agent-a"
    assert set(snap["holder"].keys()) == {
        "agent_id",
        "name",
        "lease_id",
        "acquired_at",
        "expires_at",
    }
    assert snap["queue"] == []

    await lm.acquire("agent-b", "Agent B", wait_s=0)
    snap = lm.snapshot()
    assert len(snap["queue"]) == 1
    assert set(snap["queue"][0].keys()) == {"agent_id", "name", "since"}
