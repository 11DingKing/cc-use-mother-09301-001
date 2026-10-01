"""规范序列化与内容哈希。

所有需要长期固定、跨版本复算的内容（证据包、规则包、签署快照）都先经过
:func:`canonical` 再计算 SHA-256。``sort_keys`` 消除字段顺序差异，
``ensure_ascii=False`` 固定中文字符的字节形态，分隔符固定以压缩空白差异。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

CANONICAL_SEPARATORS = (",", ":")


def canonical(value: Any) -> str:
    """生成与输入字段顺序无关的规范 JSON 字符串。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=CANONICAL_SEPARATORS,
        default=_json_default,
    )


def content_hash(value: Any) -> str:
    """对任意可 JSON 化内容计算 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _json_default(obj: Any) -> Any:  # pragma: no cover - 仅兜底
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"无法规范序列化的类型：{type(obj)!r}")
