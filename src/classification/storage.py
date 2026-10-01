"""SQLite 持久化层。

并发与重启安全的落点：
- 所有写事务以 BEGIN IMMEDIATE 立即获取写锁，写冲突抛 SQLITE_BUSY，
  由服务层转为 409，而非静默产生第二份决定；
- 业务唯一性由数据库约束保证（案件编号、幂等键、专家分配、证据版本），
  不依赖应用层先查后写；
- 证据、评估结果、签署快照均为追加式版本行，历史永不被覆盖，
  材料补交生成新版本而签署结论仍指向签署时的版本。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS rulesets (
    ruleset_version TEXT PRIMARY KEY,
    body             TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    effective_from   TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    case_no         TEXT NOT NULL UNIQUE,
    institution     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('draft','accepted','in_review','signed','reconsidering','closed')),
    ruleset_version  TEXT NOT NULL REFERENCES rulesets(ruleset_version),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);

CREATE TABLE IF NOT EXISTS experts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    org        TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, org)
);

CREATE TABLE IF NOT EXISTS case_experts (
    case_id    TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    expert_id  TEXT NOT NULL REFERENCES experts(id),
    role       TEXT NOT NULL CHECK (role IN ('reviewer','recused')),
    assigned_at TEXT NOT NULL,
    recused_at TEXT,
    PRIMARY KEY (case_id, expert_id)
);

-- 专家与院校同单位的回避登记：同单位不得成为该案评审人。
CREATE TABLE IF NOT EXISTS expert_conflicts (
    expert_id     TEXT NOT NULL REFERENCES experts(id),
    institution   TEXT NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (expert_id, institution)
);

CREATE TABLE IF NOT EXISTS evidence_versions (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    body            TEXT NOT NULL,
    submitted_by    TEXT NOT NULL,
    supersedes      TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    UNIQUE (case_id, version)
);

CREATE TABLE IF NOT EXISTS reviews (
    id                  TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    expert_id           TEXT NOT NULL REFERENCES experts(id),
    evidence_version    INTEGER NOT NULL,
    opinion             TEXT NOT NULL CHECK (opinion IN ('support','oppose','abstain')),
    comment             TEXT NOT NULL DEFAULT '',
    idempotency_key     TEXT NOT NULL UNIQUE,
    created_at          TEXT NOT NULL,
    UNIQUE (case_id, expert_id, evidence_version)
);

CREATE TABLE IF NOT EXISTS decisions (
    id                TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL UNIQUE REFERENCES cases(id),
    evidence_version  INTEGER NOT NULL,
    snapshot          TEXT NOT NULL,
    valid_from        TEXT NOT NULL,
    valid_to          TEXT NOT NULL,
    signed_by         TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconsiderations (
    id               TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES cases(id),
    requested_by     TEXT NOT NULL,
    reason           TEXT NOT NULL,
    replay_report    TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('open','resolved')),
    idempotency_key  TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reconsider_case ON reconsiderations(case_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT NOT NULL,
    actor      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_case ON events(case_id, id);
"""


class Database:
    """SQLite 访问对象。

    文件库为每个线程提供独立连接（check_same_thread=False 下的安全做法），
    共享同一个 WAL 文件；内存库主要供单线程测试使用。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if self.path == ":memory:":
            conn.execute("PRAGMA journal_mode=MEMORY")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 在首条语句前拿到保留锁。"""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def initialize(self) -> None:
        """建表（IF NOT EXISTS）。文件库全局执行一次即可对所有线程可见。"""
        self.conn.executescript(SCHEMA)

    def record_event(self, conn: sqlite3.Connection, case_id: str, event_type: str,
                     payload: dict[str, Any], actor: str, created_at: str) -> None:
        from .core import canonical
        conn.execute(
            "INSERT INTO events (case_id, event_type, payload, actor, created_at)"
            " VALUES (?,?,?,?,?)",
            (case_id, event_type, canonical(payload), actor, created_at),
        )
