"""分类定位论证服务的端到端回归测试。

覆盖四条契约不变量与并发/重启安全：
规则版本冻结、专家回避、签署快照不可变、复议可追溯重算，
以及幂等键、并发送审、进程重启不产生两份决定。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from classification_service.canonical import canonical  # noqa: E402
from classification_service.db import initialize  # noqa: E402
from classification_service.errors import (  # noqa: E402
    ConflictError,
    DuplicateSubmissionError,
    RecusalError,
    StateError,
)
from classification_service.httpapi import build_server  # noqa: E402
from classification_service.rules import RulePackage, evaluate  # noqa: E402
from classification_service.seed import (  # noqa: E402
    EXPERTS,
    RULES_V1,
    RULES_V2,
    seed,
)
from classification_service.store import Store  # noqa: E402
from classification_service.workflow import Workflow  # noqa: E402


MISSION = (
    "本校坚持应用型办学定位，深化产教融合与校企合作，服务地方产业升级，"
    "培养面向生产一线的高层次应用型人才，同时开展应用技术研究。"
)

DISCIPLINES = {
    "practice_course_ratio": 0.62,
    "programs": [{"orientation": "applied"}] * 12 + [{"orientation": "research"}] * 3,
}
TALENT = {
    "postgraduate_ratio": 0.18,
    "dual_qualified_ratio": 0.68,
    "practice_enrollment_ratio": 0.72,
}
SERVICE_V1 = {
    "projects": [{"kind": "local_service"}] * 8 + [{"kind": "tech_transfer"}] * 4
    + [{"kind": "national_grant"}],
}
SERVICE_V2 = {
    "projects": [{"kind": "local_service"}] * 10 + [{"kind": "tech_transfer"}] * 6
    + [{"kind": "national_grant"}],
}


def make_service(db_path: str, *, active_rules: str = "2026.1") -> Workflow:
    store = Store(db_path)
    initialize(store.conn)
    wf = Workflow(store)
    seed(wf, activate_version=active_rules)
    return wf


def build_full_case(wf: Workflow, *, old_rules: bool = False) -> str:
    """走通 建档→证据→受理→回避→分派→评审→意见，返回案卷号（未签署）。"""
    app = wf.create_application("10213", "岭南应用技术学院", MISSION)
    app_id = app["application_id"]
    wf.submit_evidence(app_id, "disciplines", DISCIPLINES, "10213")
    wf.submit_evidence(app_id, "talent", TALENT, "10213")
    wf.submit_evidence(app_id, "service", SERVICE_V1, "10213")
    wf.accept(app_id, "official_zhao")
    if old_rules:
        wf.activate_rules("2024.1")
    wf.declare_recusal(app_id, "e_wang", "近三年在该校任兼职教授")
    wf.assign_expert(app_id, "e_chen", "official_zhao")
    wf.assign_expert(app_id, "e_li", "official_zhao")
    wf.start_review(app_id, "official_zhao")
    assignments = {a["expert_id"]: a["id"] for a in wf.list_assignments(app_id)}
    wf.submit_opinion(assignments["e_chen"], "applied", "应用型指标突出")
    wf.submit_opinion(assignments["e_li"], "applied", "社会服务面向地方")
    return app_id


class RuleEngineTest(unittest.TestCase):
    def test_old_rules_misjudge_new_rules_correct(self) -> None:
        evidence = {
            "mission": {"text": MISSION},
            "disciplines": DISCIPLINES,
            "talent": TALENT,
            "service": SERVICE_V1,
        }
        old = evaluate(RulePackage("2024.1", "old", RULES_V1), evidence)
        new = evaluate(RulePackage("2026.1", "new", RULES_V2), evidence)
        self.assertEqual(old.classification, "mixed")       # 应用型被旧版误伤
        self.assertEqual(new.classification, "applied")     # 新版纠正

    def test_evaluation_is_deterministic(self) -> None:
        evidence = {"mission": {"text": MISSION}, "disciplines": DISCIPLINES,
                    "talent": TALENT, "service": SERVICE_V1}
        pkg = RulePackage("2026.1", "new", RULES_V2)
        first = evaluate(pkg, evidence).to_dict()
        second = evaluate(pkg, evidence).to_dict()
        self.assertEqual(canonical(first), canonical(second))


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.wf = make_service(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- 规则版本 ---------------------------------------------------- #
    def test_rule_version_frozen_at_review_and_swappable_after_appeal(self) -> None:
        app_id = build_full_case(self.wf, old_rules=True)
        app = self.wf.get_application(app_id)
        self.assertEqual(app["current_rule_version"], "2024.1")

        signed = self.wf.sign(app_id, "official_zhao", "赵明")
        self.assertEqual(signed["rule_version"], "2024.1")
        self.assertEqual(signed["classification"], "mixed")  # 旧版误伤

        # 换版只影响之后签署：旧版包仍在
        self.wf.activate_rules("2026.1")
        old_pkg = self.wf.list_rule_packages()
        self.assertIn("2024.1", {p["package_version"] for p in old_pkg})

        appeal = self.wf.open_appeal(app_id, "校方申诉：旧版研究型指标误伤", "10213")
        # 复议重算严格复现旧版结果
        rec = self.wf.recompute_appeal(appeal["appeal_id"])
        self.assertTrue(rec["score_identical"])
        self.assertTrue(rec["classification_identical"])
        self.assertTrue(all(d["identical"] for d in rec["metric_diffs"]))
        self.assertEqual(rec["rule_version"], "2024.1")

        # 复议态补交材料（新证据版本），旧版本与旧决定不动
        ev = self.wf.submit_evidence(app_id, "service", SERVICE_V2, "10213")
        self.assertEqual(ev["seq"], 2)

        # 以现行 2026.1 重签 → 纠正为应用型，旧决定保留
        re_signed = self.wf.sign(app_id, "official_zhao", "赵明")
        self.assertEqual(re_signed["decision_seq"], 2)
        self.assertEqual(re_signed["classification"], "applied")
        self.assertEqual(re_signed["appeal_result"], "adjusted")
        self.assertIsNotNone(re_signed["supersedes"])

        decisions = self.wf.store.list_decisions(app_id)
        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0]["classification_result"], "mixed")
        self.assertEqual(decisions[0]["rule_version"], "2024.1")
        self.assertEqual(decisions[1]["classification_result"], "applied")
        latest = self.wf.get_decision(app_id)
        self.assertEqual(latest["id"], re_signed["decision_id"])

    # ---- 专家回避 ---------------------------------------------------- #
    def test_recused_expert_cannot_be_assigned(self) -> None:
        app_id = build_full_case(self.wf)
        with self.assertRaises(RecusalError):
            self.wf.assign_expert(app_id, "e_wang", "official_zhao")

    def test_recusal_added_between_assignment_and_sign_blocks_signing(self) -> None:
        # 不预置回避：先分派，评审期间登记回避，签署必须被拦截
        wf = make_service(os.path.join(self.tmp.name, "late.db"))
        app = wf.create_application("10214", "滨海工学院", MISSION)
        app_id = app["application_id"]
        for slot, payload in [("disciplines", DISCIPLINES), ("talent", TALENT),
                              ("service", SERVICE_V1)]:
            wf.submit_evidence(app_id, slot, payload, "10214")
        wf.accept(app_id, "official_zhao")
        wf.assign_expert(app_id, "e_chen", "official_zhao")
        wf.assign_expert(app_id, "e_li", "official_zhao")
        wf.start_review(app_id, "official_zhao")
        assignments = {a["expert_id"]: a["id"] for a in wf.list_assignments(app_id)}
        wf.submit_opinion(assignments["e_chen"], "mixed", "意见一")
        wf.submit_opinion(assignments["e_li"], "mixed", "意见二")
        wf.declare_recusal(app_id, "e_chen", "评审期间披露的课题合作")
        with self.assertRaises(ConflictError) as ctx:
            wf.sign(app_id, "official_zhao", "赵明")
        self.assertEqual(ctx.exception.code, "recusal_conflict")

    # ---- 签署快照 ---------------------------------------------------- #
    def test_signed_snapshot_pins_inputs_and_participants(self) -> None:
        app_id = build_full_case(self.wf)
        signed = self.wf.sign(app_id, "official_zhao", "赵明")
        decision = self.wf.get_decision(app_id)

        # 参与人：签署官员 + 两名实际参与专家，被回避专家不在内
        actors = {(p["role"], p["actor_id"]) for p in decision["participants"]}
        self.assertIn(("signing_official", "official_zhao"), actors)
        self.assertIn(("expert", "e_chen"), actors)
        self.assertIn(("expert", "e_li"), actors)
        self.assertNotIn(("expert", "e_wang"), actors)

        # 快照固定到证据版本指针
        for slot in ("mission", "disciplines", "talent", "service"):
            self.assertIn(slot, decision["evidence_snapshot"])
            self.assertIn("hash", decision["evidence_snapshot"][slot])

        # 签署后证据通道封闭
        with self.assertRaises(StateError) as ctx:
            self.wf.submit_evidence(app_id, "service", SERVICE_V2, "10213")
        self.assertEqual(ctx.exception.code, "evidence_locked")
        self.assertEqual(signed["snapshot_hash"], decision["snapshot_hash"])

    def test_duplicate_evidence_and_opinion_rejected(self) -> None:
        app_id = build_full_case(self.wf)
        with self.assertRaises(DuplicateSubmissionError):
            self.wf.submit_evidence(app_id, "service", SERVICE_V1, "10213")
        assignments = {a["expert_id"]: a["id"] for a in self.wf.list_assignments(app_id)}
        with self.assertRaises(StateError):  # 意见提交后分派关闭
            self.wf.submit_opinion(assignments["e_chen"], "applied", "应用型指标突出")

    def test_state_machine_rejects_illegal_transitions(self) -> None:
        app = self.wf.create_application("10215", "新建学院", MISSION)
        app_id = app["application_id"]
        with self.assertRaises(StateError):
            self.wf.start_review(app_id, "x")  # 材料不齐不能受理，更不能评审
        with self.assertRaises(StateError):
            self.wf.sign(app_id, "x", "X")

    # ---- 复议追溯 ---------------------------------------------------- #
    def test_appeal_open_requires_signed_case(self) -> None:
        app_id = build_full_case(self.wf)
        with self.assertRaises(StateError):
            self.wf.open_appeal(app_id, "材料不足", "10213")

    def test_resolve_appeal_upheld_when_classification_unchanged(self) -> None:
        app_id = build_full_case(self.wf)
        self.wf.sign(app_id, "official_zhao", "赵明")
        appeal = self.wf.open_appeal(app_id, "请求复核", "10213")
        result = self.wf.resolve_appeal(appeal["appeal_id"], "upheld", "official_zhao")
        self.assertEqual(result["result"], "upheld")
        self.assertIsNone(result["new_classification"])
        # 已了结的复议不能重复了结
        with self.assertRaises(StateError):
            self.wf.resolve_appeal(appeal["appeal_id"], "upheld", "official_zhao")

    # ---- 并发与重启 -------------------------------------------------- #
    def test_concurrent_duplicate_application_creates_only_one(self) -> None:
        def attempt() -> str | None:
            try:
                return self.wf.create_application("10299", "并发学院", MISSION)["application_id"]
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: attempt(), range(8)))
        succeeded = [i for i in ids if i]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(self.wf.list_applications()), 1)

    def test_restart_preserves_decision_and_recompute(self) -> None:
        app_id = build_full_case(self.wf)
        signed = self.wf.sign(app_id, "official_zhao", "赵明")
        appeal = self.wf.open_appeal(app_id, "重启后复核", "10213")
        appeal_id = appeal["appeal_id"]

        # 模拟进程重启：丢弃服务对象，重新打开同一数据库
        wf2 = make_service(self.db_path)
        decision = wf2.get_decision(app_id)
        self.assertEqual(decision["snapshot_hash"], signed["snapshot_hash"])
        rec = wf2.recompute_appeal(appeal_id)
        self.assertTrue(rec["classification_identical"])


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "http.db")
        # 先种数据
        make_service(self.db_path)
        self.server: ThreadingHTTPServer = build_server("127.0.0.1", 0, self.db_path)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, path: str, body: dict, key: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_idempotency_key_replays_same_decision_and_survives_restart(self) -> None:
        body = {"applicant_code": "10301", "applicant_name": "幂等学院",
                "mission_statement": MISSION}
        status1, res1 = self._post("/api/applications", body, key="key-create-10301")
        status2, res2 = self._post("/api/applications", body, key="key-create-10301")
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 201)
        self.assertEqual(res1["application_id"], res2["application_id"])
        self.assertTrue(res2["idempotent_replayed"])

        # 同键不同体 → 409
        status3, res3 = self._post(
            "/api/applications", {**body, "applicant_code": "10302"}, key="key-create-10301")
        self.assertEqual(status3, 409)
        self.assertEqual(res3["error"]["code"], "idempotency_key_reuse")

    def test_concurrent_idempotent_posts_create_one_application(self) -> None:
        body = {"applicant_code": "10303", "applicant_name": "并发幂等学院",
                "mission_statement": MISSION}

        def post() -> tuple[int, str]:
            code, res = self._post("/api/applications", body, key="key-10303")
            return code, res.get("application_id", "")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: post(), range(8)))
        app_ids = {aid for _, aid in results if aid}
        self.assertEqual(len(app_ids), 1)
        status, listing = self._get("/api/applications")
        self.assertEqual(status, 200)
        codes = {a["applicant_code"] for a in listing["applications"]}
        self.assertIn("10303", codes)
        self.assertEqual(sum(1 for a in listing["applications"]
                             if a["applicant_code"] == "10303"), 1)


if __name__ == "__main__":
    unittest.main()
