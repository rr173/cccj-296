"""预算 HTTP 接口测试（FastAPI TestClient；缺 fastapi 时由 pytest_shim 跳过）。"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LICENSE_DB", str(tmp_path / "http_budget.db"))
    monkeypatch.setenv("LICENSE_EPOCH", "2026-01-01T00:00:00Z")
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def _ent(client, org="acme", tier="standard", seats=3):
    return client.post("/entitlements", json={
        "org": org, "tier": tier, "seats": seats,
        "effective_at": "2026-01-01T00:00:00Z",
        "expire_at": "2027-01-01T00:00:00Z"}).json()


def _grant(client, org="acme", amount=20, rid="g1",
           expire="2026-02-01T00:00:00Z"):
    return client.post("/budgets/grants", json={
        "org": org, "amount": amount, "expire_at": expire, "request_id": rid})


def test_grant_and_idempotent_retry_over_http(client):
    first = _grant(client, amount=100, rid="g-http")
    assert first.status_code == 201
    body = first.json()
    assert body["granted"] is True and body["amount"] == 100
    again = _grant(client, amount=100, rid="g-http").json()
    assert again["grant_id"] == body["grant_id"]
    mism = _grant(client, amount=101, rid="g-http")
    assert mism.status_code == 409
    assert mism.json()["error"] == "request_id_param_mismatch"
    # 非正整数由请求模型拒绝
    assert _grant(client, amount=0, rid="g-zero").status_code == 422


def test_budget_stops_session_with_spare_seats_endpoint(client):
    _ent(client, seats=5)
    _grant(client, amount=10, rid="g1")
    s = client.post("/sessions/admit", json={
        "org": "acme", "tier": "standard", "request_id": "a1"}).json()
    assert s["admitted"]
    out = client.post("/clock/advance", json={"delta_seconds": 0.000020}).json()
    assert out["charged_units"] == 10
    detail = client.get(f"/sessions/{s['session_id']}").json()
    assert detail["status"] == "revoked"
    assert detail["end_reason"] == "budget_exhausted"
    assert detail["ended_at"].startswith("2026-01-01T00:00:00.000010")

    summary = client.get("/orgs/acme/budget").json()
    assert summary["billing_enabled"] is True
    assert summary["totals"]["used"] == 10
    usage = client.get("/orgs/acme/budget/usage").json()
    assert usage["totals"]["units"] == 10
    assert usage["usage"][0]["session_id"] == s["session_id"]
    assert usage["usage"][0]["grant_id"] == summary["grants"][0]["grant_id"]
