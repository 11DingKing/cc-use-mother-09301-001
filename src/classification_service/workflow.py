"""用例编排层：申请受理、回避、签署快照、复议追溯。

关键不变量（对应领域契约）：

- **分类规则版本**：进入评审时把当前激活规则版本冻结在案卷上；签署决定与
  复议均引用该版本，之后规则换版不影响在途与已签案卷。
- **专家回避**：分派时强制校验在力回避关系；签署时参与人列表二次校验。
- **签署快照**：签署把评分明细、各槽位证据版本指针（seq+hash）、规则
  版本哈希与全部参与人固化为不可变快照；签署后证据通道封闭。
- **复议追溯**：复议冻结被复议决定的规则版本与证据快照，用原始输入
  重新执行同一评分函数，并与原评分逐项比对，结论（维持/调整）可审计。
"""
from __future__ import annotations

import json
from typing import Any

from .canonical import canonical, content_hash
from .errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    RuleVersionError,
    StateError,
)
from .rules import (
    RulePackage,
    validate_rules,
    evaluate,
)
from .store import Store

VALID_SLOTS = ("mission", "disciplines", "talent", "service")
SLOT_LABELS = {
    "mission": "院校使命",
    "disciplines": "学科结构",
    "talent": "人才培养",
    "service": "社会服务",
}


class Workflow:
    """对外的应用服务。每个公开方法对应一个用例。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ #
    # 规则版本管理
    # ------------------------------------------------------------------ #
    def publish_rules(self, version: str, display_name: str, rules: dict[str, Any]) -> dict[str, Any]:
        validate_rules(rules)

        def tx() -> dict[str, Any]:
            h = self.store.insert_rule_package(version, display_name, rules)
            # 首个规则包自动激活，其余需显式激活
            if self.store.get_active_rule_version() is None:
                self.store.activate_rule_package(version)
            return {"package_version": version, "content_hash": h,
                    "is_active": self.store.get_active_rule_version() == version}

        return self.store.write(tx)

    def activate_rules(self, version: str) -> dict[str, Any]:
        def tx() -> None:
            self.store.activate_rule_package(version)

        self.store.write(tx)
        return {"package_version": version, "is_active": True}

    def _load_package(self, version: str) -> RulePackage:
        row = self.store.get_rule_package_row(version)
        rules = json.loads(row["rules_json"])
        # 校验存储内容未被篡改
        if content_hash(rules) != row["content_hash"]:
            raise ConflictError(f"规则包 {version} 内容哈希不一致", code="rules_tampered")
        return RulePackage(row["package_version"], row["display_name"], rules)

    def active_rule_version(self) -> str:
        version = self.store.get_active_rule_version()
        if version is None:
            raise RuleVersionError("尚未发布并激活任何分类规则版本")
        return version

    def list_rule_packages(self) -> list[dict[str, Any]]:
        return self.store.list_rule_packages()

    # ------------------------------------------------------------------ #
    # 申请建档与证据
    # ------------------------------------------------------------------ #
    def create_application(
        self, applicant_code: str, applicant_name: str, mission_statement: str
    ) -> dict[str, Any]:
        if not applicant_code or not applicant_name or not mission_statement:
            raise DomainError("院校代码、名称与使命陈述均不能为空")

        def tx() -> dict[str, Any]:
            existing = self.store.find_application_by_code(applicant_code)
            if existing is not None:
                raise ConflictError(
                    f"院校 {applicant_code} 已存在申请 {existing['id']}",
                    code="application_duplicate",
                )
            app_id = self.store.insert_application(applicant_code, applicant_name, mission_statement)
            # 建档同时把使命陈述落入 mission 槽位第 1 版
            self.store.add_evidence(
                app_id, "mission",
                {"applicant_code": applicant_code, "text": mission_statement},
                submitted_by=applicant_code,
            )
            return {"application_id": app_id, "status": "草拟"}

        return self.store.write(tx)

    def submit_evidence(
        self, app_id: str, slot: str, payload: dict[str, Any], submitted_by: str
    ) -> dict[str, Any]:
        if slot not in VALID_SLOTS:
            raise DomainError(f"非法证据槽位：{slot}；合法值：{','.join(VALID_SLOTS)}")
        if not isinstance(payload, dict) or not payload:
            raise DomainError("证据负载必须是非空对象")
        if not submitted_by:
            raise DomainError("提交人不能为空")

        def tx() -> dict[str, Any]:
            seq, h = self.store.add_evidence(app_id, slot, payload, submitted_by)
            return {"application_id": app_id, "slot": slot, "seq": seq, "content_hash": h}

        return self.store.write(tx)

    def get_application(self, app_id: str) -> dict[str, Any]:
        app = dict(self.store.get_application(app_id))
        app["evidence_pointers"] = self.store.latest_evidence_pointers(app_id)
        app["decision"] = self.store.get_decision(app_id)
        return app

    def list_applications(self) -> list[dict[str, Any]]:
        return self.store.list_applications()

    def history(self, app_id: str) -> dict[str, Any]:
        return {
            "application_id": app_id,
            "events": self.store.list_events(app_id),
            "evidence": self.store.list_evidence_history(app_id),
            "recusals": self.store.list_recusals(app_id),
            "opinions": self.store.list_opinions(app_id),
        }

    # ------------------------------------------------------------------ #
    # 状态迁移
    # ------------------------------------------------------------------ #
    def accept(self, app_id: str, actor_id: str) -> dict[str, Any]:
        return self._transition(app_id, "受理", actor_id, "材料齐备，予以受理")

    def start_review(self, app_id: str, actor_id: str) -> dict[str, Any]:
        def tx() -> dict[str, Any]:
            # 进入评审时冻结规则版本
            app = self.store.get_application(app_id)
            rule_version = app["current_rule_version"] or self.active_rule_version()
            if app["current_rule_version"] is None:
                self.store.conn.execute(
                    "UPDATE applications SET current_rule_version=? WHERE id=?",
                    (rule_version, app_id),
                )
            self.store.transition(app_id, "评审", actor_id,
                                  f"冻结分类规则版本 {rule_version}")
            return {"application_id": app_id, "status": "评审",
                    "rule_version": rule_version}

        return self.store.write(tx)

    def _transition(self, app_id: str, to_status: str, actor_id: str, note: str) -> dict[str, Any]:
        def tx() -> None:
            self.store.transition(app_id, to_status, actor_id, note)

        self.store.write(tx)
        app = self.store.get_application(app_id)
        return {"application_id": app_id, "status": to_status,
                "rule_version": app["current_rule_version"]}

    # ------------------------------------------------------------------ #
    # 专家、回避与评审
    # ------------------------------------------------------------------ #
    def register_expert(self, expert_id: str, name: str, org: str) -> dict[str, Any]:
        if not all((expert_id, name, org)):
            raise DomainError("专家编号、姓名、所在机构不能为空")

        def tx() -> None:
            self.store.upsert_expert(expert_id, name, org)

        self.store.write(tx)
        return {"expert_id": expert_id, "name": name, "org": org}

    def declare_recusal(self, app_id: str, expert_id: str, reason: str) -> dict[str, Any]:
        if not reason:
            raise DomainError("回避原因不能为空")

        def tx() -> dict[str, Any]:
            self.store.add_recusal(app_id, expert_id, reason)
            return {"application_id": app_id, "expert_id": expert_id, "active": True}

        return self.store.write(tx)

    def assign_expert(self, app_id: str, expert_id: str, actor_id: str) -> dict[str, Any]:
        def tx() -> dict[str, Any]:
            app = self.store.get_application(app_id)
            if app["status"] not in ("受理", "评审"):
                raise StateError("仅在受理或评审阶段可以分派专家")
            rule_version = app["current_rule_version"] or self.active_rule_version()
            self.store.assign_expert(app_id, expert_id, rule_version)
            return {"application_id": app_id, "expert_id": expert_id,
                    "rule_version": rule_version, "status": "assigned"}

        return self.store.write(tx)

    def submit_opinion(
        self, assignment_id: str, suggested_type: str, rationale: str
    ) -> dict[str, Any]:
        def tx() -> str:
            return self.store.add_opinion(assignment_id, suggested_type, rationale)

        h = self.store.write(tx)
        return {"assignment_id": assignment_id, "opinion_hash": h}

    def list_assignments(self, app_id: str) -> list[dict[str, Any]]:
        return self.store.list_assignments(app_id)

    # ------------------------------------------------------------------ #
    # 签署：固化不可变快照
    # ------------------------------------------------------------------ #
    def sign(
        self,
        app_id: str,
        signing_official_id: str,
        signing_official_name: str,
        rule_version: str | None = None,
    ) -> dict[str, Any]:
        """签署决定。

        - 评审态首签：使用进入评审时冻结的规则版本。
        - 复议态重签：默认使用当前激活规则版本（覆盖“规则换版”情形），
          补交的新材料按最新证据版本入算；旧决定与旧快照原样保留，
          在途复议按新旧分类是否变化自动记为 adjusted/upheld。
        """
        def tx() -> dict[str, Any]:
            app = self.store.get_application(app_id)
            if app["status"] not in ("评审", "复议"):
                raise StateError(f"仅评审或复议状态可以签署，当前为 {app['status']}")
            re_signing = app["status"] == "复议"

            previous = self.store.get_decision(app_id)
            if rule_version is not None:
                chosen_version = rule_version
            elif re_signing:
                chosen_version = self.active_rule_version()
            else:
                chosen_version = app["current_rule_version"] or self.active_rule_version()
            package = self._load_package(chosen_version)

            pointers = self.store.latest_evidence_pointers(app_id)
            missing = [s for s in VALID_SLOTS if s not in pointers]
            if missing:
                raise StateError("证据槽位缺失，无法签署：" + "、".join(missing),
                                 code="evidence_incomplete")

            # 签署前再次核验参与专家的回避状态（防止评审期间新增回避）
            assignments = self.store.list_assignments(app_id)
            participating = [a for a in assignments if a["status"] == "submitted"]
            if len(participating) < 2:
                raise StateError("已提交意见的专家不足两名，不能签署", code="review_not_ready")
            participants: list[tuple[str, str, str]] = [
                ("signing_official", signing_official_id, signing_official_name)
            ]
            for a in participating:
                if self.store.active_recusal(app_id, a["expert_id"]) is not None:
                    raise ConflictError(
                        f"专家 {a['expert_name']} 存在在力回避，其意见不得进入签署决定",
                        code="recusal_conflict",
                    )
                participants.append(("expert", a["expert_id"], a["expert_name"]))

            # 用原始输入与选定规则版本计算
            payloads = self.store.load_evidence_payloads(app_id, pointers)
            evaluation = evaluate(package, payloads).to_dict()

            snapshot = {
                "application_id": app_id,
                "rule_version": chosen_version,
                "rule_hash": package.content_hash,
                "evidence": pointers,
                "score": evaluation,
                "participants": [
                    {"role": role, "actor_id": aid, "actor_name": name}
                    for role, aid, name in participants
                ],
            }
            snapshot_hash = content_hash(snapshot)

            decision_seq = self.store.next_decision_seq(app_id)
            decision_id = self.store.insert_decision(
                app_id, decision_seq, chosen_version, evaluation["classification"], evaluation,
                pointers, snapshot_hash, signing_official_id, participants,
            )

            appeal_result: str | None = None
            if re_signing and previous is not None:
                appeal_result = (
                    "adjusted"
                    if evaluation["classification"] != previous["classification_result"]
                    or chosen_version != previous["rule_version"]
                    else "upheld"
                )
                for open_appeal in [a for a in self.store.list_appeals(app_id)
                                    if a["result"] == "open"]:
                    self.store.resolve_appeal(
                        open_appeal["id"], appeal_result,
                        evaluation["classification"] if appeal_result == "adjusted" else None,
                        evaluation,
                    )

            self.store.transition(
                app_id, "签署", signing_official_id,
                f"签署决定 {decision_id}（第 {decision_seq} 版）"
                + (f"，复议结论 {appeal_result}" if appeal_result else ""),
            )
            return {
                "decision_id": decision_id,
                "decision_seq": decision_seq,
                "application_id": app_id,
                "classification": evaluation["classification"],
                "rule_version": chosen_version,
                "snapshot_hash": snapshot_hash,
                "research_score": evaluation["research_score"],
                "applied_score": evaluation["applied_score"],
                "participants": snapshot["participants"],
                "supersedes": previous["id"] if re_signing and previous else None,
                "appeal_result": appeal_result,
            }

        return self.store.write(tx)

    def get_decision(self, app_id: str) -> dict[str, Any]:
        decision = self.store.get_decision(app_id)
        if decision is None:
            raise NotFoundError(f"案卷 {app_id} 尚无签署决定")
        return decision

    def preview_evaluation(self, app_id: str, rule_version: str | None = None) -> dict[str, Any]:
        """只读试算：用案卷当前最新证据与指定（默认激活）规则版本评分。

        不改变案卷、不产生决定。复议人员可用它对比同一批证据在新旧规则
        版本下的差异，但正式复议仍只按签署时冻结的版本重算。
        """
        app = self.store.get_application(app_id)
        version = rule_version or app["current_rule_version"] or self.active_rule_version()
        package = self._load_package(version)
        pointers = self.store.latest_evidence_pointers(app_id)
        missing = [s for s in VALID_SLOTS if s not in pointers]
        if missing:
            raise StateError("证据槽位缺失，无法试算：" + "、".join(missing),
                             code="evidence_incomplete")
        payloads = self.store.load_evidence_payloads(app_id, pointers)
        evaluation = evaluate(package, payloads).to_dict()
        return {"application_id": app_id, "rule_version": version,
                "evidence_pointers": pointers, "evaluation": evaluation}

    # ------------------------------------------------------------------ #
    # 复议：用冻结的规则与原始证据重算
    # ------------------------------------------------------------------ #
    def open_appeal(self, app_id: str, reason: str, created_by: str) -> dict[str, Any]:
        if not reason:
            raise DomainError("复议理由不能为空")

        def tx() -> dict[str, Any]:
            app = self.store.get_application(app_id)
            decision = self.store.get_decision(app_id)
            if decision is None:
                raise StateError("仅已签署案卷可以发起复议")
            open_appeals = [a for a in self.store.list_appeals(app_id) if a["result"] == "open"]
            if open_appeals:
                raise ConflictError("该案卷存在未了结的复议", code="appeal_open")

            snapshot = {
                "rule_version": decision["rule_version"],
                "rule_hash": decision["score"]["rule_hash"],
                "evidence": decision["evidence_snapshot"],
            }
            appeal_id = self.store.create_appeal(
                app_id, decision["id"], reason,
                decision["rule_version"], snapshot["evidence"], created_by,
            )
            self.store.transition(app_id, "复议", created_by, f"发起复议 {appeal_id}")
            return {"appeal_id": appeal_id, "application_id": app_id,
                    "frozen_rule_version": decision["rule_version"], "result": "open"}

        return self.store.write(tx)

    def recompute_appeal(self, appeal_id: str) -> dict[str, Any]:
        """只读地用冻结规则与冻结证据快照重算并比对，不产生新决定。

        复议人员据此还原计算依据：每个指标的算子、输入路径、取值，以及
        与原评分逐项是否一致。
        """
        appeal = self.store.get_appeal(appeal_id)
        decision = self.store.get_decision(appeal["application_id"])
        package = self._load_package(appeal["rule_version"])

        # 哈希校验：规则未变、证据指针指向原始版本
        if package.content_hash != decision["score"]["rule_hash"]:
            raise ConflictError("复议规则包与签署时规则哈希不一致", code="rules_tampered")
        pointers = appeal["frozen_snapshot"]
        payloads = self.store.load_evidence_payloads(appeal["application_id"], pointers)
        recomputed = evaluate(package, payloads).to_dict()

        original = decision["score"]
        metric_diffs: list[dict[str, Any]] = []
        for key in sorted(set(original["detail"]["metrics"]) | set(recomputed["detail"]["metrics"])):
            old = original["detail"]["metrics"].get(key)
            new = recomputed["detail"]["metrics"].get(key)
            metric_diffs.append({
                "metric": key,
                "original": old,
                "recomputed": new,
                "identical": old == new,
            })
        return {
            "appeal_id": appeal_id,
            "rule_version": appeal["rule_version"],
            "rule_hash": package.content_hash,
            "evidence_snapshot": pointers,
            "original_classification": decision["classification_result"],
            "recomputed": recomputed,
            "score_identical": (
                original["research_score"] == recomputed["research_score"]
                and original["applied_score"] == recomputed["applied_score"]
            ),
            "classification_identical":
                decision["classification_result"] == recomputed["classification"],
            "metric_diffs": metric_diffs,
        }

    def resolve_appeal(
        self, appeal_id: str, result: str, official_id: str
    ) -> dict[str, Any]:
        if result not in ("upheld", "adjusted"):
            raise DomainError("复议结论必须是 upheld（维持）或 adjusted（调整）")

        def tx() -> dict[str, Any]:
            appeal = self.store.get_appeal(appeal_id)
            app_id = appeal["application_id"]
            decision = self.store.get_decision(app_id)

            # 了结前在事务内重算，确保结论基于冻结输入
            package = self._load_package(appeal["rule_version"])
            pointers = appeal["frozen_snapshot"]
            payloads = self.store.load_evidence_payloads(app_id, pointers)
            recomputed = evaluate(package, payloads).to_dict()

            if result == "adjusted":
                # 调整：复议重算分类与原分类不同才允许；原决定保留不变，
                # 案卷回到签署态并指向新结论（记录在 appeals 中可追溯）。
                if recomputed["classification"] == decision["classification_result"]:
                    raise DomainError(
                        "重算分类与原决定一致，应作维持（upheld）处理",
                        code="appeal_adjust_without_change",
                    )
            self.store.resolve_appeal(
                appeal_id, result,
                recomputed["classification"] if result == "adjusted" else None,
                recomputed,
            )
            self.store.transition(app_id, "签署", official_id,
                                  f"复议了结：{result}")
            return {"appeal_id": appeal_id, "result": result,
                    "original_classification": decision["classification_result"],
                    "new_classification": recomputed["classification"]
                    if result == "adjusted" else None,
                    "recomputed": recomputed}

        return self.store.write(tx)
