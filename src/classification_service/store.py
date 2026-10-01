"""数据访问层：行级映射与写事务。

写事务统一走 :meth:`Store.write` —— 开启 ``BEGIN IMMEDIATE`` 立即取保留写锁，
同一时刻全库只有一个写事务；并发提交要么串行化后命中唯一约束/幂等表，
要么 IntegrityError 转为 ConflictError。这保证并发送审、重复提交与进程重启
（事务原子性 + WAL）都不会制造两份决定。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from .canonical import canonical, content_hash
from .db import ALLOWED_TRANSITIONS
from .errors import ConflictError, DomainError, NotFoundError, StateError

T = TypeVar("T")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Store:
    """封装全部 SQL；工作流层不直接写库。

    连接按线程缓存（SQLite 连接不可在多线程间共享事务状态）；每个写事务
    显式 ``BEGIN IMMEDIATE``，跨线程并发写由 SQLite 写锁串行化，
    ``busy_timeout`` 内等待而不是立即报锁错。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程专用连接（惰性创建）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            from .db import connect
            conn = connect(self.db_path)
            self._local.conn = conn
            with self._all_lock:
                self._all_conns.append(conn)
        return conn

    def close(self) -> None:
        with self._all_lock:
            for conn in self._all_conns:
                conn.close()
            self._all_conns.clear()

    # ------------------------------------------------------------------ #
    # 事务
    # ------------------------------------------------------------------ #
    def write(self, fn: Callable[[], T]) -> T:
        """在单个立即写事务中执行 ``fn``；同线程嵌套调用合并为同一事务。"""
        conn = self.conn
        outermost = getattr(self._local, "tx_depth", 0) == 0
        if outermost:
            conn.execute("BEGIN IMMEDIATE")
            self._local.tx_depth = 1
        else:
            self._local.tx_depth += 1
        try:
            result = fn()
        except DomainError:
            if outermost:
                conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            if outermost:
                conn.rollback()
            raise ConflictError(f"写入冲突：{exc}") from exc
        except sqlite3.OperationalError as exc:
            if outermost:
                conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ConflictError(f"数据库写锁竞争：{exc}", code="write_lock_busy") from exc
            raise
        except Exception:
            if outermost:
                conn.rollback()
            raise
        else:
            if outermost:
                conn.commit()
            return result
        finally:
            self._local.tx_depth -= 1

    # ------------------------------------------------------------------ #
    # 幂等键
    # ------------------------------------------------------------------ #
    def get_idempotent(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT response_json, status_code FROM idempotency_keys WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return {"status_code": row["status_code"], "body": json.loads(row["response_json"])}

    def save_idempotent(self, key: str, request_hash: str, status_code: int, body: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO idempotency_keys"
            "(idempotency_key, request_hash, response_json, status_code, created_at)"
            " VALUES (?,?,?,?,?)",
            (key, request_hash, canonical(body), status_code, now_iso()),
        )

    def check_request_fingerprint(self, key: str, request_hash: str) -> None:
        """同键不同体重放视为冲突，防止键复用。"""
        row = self.conn.execute(
            "SELECT request_hash FROM idempotency_keys WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is not None and row["request_hash"] != request_hash:
            raise ConflictError("幂等键已用于不同请求体", code="idempotency_key_reuse")

    # ------------------------------------------------------------------ #
    # 规则包
    # ------------------------------------------------------------------ #
    def insert_rule_package(self, version: str, name: str, rules: dict[str, Any]) -> str:
        h = content_hash(rules)
        exists = self.conn.execute(
            "SELECT 1 FROM rule_packages WHERE content_hash=?", (h,)
        ).fetchone()
        if exists:
            raise ConflictError("内容相同的规则包已存在", code="rule_duplicate_content")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO rule_packages"
            "(package_version, display_name, rules_json, content_hash, published_at, is_active)"
            " VALUES (?,?,?,?,?,0)",
            (version, name, canonical(rules), h, ts),
        )
        return h

    def activate_rule_package(self, version: str) -> None:
        row = self.conn.execute(
            "SELECT package_version FROM rule_packages WHERE package_version=?", (version,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"规则版本不存在：{version}")
        self.conn.execute("UPDATE rule_packages SET is_active=0")
        self.conn.execute("UPDATE rule_packages SET is_active=1 WHERE package_version=?", (version,))

    def get_rule_package_row(self, version: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM rule_packages WHERE package_version=?", (version,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"规则版本不存在：{version}")
        return row

    def get_active_rule_version(self) -> str | None:
        row = self.conn.execute(
            "SELECT package_version FROM rule_packages WHERE is_active=1"
        ).fetchone()
        return row["package_version"] if row else None

    def list_rule_packages(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT package_version, display_name, content_hash, published_at, is_active"
            " FROM rule_packages ORDER BY published_at"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 申请
    # ------------------------------------------------------------------ #
    def insert_application(self, applicant_code: str, applicant_name: str, mission: str) -> str:
        app_id = new_id("app")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO applications"
            "(id, applicant_code, applicant_name, mission_statement, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (app_id, applicant_code, applicant_name, mission, "草拟", ts, ts),
        )
        self._log(app_id, None, "草拟", actor_id="system", note="建档")
        return app_id

    def get_application(self, app_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"申请不存在：{app_id}")
        return row

    def find_application_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM applications WHERE applicant_code=?", (code,)
        ).fetchone()

    def list_applications(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, applicant_code, applicant_name, status, current_rule_version, created_at"
            " FROM applications ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def transition(self, app_id: str, to_status: str, actor_id: str, note: str = "") -> None:
        app = self.get_application(app_id)
        current = app["status"]
        if to_status not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise StateError(f"状态 {current} 不允许迁移到 {to_status}")
        ts = now_iso()
        preconditions = self._transition_preconditions.get(f"{current}->{to_status}")
        if preconditions is not None:
            preconditions(self, app_id)
        self.conn.execute(
            "UPDATE applications SET status=?, updated_at=?",
            (to_status, ts),
        )
        self._log(app_id, current, to_status, actor_id, note)

    def _require_accepted(self, app_id: str) -> None:
        # 受理前置：四个必备槽位至少各有一个证据版本
        placeholders = ",".join("?" for _ in _REQUIRED_SLOT_NAMES)
        count = self.conn.execute(
            f"SELECT COUNT(DISTINCT evidence_slot) AS c FROM evidence_versions"
            f" WHERE application_id=? AND evidence_slot IN ({placeholders})",
            (app_id, *_REQUIRED_SLOT_NAMES),
        ).fetchone()["c"]
        if count < len(_REQUIRED_SLOT_NAMES):
            raise StateError("四个证据槽位（使命/学科/人才/服务）齐备前不得受理", code="evidence_incomplete")

    def _require_review_ready(self, app_id: str) -> None:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM review_assignments WHERE application_id=? AND status='assigned'",
            (app_id,),
        ).fetchone()
        if row["c"] < 2:
            raise StateError("至少需要两名无回避专家在力分派才能进入评审", code="review_not_ready")

    _transition_preconditions = {  # type: ignore[assignment]
        "草拟->受理": _require_accepted,
        "受理->评审": _require_review_ready,
    }

    def _log(
        self, app_id: str, from_status: str | None, to_status: str, actor_id: str, note: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO status_events(application_id, from_status, to_status, actor_id, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (app_id, from_status, to_status, actor_id, note, now_iso()),
        )

    def list_events(self, app_id: str) -> list[dict[str, Any]]:
        self.get_application(app_id)
        rows = self.conn.execute(
            "SELECT id, from_status, to_status, actor_id, note, created_at"
            " FROM status_events WHERE application_id=? ORDER BY id",
            (app_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 证据
    # ------------------------------------------------------------------ #
    def latest_evidence_seq(self, app_id: str, slot: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM evidence_versions"
            " WHERE application_id=? AND evidence_slot=?",
            (app_id, slot),
        ).fetchone()
        return int(row["s"])

    def add_evidence(
        self, app_id: str, slot: str, payload: dict[str, Any], submitted_by: str
    ) -> tuple[int, str]:
        """追加证据修订。返回 (序号, 内容哈希)。

        已签署后禁止追加（保全快照）；内容哈希在案卷+槽位内唯一，重复提交
        返回 :class:`DuplicateSubmissionError`。
        """
        app = self.get_application(app_id)
        if app["status"] == "签署":
            raise StateError("案卷已签署，证据不可变更；请先发起复议再补交", code="evidence_locked")
        # “复议”状态允许补交：新材料追加为新版本，原版本与原决定均不动。
        h = content_hash(payload)
        dup = self.conn.execute(
            "SELECT seq FROM evidence_versions"
            " WHERE application_id=? AND evidence_slot=? AND content_hash=?",
            (app_id, slot, h),
        ).fetchone()
        if dup is not None:
            from .errors import DuplicateSubmissionError

            raise DuplicateSubmissionError(
                f"槽位 {slot} 已存在相同内容的证据（序号 {dup['seq']}）"
            )
        seq = self.latest_evidence_seq(app_id, slot) + 1
        self.conn.execute(
            "INSERT INTO evidence_versions"
            "(id, application_id, evidence_slot, seq, payload_json, content_hash, submitted_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (new_id("ev"), app_id, slot, seq, canonical(payload), h, submitted_by, now_iso()),
        )
        return seq, h

    def get_evidence_version(self, app_id: str, slot: str, seq: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM evidence_versions WHERE application_id=? AND evidence_slot=? AND seq=?",
            (app_id, slot, seq),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"证据不存在：{slot}#v{seq}")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def latest_evidence_pointers(self, app_id: str) -> dict[str, dict[str, Any]]:
        """签署时刻各槽位最新版本指针（seq + hash），构成证据快照。"""
        rows = self.conn.execute(
            "SELECT ev.evidence_slot AS slot, ev.seq AS seq, ev.content_hash AS hash,"
            "       ev.submitted_by AS submitted_by, ev.created_at AS created_at"
            " FROM evidence_versions ev"
            " JOIN (SELECT evidence_slot, MAX(seq) AS m FROM evidence_versions"
            "       WHERE application_id=? GROUP BY evidence_slot) latest"
            " ON ev.application_id=? AND ev.evidence_slot=latest.evidence_slot AND ev.seq=latest.m",
            (app_id, app_id),
        ).fetchall()
        return {r["slot"]: dict(r) for r in rows}

    def load_evidence_payloads(
        self, app_id: str, pointers: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, dict[str, Any]]:
        """按快照指针载入证据负载；不传指针则取各槽位最新版本。

        载入时校验内容哈希，若底层行被改动立即报错，保证复议用的就是
        签署时的原始输入。
        """
        if pointers is None:
            pointers = self.latest_evidence_pointers(app_id)
        payloads: dict[str, dict[str, Any]] = {}
        for slot, ptr in pointers.items():
            version = self.get_evidence_version(app_id, slot, ptr["seq"])
            if version["content_hash"] != ptr["hash"]:
                raise ConflictError(
                    f"证据 {slot}#v{ptr['seq']} 内容与签署快照不一致", code="evidence_tampered"
                )
            payloads[slot] = version["payload"]
        return payloads

    def list_evidence_history(self, app_id: str, slot: str | None = None) -> list[dict[str, Any]]:
        self.get_application(app_id)
        if slot:
            rows = self.conn.execute(
                "SELECT id, evidence_slot, seq, content_hash, submitted_by, created_at"
                " FROM evidence_versions WHERE application_id=? AND evidence_slot=?"
                " ORDER BY evidence_slot, seq",
                (app_id, slot),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, evidence_slot, seq, content_hash, submitted_by, created_at"
                " FROM evidence_versions WHERE application_id=? ORDER BY evidence_slot, seq",
                (app_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 专家与回避
    # ------------------------------------------------------------------ #
    def upsert_expert(self, expert_id: str, name: str, org: str) -> None:
        self.conn.execute(
            "INSERT INTO experts(id, name, org, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, org=excluded.org",
            (expert_id, name, org, now_iso()),
        )

    def get_expert(self, expert_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM experts WHERE id=?", (expert_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"专家不存在：{expert_id}")
        return row

    def list_experts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM experts ORDER BY id").fetchall()]

    def add_recusal(self, app_id: str, expert_id: str, reason: str) -> None:
        self.get_application(app_id)
        self.get_expert(expert_id)
        self.conn.execute(
            "INSERT INTO recusals(id, application_id, expert_id, reason, active, created_at)"
            " VALUES (?,?,?,?,1,?)",
            (new_id("rec"), app_id, expert_id, reason, now_iso()),
        )

    def active_recusal(self, app_id: str, expert_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM recusals WHERE application_id=? AND expert_id=? AND active=1",
            (app_id, expert_id),
        ).fetchone()

    def list_recusals(self, app_id: str) -> list[dict[str, Any]]:
        self.get_application(app_id)
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, expert_id, reason, active, created_at FROM recusals"
                " WHERE application_id=? ORDER BY created_at",
                (app_id,),
            ).fetchall()
        ]

    def assign_expert(self, app_id: str, expert_id: str, rule_version: str) -> None:
        self.get_application(app_id)
        expert = self.get_expert(expert_id)
        if self.active_recusal(app_id, expert_id) is not None:
            from .errors import RecusalError

            raise RecusalError(f"专家 {expert['name']} 对本案有在力回避关系，不得分派")
        self.conn.execute(
            "INSERT INTO review_assignments(id, application_id, expert_id, rule_version, status, created_at)"
            " VALUES (?,?,?,?, 'assigned', ?)",
            (new_id("asn"), app_id, expert_id, rule_version, now_iso()),
        )

    def list_assignments(self, app_id: str) -> list[dict[str, Any]]:
        self.get_application(app_id)
        rows = self.conn.execute(
            "SELECT a.id, a.expert_id, e.name AS expert_name, a.rule_version, a.status, a.created_at"
            " FROM review_assignments a JOIN experts e ON e.id=a.expert_id"
            " WHERE a.application_id=? ORDER BY a.created_at",
            (app_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_assignment(self, assignment_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM review_assignments WHERE id=?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"分派不存在：{assignment_id}")
        return row

    def add_opinion(
        self, assignment_id: str, suggested_type: str, rationale: str
    ) -> str:
        assignment = self.get_assignment(assignment_id)
        if assignment["status"] != "assigned":
            raise StateError("该分派已提交或失效，不能重复提交意见", code="opinion_closed")
        if suggested_type not in ("research", "applied", "mixed"):
            raise DomainError("建议类型必须是 research/applied/mixed")
        expert = self.get_expert(assignment["expert_id"])
        body = {
            "assignment_id": assignment_id,
            "expert_id": expert["id"],
            "suggested_type": suggested_type,
            "rationale": rationale,
        }
        h = content_hash(body)
        dup = self.conn.execute("SELECT 1 FROM review_opinions WHERE content_hash=?", (h,)).fetchone()
        if dup:
            from .errors import DuplicateSubmissionError

            raise DuplicateSubmissionError("完全相同的评审意见已提交")
        self.conn.execute(
            "INSERT INTO review_opinions(id, assignment_id, expert_id, suggested_type, rationale, content_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (new_id("opn"), assignment_id, expert["id"], suggested_type, rationale, h, now_iso()),
        )
        self.conn.execute(
            "UPDATE review_assignments SET status='submitted' WHERE id=?", (assignment_id,)
        )
        return h

    def list_opinions(self, app_id: str) -> list[dict[str, Any]]:
        self.get_application(app_id)
        rows = self.conn.execute(
            "SELECT o.id, o.assignment_id, o.expert_id, e.name AS expert_name,"
            "       o.suggested_type, o.rationale, o.content_hash, o.created_at"
            " FROM review_opinions o"
            " JOIN review_assignments a ON a.id=o.assignment_id"
            " JOIN experts e ON e.id=o.expert_id"
            " WHERE a.application_id=? ORDER BY o.created_at",
            (app_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 签署
    # ------------------------------------------------------------------ #
    def get_decision(self, app_id: str) -> dict[str, Any] | None:
        """取案卷最新签署决定；旧版本通过 :meth:`list_decisions` 获取。"""
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE application_id=?"
            " ORDER BY decision_seq DESC LIMIT 1",
            (app_id,),
        ).fetchone()
        return self._decision_row_to_dict(row) if row is not None else None

    def list_decisions(self, app_id: str) -> list[dict[str, Any]]:
        self.get_application(app_id)
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE application_id=? ORDER BY decision_seq",
            (app_id,),
        ).fetchall()
        return [self._decision_row_to_dict(row) for row in rows]

    def _decision_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("score_json", "evidence_snapshot_json"):
            result[key.replace("_json", "")] = json.loads(result.pop(key))
        result["participants"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT role, actor_id, actor_name FROM decision_participants"
                " WHERE decision_id=? ORDER BY role, actor_id",
                (result["id"],),
            ).fetchall()
        ]
        return result

    def next_decision_seq(self, app_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(decision_seq), 0) AS s FROM decisions WHERE application_id=?",
            (app_id,),
        ).fetchone()
        return int(row["s"]) + 1

    def insert_decision(
        self,
        app_id: str,
        decision_seq: int,
        rule_version: str,
        classification: str,
        evaluation: dict[str, Any],
        snapshot: dict[str, Any],
        snapshot_hash: str,
        signed_by: str,
        participants: list[tuple[str, str, str]],
    ) -> str:
        ts = now_iso()
        decision_id = new_id("dec")
        self.conn.execute(
            "INSERT INTO decisions"
            "(id, application_id, decision_seq, rule_version, classification_result, score_json,"
            " evidence_snapshot_json, snapshot_hash, computed_at, signed_at, signed_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                app_id,
                decision_seq,
                rule_version,
                classification,
                canonical(evaluation),
                canonical(snapshot),
                snapshot_hash,
                ts,
                ts,
                signed_by,
            ),
        )
        self.conn.executemany(
            "INSERT INTO decision_participants(decision_id, role, actor_id, actor_name)"
            " VALUES (?,?,?,?)",
            [(decision_id, *p) for p in participants],
        )
        return decision_id

    # ------------------------------------------------------------------ #
    # 复议
    # ------------------------------------------------------------------ #
    def create_appeal(
        self,
        app_id: str,
        decision_id: str,
        reason: str,
        rule_version: str,
        snapshot: dict[str, Any],
        created_by: str,
    ) -> str:
        appeal_id = new_id("apl")
        self.conn.execute(
            "INSERT INTO appeals"
            "(id, application_id, decision_id, reason, rule_version, frozen_snapshot_json,"
            " result, created_by, created_at)"
            " VALUES (?,?,?,?,?,?, 'open', ?, ?)",
            (
                appeal_id,
                app_id,
                decision_id,
                reason,
                rule_version,
                canonical(snapshot),
                created_by,
                now_iso(),
            ),
        )
        return appeal_id

    def get_appeal(self, appeal_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"复议不存在：{appeal_id}")
        result = dict(row)
        result["frozen_snapshot"] = json.loads(result.pop("frozen_snapshot_json"))
        if result.get("recomputed_score_json"):
            result["recomputed_score"] = json.loads(result.pop("recomputed_score_json"))
        return result

    def list_appeals(self, app_id: str | None = None) -> list[dict[str, Any]]:
        if app_id:
            rows = self.conn.execute(
                "SELECT id, application_id, decision_id, reason, rule_version, result,"
                "       new_classification, created_by, created_at, resolved_at"
                " FROM appeals WHERE application_id=? ORDER BY created_at",
                (app_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, application_id, decision_id, reason, rule_version, result,"
                "       new_classification, created_by, created_at, resolved_at"
                " FROM appeals ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_appeal(
        self,
        appeal_id: str,
        result: str,
        new_classification: str | None,
        recomputed: dict[str, Any],
    ) -> None:
        cursor = self.conn.execute(
            "UPDATE appeals SET result=?, new_classification=?, recomputed_score_json=?, resolved_at=?"
            " WHERE id=? AND result='open'",
            (result, new_classification, canonical(recomputed), now_iso(), appeal_id),
        )
        if cursor.rowcount == 0:
            raise StateError("复议已了结或不存在")


_REQUIRED_SLOT_NAMES = ("mission", "disciplines", "talent", "service")
