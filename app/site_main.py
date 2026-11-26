"""站点 FastAPI 应用：独立进程、独立 SQLite，不依赖中心服务在线。

环境变量：
  SITE_DB          本站持久化数据库路径
  SITE_ID          本站标识（只接受签发给该 site_id 的包）
  SITE_EPOCH       逻辑时钟起点（演示两端对齐 UTC 时间线）
  LICENSE_SIGNING_KEY  中心 HMAC 验签密钥（带外部署，本站只验签不签发）
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import crypto, site_db, site_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即加载验签密钥；缺失则拒绝启动（无法验证任何包）
    app.state.signing_key = crypto.load_site_key()
    app.state.site_id = os.environ.get("SITE_ID", "default-site")
    db_path = os.environ.get("SITE_DB", "/tmp/license_site.db")
    init_conn = site_db.connect(db_path)
    site_db.init_db(init_conn)
    init_conn.close()
    yield


app = FastAPI(title="离线站点准入服务", version="1.0.0", lifespan=lifespan)


def get_conn() -> Iterator[object]:
    db_path = os.environ.get("SITE_DB", "/tmp/license_site.db")
    conn = site_db.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(site_service.SiteError)
async def _site_error_handler(_request: Request, exc: site_service.SiteError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------- 请求模型


class ImportRequest(BaseModel):
    envelope: dict


class SiteAdmitRequest(BaseModel):
    package_id: str = Field(min_length=1)
    tier: Literal["standard", "premium"]
    request_id: str = Field(min_length=1)


class SiteCloseRequest(BaseModel):
    operation_id: str = Field(min_length=1)


class SiteReturnRequest(BaseModel):
    operation_id: str = Field(min_length=1)


class ClockAdvance(BaseModel):
    to: str | None = None
    delta_seconds: float | None = None


# ---------------------------------------------------------------- 路由


@app.get("/clock")
def read_clock(conn=Depends(get_conn)):
    return {"now": site_db.get_clock(conn), "site_id": app.state.site_id}


@app.post("/clock/advance")
def advance_clock(body: ClockAdvance, conn=Depends(get_conn)):
    return site_service.advance_clock(conn, body.to, body.delta_seconds)


@app.post("/packages/import")
def import_package(body: ImportRequest, conn=Depends(get_conn)):
    return site_service.import_package(
        conn, body.envelope, app.state.site_id, app.state.signing_key)


@app.get("/packages")
def list_packages(conn=Depends(get_conn)):
    return {"site_id": app.state.site_id,
            "packages": site_service.list_packages(conn)}


@app.post("/sessions/admit")
def admit(body: SiteAdmitRequest, conn=Depends(get_conn)):
    return site_service.admit(conn, body.package_id, body.tier, body.request_id)


@app.post("/sessions/{session_id}/close")
def close(session_id: str, body: SiteCloseRequest, conn=Depends(get_conn)):
    return site_service.close(conn, session_id, body.operation_id)


@app.get("/sessions")
def list_sessions(package_id: str | None = None, conn=Depends(get_conn)):
    return {"site_id": app.state.site_id,
            "sessions": site_service.list_sessions(conn, package_id)}


@app.post("/packages/{package_id}/return")
def return_package(package_id: str, body: SiteReturnRequest, conn=Depends(get_conn)):
    """永久停用包、终止其会话并导出可重送的交回凭据。"""
    return site_service.return_package(conn, package_id, body.operation_id)
