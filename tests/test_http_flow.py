"""HTTP 端到端测试与时钟、追溯查询测试。"""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LICENSE_DB", str(tmp_path / "http.db"))
    monkeypatch.setenv("LICENSE_EPOCH", "2026-01-01T00:00:00Z")
    from app import main
    importlib.reload(main)  # 确保使用新的环境变量
    with TestClient(main.app) as c:
        yield c


def test_full_http_flow(client):
    # 初始时钟
    r = client.get("/clock")
    assert r.status_code == 200
    t0 = r.json()["now"]

    # 建权益：标准 1 + 高级 1
    r = client.post("/entitlements", json={
        "org": "acme", "tier": "standard", "seats": 1,
        "effective_at": "2026-01-01T00:00:00Z", "expire_at": "2027-01-01T00:00:00Z"})
    std_ent = r.json()["entitlement"]["ent_id"]
    r = client.post("/entitlements", json={
        "org": "acme", "tier": "premium", "seats": 1,
        "effective_at": "2026-01-01T00:00:00Z", "expire_at": "2027-01-01T00:00:00Z"})
    pre_ent = r.json()["entitlement"]["ent_id"]

    # 标准会话各占一席
    a = client.post("/sessions/admit", json={
        "org": "acme", "tier": "standard", "request_id": "req-1"}).json()
    b = client.post("/sessions/admit", json={
        "org": "acme", "tier": "standard", "request_id": "req-2"}).json()
    assert a["admitted"] and b["admitted"]

    # 第三份标准申请被拒
    c = client.post("/sessions/admit", json={
        "org": "acme", "tier": "standard", "request_id": "req-3"}).json()
    assert c["admitted"] is False

    # 高级申请：挤掉一个标准会话（开始较晚的 b）
    p = client.post("/sessions/admit", json={
        "org": "acme", "tier": "premium", "request_id": "req-p"}).json()
    assert p["admitted"] is True
    detail_b = client.get(f"/sessions/{b['session_id']}").json()
    assert detail_b["status"] == "revoked"
    assert detail_b["admit_request_id"] == "req-2"

    # 续用：旧凭据失效
    sid, secret = a["session_id"], a["credential"]["secret"]
    r = client.post(f"/sessions/{sid}/renew",
                    json={"operation_id": "op-1", "credential_secret": secret})
    new_secret = r.json()["credential"]["secret"]
    r_old = client.post(f"/sessions/{sid}/renew",
                        json={"operation_id": "op-2", "credential_secret": secret})
    assert r_old.json()["error"] == "old_credential"
    r_replay = client.post(f"/sessions/{sid}/renew",
                           json={"operation_id": "op-1", "credential_secret": new_secret})
    assert r_replay.json()["credential"]["secret"] == new_secret

    # 关闭只释放自己的席位，且可重复关闭
    r = client.post(f"/sessions/{sid}/close",
                    json={"operation_id": "op-close", "credential_secret": new_secret})
    assert r.json()["released"] is True
    r2 = client.post(f"/sessions/{sid}/close",
                     json={"operation_id": "op-close-2"})
    assert r2.json()["released"] is False

    # 列表：撤销/关闭会话仍可查到，不是悄悄消失
    listing = client.get("/orgs/acme/sessions").json()["sessions"]
    statuses = sorted(s["status"] for s in listing)
    assert statuses == ["active", "closed", "revoked", "revoked"]

    # 事件流可按组织追溯，seq 单调
    events = client.get("/events", params={"org": "acme"}).json()["events"]
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs, reverse=True)


def test_entitlement_modify_returns_actual_evictions(client):
    client.post("/entitlements", json={
        "org": "g", "tier": "standard", "seats": 2,
        "effective_at": "2026-01-01T00:00:00Z", "expire_at": "2027-01-01T00:00:00Z"})
    s1 = client.post("/sessions/admit", json={
        "org": "g", "tier": "standard", "request_id": "x1"}).json()
    s2 = client.post("/sessions/admit", json={
        "org": "g", "tier": "standard", "request_id": "x2"}).json()

    ent_id = client.get("/orgs/g/sessions").json()["sessions"][0]["current_ent_id"]

    r = client.patch(f"/entitlements/{ent_id}", json={"seats": 1})
    assert r.status_code == 200
    evicted = r.json()["evicted"]
    assert [e["session_id"] for e in evicted] == [s2["session_id"]]
    assert evicted[0]["reason"] == "no_any_capacity"

    # 权益历史保留全部版本
    hist = client.get(f"/entitlements/{ent_id}/history").json()["versions"]
    assert [v["version"] for v in hist] == [1, 2]
    assert hist[0]["status"] == "superseded"
    assert hist[1]["seats"] == 1 and hist[1]["status"] == "active"


def test_clock_advance_and_no_rewind(client):
    r = client.post("/clock/advance", json={"delta_seconds": 3600})
    assert r.json()["advanced"] is True
    assert r.json()["now"].endswith("+00:00")

    r = client.post("/clock/advance", json={"to": "2026-01-01T02:00:00Z"})
    assert r.json()["now"].startswith("2026-01-01T02:00:00")

    bad = client.post("/clock/advance", json={"to": "2026-01-01T00:00:00Z"})
    assert bad.status_code == 409
    assert bad.json()["error"] == "clock_cannot_go_back"


def test_request_id_param_mismatch_rejected(client):
    client.post("/entitlements", json={
        "org": "z", "tier": "standard", "seats": 1,
        "effective_at": "2026-01-01T00:00:00Z", "expire_at": "2027-01-01T00:00:00Z"})
    client.post("/sessions/admit", json={
        "org": "z", "tier": "standard", "request_id": "dup"})
    r = client.post("/sessions/admit", json={
        "org": "z", "tier": "premium", "request_id": "dup"})
    assert r.status_code == 409
    assert r.json()["error"] == "request_id_param_mismatch"
