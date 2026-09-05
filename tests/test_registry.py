"""Tests for dibs/registry.py: agents + admin token, persisted to data_dir."""
from __future__ import annotations

from dibs.registry import Registry


def test_register_creates_agent_with_slug_and_token(tmp_path):
    reg = Registry(tmp_path / "data")
    agent = reg.register("Claude Code", "automated testing")

    assert agent.agent_id.startswith("claude-code-")
    suffix = agent.agent_id.rsplit("-", 1)[-1]
    assert len(suffix) == 4
    assert all(c in "0123456789abcdef" for c in suffix)

    assert agent.name == "Claude Code"
    assert agent.purpose == "automated testing"
    assert agent.token and len(agent.token) >= 32
    assert agent.revoked is False
    assert agent.action_count == 0
    assert agent.last_seen is None
    assert agent.created_at


def test_admin_token_generated_once_and_persists(tmp_path):
    data_dir = tmp_path / "data"
    token1 = Registry(data_dir).admin_token()
    token2 = Registry(data_dir).admin_token()
    assert token1 == token2
    assert len(token1) >= 32


def test_agents_persist_across_reloads(tmp_path):
    data_dir = tmp_path / "data"
    agent = Registry(data_dir).register("agent-a", "purpose-a")

    reloaded = Registry(data_dir).get(agent.agent_id)
    assert reloaded is not None
    assert reloaded.token == agent.token
    assert reloaded.name == "agent-a"
    assert reloaded.purpose == "purpose-a"


def test_by_token_lookup(tmp_path):
    reg = Registry(tmp_path / "data")
    agent = reg.register("agent-a", "purpose")

    found = reg.by_token(agent.token)
    assert found is not None
    assert found.agent_id == agent.agent_id
    assert reg.by_token("not-a-real-token") is None


def test_revoke_disables_token_lookup(tmp_path):
    reg = Registry(tmp_path / "data")
    agent = reg.register("agent-a", "purpose")

    reg.revoke(agent.agent_id)

    assert reg.by_token(agent.token) is None
    reloaded = reg.get(agent.agent_id)
    assert reloaded is not None
    assert reloaded.revoked is True


def test_revoke_unknown_agent_is_noop(tmp_path):
    reg = Registry(tmp_path / "data")
    reg.revoke("does-not-exist-0000")  # must not raise


def test_touch_updates_last_seen_and_action_count(tmp_path):
    reg = Registry(tmp_path / "data")
    agent = reg.register("agent-a", "purpose")
    assert agent.last_seen is None
    assert agent.action_count == 0

    reg.touch(agent.agent_id)
    reg.touch(agent.agent_id)

    updated = reg.get(agent.agent_id)
    assert updated.action_count == 2
    assert updated.last_seen is not None


def test_unique_agent_ids_for_same_name(tmp_path):
    reg = Registry(tmp_path / "data")
    a1 = reg.register("dup", "p")
    a2 = reg.register("dup", "p")
    assert a1.agent_id != a2.agent_id


def test_list_returns_all_in_creation_order(tmp_path):
    reg = Registry(tmp_path / "data")
    a1 = reg.register("first", "p")
    a2 = reg.register("second", "p")
    ids = [a.agent_id for a in reg.list()]
    assert ids == [a1.agent_id, a2.agent_id]
