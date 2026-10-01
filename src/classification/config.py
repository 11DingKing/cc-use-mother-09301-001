"""进程级配置。数据库路径可由环境变量覆盖，便于测试与多实例部署。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: str
    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def from_env(cls, db_path: str | None = None, host: str | None = None,
                 port: int | None = None) -> "Settings":
        return cls(
            db_path=db_path or os.environ.get("CLASSIFICATION_DB", "data/classification.db"),
            host=host or os.environ.get("CLASSIFICATION_HOST", "127.0.0.1"),
            port=port or int(os.environ.get("CLASSIFICATION_PORT", "8080")),
        )

    def ensure_parent_dir(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
