"""SQLite 结构定义与连接管理。

并发与重启安全的物理基础：

- 所有写操作经 ``BEGIN IMMEDIATE`` 获取保留写锁，配合应用层幂等表，
  并发送审与重复提交不会产生两份决定。
- 每个写库连接打开 ``WAL`` 与外键约束；WAL 保证读写互不长期阻塞，
  进程崩溃后已提交事务仍可恢复。
- 已签署结论只以哈希引用证据版本与规则版本，原始行永不更新删除，
  只追加新版本，因此重启与规则换版都不会改写历史。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 版本化分类规则包：规则只追加，换版不覆盖旧版，已签结论永远可复算。
CREATE TABLE IF NOT EXISTS rule_packages (
    package_version TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    rules_json      TEXT NOT NULL,          -- canonical JSON
    content_hash    TEXT NOT NULL UNIQUE,
    published_at    TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 0
);

-- 高校申请（案卷）。业务幂等由自然键 applicant_code 唯一约束兜底。
CREATE TABLE IF NOT EXISTS applications (
    id                  TEXT PRIMARY KEY,
    applicant_code      TEXT NOT NULL UNIQUE,
    applicant_name      TEXT NOT NULL,
    mission_statement   TEXT NOT NULL,
    status              TEXT NOT NULL,
    current_rule_version TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- 证据修订：同一 evidence_slot 每次补交追加一行，hash 唯一防重复落库。
CREATE TABLE IF NOT EXISTS evidence_versions (
    id              TEXT PRIMARY KEY,
    application_id  TEXT NOT NULL REFERENCES applications(id),
    evidence_slot   TEXT NOT NULL,          -- 如 discipline / talent / service
    seq             INTEGER NOT NULL,
    payload_json    TEXT NOT NULL,          -- canonical JSON
    content_hash    TEXT NOT NULL,
    submitted_by    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (application_id, evidence_slot, seq),
    UNIQUE (application_id, evidence_slot, content_hash)
);

-- 评审专家。
CREATE TABLE IF NOT EXISTS experts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    org        TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 回避关系（专家与申请之间的利益关联），只追加，解除也新增一行带原因。
CREATE TABLE IF NOT EXISTS recusals (
    id             TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    expert_id      TEXT NOT NULL REFERENCES experts(id),
    reason         TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    UNIQUE (application_id, expert_id, reason)
);

-- 案卷内的评审分派：同一案卷同一专家只能有一条在力分派。
CREATE TABLE IF NOT EXISTS review_assignments (
    id             TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    expert_id      TEXT NOT NULL REFERENCES experts(id),
    rule_version   TEXT NOT NULL,
    status         TEXT NOT NULL,           -- assigned / submitted / invalidated
    created_at     TEXT NOT NULL,
    UNIQUE (application_id, expert_id)
);

-- 专家评审意见。
CREATE TABLE IF NOT EXISTS review_opinions (
    id             TEXT PRIMARY KEY,
    assignment_id  TEXT NOT NULL REFERENCES review_assignments(id),
    expert_id      TEXT NOT NULL REFERENCES experts(id),
    suggested_type TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    content_hash   TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL
);

-- 签署决定只追加：同一案卷每次签署产生新版本行，旧决定永久保留，
-- application_id 不设唯一约束；最新决定由 signed_at 决定。
CREATE TABLE IF NOT EXISTS decisions (
    id                    TEXT PRIMARY KEY,
    application_id        TEXT NOT NULL REFERENCES applications(id),
    decision_seq          INTEGER NOT NULL,
    rule_version          TEXT NOT NULL,
    classification_result TEXT NOT NULL,    -- research / applied / mixed
    score_json            TEXT NOT NULL,     -- 评分明细
    evidence_snapshot_json TEXT NOT NULL,    -- {slot: {seq,hash}} 全部证据版本指针
    snapshot_hash         TEXT NOT NULL UNIQUE,
    computed_at           TEXT NOT NULL,
    signed_at             TEXT NOT NULL,
    signed_by             TEXT NOT NULL,
    UNIQUE (application_id, decision_seq)
);
CREATE INDEX IF NOT EXISTS idx_decisions_app ON decisions(application_id, signed_at);

-- 参与人快照：签署时刻实际参与计算/评审的人，随决定永久固定。
CREATE TABLE IF NOT EXISTS decision_participants (
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    role        TEXT NOT NULL,              -- signing_official / expert
    actor_id    TEXT NOT NULL,
    actor_name  TEXT NOT NULL,
    PRIMARY KEY (decision_id, role, actor_id)
);

-- 复议：对已签决定发起，冻结当时规则版本与快照重新计算。
CREATE TABLE IF NOT EXISTS appeals (
    id                TEXT PRIMARY KEY,
    application_id    TEXT NOT NULL REFERENCES applications(id),
    decision_id       TEXT NOT NULL REFERENCES decisions(id),
    reason            TEXT NOT NULL,
    rule_version      TEXT NOT NULL,         -- 冻结被复议决定的规则版本
    frozen_snapshot_json TEXT NOT NULL,
    recomputed_score_json TEXT,
    result            TEXT,                  -- open / upheld / adjusted
    new_classification TEXT,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    resolved_at       TEXT
);

-- 幂等键：同一客户端请求去重，重启与并发下安全（唯一约束 + 写锁）。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

-- 状态机事件日志：每次状态迁移追加一行，支撑审计与还原。
CREATE TABLE IF NOT EXISTS status_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications(id),
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    actor_id       TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
"""

# 允许的状态迁移，对应契约五状态：草拟/受理/评审/签署/复议。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "草拟": frozenset({"受理"}),
    "受理": frozenset({"评审"}),
    "评审": frozenset({"签署", "受理"}),
    "签署": frozenset({"复议"}),
    "复议": frozenset({"签署"}),
}

# 迁移前置条件按 “源状态->目标状态” 注册；复议后重签复用既有评审，
# 不再重复要求“在力分派数 ≥ 2”。
TRANSITION_PRECONDITION_KEYS = ("受理->受理", "受理->评审", "评审->签署")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开一个带外键约束的 SQLite 连接。

    ``isolation_level=None`` 关闭驱动的隐式事务，全部事务由应用层显式
    ``BEGIN IMMEDIATE``/``COMMIT`` 控制，避免多线程下隐式事务互相干扰。
    """
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """创建全部表并记录结构版本。"""
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
