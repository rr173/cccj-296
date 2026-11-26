"""独立运行的测试收款方（payment stub）。

它 **不是** 会话准入服务的一部分：单独进程、单独 SQLite 文件、单独端口。
只用于本机演练，不做真实支付。

语义（严格按需求）：
  * 按 `merchant_order_id`（商户订单号）辨别重复操作：同一商户单号重复创建
    返回同一笔交易，绝不重复扣款。
  * 支持直接扣款（mode=direct，创建即 captured）与冻结后扣款
    （mode=freeze，创建即 frozen；`capture` 才真正扣款）。
  * 冻结成功后，资金一直足额可扣，直到商户主动 `void`（解冻）为止。
  * 可查询交易、关闭未扣款交易（void）、按原额全额退款（refund）。
  * 关闭（void）成功后交易进入终态：迟到请求（capture/refund/再次创建）
    都不可能再扣该单。
  * 测试钩子：让请求在「已处理并落库之后」丢失响应；让查询暂时不可用；
    让收款方卡住（延迟）；对指定商户单号明确拒绝支付。

运行：
    python3 -m app.payment_stub --port 8100 --db /tmp/payment_stub.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
    charge_id         TEXT PRIMARY KEY,
    merchant_order_id TEXT NOT NULL UNIQUE,
    amount_cents      INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN ('direct', 'freeze')),
    state             TEXT NOT NULL CHECK (state IN (
                          'frozen', 'captured', 'voided', 'refunded')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    captures          INTEGER NOT NULL DEFAULT 0,
    refunds           INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS charge_ops (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    charge_id  TEXT,
    op         TEXT NOT NULL,
    at         TEXT NOT NULL,
    detail     TEXT NOT NULL
);
"""


class StubState:
    """进程内测试控制：丢失响应 / 查询不可用 / 卡住 / 明确拒绝。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # {("create", merchant_order_id or None): 剩余丢响应次数}
        self.drop_responses: dict[tuple[str, str | None], int] = {}
        self.queries_unavailable = False
        self.mutations_unavailable = False
        self.creates_unavailable = False
        self.hang_seconds: dict[str, float] = {}
        self.decline_create: set[str] = set()
        self.decline_capture: set[str] = set()

    def reset(self) -> None:
        with self.lock:
            self.drop_responses.clear()
            self.queries_unavailable = False
            self.mutations_unavailable = False
            self.creates_unavailable = False
            self.hang_seconds.clear()
            self.decline_create.clear()
            self.decline_capture.clear()

    def consume_drop(self, op: str, merchant_order_id: str | None) -> bool:
        with self.lock:
            for key in ((op, merchant_order_id), (op, None)):
                if self.drop_responses.get(key, 0) > 0:
                    self.drop_responses[key] -= 1
                    return True
        return False

    def hang_for(self, op: str) -> float:
        with self.lock:
            return self.hang_seconds.get(op, 0.0) or self.hang_seconds.get("*", 0.0)


class StubHandler(BaseHTTPRequestHandler):
    server_version = "PaymentStub/1.0"
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------------------- 基础工具

    def log_message(self, *args):  # 静默
        pass

    @property
    def db_path(self) -> str:
        return self.server.db_path  # type: ignore[attr-defined]

    @property
    def state(self) -> StubState:
        return self.server.state  # type: ignore[attr-defined]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _send_json(self, status: int, payload: dict) -> bool:
        """返回 False 表示按测试钩子要求丢弃响应（此时数据已落库）。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _drop_response(self, op: str, merchant_order_id: str | None) -> None:
        # 数据已经提交；这里直接把连接关掉，客户端只会看到连接中断。
        self.close_connection = True
        self.connection.close()

    def _maybe_stall(self, op: str) -> None:
        seconds = self.state.hang_for(op)
        if seconds:
            time.sleep(seconds)

    def _log(self, conn: sqlite3.Connection, charge_id: str | None, op: str,
             detail: dict) -> None:
        conn.execute(
            "INSERT INTO charge_ops(charge_id, op, at, detail) VALUES (?,?,?,?)",
            (charge_id, op, _now(), json.dumps(detail, sort_keys=True)),
        )

    # -------------------------------------------------------------- 路由

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/_state":
            return self._handle_state()
        if parsed.path == "/charges":
            return self._handle_find(parse_qs(parsed.query).get("merchant_order_id", [None])[0])
        if parsed.path.startswith("/charges/"):
            return self._handle_get(parsed.path.rsplit("/", 1)[-1])
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/_control":
            return self._handle_control()
        body = self._read_body()
        if parsed.path == "/charges":
            return self._handle_create(body)
        if parsed.path.endswith("/capture"):
            return self._handle_capture(parsed.path.split("/")[2])
        if parsed.path.endswith("/void"):
            return self._handle_void(parsed.path.split("/")[2])
        if parsed.path.endswith("/refund"):
            return self._handle_refund(parsed.path.split("/")[2])
        self._send_json(404, {"error": "not_found"})

    # -------------------------------------------------------------- 交易操作

    def _charge_payload(self, row: sqlite3.Row) -> dict:
        return {
            "charge_id": row["charge_id"],
            "merchant_order_id": row["merchant_order_id"],
            "amount_cents": row["amount_cents"],
            "currency": row["currency"],
            "mode": row["mode"],
            "state": row["state"],
            "captures": row["captures"],
            "refunds": row["refunds"],
        }

    def _handle_create(self, body: dict):
        self._maybe_stall("create")
        if self.state.creates_unavailable:
            # 收款方暂时不接受创建：完全没有落任何交易
            return self._send_json(503, {"error": "creates_unavailable"})
        merchant_order_id = body.get("merchant_order_id")
        amount = body.get("amount_cents")
        currency = body.get("currency") or "CNY"
        mode = body.get("mode") or "direct"
        if not merchant_order_id or not isinstance(amount, int) or amount <= 0 \
                or mode not in ("direct", "freeze"):
            return self._send_json(400, {"error": "invalid_charge_request"})

        conn = self._connect()
        try:
            with self.state.lock:
                declined = merchant_order_id in self.state.decline_create
            if declined:
                self._log(conn, None, "create", {"merchant_order_id": merchant_order_id,
                                                 "result": "declined"})
                return self._send_json(402, {"error": "card_declined",
                                             "merchant_order_id": merchant_order_id})

            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM charges WHERE merchant_order_id=?", (merchant_order_id,)
            ).fetchone()
            if existing is not None:
                # 按商户单号辨别重复：同参数返回同一交易，绝不重复扣款
                conn.execute("COMMIT")
                if (existing["amount_cents"] != amount or existing["currency"] != currency
                        or existing["mode"] != mode):
                    return self._send_json(409, {"error": "merchant_order_conflict",
                                                 "charge": self._charge_payload(existing)})
                self._log(conn, existing["charge_id"], "create",
                          {"merchant_order_id": merchant_order_id, "result": "duplicate"})
                return self._send_json(200, {"charge": self._charge_payload(existing)})

            charge_id = "ch_" + merchant_order_id
            state = "captured" if mode == "direct" else "frozen"
            now = _now()
            conn.execute(
                "INSERT INTO charges(charge_id, merchant_order_id, amount_cents, currency, "
                "mode, state, created_at, updated_at, captures) VALUES (?,?,?,?,?,?,?,?,?)",
                (charge_id, merchant_order_id, amount, currency, mode, state, now, now,
                 1 if mode == "direct" else 0),
            )
            conn.execute("COMMIT")
            self._log(conn, charge_id, "create",
                      {"merchant_order_id": merchant_order_id, "result": "created",
                       "mode": mode})
            row = conn.execute("SELECT * FROM charges WHERE charge_id=?", (charge_id,)).fetchone()
            payload = {"charge": self._charge_payload(row)}
            if self.state.consume_drop("create", merchant_order_id):
                return self._drop_response("create", merchant_order_id)
            return self._send_json(201, payload)
        finally:
            conn.close()

    def _load_charge(self, conn: sqlite3.Connection, charge_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM charges WHERE charge_id=?", (charge_id,)).fetchone()

    def _handle_get(self, charge_id: str):
        self._maybe_stall("query_charge")
        if self.state.queries_unavailable:
            return self._send_json(503, {"error": "queries_unavailable"})
        conn = self._connect()
        try:
            row = self._load_charge(conn, charge_id)
            if row is None:
                return self._send_json(404, {"error": "charge_not_found"})
            return self._send_json(200, {"charge": self._charge_payload(row)})
        finally:
            conn.close()

    def _handle_find(self, merchant_order_id: str | None):
        self._maybe_stall("query_merchant")
        if self.state.queries_unavailable:
            return self._send_json(503, {"error": "queries_unavailable"})
        if not merchant_order_id:
            return self._send_json(400, {"error": "merchant_order_id_required"})
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM charges WHERE merchant_order_id=?", (merchant_order_id,)
            ).fetchone()
            if row is None:
                return self._send_json(404, {"error": "charge_not_found"})
            return self._send_json(200, {"charge": self._charge_payload(row)})
        finally:
            conn.close()

    def _mutation_allowed(self) -> bool:
        return not self.state.mutations_unavailable

    def _handle_capture(self, charge_id: str):
        self._maybe_stall("capture")
        if not self._mutation_allowed():
            return self._send_json(503, {"error": "mutations_unavailable"})
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_charge(conn, charge_id)
            if row is None:
                conn.execute("COMMIT")
                return self._send_json(404, {"error": "charge_not_found"})
            if row["merchant_order_id"] in self.state.decline_capture:
                conn.execute("COMMIT")
                return self._send_json(402, {"error": "capture_declined"})
            if row["state"] == "captured":
                conn.execute("COMMIT")
                return self._send_json(200, {"charge": self._charge_payload(row),
                                             "duplicate": True})
            if row["state"] != "frozen":
                conn.execute("COMMIT")
                return self._send_json(409, {"error": "charge_closed",
                                             "charge": self._charge_payload(row)})
            now = _now()
            conn.execute(
                "UPDATE charges SET state='captured', captures=captures+1, updated_at=? "
                "WHERE charge_id=?", (now, charge_id))
            conn.execute("COMMIT")
            self._log(conn, charge_id, "capture", {"result": "captured"})
            row = self._load_charge(conn, charge_id)
            payload = {"charge": self._charge_payload(row)}
            if self.state.consume_drop("capture", row["merchant_order_id"]):
                return self._drop_response("capture", row["merchant_order_id"])
            return self._send_json(200, payload)
        finally:
            conn.close()

    def _handle_void(self, charge_id: str):
        self._maybe_stall("void")
        if not self._mutation_allowed():
            return self._send_json(503, {"error": "mutations_unavailable"})
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_charge(conn, charge_id)
            if row is None:
                conn.execute("COMMIT")
                return self._send_json(404, {"error": "charge_not_found"})
            if row["state"] == "voided":
                conn.execute("COMMIT")
                return self._send_json(200, {"charge": self._charge_payload(row),
                                             "duplicate": True})
            if row["state"] == "captured":
                # 已扣款交易不能关闭，必须走原额退款
                conn.execute("COMMIT")
                return self._send_json(409, {"error": "already_captured",
                                             "charge": self._charge_payload(row)})
            if row["state"] == "refunded":
                conn.execute("COMMIT")
                return self._send_json(409, {"error": "charge_refunded",
                                             "charge": self._charge_payload(row)})
            # frozen -> voided（解冻并关闭；此后迟到请求不能再扣该单）
            now = _now()
            conn.execute(
                "UPDATE charges SET state='voided', updated_at=? WHERE charge_id=?",
                (now, charge_id))
            conn.execute("COMMIT")
            self._log(conn, charge_id, "void", {"result": "voided"})
            row = self._load_charge(conn, charge_id)
            payload = {"charge": self._charge_payload(row)}
            if self.state.consume_drop("void", row["merchant_order_id"]):
                return self._drop_response("void", row["merchant_order_id"])
            return self._send_json(200, payload)
        finally:
            conn.close()

    def _handle_refund(self, charge_id: str):
        self._maybe_stall("refund")
        if not self._mutation_allowed():
            return self._send_json(503, {"error": "mutations_unavailable"})
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_charge(conn, charge_id)
            if row is None:
                conn.execute("COMMIT")
                return self._send_json(404, {"error": "charge_not_found"})
            if row["state"] == "refunded":
                conn.execute("COMMIT")
                return self._send_json(200, {"charge": self._charge_payload(row),
                                             "duplicate": True})
            if row["state"] != "captured":
                conn.execute("COMMIT")
                return self._send_json(409, {"error": "not_captured",
                                             "charge": self._charge_payload(row)})
            now = _now()
            # 原额全额退款
            conn.execute(
                "UPDATE charges SET state='refunded', refunds=refunds+1, updated_at=? "
                "WHERE charge_id=?", (now, charge_id))
            conn.execute("COMMIT")
            self._log(conn, charge_id, "refund", {"result": "refunded",
                                                  "amount_cents": row["amount_cents"]})
            row = self._load_charge(conn, charge_id)
            payload = {"charge": self._charge_payload(row)}
            if self.state.consume_drop("refund", row["merchant_order_id"]):
                return self._drop_response("refund", row["merchant_order_id"])
            return self._send_json(200, payload)
        finally:
            conn.close()

    # -------------------------------------------------------------- 测试控制

    def _handle_state(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM charges ORDER BY created_at").fetchall()
            ops = conn.execute("SELECT op, COUNT(*) AS n FROM charge_ops GROUP BY op").fetchall()
            ledger = conn.execute(
                "SELECT charge_id, op, at, detail FROM charge_ops ORDER BY id").fetchall()
            return self._send_json(200, {
                "charges": [self._charge_payload(r) for r in rows],
                "ops": {r["op"]: r["n"] for r in ops},
                "ledger": [{"charge_id": r["charge_id"], "op": r["op"], "at": r["at"],
                            "detail": json.loads(r["detail"])} for r in ledger],
                "controls": {
                    "queries_unavailable": self.state.queries_unavailable,
                    "mutations_unavailable": self.state.mutations_unavailable,
                    "creates_unavailable": self.state.creates_unavailable,
                    "hang_seconds": dict(self.state.hang_seconds),
                },
            })
        finally:
            conn.close()

    def _handle_control(self):
        body = self._read_body()
        st = self.state
        with st.lock:
            action = body.get("action") or "set"
            if action == "reset":
                st.reset()
                return self._send_json(200, {"ok": True, "reset": True})
            if action == "drop":
                op = body.get("op", "create")
                key = (op, body.get("merchant_order_id"))
                st.drop_responses[key] = int(body.get("count", 1))
                return self._send_json(200, {"ok": True})
            if "queries_unavailable" in body:
                st.queries_unavailable = bool(body["queries_unavailable"])
            if "mutations_unavailable" in body:
                st.mutations_unavailable = bool(body["mutations_unavailable"])
            if "creates_unavailable" in body:
                st.creates_unavailable = bool(body["creates_unavailable"])
            if "hang_seconds" in body:
                op = body.get("op", "*")
                st.hang_seconds[op] = float(body["hang_seconds"])
            if "decline_create" in body:
                st.decline_create.add(body["decline_create"])
                st.decline_create.discard(None)
            if "decline_capture" in body:
                st.decline_capture.add(body["decline_capture"])
                st.decline_capture.discard(None)
        return self._send_json(200, {"ok": True})


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class StubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, db_path: str):
        super().__init__(address, StubHandler)
        self.db_path = db_path
        self.state = StubState()


def init_stub(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def make_server(port: int, db_path: str) -> StubServer:
    init_stub(db_path)
    return StubServer(("127.0.0.1", port), db_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="独立测试收款方")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--db", default="/tmp/payment_stub.db")
    args = parser.parse_args()
    init_stub(args.db)
    server = StubServer(("127.0.0.1", args.port), args.db)
    print(f"测试收款方已启动: http://127.0.0.1:{args.port}  数据库: {args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
