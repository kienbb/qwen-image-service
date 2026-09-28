"""SQLite (WAL) storage with ordered in-code migrations.

One connection shared by the process behind a lock: every statement is tiny, so calling it from the event loop
is cheaper than a thread hop. Rows come back as dicts; JSON columns are (de)serialised by the helpers.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1: core schema (DESIGN §4)
    """
    CREATE TABLE nodes(id TEXT PRIMARY KEY, name TEXT, public_url TEXT, pubkey TEXT, created_at REAL);

    CREATE TABLE users(
        id TEXT PRIMARY KEY, email TEXT UNIQUE, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer', disabled INTEGER NOT NULL DEFAULT 0, must_change_pw INTEGER NOT NULL DEFAULT 0,
        failed_logins INTEGER NOT NULL DEFAULT 0, locked_until REAL, created_at REAL, last_login_at REAL);
    CREATE TABLE sessions(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf TEXT NOT NULL, ip TEXT, ua TEXT,
        created_at REAL, last_seen REAL, expires_at REAL, revoked INTEGER NOT NULL DEFAULT 0);

    CREATE TABLE api_keys(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, owner_user_id TEXT,
        scopes TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'interactive', max_pending INTEGER, rpm INTEGER,
        daily_images INTEGER, allowed_models TEXT, created_at REAL, last_used_at REAL, expires_at REAL, revoked_at REAL);

    CREATE TABLE jobs(
        id TEXT PRIMARY KEY, api_key_id TEXT, user_id TEXT, origin_node TEXT, exec_node TEXT, kind TEXT NOT NULL,
        spec TEXT NOT NULL, preset TEXT, status TEXT NOT NULL, priority TEXT NOT NULL, model_affinity TEXT,
        cost_s REAL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT, progress TEXT, degraded INTEGER DEFAULT 0,
        created_at REAL, started_at REAL, finished_at REAL, idempotency_key TEXT, webhook_url TEXT, retention_hours REAL);
    CREATE UNIQUE INDEX jobs_idem ON jobs(api_key_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
    CREATE INDEX jobs_status ON jobs(status, created_at);
    CREATE INDEX jobs_key ON jobs(api_key_id, created_at);
    CREATE TABLE job_events(job_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL, type TEXT, data TEXT,
        PRIMARY KEY(job_id, seq));

    CREATE TABLE blobs(sha256 TEXT PRIMARY KEY, bytes INTEGER, path TEXT, refcount INTEGER NOT NULL DEFAULT 0, created_at REAL);
    CREATE TABLE files(
        id TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES blobs(sha256), bytes INTEGER, mime TEXT, width INTEGER,
        height INTEGER, has_alpha INTEGER, purpose TEXT, owner_key_id TEXT, job_id TEXT, name TEXT, created_at REAL, expires_at REAL);
    CREATE INDEX files_expiry ON files(expires_at);
    CREATE INDEX files_job ON files(job_id);

    CREATE TABLE prompt_cache(key TEXT PRIMARY KEY, request TEXT, prompt TEXT, ratio TEXT, created_at REAL, hits INTEGER DEFAULT 0);
    CREATE TABLE settings(scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_by TEXT, updated_at REAL,
        PRIMARY KEY(scope, key));
    CREATE TABLE audit(id TEXT PRIMARY KEY, ts REAL, actor TEXT, action TEXT, target TEXT, data TEXT, ip TEXT);
    CREATE INDEX audit_ts ON audit(ts);
    CREATE TABLE stats_minute(ts INTEGER PRIMARY KEY, jobs_done INTEGER DEFAULT 0, jobs_failed INTEGER DEFAULT 0,
        gpu_s REAL DEFAULT 0, queue_depth INTEGER DEFAULT 0, images INTEGER DEFAULT 0);
    """,
]

JSON_COLS = {"spec", "error", "result", "progress", "scopes", "allowed_models", "data", "value"}


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> int:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
                self.conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version={i};\nCOMMIT;")
            return len(MIGRATIONS)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ---------- helpers ----------
    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_COLS & d.keys():
            if isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    @staticmethod
    def _enc(v: Any) -> Any:
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, [self._enc(p) for p in params])

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute(sql, [self._enc(p) for p in params]).fetchone())

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.conn.execute(sql, [self._enc(p) for p in params]).fetchall()]

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        with self.lock:
            r = self.conn.execute(sql, [self._enc(p) for p in params]).fetchone()
            return r[0] if r else None

    def insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.execute(f"INSERT INTO {table}({cols}) VALUES({marks})", list(row.values()))

    def update(self, table: str, key: str, key_value: Any, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*fields.values(), key_value])

    def backup(self, dest: str | Path) -> None:
        with self.lock:
            target = sqlite3.connect(str(dest))
            self.conn.backup(target)
            target.close()

    def audit(self, actor: str, action: str, target: str = "", data: dict | None = None, ip: str = "") -> None:
        from .ids import new_id
        self.insert("audit", {"id": new_id("aud"), "ts": time.time(), "actor": actor, "action": action,
                              "target": target, "data": data or {}, "ip": ip})
