"""HTTP 路由：请求模型、错误处理与服务层编排。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db, purchase, service
from .payment_client import PaymentClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = os.environ.get("LICENSE_DB", "/tmp/license_admission.db")
    init_conn = db.connect(db_path)
    db.init_db(init_conn)
    init_conn.close()
    yield


app = FastAPI(title="会话准入后端", version="1.1.0", lifespan=lifespan)


def get_conn() -> Iterator[object]:
    """每个请求一条独立连接：HTTP 调用在事务之外，不会互相拖住写锁。"""
    db_path = os.environ.get("LICENSE_DB", "/tmp/license_admission.db")
    conn = db.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_payment() -> PaymentClient:
    base = os.environ.get("PAYMENT_BASE_URL", "http://127.0.0.1:8100")
    timeout = float(os.environ.get("PAYMENT_TIMEOUT", "2.0"))
    return PaymentClient(base, timeout=timeout)


@app.exception_handler(service.ServiceError)
async def _service_error_handler(_request: Request, exc: service.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------- 请求模型


class AdmitRequest(BaseModel):
    org: str = Field(min_length=1)
    tier: Literal["standard", "premium"]
    request_id: str = Field(min_length=1)


class RenewRequest(BaseModel):
    operation_id: str = Field(min_length=1)
    credential_secret: str = Field(min_length=1)


class CloseRequest(BaseModel):
    operation_id: str = Field(min_length=1)
    credential_secret: str | None = None


class EntitlementCreate(BaseModel):
    org: str = Field(min_length=1)
    tier: Literal["standard", "premium"]
    seats: int = Field(ge=0)
    effective_at: str
    expire_at: str


class EntitlementModify(BaseModel):
    seats: int | None = Field(default=None, ge=0)
    effective_at: str | None = None
    expire_at: str | None = None


class EntitlementRevoke(BaseModel):
    reason: str = Field(min_length=1)


class ClockAdvance(BaseModel):
    to: str | None = None
    delta_seconds: float | None = None


class PackageCreate(BaseModel):
    code: str = Field(min_length=1)
    price_cents: int = Field(gt=0)
    currency: str = Field(min_length=1)
    tier: Literal["standard", "premium"]
    seats: int = Field(ge=0)
    duration_seconds: int = Field(gt=0)
    charge_mode: Literal["direct", "freeze"] = "direct"


class PurchaseCreate(BaseModel):
    org: str = Field(min_length=1)
    package_code: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class PurchaseCancel(BaseModel):
    cancel_request_id: str = Field(min_length=1)


# ---------------------------------------------------------------- 路由：时钟


@app.get("/clock")
def read_clock(conn=Depends(get_conn)):
    return {"now": db.get_clock(conn)}


@app.post("/clock/advance")
def advance_clock(body: ClockAdvance, conn=Depends(get_conn)):
    return service.advance_clock(conn, body.to, body.delta_seconds)


# ---------------------------------------------------------------- 路由：权益


@app.post("/entitlements", status_code=201)
def create_entitlement(body: EntitlementCreate, conn=Depends(get_conn)):
    return service.create_entitlement(
        conn, body.org, body.tier, body.seats, body.effective_at, body.expire_at)


@app.patch("/entitlements/{ent_id}")
def modify_entitlement(ent_id: str, body: EntitlementModify, conn=Depends(get_conn)):
    return service.modify_entitlement(
        conn, ent_id, seats=body.seats,
        effective_at=body.effective_at, expire_at=body.expire_at)


@app.post("/entitlements/{ent_id}/revoke")
def revoke_entitlement(ent_id: str, body: EntitlementRevoke, conn=Depends(get_conn)):
    return service.revoke_entitlement(conn, ent_id, body.reason)


@app.get("/entitlements/{ent_id}/history")
def entitlement_history(ent_id: str, conn=Depends(get_conn)):
    return {"ent_id": ent_id, "versions": service.list_entitlement_history(conn, ent_id)}


# ---------------------------------------------------------------- 路由：会话


@app.post("/sessions/admit")
def admit_session(body: AdmitRequest, conn=Depends(get_conn)):
    return service.admit_session(conn, body.org, body.tier, body.request_id)


@app.post("/sessions/{session_id}/renew")
def renew_session(session_id: str, body: RenewRequest, conn=Depends(get_conn)):
    return service.renew_session(conn, session_id, body.credential_secret, body.operation_id)


@app.post("/sessions/{session_id}/close")
def close_session(session_id: str, body: CloseRequest, conn=Depends(get_conn)):
    return service.close_session(conn, session_id, body.credential_secret, body.operation_id)


@app.get("/sessions/{session_id}")
def session_detail(session_id: str, conn=Depends(get_conn)):
    return service.get_session_detail(conn, session_id)


@app.get("/orgs/{org}/sessions")
def org_sessions(org: str, conn=Depends(get_conn)):
    return {"org": org, "sessions": service.list_org_sessions(conn, org)}


# ---------------------------------------------------------------- 路由：套餐与购权


@app.post("/packages", status_code=201)
def create_package(body: PackageCreate, conn=Depends(get_conn)):
    return purchase.create_package(
        conn, body.code, body.price_cents, body.currency, body.tier,
        body.seats, body.duration_seconds, body.charge_mode)


@app.get("/packages")
def list_packages(conn=Depends(get_conn)):
    return {"packages": purchase.list_packages(conn)}


@app.post("/purchases", status_code=201)
def place_purchase(body: PurchaseCreate, conn=Depends(get_conn),
                   payment: PaymentClient = Depends(get_payment)):
    return purchase.place_order(conn, payment, body.org, body.package_code, body.request_id)


@app.get("/purchases/{order_id}")
def get_purchase(order_id: str, conn=Depends(get_conn)):
    return purchase.get_order(conn, order_id)


@app.post("/purchases/{order_id}/reconcile")
def reconcile_purchase(order_id: str, conn=Depends(get_conn),
                       payment: PaymentClient = Depends(get_payment)):
    """显式对账推进：进程重启后、或收到「结果未知」后由此继续。"""
    return purchase.advance_order(conn, payment, order_id)


@app.post("/purchases/{order_id}/cancel")
def cancel_purchase(order_id: str, body: PurchaseCancel, conn=Depends(get_conn),
                    payment: PaymentClient = Depends(get_payment)):
    return purchase.request_cancel(conn, payment, order_id, body.cancel_request_id)


@app.get("/orgs/{org}/purchases")
def org_purchases(org: str, conn=Depends(get_conn)):
    return {"org": org, "orders": purchase.list_orders(conn, org)}


# ---------------------------------------------------------------- 路由：事件流


@app.get("/events")
def events(conn=Depends(get_conn), org: str | None = None, limit: int = 200):
    return {"events": service.list_events(conn, org, limit)}
