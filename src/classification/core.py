"""确定性编码、哈希、标识与时间工具。

快照与重放必须跨进程、跨重启逐字节一致，因此所有 JSON 均经 canonical()
规范化（排序键、无空白、ensure_ascii=False），数值计算统一走 Decimal。
"""
from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date, datetime, timezone
from uuid import uuid4


def canonical(obj: object) -> str:
    """规范化 JSON 序列化，作为哈希与重放比对的唯一字节来源。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(obj: object) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today() -> date:
    return datetime.now(timezone.utc).date()


def add_years(value: date, years: int) -> date:
    """整年后的同日（处理 2 月 29 日），用于建议生效期。"""
    year = value.year + years
    day = min(value.day, calendar.monthrange(year, value.month)[1])
    return date(year, value.month, day)
