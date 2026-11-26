"""中心进程入口（独立 OS 进程）。

环境： LICENSE_DB / LICENSE_EPOCH / LICENSE_SIGNING_KEY / CENTER_PORT
用法： python3 -m tools.run_center
"""
from __future__ import annotations

import os

from app import crypto, db
from app.stdlib_http import make_center_server


def main() -> None:
    db_path = os.environ["LICENSE_DB"]
    port = int(os.environ.get("CENTER_PORT", "8200"))
    conn = db.connect(db_path)
    db.init_db(conn)
    with db.transaction(conn):
        crypto.get_or_create_center_key(conn)  # 固化/加载签名密钥
    conn.close()
    server = make_center_server(port, db_path)
    print(f"[center] pid={os.getpid()} 监听 127.0.0.1:{port} db={db_path}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
