"""命令行入口：python3 -m classification [--host H] [--port P] [--db PATH]"""
from __future__ import annotations

import argparse

from .config import Settings
from .http_app import run


def main() -> None:
    parser = argparse.ArgumentParser(description="高校分类定位论证服务端")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--db", default=None, help="SQLite 路径（默认 data/classification.db）")
    args = parser.parse_args()
    settings = Settings.from_env(db_path=args.db, host=args.host, port=args.port)
    run(settings)


if __name__ == "__main__":
    main()
