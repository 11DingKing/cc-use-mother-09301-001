"""分类规则引擎：纯函数、确定性、可重放。

规则以版本化规则集（ruleset）形式存在。引擎输入为规则集 + 证据包，
输出分类建议与逐项指标；同样输入永远得到同样输出（Decimal 定点数、
规范化 JSON），复议时对签署快照重放即可还原全部计算依据。

评分模型：
- 四个证据维度 mission/disciplines/talent/service，权重之和须为 1。
- 每个候选类别在每个维度维护一组锚定词；维度得分 =
  命中的不同锚定词数 / 该类别该维度锚定词总数（覆盖率，0~1）。
- 类别总分 = 各维度得分按权重求和；建议类别为总分最高者。
- 最高分与次高分差距小于 min_margin 时结论为 indeterminate（材料不足以定论）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..core import canonical, sha256_hex

DIMENSIONS = ("mission", "disciplines", "talent", "service")
DIMENSION_NAMES = {
    "mission": "院校使命",
    "disciplines": "学科结构",
    "talent": "人才培养",
    "service": "社会服务",
}
QUANT = Decimal("0.000001")


def _q(value: Decimal) -> Decimal:
    return value.quantize(QUANT, rounding=ROUND_HALF_UP)


def validate_ruleset(ruleset: dict[str, Any]) -> None:
    for key in ("ruleset_version", "weights", "categories", "thresholds"):
        if key not in ruleset:
            raise ValueError(f"规则集缺少字段：{key}")
    weights = ruleset["weights"]
    if set(weights) != set(DIMENSIONS):
        raise ValueError("权重必须恰好覆盖四个维度：mission/disciplines/talent/service")
    if sum((Decimal(str(w)) for w in weights.values()), Decimal(0)) != Decimal(1):
        raise ValueError("维度权重之和必须为 1")
    categories = ruleset["categories"]
    if not isinstance(categories, list) or len(categories) < 2:
        raise ValueError("候选类别至少需要两个")
    keys = [c["key"] for c in categories]
    if len(keys) != len(set(keys)):
        raise ValueError("类别 key 不能重复")
    for category in categories:
        anchors = category.get("anchors", {})
        if set(anchors) != set(DIMENSIONS):
            raise ValueError(f"类别 {category['key']} 的锚定词必须覆盖四个维度")
        for dim, terms in anchors.items():
            if not isinstance(terms, list) or not terms or len(terms) != len(set(terms)):
                raise ValueError(f"类别 {category['key']} 维度 {dim} 的锚定词须为非空且不重复的列表")
    if "min_margin" not in ruleset["thresholds"]:
        raise ValueError("阈值缺少 min_margin")


def ruleset_fingerprint(ruleset: dict[str, Any]) -> str:
    """规则内容指纹：规则换版（即使版本号相同）也会改变指纹。"""
    return sha256_hex(ruleset)


def validate_evidence(evidence: list[dict[str, Any]]) -> None:
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("证据包不能为空")
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise ValueError(f"证据 #{index} 必须是对象")
        if item.get("dimension") not in DIMENSIONS:
            raise ValueError(f"证据 #{index} 的 dimension 非法")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"证据 #{index} 的 content 不能为空")
        if not item.get("submitted_by"):
            raise ValueError(f"证据 #{index} 缺少提交人 submitted_by")


def evaluate(ruleset: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """对证据包执行确定性评估，返回建议、逐类逐维得分与命中明细。"""
    validate_ruleset(ruleset)
    validate_evidence(evidence)

    # 按维度合并证据文本（保持证据列表顺序，使命中统计可复现）。
    texts: dict[str, str] = {dim: "" for dim in DIMENSIONS}
    for item in evidence:
        texts[item["dimension"]] += item["content"] + "\n"

    weights = {dim: Decimal(str(ruleset["weights"][dim])) for dim in DIMENSIONS}
    min_margin = Decimal(str(ruleset["thresholds"]["min_margin"]))

    scores: dict[str, dict[str, Any]] = {}
    for category in ruleset["categories"]:
        key = category["key"]
        by_dimension: dict[str, str] = {}
        hits: dict[str, list[str]] = {}
        total = Decimal(0)
        for dim in DIMENSIONS:
            terms = category["anchors"][dim]
            matched = sorted({term for term in terms if term in texts[dim]})
            dim_score = _q(Decimal(len(matched)) / Decimal(len(terms)))
            by_dimension[dim] = str(dim_score)
            hits[dim] = matched
            total += dim_score * weights[dim]
        total = _q(total)
        scores[key] = {"total": str(total), "by_dimension": by_dimension, "matched_terms": hits}

    ranked = sorted(
        ((Decimal(v["total"]), key) for key, v in scores.items()),
        reverse=True,
    )
    top_score, top_key = ranked[0]
    runner_up_score = ranked[1][0]
    margin = _q(top_score - runner_up_score)
    indeterminate = margin < min_margin

    result = {
        "recommendation": None if indeterminate else top_key,
        "indeterminate": indeterminate,
        "scores": scores,
        "ranking": [key for _, key in ranked],
        "margin": str(margin),
        "thresholds": {"min_margin": str(min_margin)},
        "weights": {dim: str(weights[dim]) for dim in DIMENSIONS},
    }
    result["result_fingerprint"] = sha256_hex(
        {k: result[k] for k in ("recommendation", "scores", "ranking", "margin", "thresholds", "weights")}
    )
    return result


def replay(snapshot: dict[str, Any]) -> dict[str, Any]:
    """对签署快照重放评估，供复议还原计算依据并校验结果是否漂移。"""
    rerun = evaluate(snapshot["ruleset"], snapshot["evidence"])
    original = snapshot["evaluation"]
    return {
        "reproduced": canonical(
            {k: original[k] for k in ("recommendation", "scores", "ranking", "margin")}
        )
        == canonical({k: rerun[k] for k in ("recommendation", "scores", "ranking", "margin")}),
        "original_result_fingerprint": original["result_fingerprint"],
        "rerun_result_fingerprint": rerun["result_fingerprint"],
        "rerun": rerun,
    }
