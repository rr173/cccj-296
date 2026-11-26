"""站点进程入口（独立 OS 进程，独立 SQLite，不访问中心库）。

环境： SITE_DB / SITE_ID / SITE_EPOCH / LICENSE_SIGNING_KEY / SITE_PORT
用法： python3 -m tools.run_site
"""
from __future__ import annotations

import os

from app import crypto, site_db
from app.stdlib_http import make_site_server


def main() -> None:
    db_path = os.environ["SITE_DB"]
    port = int(os.environ.get("SITE_PORT", "8300"))
    site_id = os.environ.get("SITE_ID", "site-A")
    signing_key = crypto.load_site_key()  # 缺失密钥直接拒绝启动

    conn = site_db.connect(db_path)
    site_db.init_db(conn)
    conn.close()
    server = make_site_server(port, db_path, site_id, signing_key)
    print(f"[site {site_id}] pid={os.getpid()} 监听 127.0.0.1:{port} db={db_path}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
