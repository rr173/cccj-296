"""pytest 公共夹具：每个用例一个临时数据库，逻辑时钟固定在 2026-01-01T00:00:00Z。"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db  # noqa: E402

EPOCH = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    c.execute("UPDATE clock SET now_iso=? WHERE id=1", (db.normalize_ts(EPOCH),))
    yield c
    c.close()


@pytest.fixture()
def payments(tmp_path):
    """独立收款方：单独 SQLite 文件、单独进程内端口；两种库绝不共用。"""
    from app.payment_client import PaymentClient
    from app.payment_stub import make_server

    server = make_server(0, str(tmp_path / "payment_stub.db"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    client = PaymentClient(f"http://127.0.0.1:{port}", timeout=1.0)
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)



def make_ent(svc, conn, org, tier, seats, start="2026-01-01T00:00:00Z",
             end="2027-01-01T00:00:00Z"):
    return svc.create_entitlement(conn, org, tier, seats, start, end)


def admit(svc, conn, org, tier, rid):
    return svc.admit_session(conn, org, tier, rid)


def active_ids(svc, conn, org):
    return [s["session_id"] for s in svc.list_org_sessions(conn, org)
            if s["status"] == "active"]
