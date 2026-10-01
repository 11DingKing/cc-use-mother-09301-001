"""内置规则集种子：2024.1 为旧版，2026.1 为现行版。

换版只影响换版后受理的案件；已签署结论永久内嵌其作出时的规则全文。
"""
from __future__ import annotations

import copy

_SEED_RULESETS: list[dict] = [
    {
        "ruleset_version": "2024.1",
        "effective_from": "2024-01-01",
        "weights": {"mission": 0.30, "disciplines": 0.30, "talent": 0.25, "service": 0.15},
        "thresholds": {"min_margin": "0.05"},
        "categories": [
            {
                "key": "research",
                "name": "研究型",
                "anchors": {
                    "mission": ["学术前沿", "知识创新", "世界一流", "学科高峰"],
                    "disciplines": ["博士学位授权", "基础学科", "国家重点实验室", "学术学位"],
                    "talent": ["学术型人才", "博士生培养", "科研训练", "学术导师"],
                    "service": ["高水平论文", "国家重大科研项目", "学术影响力", "原始创新"],
                },
            },
            {
                "key": "applied",
                "name": "应用型",
                "anchors": {
                    "mission": ["服务区域", "应用型办学", "产教融合", "地方发展"],
                    "disciplines": ["专业学位", "应用学科", "行业特色", "实训平台"],
                    "talent": ["应用型人才", "双师型教师", "实习实训", "产学合作"],
                    "service": ["技术服务", "成果转化", "企业合作", "对口支援"],
                },
            },
            {
                "key": "skill",
                "name": "技能型",
                "anchors": {
                    "mission": ["职业技能", "就业导向", "技术技能", "工匠精神"],
                    "disciplines": ["职业技能等级证书", "实训课程", "岗位标准", "技能竞赛"],
                    "talent": ["技能型人才", "学徒制", "顶岗实习", "实操训练"],
                    "service": ["职工培训", "技能鉴定", "社区服务", "区域用工"],
                },
            },
        ],
    },
    {
        "ruleset_version": "2026.1",
        "effective_from": "2026-01-01",
        "weights": {"mission": 0.25, "disciplines": 0.25, "talent": 0.25, "service": 0.25},
        "thresholds": {"min_margin": "0.05"},
        "categories": [
            {
                "key": "research",
                "name": "研究型",
                "anchors": {
                    "mission": ["学术前沿", "知识创新", "世界一流", "学科高峰", "基础研究使命"],
                    "disciplines": ["博士学位授权", "基础学科", "国家重点实验室", "学术学位", "前沿科学中心"],
                    "talent": ["学术型人才", "博士生培养", "科研训练", "学术导师", "本硕博贯通"],
                    "service": ["高水平论文", "国家重大科研项目", "学术影响力", "原始创新", "科学发现"],
                },
            },
            {
                "key": "applied",
                "name": "应用型",
                "anchors": {
                    "mission": ["服务区域", "应用型办学", "产教融合", "地方发展", "应用型办学定位"],
                    "disciplines": ["专业学位", "应用学科", "行业特色", "实训平台", "现代产业学院"],
                    "talent": ["应用型人才", "双师型教师", "实习实训", "产学合作", "项目式教学"],
                    "service": ["技术服务", "成果转化", "企业合作", "对口支援", "横向课题", "地方智库"],
                },
            },
            {
                "key": "skill",
                "name": "技能型",
                "anchors": {
                    "mission": ["职业技能", "就业导向", "技术技能", "工匠精神", "技能社会服务"],
                    "disciplines": ["职业技能等级证书", "实训课程", "岗位标准", "技能竞赛", "工学一体化"],
                    "talent": ["技能型人才", "学徒制", "顶岗实习", "实操训练", "岗课赛证"],
                    "service": ["职工培训", "技能鉴定", "社区服务", "区域用工", "技术推广"],
                },
            },
        ],
    },
]

DEFAULT_VERSION = "2026.1"


def seed_rulesets() -> list[dict]:
    return copy.deepcopy(_SEED_RULESETS)
