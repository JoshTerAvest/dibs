"""SQLite audit log + rolling screenshot files. Owner: hub agent.

DB at `<data_dir>/dibs.db` (WAL mode), table `actions` exactly per SPEC. Screenshot/zoom
results are written to `<data_dir>/shots/<id>.png` with a rolling cleanup that keeps only the
newest `keep_screenshots` files (older screenshot files are deleted; the audit rows themselves
are never deleted). `agent_name` is intentionally NOT stored here (the schema in SPEC has no
such column) — the caller (Hub) enriches rows with the name from the registry.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    input TEXT,
    ok INTEGER NOT NULL,
    error TEXT,
    duration_ms INTEGER,
    screenshot_path TEXT
)
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "dibs.db"
        self.shots_dir = self.data_dir / "shots"
        self.keep_screenshots = 200
        self._conn: sqlite3.Connection | None = None

    def open(self, keep_screenshots: int = 200) -> None:
        self.keep_screenshots = keep_screenshots
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "AuditLog.open() not called"
        return self._conn

    def record(
        self,
        *,
        agent_id: str,
        action: str,
        input_data: dict[str, Any] | None,
        ok: bool,
        error: str | None,
        duration_ms: int,
        png: bytes | None = None,
    ) -> int:
        ts = _now_iso()
        cur = self.conn.execute(
            "INSERT INTO actions (ts, agent_id, action, input, ok, error, duration_ms, screenshot_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                agent_id,
                action,
                json.dumps(input_data) if input_data is not None else None,
                1 if ok else 0,
                error,
                duration_ms,
                None,
            ),
        )
        row_id = cur.lastrowid
        if png is not None:
            shot_path = self.shots_dir / f"{row_id}.png"
            shot_path.write_bytes(png)
            self.conn.execute(
                "UPDATE actions SET screenshot_path = ? WHERE id = ?", (str(shot_path), row_id)
            )
        self.conn.commit()
        if png is not None:
            self._cleanup_screenshots()
        return row_id

    def _cleanup_screenshots(self) -> None:
        rows = self.conn.execute(
            "SELECT id, screenshot_path FROM actions WHERE screenshot_path IS NOT NULL ORDER BY id DESC"
        ).fetchall()
        stale = rows[self.keep_screenshots :]
        if not stale:
            return
        for row_id, path in stale:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            self.conn.execute("UPDATE actions SET screenshot_path = NULL WHERE id = ?", (row_id,))
        self.conn.commit()

    def recent(self, limit: int = 50, agent_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT id, ts, agent_id, action, input, ok, error, duration_ms, screenshot_path "
            "FROM actions"
        )
        params: list[Any] = []
        if agent_id:
            query += " WHERE agent_id = ?"
            params.append(agent_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        out = []
        for row_id, ts, aid, action, input_json, ok, error, duration_ms, shot_path in rows:
            out.append(
                {
                    "id": row_id,
                    "ts": ts,
                    "agent_id": aid,
                    "action": action,
                    "input": json.loads(input_json) if input_json else None,
                    "ok": bool(ok),
                    "error": error,
                    "duration_ms": duration_ms,
                    "screenshot_url": f"/v1/shots/{row_id}.png" if shot_path else None,
                }
            )
        return out

    def stats(self) -> dict[str, int]:
        total = self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        failed = self.conn.execute("SELECT COUNT(*) FROM actions WHERE ok = 0").fetchone()[0]
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        last_5m = self.conn.execute(
            "SELECT COUNT(*) FROM actions WHERE ts >= ?", (cutoff,)
        ).fetchone()[0]
        return {"actions_total": total, "actions_failed": failed, "actions_last_5m": last_5m}

    def screenshot_path(self, shot_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT screenshot_path FROM actions WHERE id = ?", (shot_id,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        return None
