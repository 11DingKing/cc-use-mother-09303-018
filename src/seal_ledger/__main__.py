"""命令行入口：python -m seal_ledger --db seal.sqlite3 --port 8080"""
from __future__ import annotations

import argparse

from .api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="合作成果报告封账后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="seal_ledger.sqlite3")
    args = parser.parse_args()
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
