"""服务启动入口：``python -m classification_service [--db PATH] [--seed] [--port N]``。"""
from __future__ import annotations

import argparse

from .db import initialize
from .httpapi import serve
from .seed import seed
from .store import Store
from .workflow import Workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="高校分类定位论证服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="classification.db")
    parser.add_argument("--seed", action="store_true", help="写入示例规则版本与专家")
    args = parser.parse_args()

    if args.seed:
        store = Store(args.db)
        initialize(store.conn)
        seed(Workflow(store))
        print(f"[seed] 已写入示例数据：{args.db}")
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
