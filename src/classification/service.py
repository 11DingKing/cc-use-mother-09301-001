"""服务层：案件工作流、签署快照、回避、复议。

状态机：
    draft ──accept──▶ accepted ──start_review──▶ in_review ──sign──▶ signed
                                          │                          │
                                          └─证据补交仍产生新版本      └─request_reconsider──▶ reconsidering
                                                                     └─resolve──▶ signed

关键保证：
- 每次证据提交产生追加式新版本，旧版本行永不修改；
- 签署时把规则全文、证据包、评估明细、参与人冻结为不可变快照；
- 签署要求全体评审人对“最新”证据版本出具意见，材料补交后旧意见自然失效；
- 规则换版只在受理时绑定版本，旧案重放仍用快照内规则全文；
- 全部写操作在 BEGIN IMMEDIATE 事务内并携带幂等键。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core import add_years, canonical, new_id, now_iso, sha256_hex, today
from .services import rules as rules_engine
from .services.errors import ServiceError, bad_request, conflict, forbidden, not_found
from .services.seeds import DEFAULT_VERSION, seed_rulesets
from .storage import Database


def _loads(text: str) -> Any:
    return json.loads(text)


def _idem_key(payload: dict[str, Any]) -> str:
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        raise bad_request("缺少 idempotency_key")
    return key.strip()


class ClassificationService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- 初始化 -------------------------------------------------------

    def seed(self) -> None:
        with self.db.tx() as conn:
            existing = conn.execute("SELECT 1 FROM rulesets LIMIT 1").fetchone()
            if existing:
                return
            for raw in seed_rulesets():
                body = dict(raw)
                effective_from = body.pop("effective_from")
                rules_engine.validate_ruleset(body)
                conn.execute(
                    "INSERT INTO rulesets (ruleset_version, body, fingerprint, effective_from, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (
                        body["ruleset_version"],
                        canonical(body),
                        rules_engine.ruleset_fingerprint(body),
                        effective_from,
                        now_iso(),
                    ),
                )

    # ---- 规则集 -------------------------------------------------------

    def list_rulesets(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT ruleset_version, body, fingerprint, effective_from, created_at"
            " FROM rulesets ORDER BY effective_from"
        )
        return [
            {
                "ruleset_version": r["ruleset_version"],
                "effective_from": r["effective_from"],
                "fingerprint": r["fingerprint"],
                "created_at": r["created_at"],
                "ruleset": _loads(r["body"]),
            }
            for r in rows
        ]

    def get_ruleset(self, version: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT ruleset_version, body, fingerprint, effective_from, created_at"
            " FROM rulesets WHERE ruleset_version=?",
            (version,),
        )
        if row is None:
            raise not_found("规则集", version)
        return {
            "ruleset_version": row["ruleset_version"],
            "effective_from": row["effective_from"],
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
            "ruleset": _loads(row["body"]),
        }

    def publish_ruleset(self, payload: dict[str, Any]) -> dict[str, Any]:
        ruleset = payload.get("ruleset")
        if not isinstance(ruleset, dict):
            raise bad_request("缺少 ruleset 对象")
        effective_from = payload.get("effective_from")
        if not isinstance(effective_from, str) or not effective_from.strip():
            raise bad_request("缺少 effective_from")
        try:
            rules_engine.validate_ruleset(ruleset)
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        fingerprint = rules_engine.ruleset_fingerprint(ruleset)
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO rulesets (ruleset_version, body, fingerprint, effective_from, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (
                        ruleset["ruleset_version"],
                        canonical(ruleset),
                        fingerprint,
                        effective_from,
                        now_iso(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise conflict(f"规则集版本已存在：{ruleset['ruleset_version']}") from exc
        return self.get_ruleset(ruleset["ruleset_version"])

    # ---- 专家 ---------------------------------------------------------

    def register_expert(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = payload.get("name")
        org = payload.get("org")
        if not name or not org:
            raise bad_request("专家缺少 name 或 org")
        conflicts = payload.get("conflicts", [])
        if not isinstance(conflicts, list) or any(not isinstance(c, str) for c in conflicts):
            raise bad_request("conflicts 须为机构名称字符串列表")
        expert_id = new_id("exp")
        ts = now_iso()
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO experts (id, name, org, created_at) VALUES (?,?,?,?)",
                    (expert_id, name, org, ts),
                )
                for institution in set(conflicts):
                    conn.execute(
                        "INSERT INTO expert_conflicts (expert_id, institution, reason, created_at)"
                        " VALUES (?,?,?,?)",
                        (expert_id, institution, "同单位回避", ts),
                    )
        except sqlite3.IntegrityError as exc:
            raise conflict(f"专家已登记：{name} / {org}") from exc
        return self.get_expert(expert_id)

    def get_expert(self, expert_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM experts WHERE id=?", (expert_id,))
        if row is None:
            raise not_found("专家", expert_id)
        conflicts = self.db.query_all(
            "SELECT institution, reason FROM expert_conflicts WHERE expert_id=?",
            (expert_id,),
        )
        return {
            "id": row["id"],
            "name": row["name"],
            "org": row["org"],
            "conflicts": [{"institution": c["institution"], "reason": c["reason"]} for c in conflicts],
            "created_at": row["created_at"],
        }

    def list_experts(self) -> list[dict[str, Any]]:
        rows = self.db.query_all("SELECT id FROM experts ORDER BY created_at")
        return [self.get_expert(r["id"]) for r in rows]

    def assign_expert(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        expert_id = payload.get("expert_id")
        if not expert_id:
            raise bad_request("缺少 expert_id")
        try:
            with self.db.tx() as conn:
                case = self._locked_case(conn, case_id)
                if case["status"] not in ("accepted", "in_review"):
                    raise conflict("案件需先受理才能指派评审人")
                expert = conn.execute("SELECT * FROM experts WHERE id=?", (expert_id,)).fetchone()
                if expert is None:
                    raise not_found("专家", expert_id)
                existing = conn.execute(
                    "SELECT role FROM case_experts WHERE case_id=? AND expert_id=?",
                    (case_id, expert_id),
                ).fetchone()
                if existing is not None:
                    if existing["role"] == "recused":
                        raise forbidden("该专家已回避，不能参与本案")
                    return self._case_detail(case_id)
                hit = conn.execute(
                    "SELECT reason FROM expert_conflicts WHERE expert_id=? AND institution=?",
                    (expert_id, case["institution"]),
                ).fetchone()
                if expert["org"] == case["institution"]:
                    raise forbidden(
                        f"专家 {expert['name']} 任职于 {case['institution']}，与被评院校同单位，应予回避"
                    )
                if hit is not None:
                    raise forbidden(
                        f"专家 {expert['name']} 与院校 {case['institution']} 存在{hit['reason']}关系，应予回避"
                    )
                ts = now_iso()
                conn.execute(
                    "INSERT INTO case_experts (case_id, expert_id, role, assigned_at) VALUES (?,?, 'reviewer', ?)",
                    (case_id, expert_id, ts),
                )
                self.db.record_event(conn, case_id, "expert_assigned",
                                     {"expert_id": expert_id}, "主管部门", ts)
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
        return self._case_detail(case_id)

    def recuse_expert(self, case_id: str, expert_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        reason = payload.get("reason") or "专家回避"
        with self.db.tx() as conn:
            case = self._locked_case(conn, case_id)
            if case["status"] not in ("accepted", "in_review"):
                raise conflict("案件已进入签署或复议阶段，不能变更评审人")
            row = conn.execute(
                "SELECT role FROM case_experts WHERE case_id=? AND expert_id=?",
                (case_id, expert_id),
            ).fetchone()
            if row is None:
                raise not_found("该案专家分配", expert_id)
            if row["role"] == "recused":
                return self._case_detail(case_id)
            ts = now_iso()
            conn.execute(
                "UPDATE case_experts SET role='recused', recused_at=? WHERE case_id=? AND expert_id=?",
                (ts, case_id, expert_id),
            )
            self.db.record_event(conn, case_id, "expert_recused",
                                 {"expert_id": expert_id, "reason": reason}, expert_id, ts)
        return self._case_detail(case_id)

    # ---- 案件 ---------------------------------------------------------

    def create_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = _idem_key(payload)
        case_no = payload.get("case_no")
        institution = payload.get("institution")
        if not case_no or not institution:
            raise bad_request("缺少 case_no 或 institution")
        ruleset_version = payload.get("ruleset_version", DEFAULT_VERSION)
        if not isinstance(ruleset_version, str):
            raise bad_request("ruleset_version 非法")
        case_id = new_id("case")
        ts = now_iso()
        replay_id: str | None = None
        try:
            with self.db.tx() as conn:
                # 事务内复查：并发送审在锁上排队后能看到前一个事务已提交的同键案件。
                dup = conn.execute("SELECT id FROM cases WHERE idempotency_key=?", (key,)).fetchone()
                if dup is not None:
                    replay_id = dup["id"]
                else:
                    if conn.execute("SELECT 1 FROM rulesets WHERE ruleset_version=?",
                                    (ruleset_version,)).fetchone() is None:
                        raise bad_request(f"规则集版本不存在：{ruleset_version}")
                    conn.execute(
                        "INSERT INTO cases (id, case_no, institution, status, ruleset_version,"
                        " idempotency_key, created_at, updated_at)"
                        " VALUES (?,?,?, 'draft', ?,?,?,?)",
                        (case_id, case_no, institution, ruleset_version, key, ts, ts),
                    )
                    self.db.record_event(conn, case_id, "case_created",
                                         {"case_no": case_no, "institution": institution,
                                          "ruleset_version": ruleset_version},
                                         "主管部门", ts)
        except sqlite3.IntegrityError as exc:
            row = self.db.query_one("SELECT id FROM cases WHERE idempotency_key=?", (key,))
            if row is not None:
                return self.get_case(row["id"])
            raise conflict(f"案件编号已存在：{case_no}") from exc
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
        return self.get_case(replay_id or case_id)

    def accept_case(self, case_id: str) -> dict[str, Any]:
        with self.db.tx() as conn:
            case = self._locked_case(conn, case_id)
            if case["status"] != "draft":
                if case["status"] in ("accepted", "in_review"):
                    return self._case_detail(case_id)
                raise conflict("案件已越过受理阶段")
            ts = now_iso()
            conn.execute("UPDATE cases SET status='accepted', updated_at=? WHERE id=?", (ts, case_id))
            self.db.record_event(conn, case_id, "case_accepted", {}, "主管部门", ts)
        return self._case_detail(case_id)

    def start_review(self, case_id: str) -> dict[str, Any]:
        with self.db.tx() as conn:
            case = self._locked_case(conn, case_id)
            if case["status"] == "in_review":
                return self._case_detail(case_id)
            if case["status"] != "accepted":
                raise conflict("仅已受理案件可以进入评审")
            self._assert_review_ready(conn, case_id)
            ts = now_iso()
            conn.execute("UPDATE cases SET status='in_review', updated_at=? WHERE id=?", (ts, case_id))
            self.db.record_event(conn, case_id, "review_started", {}, "主管部门", ts)
        return self._case_detail(case_id)

    def get_case(self, case_id: str) -> dict[str, Any]:
        return self._case_detail(case_id)

    def list_cases(self) -> list[dict[str, Any]]:
        rows = self.db.query_all("SELECT id FROM cases ORDER BY created_at")
        return [self._case_detail(r["id"]) for r in rows]

    # ---- 证据 ---------------------------------------------------------

    def submit_evidence(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = _idem_key(payload)
        evidence = payload.get("evidence")
        submitted_by = payload.get("submitted_by")
        if not submitted_by:
            raise bad_request("缺少 submitted_by")
        try:
            rules_engine.validate_evidence(evidence)
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        evidence_id = new_id("evd")
        try:
            with self.db.tx() as conn:
                # 事务内复查幂等键：并发送审在写锁上排队后能看到前一个已提交版本。
                dup = conn.execute(
                    "SELECT 1 FROM evidence_versions WHERE idempotency_key=?", (key,)
                ).fetchone()
                if dup is None:
                    case = self._locked_case(conn, case_id)
                    if case["status"] not in ("accepted", "in_review"):
                        raise conflict("仅受理后、签署前可以提交或补交材料")
                    last = conn.execute(
                        "SELECT id, version FROM evidence_versions WHERE case_id=?"
                        " ORDER BY version DESC LIMIT 1",
                        (case_id,),
                    ).fetchone()
                    version = 1 if last is None else last["version"] + 1
                    supersedes = None if last is None else last["id"]
                    ts = now_iso()
                    conn.execute(
                        "INSERT INTO evidence_versions (id, case_id, version, body, submitted_by,"
                        " supersedes, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (evidence_id, case_id, version, canonical(evidence), submitted_by,
                         supersedes, key, ts),
                    )
                    self.db.record_event(conn, case_id, "evidence_submitted",
                                         {"version": version, "supersedes": supersedes,
                                          "item_count": len(evidence)},
                                         submitted_by, ts)
        except sqlite3.IntegrityError as exc:
            row = self.db.query_one(
                "SELECT case_id FROM evidence_versions WHERE idempotency_key=?", (key,)
            )
            if row is not None:
                return self._evidence_detail(row["case_id"], key)
            raise conflict("证据版本写入冲突") from exc
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
        return self._evidence_detail(case_id, key)

    # ---- 评审意见 -----------------------------------------------------

    def submit_review(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = _idem_key(payload)
        expert_id = payload.get("expert_id")
        opinion = payload.get("opinion")
        comment = payload.get("comment", "")
        if not expert_id:
            raise bad_request("缺少 expert_id")
        if opinion not in ("support", "oppose", "abstain"):
            raise bad_request("opinion 必须是 support/oppose/abstain")
        review_id = new_id("rev")
        replay_id: str | None = None
        try:
            with self.db.tx() as conn:
                dup = conn.execute(
                    "SELECT id FROM reviews WHERE idempotency_key=?", (key,)
                ).fetchone()
                if dup is not None:
                    replay_id = dup["id"]
                else:
                    case = self._locked_case(conn, case_id)
                    if case["status"] != "in_review":
                        raise conflict("仅评审中的案件可以出具意见")
                    assignment = conn.execute(
                        "SELECT role FROM case_experts WHERE case_id=? AND expert_id=?",
                        (case_id, expert_id),
                    ).fetchone()
                    if assignment is None:
                        raise forbidden("专家未参与本案")
                    if assignment["role"] == "recused":
                        raise forbidden("该专家已回避，不能出具意见")
                    latest = conn.execute(
                        "SELECT version FROM evidence_versions WHERE case_id=?"
                        " ORDER BY version DESC LIMIT 1",
                        (case_id,),
                    ).fetchone()
                    if latest is None:
                        raise conflict("案件尚无证据材料")
                    ts = now_iso()
                    conn.execute(
                        "INSERT INTO reviews (id, case_id, expert_id, evidence_version, opinion,"
                        " comment, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (review_id, case_id, expert_id, latest["version"], opinion,
                         comment, key, ts),
                    )
                    self.db.record_event(conn, case_id, "review_submitted",
                                         {"expert_id": expert_id,
                                          "evidence_version": latest["version"], "opinion": opinion},
                                         expert_id, ts)
        except sqlite3.IntegrityError as exc:
            row = self.db.query_one("SELECT id FROM reviews WHERE idempotency_key=?", (key,))
            if row is not None:
                return self._review_detail(row["id"])
            raise conflict("该专家对当前证据版本已出具意见，材料补交后可再次评审") from exc
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
        return self._review_detail(replay_id or review_id)

    # ---- 签署决定 -----------------------------------------------------

    def sign_decision(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = _idem_key(payload)
        signed_by = payload.get("signed_by")
        if not signed_by:
            raise bad_request("缺少 signed_by")
        valid_years = payload.get("valid_years", 5)
        if not isinstance(valid_years, int) or valid_years <= 0:
            raise bad_request("valid_years 必须是正整数")
        decision_id = new_id("dec")
        replay_id: str | None = None
        try:
            with self.db.tx() as conn:
                # 同键并发签署：后到事务拿锁后直接回放首个结果，绝不产生第二份决定。
                dup = conn.execute(
                    "SELECT id FROM decisions WHERE idempotency_key=?", (key,)
                ).fetchone()
                if dup is not None:
                    replay_id = dup["id"]
                else:
                    case = self._locked_case(conn, case_id)
                    if case["status"] != "in_review":
                        if case["status"] in ("signed", "reconsidering"):
                            row = conn.execute(
                                "SELECT id FROM decisions WHERE case_id=?", (case_id,)
                            ).fetchone()
                            raise conflict("案件已签署，结论不可更改；如需处理请走复议",
                                           decision_id=row["id"] if row else None)
                        raise conflict("仅评审中的案件可以签署")
                    evidence_row = conn.execute(
                        "SELECT * FROM evidence_versions WHERE case_id=?"
                        " ORDER BY version DESC LIMIT 1",
                        (case_id,),
                    ).fetchone()
                    if evidence_row is None:
                        raise conflict("案件尚无证据材料")
                    reviewers = conn.execute(
                        "SELECT e.id AS expert_id, e.name, e.org FROM case_experts ce"
                        " JOIN experts e ON e.id=ce.expert_id"
                        " WHERE ce.case_id=? AND ce.role='reviewer'",
                        (case_id,),
                    ).fetchall()
                    if len(reviewers) < 2:
                        raise conflict("签署前至少需要两名未回避的评审专家")
                    review_rows = conn.execute(
                        "SELECT * FROM reviews WHERE case_id=? AND evidence_version=?",
                        (case_id, evidence_row["version"]),
                    ).fetchall()
                    reviews_by_expert = {r["expert_id"]: r for r in review_rows}
                    missing = [r["name"] for r in reviewers if r["expert_id"] not in reviews_by_expert]
                    if missing:
                        raise conflict("尚有评审人未对最新证据版本出具意见",
                                       evidence_version=evidence_row["version"],
                                       awaiting=missing)
                    ruleset_row = conn.execute(
                        "SELECT * FROM rulesets WHERE ruleset_version=?",
                        (case["ruleset_version"],),
                    ).fetchone()
                    ruleset = _loads(ruleset_row["body"])
                    evidence = _loads(evidence_row["body"])
                    evaluation = rules_engine.evaluate(ruleset, evidence)
                    if evaluation["indeterminate"]:
                        raise conflict(
                            "证据不足以形成明确分类建议（类别分差低于 min_margin），退回补正后重新评审",
                            margin=evaluation["margin"],
                            min_margin=evaluation["thresholds"]["min_margin"],
                            ranking=evaluation["ranking"],
                        )
                    signed_at = now_iso()
                    participant_reviews = []
                    for r in reviewers:
                        rev = reviews_by_expert[r["expert_id"]]
                        participant_reviews.append({
                            "review_id": rev["id"],
                            "expert_id": r["expert_id"],
                            "name": r["name"],
                            "org": r["org"],
                            "opinion": rev["opinion"],
                            "comment": rev["comment"],
                            "evidence_version": rev["evidence_version"],
                            "submitted_at": rev["created_at"],
                        })
                    submitters_rows = conn.execute(
                        "SELECT DISTINCT submitted_by FROM evidence_versions WHERE case_id=?",
                        (case_id,),
                    ).fetchall()
                    # 证据链与意见史一并入快照：补交产生新版本，但原始输入与历任参与人可追溯。
                    all_versions = conn.execute(
                        "SELECT version, body, submitted_by, created_at, supersedes"
                        " FROM evidence_versions WHERE case_id=? ORDER BY version",
                        (case_id,),
                    ).fetchall()
                    evidence_history = [
                        {"version": v["version"],
                         "submitted_by": v["submitted_by"],
                         "created_at": v["created_at"],
                         "supersedes": v["supersedes"],
                         "evidence": _loads(v["body"]),
                         "body_sha256": sha256_hex(_loads(v["body"]))}
                        for v in all_versions
                    ]
                    all_reviews = conn.execute(
                        "SELECT r.id, r.expert_id, e.name, e.org, r.evidence_version,"
                        " r.opinion, r.comment, r.created_at"
                        " FROM reviews r JOIN experts e ON e.id=r.expert_id"
                        " WHERE r.case_id=? ORDER BY r.created_at",
                        (case_id,),
                    ).fetchall()
                    review_history = [
                        {"review_id": r["id"], "expert_id": r["expert_id"], "name": r["name"],
                         "org": r["org"], "evidence_version": r["evidence_version"],
                         "opinion": r["opinion"], "comment": r["comment"],
                         "submitted_at": r["created_at"]}
                        for r in all_reviews
                    ]
                    snapshot = {
                        "snapshot_version": 1,
                        "case_id": case_id,
                        "case_no": case["case_no"],
                        "institution": case["institution"],
                        "ruleset_version": case["ruleset_version"],
                        "ruleset_fingerprint": ruleset_row["fingerprint"],
                        "ruleset": ruleset,
                        "evidence_version": evidence_row["version"],
                        "evidence": evidence,
                        "evidence_history": evidence_history,
                        "evaluation": evaluation,
                        "participants": {
                            "signed_by": signed_by,
                            "evidence_submitters": [r["submitted_by"] for r in submitters_rows],
                            "reviewers": participant_reviews,
                            "review_history": review_history,
                        },
                        "signed_at": signed_at,
                    }
                    snapshot_hash = sha256_hex(snapshot)
                    valid_from = today().isoformat()
                    valid_to = add_years(today(), valid_years).isoformat()
                    conn.execute(
                        "INSERT INTO decisions (id, case_id, evidence_version, snapshot,"
                        " valid_from, valid_to, signed_by, idempotency_key, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (decision_id, case_id, evidence_row["version"], canonical(snapshot),
                         valid_from, valid_to, signed_by, key, signed_at),
                    )
                    conn.execute(
                        "UPDATE cases SET status='signed', updated_at=? WHERE id=?",
                        (signed_at, case_id),
                    )
                    self.db.record_event(conn, case_id, "decision_signed",
                                         {"decision_id": decision_id,
                                          "recommendation": evaluation["recommendation"],
                                          "valid_from": valid_from, "valid_to": valid_to,
                                          "snapshot_hash": snapshot_hash},
                                         signed_by, signed_at)
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
        return self._decision_detail(replay_id or decision_id)

    def get_decision(self, case_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT id FROM decisions WHERE case_id=?", (case_id,))
        if row is None:
            raise not_found("签署决定", f"案件 {case_id}")
        return self._decision_detail(row["id"])

    # ---- 复议 ---------------------------------------------------------

    def request_reconsideration(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = _idem_key(payload)
        requested_by = payload.get("requested_by")
        reason = payload.get("reason")
        if not requested_by or not reason:
            raise bad_request("缺少 requested_by 或 reason")
        recon_id = new_id("rec")
        replay_id: str | None = None
        try:
            with self.db.tx() as conn:
                dup = conn.execute(
                    "SELECT id FROM reconsiderations WHERE idempotency_key=?", (key,)
                ).fetchone()
                if dup is not None:
                    replay_id = dup["id"]
                else:
                    case = self._locked_case(conn, case_id)
                    if case["status"] not in ("signed", "reconsidering"):
                        raise conflict("只有已签署案件可以申请复议")
                    decision_row = conn.execute(
                        "SELECT snapshot FROM decisions WHERE case_id=?", (case_id,)
                    ).fetchone()
                    if decision_row is None:
                        raise not_found("签署决定", f"案件 {case_id}")
                    snapshot = _loads(decision_row["snapshot"])
                    report = rules_engine.replay(snapshot)
                    ts = now_iso()
                    conn.execute(
                        "INSERT INTO reconsiderations (id, case_id, requested_by, reason,"
                        " replay_report, status, idempotency_key, created_at)"
                        " VALUES (?,?,?,?,?, 'open', ?,?)",
                        (recon_id, case_id, requested_by, reason,
                         canonical(report), key, ts),
                    )
                    if case["status"] == "signed":
                        conn.execute(
                            "UPDATE cases SET status='reconsidering', updated_at=? WHERE id=?",
                            (ts, case_id),
                        )
                    self.db.record_event(conn, case_id, "reconsideration_requested",
                                         {"reconsideration_id": recon_id,
                                          "reproduced": report["reproduced"]},
                                         requested_by, ts)
        except sqlite3.IntegrityError as exc:
            row = self.db.query_one(
                "SELECT id FROM reconsiderations WHERE idempotency_key=?", (key,)
            )
            if row is not None:
                return self._reconsideration_detail(row["id"])
            raise conflict("复议申请写入冲突") from exc
        except sqlite3.OperationalError as exc:
            self._raise_if_busy(exc)
        return self._reconsideration_detail(replay_id or recon_id)

    def resolve_reconsideration(self, case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        recon_id = payload.get("reconsideration_id")
        outcome = payload.get("outcome")
        comment = payload.get("comment", "")
        if outcome not in ("upheld", "superseded"):
            raise bad_request("outcome 必须是 upheld（维持）或 superseded（另案重处）")
        with self.db.tx() as conn:
            case = self._locked_case(conn, case_id)
            if case["status"] != "reconsidering":
                raise conflict("案件不在复议中")
            where = "case_id=? AND status='open'"
            params: tuple[Any, ...] = (case_id,)
            if recon_id:
                where += " AND id=?"
                params = (case_id, recon_id)
            row = conn.execute(
                f"SELECT id FROM reconsiderations WHERE {where} ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                raise not_found("未结复议", recon_id or case_id)
            ts = now_iso()
            conn.execute(
                "UPDATE reconsiderations SET status='resolved' WHERE id=?", (row["id"],)
            )
            # 原决定不可变：维持即恢复 signed；需要换结论的，另立新案处理。
            if outcome == "upheld":
                conn.execute(
                    "UPDATE cases SET status='signed', updated_at=? WHERE id=?", (ts, case_id)
                )
            else:
                conn.execute(
                    "UPDATE cases SET status='closed', updated_at=? WHERE id=?", (ts, case_id)
                )
            self.db.record_event(conn, case_id, "reconsideration_resolved",
                                 {"reconsideration_id": row["id"], "outcome": outcome,
                                  "comment": comment},
                                 payload.get("resolved_by", "主管部门"), ts)
        return self._case_detail(case_id)

    def list_reconsiderations(self, case_id: str | None = None) -> list[dict[str, Any]]:
        if case_id:
            rows = self.db.query_all(
                "SELECT id FROM reconsiderations WHERE case_id=? ORDER BY created_at", (case_id,)
            )
        else:
            rows = self.db.query_all("SELECT id FROM reconsiderations ORDER BY created_at")
        return [self._reconsideration_detail(r["id"]) for r in rows]

    def events(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT event_type, payload, actor, created_at FROM events"
            " WHERE case_id=? ORDER BY id",
            (case_id,),
        )
        return [
            {"event_type": r["event_type"], "payload": _loads(r["payload"]),
             "actor": r["actor"], "created_at": r["created_at"]}
            for r in rows
        ]

    # ---- 内部辅助 -----------------------------------------------------

    @staticmethod
    def _locked_case(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if case is None:
            raise not_found("案件", case_id)
        return case

    @staticmethod
    def _raise_if_busy(exc: sqlite3.OperationalError) -> None:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise conflict("并发写入冲突（另一事务正在处理同一资源），请重试") from exc
        raise exc

    def _assert_review_ready(self, conn: sqlite3.Connection, case_id: str) -> None:
        if conn.execute(
            "SELECT 1 FROM evidence_versions WHERE case_id=? LIMIT 1", (case_id,)
        ).fetchone() is None:
            raise conflict("案件尚无证据材料")
        reviewer_count = conn.execute(
            "SELECT COUNT(*) AS n FROM case_experts WHERE case_id=? AND role='reviewer'",
            (case_id,),
        ).fetchone()["n"]
        if reviewer_count < 2:
            raise conflict("进入评审至少需要两名未回避的评审专家")

    def _case_detail(self, case_id: str) -> dict[str, Any]:
        case = self.db.query_one("SELECT * FROM cases WHERE id=?", (case_id,))
        if case is None:
            raise not_found("案件", case_id)
        assignments = self.db.query_all(
            "SELECT expert_id, role, assigned_at, recused_at FROM case_experts"
            " WHERE case_id=? ORDER BY assigned_at",
            (case_id,),
        )
        versions = self.db.query_all(
            "SELECT id, version, submitted_by, supersedes, created_at FROM evidence_versions"
            " WHERE case_id=? ORDER BY version",
            (case_id,),
        )
        decision = self.db.query_one("SELECT id FROM decisions WHERE case_id=?", (case_id,))
        return {
            "id": case["id"],
            "case_no": case["case_no"],
            "institution": case["institution"],
            "status": case["status"],
            "ruleset_version": case["ruleset_version"],
            "experts": [dict(a) for a in assignments],
            "evidence_versions": [
                {"id": v["id"], "version": v["version"], "submitted_by": v["submitted_by"],
                 "supersedes": v["supersedes"], "created_at": v["created_at"]}
                for v in versions
            ],
            "decision_id": decision["id"] if decision else None,
            "created_at": case["created_at"],
            "updated_at": case["updated_at"],
        }

    def _evidence_detail(self, case_id: str, idem_key: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM evidence_versions WHERE idempotency_key=?", (idem_key,)
        )
        if row is None:
            raise not_found("证据版本", idem_key)
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "version": row["version"],
            "evidence": _loads(row["body"]),
            "submitted_by": row["submitted_by"],
            "supersedes": row["supersedes"],
            "created_at": row["created_at"],
        }

    def _review_detail(self, review_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM reviews WHERE id=?", (review_id,))
        if row is None:
            raise not_found("评审意见", review_id)
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "expert_id": row["expert_id"],
            "evidence_version": row["evidence_version"],
            "opinion": row["opinion"],
            "comment": row["comment"],
            "created_at": row["created_at"],
        }

    def _decision_detail(self, decision_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM decisions WHERE id=?", (decision_id,))
        if row is None:
            raise not_found("签署决定", decision_id)
        snapshot = _loads(row["snapshot"])
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "evidence_version": row["evidence_version"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "signed_by": row["signed_by"],
            "created_at": row["created_at"],
            "snapshot_hash": sha256_hex(snapshot),
            "snapshot": snapshot,
        }

    def _reconsideration_detail(self, recon_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM reconsiderations WHERE id=?", (recon_id,))
        if row is None:
            raise not_found("复议申请", recon_id)
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "requested_by": row["requested_by"],
            "reason": row["reason"],
            "status": row["status"],
            "replay_report": _loads(row["replay_report"]),
            "created_at": row["created_at"],
        }
