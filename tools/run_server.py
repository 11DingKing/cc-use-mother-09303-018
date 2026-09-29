"""启动合作成果报告封账后端 HTTP 服务。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sealing_backend.api import create_server
from sealing_backend.services import SealingService
from sealing_backend.storage import Storage


def main() -> None:
    parser = argparse.ArgumentParser(description="合作成果报告封账后端")
    parser.add_argument("--db", default=str(ROOT / "var" / "sealing.db"), help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--chunk-size", type=int, default=4096, help="导出分块字节数")
    args = parser.parse_args()

    service = SealingService(Storage(args.db), chunk_size=args.chunk_size)
    httpd = create_server(service, host=args.host, port=args.port)
    print(f"封账后端已启动：http://{args.host}:{args.port}（数据库：{args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
