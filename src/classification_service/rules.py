"""版本化分类规则与确定性评分引擎。

规则包是纯数据（指标运算均由本模块固定的算子解释），因此：

- 换版 = 发布并激活新规则包；旧包永久保留，已签结论引用的旧版本随时可取。
- 同一份证据 + 同一个规则版本 ⇒ 逐位相同的评分明细（指标按 key 排序、
  分值统一保留 6 位小数），复议重算可以直接与原评分逐字段比对。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import canonical, content_hash

RESEARCH = "research"
APPLIED = "applied"
MIXED = "mixed"

CLASSIFICATION_LABELS = {
    RESEARCH: "研究型",
    APPLIED: "应用型",
    MIXED: "混合型",
}

REQUIRED_SLOTS = ("mission", "disciplines", "talent", "service")
ROUND_DIGITS = 6


class RulePackageError(ValueError):
    """规则包结构非法。"""


@dataclass(frozen=True)
class RulePackage:
    package_version: str
    display_name: str
    rules: dict[str, Any]

    @property
    def content_hash(self) -> str:
        return content_hash(self.rules)

    def to_storage(self) -> tuple[str, str, str, str]:
        return self.package_version, self.display_name, canonical(self.rules), self.content_hash


def validate_rules(rules: dict[str, Any]) -> None:
    """校验规则包结构，失败抛出 :class:`RulePackageError`。"""
    if not isinstance(rules, dict):
        raise RulePackageError("规则必须是对象")
    slots = rules.get("slots")
    if not isinstance(slots, dict) or not slots:
        raise RulePackageError("slots 必须是非空对象")
    missing = [name for name in REQUIRED_SLOTS if name not in slots]
    if missing:
        raise RulePackageError("缺少必备证据槽位：" + "、".join(missing))
    total_weight = 0.0
    for slot_name in sorted(slots):
        slot = slots[slot_name]
        weight = slot.get("weight")
        if not isinstance(weight, (int, float)) or weight <= 0:
            raise RulePackageError(f"槽位 {slot_name} 的 weight 必须为正数")
        total_weight += float(weight)
        metrics = slot.get("metrics")
        if not isinstance(metrics, list) or not metrics:
            raise RulePackageError(f"槽位 {slot_name} 至少包含一个指标")
        keys: set[str] = set()
        for metric in metrics:
            for field in ("key", "axis", "op", "path"):
                if field not in metric:
                    raise RulePackageError(f"槽位 {slot_name} 存在缺少 {field} 的指标")
            if metric["axis"] not in (RESEARCH, APPLIED):
                raise RulePackageError(f"指标 {metric['key']} 的 axis 非法")
            if metric["key"] in keys:
                raise RulePackageError(f"槽位 {slot_name} 内指标 key 重复：{metric['key']}")
            keys.add(metric["key"])
            if metric["op"] not in _OPS:
                raise RulePackageError(f"指标 {metric['key']} 使用了未知算子 {metric['op']}")
    if abs(total_weight - 1.0) > 1e-6:
        raise RulePackageError(f"槽位权重之和必须为 1，当前为 {total_weight}")
    decision = rules.get("decision")
    if not isinstance(decision, dict):
        raise RulePackageError("decision 阈值块缺失")
    for field in ("research_threshold", "applied_threshold", "margin"):
        value = decision.get(field)
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise RulePackageError(f"decision.{field} 必须是 0~1 的数值")


# --------------------------------------------------------------------------- #
# 算子：全部为纯函数，输入证据负载与指标参数，输出 0~1 的分值。
# --------------------------------------------------------------------------- #

def _walk(payload: Any, path: str) -> Any:
    cur = payload
    for part in path.split("."):
        if not part:
            continue
        if not isinstance(cur, dict) or part not in cur:
            raise RulePackageError(f"证据缺少路径 {path}")
        cur = cur[part]
    return cur


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def op_keyword_density(payload: Any, spec: dict[str, Any]) -> float:
    """关键词命中密度：每百字符命中次数，截断到 0~1。"""
    text = str(_walk(payload, spec["path"]))
    keywords = spec.get("keywords") or []
    if not isinstance(keywords, list) or not keywords:
        raise RulePackageError("keyword_density 需要非空 keywords 列表")
    hits = sum(text.count(str(keyword)) for keyword in keywords)
    return _clip01(hits / max(1.0, len(text) / 100.0))


def op_ratio(payload: Any, spec: dict[str, Any]) -> float:
    """直接读取一个 0~1 的比例字段。"""
    value = _walk(payload, spec["path"])
    if not isinstance(value, (int, float)):
        raise RulePackageError(f"路径 {spec['path']} 必须是数值")
    return _clip01(float(value))


def op_share(payload: Any, spec: dict[str, Any]) -> float:
    """列表中满足 match 条件的元素占比。"""
    items = _walk(payload, spec["path"])
    if not isinstance(items, list) or not items:
        return 0.0
    match = spec.get("match") or {}
    if not isinstance(match, dict):
        raise RulePackageError("share 的 match 必须是对象")
    hit = sum(1 for item in items if all(isinstance(item, dict) and item.get(k) == v for k, v in match.items()))
    return _clip01(hit / len(items))


_OPS = {
    "keyword_density": op_keyword_density,
    "ratio": op_ratio,
    "share": op_share,
}


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Evaluation:
    rule_version: str
    rule_hash: str
    classification: str
    research_score: float
    applied_score: float
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_version": self.rule_version,
            "rule_hash": self.rule_hash,
            "classification": self.classification,
            "research_score": self.research_score,
            "applied_score": self.applied_score,
            "detail": self.detail,
        }


def evaluate(package: RulePackage, evidence: dict[str, dict[str, Any]]) -> Evaluation:
    """按规则包对全部槽位的最新证据负载评分。

    ``evidence`` 形如 ``{slot: payload}``。结果含逐项分值，供签署快照与
    复议还原使用。
    """
    rules = package.rules
    detail: dict[str, Any] = {"slots": {}, "metrics": {}}
    axis_totals = {RESEARCH: 0.0, APPLIED: 0.0}

    for slot_name in sorted(rules["slots"]):
        slot = rules["slots"][slot_name]
        if slot_name not in evidence:
            raise RulePackageError(f"缺少槽位证据：{slot_name}")
        payload = evidence[slot_name]
        axis_values: dict[str, list[float]] = {RESEARCH: [], APPLIED: []}
        for metric in sorted(slot["metrics"], key=lambda m: m["key"]):
            value = round(float(_OPS[metric["op"]](payload, metric)), ROUND_DIGITS)
            detail["metrics"][metric["key"]] = {
                "slot": slot_name,
                "axis": metric["axis"],
                "op": metric["op"],
                "value": value,
            }
            axis_values[metric["axis"]].append(value)
        slot_axis = {
            axis: round(sum(values) / len(values), ROUND_DIGITS) if values else 0.0
            for axis, values in axis_values.items()
        }
        detail["slots"][slot_name] = {"weight": slot["weight"], **slot_axis}
        for axis in (RESEARCH, APPLIED):
            axis_totals[axis] += slot["weight"] * slot_axis[axis]

    research_score = round(axis_totals[RESEARCH], ROUND_DIGITS)
    applied_score = round(axis_totals[APPLIED], ROUND_DIGITS)
    decision_cfg = rules["decision"]
    classification = _classify(research_score, applied_score, decision_cfg)
    detail["decision_config"] = decision_cfg
    return Evaluation(
        rule_version=package.package_version,
        rule_hash=package.content_hash,
        classification=classification,
        research_score=research_score,
        applied_score=applied_score,
        detail=detail,
    )


def _classify(research: float, applied: float, cfg: dict[str, Any]) -> str:
    margin = float(cfg["margin"])
    research_ok = research >= cfg["research_threshold"] and research - applied >= margin
    applied_ok = applied >= cfg["applied_threshold"] and applied - research >= margin
    if research_ok:
        return RESEARCH
    if applied_ok:
        return APPLIED
    return MIXED
