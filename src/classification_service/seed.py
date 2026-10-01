"""种子数据：两版分类规则、示范专家与院校案卷。

``2024.1`` 为科研导向旧版：指标少、阈值偏研究型，应用型学校易被研究生比例、
国家级课题等研究型指标拖累；``2026.1`` 增加实践课程、双师型教师、技术转化
等应用型指标并重校阈值。两版并存，已签旧版结论永远可用旧版复算。
"""
from __future__ import annotations

from typing import Any

from .workflow import Workflow

RULES_V1: dict[str, Any] = {
    "slots": {
        "mission": {
            "weight": 0.2,
            "metrics": [
                {"key": "m_research", "axis": "research", "op": "keyword_density",
                 "path": "text", "keywords": ["研究", "学术", "理论", "学科前沿"]},
                {"key": "m_applied", "axis": "applied", "op": "keyword_density",
                 "path": "text", "keywords": ["应用", "产教融合", "服务地方", "职业", "应用型"]},
            ],
        },
        "disciplines": {
            "weight": 0.3,
            "metrics": [
                {"key": "d_research_share", "axis": "research", "op": "share",
                 "path": "programs", "match": {"orientation": "research"}},
                {"key": "d_applied_share", "axis": "applied", "op": "share",
                 "path": "programs", "match": {"orientation": "applied"}},
            ],
        },
        "talent": {
            "weight": 0.3,
            "metrics": [
                {"key": "t_postgraduate_ratio", "axis": "research", "op": "ratio",
                 "path": "postgraduate_ratio"},
                {"key": "t_dual_qualified", "axis": "applied", "op": "ratio",
                 "path": "dual_qualified_ratio"},
            ],
        },
        "service": {
            "weight": 0.2,
            "metrics": [
                {"key": "s_grant_share", "axis": "research", "op": "share",
                 "path": "projects", "match": {"kind": "national_grant"}},
                {"key": "s_local_share", "axis": "applied", "op": "share",
                 "path": "projects", "match": {"kind": "local_service"}},
            ],
        },
    },
    "decision": {"research_threshold": 0.35, "applied_threshold": 0.8, "margin": 0.15},
}

RULES_V2: dict[str, Any] = {
    "slots": {
        "mission": {
            "weight": 0.2,
            "metrics": [
                {"key": "m_research", "axis": "research", "op": "keyword_density",
                 "path": "text", "keywords": ["研究", "学术", "理论", "学科前沿"]},
                {"key": "m_applied", "axis": "applied", "op": "keyword_density",
                 "path": "text", "keywords": ["应用", "产教融合", "服务地方", "职业", "应用型"]},
            ],
        },
        "disciplines": {
            "weight": 0.3,
            "metrics": [
                {"key": "d_research_share", "axis": "research", "op": "share",
                 "path": "programs", "match": {"orientation": "research"}},
                {"key": "d_applied_share", "axis": "applied", "op": "share",
                 "path": "programs", "match": {"orientation": "applied"}},
                {"key": "d_practice_course_ratio", "axis": "applied", "op": "ratio",
                 "path": "practice_course_ratio"},
            ],
        },
        "talent": {
            "weight": 0.3,
            "metrics": [
                {"key": "t_postgraduate_ratio", "axis": "research", "op": "ratio",
                 "path": "postgraduate_ratio"},
                {"key": "t_dual_qualified", "axis": "applied", "op": "ratio",
                 "path": "dual_qualified_ratio"},
                {"key": "t_practice_enrollment", "axis": "applied", "op": "ratio",
                 "path": "practice_enrollment_ratio"},
            ],
        },
        "service": {
            "weight": 0.2,
            "metrics": [
                {"key": "s_grant_share", "axis": "research", "op": "share",
                 "path": "projects", "match": {"kind": "national_grant"}},
                {"key": "s_local_share", "axis": "applied", "op": "share",
                 "path": "projects", "match": {"kind": "local_service"}},
                {"key": "s_tech_transfer", "axis": "applied", "op": "share",
                 "path": "projects", "match": {"kind": "tech_transfer"}},
            ],
        },
    },
    "decision": {"research_threshold": 0.55, "applied_threshold": 0.45, "margin": 0.05},
}

EXPERTS = [
    ("e_chen", "陈静", "省城理工大学"),
    ("e_li", "李卫国", "省教育科学研究院"),
    ("e_wang", "王敏", "城南职业技术学院"),
]


def seed(wf: Workflow, *, activate_version: str = "2026.1") -> dict[str, str]:
    """幂等写入种子数据，返回规则版本哈希表。已存在则跳过。"""
    hashes: dict[str, str] = {}
    existing = {p["package_version"] for p in wf.list_rule_packages()}
    if "2024.1" not in existing:
        hashes["2024.1"] = wf.publish_rules(
            "2024.1", "2024 科研导向版（旧）", RULES_V1)["content_hash"]
    if "2026.1" not in existing:
        hashes["2026.1"] = wf.publish_rules(
            "2026.1", "2026 分类评价版（现行）", RULES_V2)["content_hash"]
    if hashes:
        wf.activate_rules(activate_version)
    for expert_id, name, org in EXPERTS:
        # 已存在时注册是幂等更新
        wf.register_expert(expert_id, name, org)
    return hashes
