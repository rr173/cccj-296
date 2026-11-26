"""组织购权的 HTTP 端到端：套餐、下单、查询、显式对账、取消、权益可用。"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """真实 HTTP 服务进程之外的进程内等价物：
    * 服务库、收款方库=两个独立 SQLite 文件；
    * 服务通过 HTTP 访问独立收款方（真端口）。
    """
    from app.payment_stub import make_server
    import threading

    stub = make_server(0, str(tmp_path / "stub.db"))
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    port = stub.server_address[1]
    monkeypatch.setenv("LICENSE_DB", str(tmp_path / "service.db"))
    monkeypatch.setenv("LICENSE_EPOCH", "2026-01-01T00:00:00Z")
    monkeypatch.setenv("PAYMENT_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("PAYMENT_TIMEOUT", "1.0")
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        c.stub = stub
        yield c
    stub.shutdown()
    stub.server_close()
    thread.join(timeout=5)


def test_http_purchase_flow_delivers_entitlement_for_admission(client):
    r = client.post("/packages", json={
        "code": "pro", "price_cents": 12800, "currency": "CNY",
        "tier": "premium", "seats": 2, "duration_seconds": 86400 * 30})
    assert r.status_code == 201

    r = client.post("/purchases", json={
        "org": "acme", "package_code": "pro", "request_id": "buy-1"})
    assert r.status_code == 201
    order = r.json()
    assert order["stage"] == "delivered"
    assert order["money"]["captured"] is True

    # 查询能区分阶段、追查交易与权益
    detail = client.get(f"/purchases/{order['order_id']}").json()
    assert detail["stage"] == "delivered"
    assert detail["money"]["charge_id"]
    assert detail["entitlement"]["seats"] == 2
    assert any(op["op_type"] == "create" for op in detail["money_ops"])

    # 权益立即可用于现有会话准入
    r = client.post("/sessions/admit", json={
        "org": "acme", "tier": "premium", "request_id": "sess-1"})
    assert r.json()["admitted"] is True

    # 同组织重复相同下单：同一个订单，不重复扣款
    again = client.post("/purchases", json={
        "org": "acme", "package_code": "pro", "request_id": "buy-1"}).json()
    assert again["order_id"] == order["order_id"]
    assert len(_stub_charges(client)) == 1


def _stub_charges(client):
    import urllib.request, json as _json
    port = client.stub.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/_state") as r:
        return _json.loads(r.read())["charges"]


def test_http_unknown_then_reconcile_and_cancel_clears_money(client):
    client.post("/packages", json={
        "code": "fz", "price_cents": 5000, "currency": "CNY",
        "tier": "standard", "seats": 1, "duration_seconds": 86400,
        "charge_mode": "freeze"})

    # 1) 创建响应丢失 -> 处理中，无权益
    import urllib.request, json as _json
    port = client.stub.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/_control",
                                 data=_json.dumps({"action": "drop", "op": "create"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()

    order = client.post("/purchases", json={
        "org": "acme", "package_code": "fz", "request_id": "buy-1"}).json()
    assert order["stage"] == "processing"
    assert order["entitlement"] is None

    # 2) 显式对账恢复 -> 冻结保障可承诺；此时扣款暂不可用，故尚未发权益
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{port}/_control",
        data=_json.dumps({"mutations_unavailable": True}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")).read()
    recovered = client.post(f"/purchases/{order['order_id']}/reconcile").json()
    assert recovered["stage"] == "promised"
    assert recovered["money"]["state"] == "frozen"
    assert recovered["entitlement"] is None

    # 3) 取消被明确拒绝（已承诺），服务继续履约
    r = client.post(f"/purchases/{order['order_id']}/cancel",
                    json={"cancel_request_id": "c-1"})
    assert r.status_code == 409
    assert r.json()["error"] == "cancel_after_promise"

    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{port}/_control",
        data=_json.dumps({"mutations_unavailable": False}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")).read()
    delivered = client.post(f"/purchases/{order['order_id']}/reconcile").json()
    assert delivered["stage"] == "delivered"
    assert delivered["entitlement"]["ent_id"]


def test_http_cancel_after_unknown_money_ends_without_entitlement(client):
    client.post("/packages", json={
        "code": "dz", "price_cents": 5000, "currency": "CNY",
        "tier": "standard", "seats": 1, "duration_seconds": 86400})
    import urllib.request, json as _json
    port = client.stub.server_address[1]
    for control in ({"action": "drop", "op": "create"},
                    {"queries_unavailable": True},
                    {"mutations_unavailable": True}):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/_control",
                                     data=_json.dumps(control).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req).read()

    order = client.post("/purchases", json={
        "org": "acme", "package_code": "dz", "request_id": "buy-1"}).json()
    assert order["stage"] == "processing"

    canceled = client.post(f"/purchases/{order['order_id']}/cancel",
                           json={"cancel_request_id": "c-1"}).json()
    assert canceled["status"] == "cancel_pending"
    assert canceled["cancel"]["settled"] is False

    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{port}/_control",
        data=_json.dumps({"queries_unavailable": False,
                          "mutations_unavailable": False}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")).read()

    done = client.post(f"/purchases/{order['order_id']}/reconcile").json()
    assert done["stage"] == "canceled"
    assert done["cancel"]["settled"] is True
    assert done["cancel"]["settlement"] in ("refund_confirmed", "void_confirmed", "no_charge")
    assert done["entitlement"] is None
    # 组织里没有任何会话权益
    assert client.get("/orgs/acme/sessions").json()["sessions"] == []
