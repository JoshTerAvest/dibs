"""Agent registry + admin token, persisted under data_dir. Owner: hub agent.

Agents live in `<data_dir>/agents.json`; the admin token lives in `<data_dir>/secrets.json`
(generated once on first run). Both files are plain JSON written atomically (temp file +
os.replace) so a crash mid-write can't corrupt them. Tokens are stored in plaintext — this
is a local trust boundary (the files live in a gitignored `data/` dir on the operator's own
machine), not a multi-tenant secret store; see the final report for the tradeoff.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import string
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "agent"


@dataclass
class Agent:
    agent_id: str
    name: str
    purpose: str
    token: str
    created_at: str
    last_seen: str | None = None
    action_count: int = 0
    revoked: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        d = {
            "agent_id": self.agent_id,
            "name": self.name,
            "purpose": self.purpose,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "action_count": self.action_count,
            "revoked": self.revoked,
        }
        if include_token:
            d["token"] = self.token
        return d


class Registry:
    """Agents + admin token, persisted to `<data_dir>/agents.json` and `secrets.json`."""

    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)
        self.agents_path = self.data_dir / "agents.json"
        self.secrets_path = self.data_dir / "secrets.json"
        self._agents: dict[str, Agent] = {}
        self._tokens: dict[str, str] = {}  # token -> agent_id
        self._admin_token: str | None = None
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

        if self.agents_path.is_file():
            try:
                raw = json.loads(self.agents_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            for agent_id, rec in (raw or {}).items():
                agent = Agent(
                    agent_id=agent_id,
                    name=rec.get("name", agent_id),
                    purpose=rec.get("purpose", ""),
                    token=rec["token"],
                    created_at=rec.get("created_at") or _now_iso(),
                    last_seen=rec.get("last_seen"),
                    action_count=rec.get("action_count", 0),
                    revoked=rec.get("revoked", False),
                )
                self._agents[agent_id] = agent
                self._tokens[agent.token] = agent_id

        if self.secrets_path.is_file():
            try:
                raw = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            self._admin_token = raw.get("admin_token")

        if not self._admin_token:
            self._admin_token = secrets.token_urlsafe(32)
            self._save_secrets()

    def _save_agents(self) -> None:
        raw = {a.agent_id: {
            "name": a.name,
            "purpose": a.purpose,
            "token": a.token,
            "created_at": a.created_at,
            "last_seen": a.last_seen,
            "action_count": a.action_count,
            "revoked": a.revoked,
        } for a in self._agents.values()}
        _atomic_write_json(self.agents_path, raw)

    def _save_secrets(self) -> None:
        _atomic_write_json(self.secrets_path, {"admin_token": self._admin_token})

    # ---- admin token ----

    def admin_token(self) -> str:
        assert self._admin_token is not None
        return self._admin_token

    # ---- agents ----

    def register(self, name: str, purpose: str) -> Agent:
        base_slug = _slugify(name)
        while True:
            suffix = "".join(secrets.choice(string.hexdigits.lower()[:16]) for _ in range(4))
            agent_id = f"{base_slug}-{suffix}"
            if agent_id not in self._agents:
                break
        agent = Agent(
            agent_id=agent_id,
            name=name,
            purpose=purpose,
            token=secrets.token_urlsafe(32),
            created_at=_now_iso(),
        )
        self._agents[agent_id] = agent
        self._tokens[agent.token] = agent_id
        self._save_agents()
        return agent

    def revoke(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        agent.revoked = True
        self._tokens.pop(agent.token, None)
        self._save_agents()

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def by_token(self, token: str) -> Agent | None:
        agent_id = self._tokens.get(token)
        if agent_id is None:
            return None
        agent = self._agents.get(agent_id)
        if agent is None or agent.revoked:
            return None
        return agent

    def touch(self, agent_id: str) -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        agent.last_seen = _now_iso()
        agent.action_count += 1
        self._save_agents()

    def list(self) -> list[Agent]:
        return sorted(self._agents.values(), key=lambda a: a.created_at)
