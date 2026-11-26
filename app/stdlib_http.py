"""仅依赖标准库的 HTTP 服务包装（本机无 pip/外网，装不了 uvicorn/fastapi）。

中心与站点各跑一个独立 OS 进程、各自独立 SQLite 文件；
业务逻辑全部复用 app.service / app.offline / app.site_service，
HTTP 层只做 JSON 编解码与路由，不包含任何业务规则。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import budget, db, offline, service, site_db, site_service


class _CenterHandler(BaseHTTPRequestHandler):
    server_version = "offline-demo-center/1.0"

    def log_message(self, *args):  # 安静
        pass

    def _send(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):  # noqa: N802
        conn = db.connect(self.server.db_path)
        try:
            if self.path == "/clock":
                self._send(200, {"now": db.get_clock(conn)})
            elif self.path.startswith("/orgs/") and self.path.endswith("/offline-packages"):
                org = self.path.split("/")[2]
                self._send(200, {"org": org,
                                 "packages": offline.list_org_packages(conn, org)})
            elif self.path.startswith("/orgs/") and self.path.endswith("/budget"):
                org = self.path.split("/")[2]
                self._send(200, budget.summary(conn, org))
            elif self.path.startswith("/orgs/") and "/budget/usage" in self.path:
                org = self.path.split("/")[2]
                sess = None
                if "session_id=" in self.path:
                    sess = self.path.split("session_id=", 1)[1].split("&", 1)[0]
                self._send(200, {
                    "org": org,
                    "totals": budget.usage_totals(conn, org),
                    "usage": budget.usage(conn, org, sess),
                })
            elif self.path.startswith("/sessions/") and not self.path.endswith("/sessions"):
                session_id = self.path.split("/")[2]
                self._send(200, service.get_session_detail(conn, session_id))
            elif self.path.startswith("/events"):
                org = None
                if "org=" in self.path:
                    org = self.path.split("org=", 1)[1].split("&", 1)[0]
                self._send(200, {"events": service.list_events(
                    conn, org=org, limit=200)})
            elif self.path.startswith("/orgs/") and self.path.endswith("/sessions"):
                org = self.path.split("/")[2]
                self._send(200, {"org": org,
                                 "sessions": service.list_org_sessions(conn, org)})
            else:
                self._send(404, {"error": "not_found"})
        except service.ServiceError as exc:
            self._send(exc.http_status, {"error": exc.code, "message": exc.message})
        finally:
            conn.close()

    def do_POST(self):  # noqa: N802
        conn = db.connect(self.server.db_path)
        try:
            body = self._body()
            if self.path == "/clock/advance":
                self._send(200, service.advance_clock(
                    conn, body.get("to"), body.get("delta_seconds")))
            elif self.path == "/entitlements":
                self._send(201, service.create_entitlement(
                    conn, body["org"], body["tier"], body["seats"],
                    body["effective_at"], body["expire_at"]))
            elif self.path == "/sessions/admit":
                self._send(200, service.admit_session(
                    conn, body["org"], body["tier"], body["request_id"]))
            elif self.path == "/budgets/grants":
                self._send(201, budget.grant(
                    conn, body["org"], body["amount"], body["expire_at"],
                    body["request_id"]))
            elif self.path == "/offline/packages/issue":
                self._send(200, offline.issue_package(
                    conn, body["org"], body["site_id"],
                    body["standard_seats"], body["premium_seats"],
                    body["expire_at"], body["request_id"]))
            elif self.path == "/offline/packages/return":
                self._send(200, offline.return_package(
                    conn, body["return_credential"], body.get("operation_id")))
            else:
                self._send(404, {"error": "not_found"})
        except service.ServiceError as exc:
            self._send(exc.http_status, {"error": exc.code, "message": exc.message})
        finally:
            conn.close()


class _SiteHandler(BaseHTTPRequestHandler):
    server_version = "offline-demo-site/1.0"

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):  # noqa: N802
        conn = site_db.connect(self.server.db_path)
        try:
            if self.path == "/clock":
                self._send(200, {"now": site_db.get_clock(conn),
                                 "site_id": self.server.site_id})
            elif self.path == "/packages":
                self._send(200, {"site_id": self.server.site_id,
                                 "packages": site_service.list_packages(conn)})
            elif self.path == "/sessions":
                self._send(200, {"site_id": self.server.site_id,
                                 "sessions": site_service.list_sessions(conn)})
            else:
                self._send(404, {"error": "not_found"})
        except site_service.SiteError as exc:
            self._send(exc.http_status, {"error": exc.code, "message": exc.message})
        finally:
            conn.close()

    def do_POST(self):  # noqa: N802
        conn = site_db.connect(self.server.db_path)
        try:
            body = self._body()
            if self.path == "/clock/advance":
                self._send(200, site_service.advance_clock(
                    conn, body.get("to"), body.get("delta_seconds")))
            elif self.path == "/packages/import":
                self._send(200, site_service.import_package(
                    conn, body["envelope"], self.server.site_id,
                    self.server.signing_key))
            elif self.path == "/sessions/admit":
                self._send(200, site_service.admit(
                    conn, body["package_id"], body["tier"], body["request_id"]))
            elif self.path.startswith("/sessions/") and self.path.endswith("/close"):
                session_id = self.path.split("/")[2]
                self._send(200, site_service.close(
                    conn, session_id, body["operation_id"]))
            elif self.path.startswith("/packages/") and self.path.endswith("/return"):
                package_id = self.path.split("/")[2]
                self._send(200, site_service.return_package(
                    conn, package_id, body["operation_id"]))
            else:
                self._send(404, {"error": "not_found"})
        except site_service.SiteError as exc:
            self._send(exc.http_status, {"error": exc.code, "message": exc.message})
        finally:
            conn.close()


def make_center_server(port: int, db_path: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _CenterHandler)
    server.db_path = db_path
    return server


def make_site_server(port: int, db_path: str, site_id: str,
                     signing_key: bytes) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _SiteHandler)
    server.db_path = db_path
    server.site_id = site_id
    server.signing_key = signing_key
    return server
