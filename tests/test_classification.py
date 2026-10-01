"""端到端测试：规则引擎确定性、回避、材料补交、规则换版、
签署快照、复议重放，以及幂等/并发/重启下“一案一决定”。
"""
from __future__ import annotations

import json
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

from classification.http_app import _make_handler, create_app
from classification.service import ClassificationService
from classification.services import rules as engine
from classification.services.errors import ServiceError
from classification.services.seeds import seed_rulesets
from classification.storage import Database


# ---- 证据夹具：明确的应用型院校画像 --------------------------------------

APPLIED_EVIDENCE = [
    {"dimension": "mission", "submitted_by": "高校-王老师",
     "content": "学校坚持应用型办学定位，深化产教融合，主动服务区域与地方发展。"},
    {"dimension": "disciplines", "submitted_by": "高校-王老师",
     "content": "以专业学位和应用学科为主，建设现代产业学院与实训平台，行业特色鲜明。"},
    {"dimension": "talent", "submitted_by": "高校-李老师",
     "content": "面向一线培养应用型人才，推行项目式教学、实习实训，双师型教师与产学合作覆盖主干专业。"},
    {"dimension": "service", "submitted_by": "高校-李老师",
     "content": "与企业合作开展技术服务、成果转化、横向课题，建设地方智库并对口支援地市产业。"},
]

WEAK_EVIDENCE = [
    {"dimension": "mission", "submitted_by": "高校-王老师", "content": "学校全面履行办学职责。"},
    {"dimension": "disciplines", "submitted_by": "高校-王老师", "content": "学科门类较为齐全。"},
    {"dimension": "talent", "submitted_by": "高校-李老师", "content": "重视各类人才培养。"},
    {"dimension": "service", "submitted_by": "高校-李老师", "content": "积极开展社会合作。"},
]

INSTITUTION = "江北应用技术学院"


def make_service(db_path: str) -> ClassificationService:
    db = Database(db_path)
    service = ClassificationService(db)
    service.seed()
    return service


def idem(prefix: str) -> str:
    # 唯一键由测试侧生成；真实客户端应生成稳定 UUID 并在重试时复用。
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def prepare_signed_case(service: ClassificationService, evidence=None,
                        ruleset_version: str | None = None) -> tuple[str, dict]:
    """走完受理→指派→证据→评审全流程并签署，返回 (case_id, decision)。"""
    create_payload = {"idempotency_key": idem("case"), "case_no": idem("NO"),
                      "institution": INSTITUTION}
    if ruleset_version:
        create_payload["ruleset_version"] = ruleset_version
    case = service.create_case(create_payload)
    case_id = case["id"]
    service.accept_case(case_id)

    experts = []
    for name in ("张评审", "刘评审"):
        expert = service.register_expert({"name": name, "org": "省教育研究院"})
        experts.append(expert["id"])
        service.assign_expert(case_id, {"expert_id": expert["id"]})

    service.submit_evidence(case_id, {"idempotency_key": idem("ev"),
                                      "submitted_by": "高校-王老师",
                                      "evidence": evidence or APPLIED_EVIDENCE})
    service.start_review(case_id)
    for expert_id in experts:
        service.submit_review(case_id, {"idempotency_key": idem("rev"),
                                        "expert_id": expert_id, "opinion": "support"})
    decision = service.sign_decision(case_id, {"idempotency_key": idem("sign"),
                                               "signed_by": "主管部门-赵处长"})
    return case_id, decision


class RulesEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ruleset = seed_rulesets()[1]  # 2026.1

    def test_deterministic_scores_and_applied_wins(self) -> None:
        r1 = engine.evaluate(self.ruleset, APPLIED_EVIDENCE)
        r2 = engine.evaluate(self.ruleset, APPLIED_EVIDENCE)
        self.assertEqual(r1, r2)
        self.assertEqual(r1["recommendation"], "applied")
        self.assertFalse(r1["indeterminate"])
        self.assertEqual(r1["ranking"][0], "applied")
        self.assertEqual(r1["scores"]["applied"]["total"], "1.000000")

    def test_weak_evidence_is_indeterminate(self) -> None:
        result = engine.evaluate(self.ruleset, WEAK_EVIDENCE)
        self.assertIsNone(result["recommendation"])
        self.assertTrue(result["indeterminate"])

    def test_weights_must_sum_to_one(self) -> None:
        bad = json.loads(json.dumps(self.ruleset))
        bad["weights"]["mission"] = 0.9
        with self.assertRaises(ValueError):
            engine.validate_ruleset(bad)

    def test_replay_reproduces_result(self) -> None:
        result = engine.evaluate(self.ruleset, APPLIED_EVIDENCE)
        snapshot = {"ruleset": self.ruleset, "evidence": APPLIED_EVIDENCE,
                    "evaluation": result}
        report = engine.replay(snapshot)
        self.assertTrue(report["reproduced"])
        self.assertEqual(report["original_result_fingerprint"],
                         report["rerun_result_fingerprint"])

    def test_replay_detects_tampered_ruleset(self) -> None:
        result = engine.evaluate(self.ruleset, APPLIED_EVIDENCE)
        tampered = json.loads(json.dumps(self.ruleset))
        # 替换一条实际命中的锚定词，使重放覆盖率与原结果不一致
        applied = next(c for c in tampered["categories"] if c["key"] == "applied")
        applied["anchors"]["mission"].remove("产教融合")
        applied["anchors"]["mission"].append("从未出现的表述")
        snapshot = {"ruleset": tampered, "evidence": APPLIED_EVIDENCE,
                    "evaluation": result}
        report = engine.replay(snapshot)
        self.assertFalse(report["reproduced"])


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = make_service(str(Path(self.tmp.name) / "workflow.db"))

    def tearDown(self) -> None:
        self.service.db.close()
        self.tmp.cleanup()

    def test_full_flow_signed_snapshot_and_validity(self) -> None:
        case_id, decision = prepare_signed_case(self.service)
        self.assertEqual(decision["snapshot"]["evaluation"]["recommendation"], "applied")
        self.assertLess(decision["valid_from"], decision["valid_to"])
        self.assertEqual(decision["valid_to"][:4],
                         str(int(decision["valid_from"][:4]) + 5))
        snap = decision["snapshot"]
        self.assertEqual(snap["ruleset_version"], "2026.1")
        self.assertEqual(snap["participants"]["signed_by"], "主管部门-赵处长")
        self.assertEqual(len(snap["participants"]["reviewers"]), 2)
        self.assertIn("高校-王老师", snap["participants"]["evidence_submitters"])
        case = self.service.get_case(case_id)
        self.assertEqual(case["status"], "signed")

    def test_conflicted_expert_is_blocked_and_recused_cannot_review(self) -> None:
        case = self.service.create_case({"idempotency_key": idem("case"),
                                         "case_no": idem("NO"),
                                         "institution": INSTITUTION})
        self.service.accept_case(case["id"])
        insider = self.service.register_expert(
            {"name": "钱教授", "org": INSTITUTION})
        with self.assertRaises(ServiceError) as caught:
            self.service.assign_expert(case["id"], {"expert_id": insider["id"]})
        self.assertEqual(caught.exception.status, 403)

        outsider = self.service.register_expert({"name": "孙教授", "org": "省外大学"})
        self.service.assign_expert(case["id"], {"expert_id": outsider["id"]})
        self.service.recuse_expert(case["id"], outsider["id"], {"reason": "近期合作项目"})
        with self.assertRaises(ServiceError):
            self.service.submit_review(case["id"],
                                       {"idempotency_key": idem("rev"),
                                        "expert_id": outsider["id"], "opinion": "support"})
        # 仅一名未回避专家：不能进入评审，更不能签署
        other = self.service.register_expert({"name": "周教授", "org": "省教育研究院"})
        self.service.assign_expert(case["id"], {"expert_id": other["id"]})
        self.service.submit_evidence(case["id"], {"idempotency_key": idem("ev"),
                                                  "submitted_by": "高校-王老师",
                                                  "evidence": APPLIED_EVIDENCE})
        detail = self.service.get_case(case["id"])
        active = [e for e in detail["experts"] if e["role"] == "reviewer"]
        self.assertEqual(len(active), 1)
        with self.assertRaises(ServiceError):
            self.service.start_review(case["id"])
        self.assertEqual(self.service.get_case(case["id"])["status"], "accepted")
        with self.assertRaises(ServiceError):
            self.service.sign_decision(case["id"], {"idempotency_key": idem("sign"),
                                                    "signed_by": "主管部门-赵处长"})

    def test_start_review_requires_two_reviewers_and_evidence(self) -> None:
        case = self.service.create_case({"idempotency_key": idem("case"),
                                         "case_no": idem("NO"),
                                         "institution": INSTITUTION})
        self.service.accept_case(case["id"])
        with self.assertRaises(ServiceError) as caught:
            self.service.start_review(case["id"])
        self.assertEqual(caught.exception.status, 409)

    def test_indeterminate_evidence_blocks_signing(self) -> None:
        case = self.service.create_case({"idempotency_key": idem("case"),
                                         "case_no": idem("NO"),
                                         "institution": INSTITUTION})
        case_id = case["id"]
        self.service.accept_case(case_id)
        experts = []
        for name in ("甲专家", "乙专家"):
            expert = self.service.register_expert({"name": name, "org": "省教育研究院"})
            experts.append(expert["id"])
            self.service.assign_expert(case_id, {"expert_id": expert["id"]})
        self.service.submit_evidence(case_id, {"idempotency_key": idem("ev"),
                                               "submitted_by": "高校-王老师",
                                               "evidence": WEAK_EVIDENCE})
        self.service.start_review(case_id)
        for expert_id in experts:
            self.service.submit_review(case_id, {"idempotency_key": idem("rev"),
                                                 "expert_id": expert_id, "opinion": "support"})
        with self.assertRaises(ServiceError) as caught:
            self.service.sign_decision(case_id, {"idempotency_key": idem("sign"),
                                                 "signed_by": "主管部门-赵处长"})
        self.assertIn("min_margin", caught.exception.extra)


class EvidenceSupplementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = make_service(str(Path(self.tmp.name) / "supplement.db"))
        case = self.service.create_case({"idempotency_key": idem("case"),
                                         "case_no": idem("NO"),
                                         "institution": INSTITUTION})
        self.case_id = case["id"]
        self.service.accept_case(self.case_id)
        self.experts = []
        for name in ("甲评审", "乙评审"):
            expert = self.service.register_expert({"name": name, "org": "省教育研究院"})
            self.experts.append(expert["id"])
            self.service.assign_expert(self.case_id, {"expert_id": expert["id"]})
        self.service.submit_evidence(self.case_id, {"idempotency_key": idem("ev"),
                                                    "submitted_by": "高校-王老师",
                                                    "evidence": APPLIED_EVIDENCE})
        self.service.start_review(self.case_id)

    def tearDown(self) -> None:
        self.service.db.close()
        self.tmp.cleanup()

    def test_supplement_invalidates_old_reviews_but_snapshot_keeps_history(self) -> None:
        for expert_id in self.experts:
            self.service.submit_review(self.case_id, {"idempotency_key": idem("rev"),
                                                      "expert_id": expert_id,
                                                      "opinion": "support"})
        # 补交材料 → 版本 2，旧意见挂在版本 1
        supplemented = [dict(item, content=item["content"] + "（补充年度质量报告佐证）")
                        for item in APPLIED_EVIDENCE]
        v2 = self.service.submit_evidence(self.case_id,
                                          {"idempotency_key": idem("ev2"),
                                           "submitted_by": "高校-补充材料专员",
                                           "evidence": supplemented})
        self.assertEqual(v2["version"], 2)
        self.assertIsNotNone(v2["supersedes"])

        with self.assertRaises(ServiceError) as caught:
            self.service.sign_decision(self.case_id,
                                       {"idempotency_key": idem("sign"),
                                        "signed_by": "主管部门-赵处长"})
        self.assertEqual(caught.exception.extra["evidence_version"], 2)
        self.assertEqual(len(caught.exception.extra["awaiting"]), 2)

        for expert_id in self.experts:
            self.service.submit_review(self.case_id, {"idempotency_key": idem("rev2"),
                                                      "expert_id": expert_id,
                                                      "opinion": "support"})
        decision = self.service.sign_decision(self.case_id,
                                              {"idempotency_key": idem("sign2"),
                                               "signed_by": "主管部门-赵处长"})
        snap = decision["snapshot"]
        self.assertEqual(snap["evidence_version"], 2)
        self.assertEqual(len(snap["evidence_history"]), 2)
        self.assertEqual(snap["evidence_history"][0]["version"], 1)
        # 历史意见（针对 v1）与最终意见（针对 v2）都在快照中
        self.assertEqual(len(snap["participants"]["review_history"]), 4)
        self.assertEqual(
            {r["expert_id"] for r in snap["participants"]["reviewers"]},
            set(self.experts),
        )


class IdempotencyConcurrencyRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "concurrency.db")
        self.service = make_service(self.path)

    def tearDown(self) -> None:
        self.service.db.close()
        self.tmp.cleanup()

    def _ready_case(self) -> tuple[str, list[str], str]:
        case = self.service.create_case({"idempotency_key": idem("case"),
                                         "case_no": idem("NO"),
                                         "institution": INSTITUTION})
        case_id = case["id"]
        self.service.accept_case(case_id)
        expert_ids = []
        for name in ("并发专家甲", "并发专家乙"):
            expert = self.service.register_expert({"name": name, "org": "省教育研究院"})
            expert_ids.append(expert["id"])
            self.service.assign_expert(case_id, {"expert_id": expert["id"]})
        self.service.submit_evidence(case_id, {"idempotency_key": idem("ev"),
                                               "submitted_by": "高校-王老师",
                                               "evidence": APPLIED_EVIDENCE})
        self.service.start_review(case_id)
        for expert_id in expert_ids:
            self.service.submit_review(case_id, {"idempotency_key": idem("rev"),
                                                 "expert_id": expert_id, "opinion": "support"})
        return case_id, expert_ids, idem("sign")

    def test_duplicate_submission_returns_same_resource(self) -> None:
        key = idem("case")
        first = self.service.create_case({"idempotency_key": key, "case_no": "DUP-1",
                                          "institution": INSTITUTION})
        second = self.service.create_case({"idempotency_key": key, "case_no": "DUP-1",
                                           "institution": INSTITUTION})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.list_cases()), 1)

    def test_concurrent_sign_with_same_key_makes_one_decision(self) -> None:
        case_id, _, key = self._ready_case()
        payload = {"idempotency_key": key, "signed_by": "主管部门-赵处长"}

        def sign() -> dict:
            return self.service.sign_decision(case_id, payload)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: sign(), range(8)))
        decision_ids = {r["id"] for r in results}
        self.assertEqual(len(decision_ids), 1)
        count = self.service.db.query_one(
            "SELECT COUNT(*) AS n FROM decisions WHERE case_id=?", (case_id,)
        )["n"]
        self.assertEqual(count, 1)
        signed_events = self.service.db.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE case_id=? AND event_type='decision_signed'",
            (case_id,),
        )["n"]
        self.assertEqual(signed_events, 1)

    def test_second_distinct_sign_is_rejected(self) -> None:
        case_id, _, _ = self._ready_case()
        self.service.sign_decision(case_id, {"idempotency_key": idem("sign"),
                                             "signed_by": "主管部门-赵处长"})
        with self.assertRaises(ServiceError) as caught:
            self.service.sign_decision(case_id, {"idempotency_key": idem("sign"),
                                                 "signed_by": "主管部门-赵处长"})
        self.assertEqual(caught.exception.status, 409)

    def test_state_survives_restart(self) -> None:
        case_id, decision = prepare_signed_case(self.service)
        decision_id = decision["id"]
        self.service.db.close()

        restarted = make_service(self.path)  # 进程重启：重新打开同一数据库
        try:
            fetched = restarted.get_decision(case_id)
            self.assertEqual(fetched["id"], decision_id)
            self.assertEqual(fetched["snapshot_hash"], decision["snapshot_hash"])
            recon = restarted.request_reconsideration(
                case_id, {"idempotency_key": idem("rec"),
                          "requested_by": "高校-王老师", "reason": "申请核实计算依据"})
            self.assertTrue(recon["replay_report"]["reproduced"])
        finally:
            restarted.db.close()


class RulesVersioningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = make_service(str(Path(self.tmp.name) / "version.db"))

    def tearDown(self) -> None:
        self.service.db.close()
        self.tmp.cleanup()

    def test_signed_case_pins_old_ruleset_after_new_version_published(self) -> None:
        # 旧规则 2024.1 下签署
        case_id, decision = prepare_signed_case(self.service,
                                                ruleset_version="2024.1")
        self.assertEqual(decision["snapshot"]["ruleset_version"], "2024.1")
        old_anchor_count = len(
            decision["snapshot"]["ruleset"]["categories"][0]["anchors"]["mission"])
        self.assertEqual(old_anchor_count, 4)

        # 换版：发布 2027.1（新权重），只影响之后受理的案件
        new_ruleset = json.loads(json.dumps(seed_rulesets()[1]))
        new_ruleset["ruleset_version"] = "2027.1"
        new_ruleset["weights"] = {"mission": 0.20, "disciplines": 0.20,
                                  "talent": 0.30, "service": 0.30}
        self.service.publish_ruleset({"ruleset": new_ruleset,
                                      "effective_from": "2027-01-01",
                                      "idempotency_key": idem("rs")})

        # 已签结论重放仍用快照内旧规则全文
        recon = self.service.request_reconsideration(
            case_id, {"idempotency_key": idem("rec"),
                      "requested_by": "高校-王老师", "reason": "规则换版后复核"})
        self.assertTrue(recon["replay_report"]["reproduced"])
        rerun = recon["replay_report"]["rerun"]
        self.assertEqual(rerun["weights"]["talent"], "0.25")  # 旧权重

        # 新案件绑定新版本
        case = self.service.create_case({"idempotency_key": idem("case"),
                                         "case_no": idem("NO"),
                                         "institution": INSTITUTION,
                                         "ruleset_version": "2027.1"})
        self.assertEqual(case["ruleset_version"], "2027.1")

    def test_duplicate_ruleset_version_rejected(self) -> None:
        with self.assertRaises(ServiceError):
            self.service.publish_ruleset({"ruleset": seed_rulesets()[0],
                                          "effective_from": "2024-01-01"})


class ReconsiderationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = make_service(str(Path(self.tmp.name) / "recon.db"))
        self.case_id, _ = prepare_signed_case(self.service)

    def tearDown(self) -> None:
        self.service.db.close()
        self.tmp.cleanup()

    def test_reconsideration_replay_and_resolve_upheld(self) -> None:
        recon = self.service.request_reconsideration(
            self.case_id, {"idempotency_key": idem("rec"),
                           "requested_by": "高校-王老师", "reason": "对评分依据有疑问"})
        self.assertEqual(recon["status"], "open")
        self.assertTrue(recon["replay_report"]["reproduced"])
        self.assertEqual(
            recon["replay_report"]["rerun"]["scores"]["applied"]["total"],
            "1.000000",
        )
        self.assertEqual(self.service.get_case(self.case_id)["status"], "reconsidering")

        self.service.resolve_reconsideration(
            self.case_id, {"reconsideration_id": recon["id"], "outcome": "upheld",
                           "resolved_by": "主管部门-赵处长", "comment": "重放一致，维持原结论"})
        self.assertEqual(self.service.get_case(self.case_id)["status"], "signed")
        # 原决定内容不变
        decision = self.service.get_decision(self.case_id)
        self.assertEqual(decision["snapshot"]["evaluation"]["recommendation"], "applied")

    def test_reconsideration_requires_signed_case(self) -> None:
        case = self.service.create_case({"idempotency_key": idem("case"),
                                         "case_no": idem("NO"),
                                         "institution": INSTITUTION})
        with self.assertRaises(ServiceError):
            self.service.request_reconsideration(
                case["id"], {"idempotency_key": idem("rec"),
                             "requested_by": "x", "reason": "y"})


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Database(str(Path(cls.tmp.name) / "http.db"))
        cls.service = create_app(cls.db)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cls.service))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.close()
        cls.tmp.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_and_rulesets(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        status, body = self._request("GET", "/api/rulesets")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["rulesets"]), 2)

    def test_duplicate_post_over_http_is_idempotent(self) -> None:
        key = idem("http-case")
        payload = {"idempotency_key": key, "case_no": "HTTP-1",
                   "institution": INSTITUTION}
        s1, b1 = self._request("POST", "/api/cases", payload)
        s2, b2 = self._request("POST", "/api/cases", payload)
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(b1["id"], b2["id"])

    def test_missing_idempotency_key_rejected(self) -> None:
        status, body = self._request("POST", "/api/cases",
                                     {"case_no": "X", "institution": INSTITUTION})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bad_request")

    def test_concurrent_sign_over_http_yields_one_decision(self) -> None:
        # 准备案件
        key = idem("http-case2")
        _, case = self._request("POST", "/api/cases",
                                {"idempotency_key": key, "case_no": "HTTP-2",
                                 "institution": INSTITUTION})
        case_id = case["id"]
        self._request("POST", f"/api/cases/{case_id}/accept")
        expert_ids = []
        for name in ("网络专家甲", "网络专家乙"):
            _, expert = self._request("POST", "/api/experts",
                                      {"name": name, "org": "省教育研究院"})
            expert_ids.append(expert["id"])
            self._request("POST", f"/api/cases/{case_id}/experts",
                          {"expert_id": expert["id"]})
        self._request("POST", f"/api/cases/{case_id}/evidence",
                      {"idempotency_key": idem("http-ev"),
                       "submitted_by": "高校-王老师", "evidence": APPLIED_EVIDENCE})
        self._request("POST", f"/api/cases/{case_id}/review/start")
        for expert_id in expert_ids:
            self._request("POST", f"/api/cases/{case_id}/reviews",
                          {"idempotency_key": idem("http-rev"),
                           "expert_id": expert_id, "opinion": "support"})
        sign_key = idem("http-sign")

        def sign() -> tuple[int, dict]:
            return self._request("POST", f"/api/cases/{case_id}/sign",
                                 {"idempotency_key": sign_key,
                                  "signed_by": "主管部门-赵处长"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda _: sign(), range(6)))
        decision_ids = {body["id"] for _, body in responses}
        self.assertEqual(len(decision_ids), 1)
        status, body = self._request("GET", f"/api/cases/{case_id}/decision")
        self.assertEqual(status, 200)
        self.assertEqual(body["snapshot"]["evaluation"]["recommendation"], "applied")


if __name__ == "__main__":
    unittest.main()
