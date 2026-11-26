"""HTTP 路由：请求模型、错误处理与服务层编排。"""
from __future__ import annotations

import os
from typing import Literal

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db, service


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = os.environ.get("LICENSE_DB", "/tmp/license_admission.db")
    app.state.conn = db.connect(db_path)
    db.init_db(app.state.conn)
    yield


app = FastAPI(title="会话准入后端", version="1.0.0", lifespan=lifespan)


def get_conn(request: Request):
    return request.app.state.conn


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


# ---------------------------------------------------------------- 路由：时钟


@app.get("/clock")
def read_clock(request: Request):
    return {"now": db.get_clock(get_conn(request))}


@app.post("/clock/advance")
def advance_clock(body: ClockAdvance, request: Request):
    return service.advance_clock(get_conn(request), body.to, body.delta_seconds)


# ---------------------------------------------------------------- 路由：权益


@app.post("/entitlements", status_code=201)
def create_entitlement(body: EntitlementCreate, request: Request):
    return service.create_entitlement(
        get_conn(request), body.org, body.tier, body.seats,
        body.effective_at, body.expire_at,
    )


@app.patch("/entitlements/{ent_id}")
def modify_entitlement(ent_id: str, body: EntitlementModify, request: Request):
    return service.modify_entitlement(
        get_conn(request), ent_id, seats=body.seats,
        effective_at=body.effective_at, expire_at=body.expire_at,
    )


@app.post("/entitlements/{ent_id}/revoke")
def revoke_entitlement(ent_id: str, body: EntitlementRevoke, request: Request):
    return service.revoke_entitlement(get_conn(request), ent_id, body.reason)


@app.get("/entitlements/{ent_id}/history")
def entitlement_history(ent_id: str, request: Request):
    return {"ent_id": ent_id, "versions": service.list_entitlement_history(get_conn(request), ent_id)}


# ---------------------------------------------------------------- 路由：会话


@app.post("/sessions/admit")
def admit_session(body: AdmitRequest, request: Request):
    result = service.admit_session(get_conn(request), body.org, body.tier, body.request_id)
    return result


@app.post("/sessions/{session_id}/renew")
def renew_session(session_id: str, body: RenewRequest, request: Request):
    return service.renew_session(
        get_conn(request), session_id, body.credential_secret, body.operation_id)


@app.post("/sessions/{session_id}/close")
def close_session(session_id: str, body: CloseRequest, request: Request):
    return service.close_session(
        get_conn(request), session_id, body.credential_secret, body.operation_id)


@app.get("/sessions/{session_id}")
def session_detail(session_id: str, request: Request):
    return service.get_session_detail(get_conn(request), session_id)


@app.get("/orgs/{org}/sessions")
def org_sessions(org: str, request: Request):
    return {"org": org, "sessions": service.list_org_sessions(get_conn(request), org)}


# ---------------------------------------------------------------- 路由：事件流


@app.get("/events")
def events(request: Request, org: str | None = None, limit: int = 200):
    return {"events": service.list_events(get_conn(request), org, limit)}
