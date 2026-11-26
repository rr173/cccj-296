"""站点侧业务：离线包导入、本地会话准入/关闭、时钟推进、提前还席。

站点与中心完全离线：
- 只接受中心签发（HMAC 验签通过）、签给本站（site_id 匹配）、未被篡改、
  尚在有效期、且未被本站交回过的包；
- 按包独立计并发：高级会话只用高级席，标准会话两类席都可用；
- 并发、重启、重复导入都不能多开（包唯一约束 + 准入幂等账 + 写锁事务）；
- 逻辑时钟显式推进，到期瞬间该包会话全部终止且不能再开新会话；
- 提前还席：永久停用包、终止其会话后，导出可重送的交回凭据（来自包内预签）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import crypto
from .db import new_id, normalize_ts
from .site_db import get_clock, next_seq, transaction


class SiteError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _emit(conn, kind: str, detail: dict, seq: int | None = None) -> int:
    if seq is None:
        seq = next_seq(conn)
    conn.execute(
        "INSERT INTO site_events(seq, kind, at_iso, detail_json) VALUES (?,?,?,?)",
        (seq, kind, get_clock(conn),
         json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )
    return seq


def _active_packages(conn, at: str):
    return conn.execute(
        "SELECT * FROM imported_packages WHERE status='active' "
        "AND issued_at <= ? AND expire_at > ? ORDER BY imported_seq, package_id",
        (at, at),
    ).fetchall()


def _active_session_count(conn, package_id: str, tier: str | None = None) -> int:
    sql = ("SELECT COUNT(*) FROM site_sessions WHERE package_id=? AND status='active'")
    params: list = [package_id]
    if tier is not None:
        sql += " AND tier=?"
        params.append(tier)
    return conn.execute(sql, params).fetchone()[0]


def _sweep_expired(conn, at: str) -> list[dict]:
    """终止所有在 at 时刻已到期的活跃包内的活跃会话。"""
    terminated: list[dict] = []
    expired_pkgs = conn.execute(
        "SELECT package_id FROM imported_packages WHERE status='active' AND expire_at <= ?",
        (at,),
    ).fetchall()
    for p in expired_pkgs:
        rows = conn.execute(
            "SELECT session_id, tier FROM site_sessions "
            "WHERE package_id=? AND status='active'", (p["package_id"],)
        ).fetchall()
        for r in rows:
            seq = next_seq(conn)
            conn.execute(
                "UPDATE site_sessions SET status='expired', ended_seq=?, "
                "end_reason='package_expired' WHERE session_id=?",
                (seq, r["session_id"]),
            )
            _emit(conn, "site_session.expired",
                  {"session_id": r["session_id"], "package_id": p["package_id"],
                   "tier": r["tier"]}, seq=seq)
            terminated.append({"session_id": r["session_id"],
                               "package_id": p["package_id"], "tier": r["tier"]})
    return terminated


# ---------------------------------------------------------------- 导入


def import_package(conn, envelope: dict, site_id: str, key: bytes) -> dict:
    with transaction(conn):
        at = get_clock(conn)
        try:
            payload = crypto.verify(key, envelope)
        except crypto.InvalidSignature as exc:
            raise SiteError("invalid_package_signature", str(exc), 409)
        if payload.get("kind") != "offline-seat-package":
            raise SiteError("invalid_package", "信封类型不是离线席位包", 422)
        if payload.get("site_id") != site_id:
            raise SiteError("package_site_mismatch",
                            f"该包签发给站点 {payload.get('site_id')}，本站是 {site_id}", 403)

        package_id = payload["package_id"]
        existing = conn.execute(
            "SELECT * FROM imported_packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if existing is not None:
            # 重复导入不新增、不多开；已交回的包永远不能重新启用
            if existing["status"] == "returned":
                raise SiteError("package_already_returned",
                                "该包已交回，不能重新启用", 409)
            return {"package_id": package_id, "imported": False,
                    "note": "包已存在，重复导入不产生新额度",
                    "status": "active" if at < existing["expire_at"] else "expired"}

        if at >= normalize_ts(payload["expire_at"]):
            raise SiteError("package_expired", "包导入时已过截止时间", 409)

        return_credential = payload.get("return_credential")
        if not isinstance(return_credential, dict):
            raise SiteError("invalid_package", "包内缺少中心预签的交回凭据", 422)

        seq = _emit(conn, "site_package.imported", {
            "package_id": package_id, "org": payload["org"],
            "standard_seats": payload["standard_seats"],
            "premium_seats": payload["premium_seats"],
            "expire_at": payload["expire_at"],
        })
        conn.execute(
            "INSERT INTO imported_packages(package_id, org, standard_seats, premium_seats, "
            "issued_at, expire_at, status, imported_seq, return_credential_json) "
            "VALUES (?,?,?,?,?,?,'active', ?, ?)",
            (package_id, payload["org"], payload["standard_seats"],
             payload["premium_seats"], payload["issued_at"],
             payload["expire_at"], seq,
             json.dumps(return_credential, ensure_ascii=False)),
        )
        return {"package_id": package_id, "imported": True,
                "org": payload["org"], "status": "active",
                "standard_seats": payload["standard_seats"],
                "premium_seats": payload["premium_seats"],
                "expire_at": payload["expire_at"]}


# ---------------------------------------------------------------- 准入/关闭


def admit(conn, package_id: str, tier: str, request_id: str) -> dict:
    if tier not in ("standard", "premium"):
        raise SiteError("invalid_tier", "tier 必须是 standard 或 premium", 422)
    with transaction(conn):
        at = get_clock(conn)
        _sweep_expired(conn, at)

        dup = conn.execute(
            "SELECT * FROM site_admission_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if dup is not None:
            if dup["package_id"] != package_id or dup["tier"] != tier:
                raise SiteError("request_id_param_mismatch",
                                "同一 request_id 已用于不同的包/tier", 409)
            return json.loads(dup["response_json"])

        pkg = conn.execute(
            "SELECT * FROM imported_packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if pkg is None:
            raise SiteError("package_not_found", "包未导入本站", 404)
        if pkg["status"] == "returned":
            return _reject_admit(conn, package_id, tier, request_id,
                                 "package_returned", at)
        if at >= pkg["expire_at"]:
            return _reject_admit(conn, package_id, tier, request_id,
                                 "package_expired", at)

        active_rows = conn.execute(
            "SELECT * FROM site_sessions WHERE package_id=? AND status='active' "
            "ORDER BY created_seq, session_id", (package_id,)).fetchall()
        active_premium = [r for r in active_rows if r["tier"] == "premium"]
        active_standard = [r for r in active_rows if r["tier"] == "standard"]

        # 模拟加入新会话后的包内再平衡（与中心同规则：高级优先，同类先到先得，
        # 标准会话先占标准席、满后借高级席），判断新会话能否被容纳
        def fits(tier_of_new: str) -> tuple[bool, list[str]]:
            premium_used = len(active_premium)
            standard_used = 0
            evict: list[str] = []
            ordered = active_standard + [None]  # None 代表新会话占位
            for idx, row in enumerate(ordered):
                is_new = row is None
                t = tier_of_new if is_new else row["tier"]
                if t == "premium":
                    if premium_used < pkg["premium_seats"]:
                        premium_used += 1
                    elif is_new:
                        return False, evict
                else:
                    if standard_used < pkg["standard_seats"]:
                        standard_used += 1
                    elif premium_used < pkg["premium_seats"]:
                        premium_used += 1
                    elif is_new:
                        return False, evict
            return True, evict

        if tier == "premium":
            ok, _ = fits("premium")
            if not ok:
                return _reject_admit(conn, package_id, tier, request_id,
                                     "no_premium_capacity", at)
        else:
            ok, _ = fits("standard")
            if not ok:
                return _reject_admit(conn, package_id, tier, request_id,
                                     "no_any_capacity", at)

        session_id = new_id("ssess")
        seq = _emit(conn, "site_session.admitted", {
            "session_id": session_id, "package_id": package_id,
            "tier": tier, "request_id": request_id})
        conn.execute(
            "INSERT INTO site_sessions(session_id, package_id, org, tier, status, "
            "request_id, created_seq) VALUES (?,?,?,?, 'active', ?, ?)",
            (session_id, package_id, pkg["org"], tier, request_id, seq),
        )
        payload = {"admitted": True, "session_id": session_id,
                   "package_id": package_id, "org": pkg["org"], "tier": tier,
                   "request_id": request_id, "status": "active", "at": at}
        conn.execute(
            "INSERT INTO site_admission_requests(request_id, package_id, tier, "
            "first_status, response_json, created_seq) "
            "VALUES (?,?,?,'admitted',?,?)",
            (request_id, package_id, tier,
             json.dumps(payload, ensure_ascii=False), seq),
        )
        return payload


def _reject_admit(conn, package_id, tier, request_id, code, at) -> dict:
    seq = _emit(conn, "site_session.rejected", {
        "request_id": request_id, "package_id": package_id, "reason": code})
    payload = {"admitted": False, "package_id": package_id, "tier": tier,
               "request_id": request_id, "reason": code, "at": at}
    conn.execute(
        "INSERT INTO site_admission_requests(request_id, package_id, tier, "
        "first_status, reject_code, response_json, created_seq) "
        "VALUES (?,?,?,'rejected', ?, ?, ?)",
        (request_id, package_id, tier, code,
         json.dumps(payload, ensure_ascii=False), seq),
    )
    return payload


def close(conn, session_id: str, operation_id: str) -> dict:
    with transaction(conn):
        dup = conn.execute(
            "SELECT * FROM site_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if dup is not None:
            if dup["op_type"] != "close" or dup["session_id"] != session_id:
                raise SiteError("operation_id_conflict",
                                "同一 operation_id 已用于不同操作", 409)
            return json.loads(dup["response_json"])

        sess = conn.execute(
            "SELECT * FROM site_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if sess is None:
            raise SiteError("session_not_found", "本地会话不存在", 404)

        seq = _emit(conn, "site_session.close_attempted",
                    {"session_id": session_id, "operation_id": operation_id})
        if sess["status"] != "active":
            payload = {"operation_id": operation_id, "session_id": session_id,
                       "ok": True, "released": False,
                       "session_status": sess["status"],
                       "note": "会话已处于终态，不释放额度"}
        else:
            conn.execute(
                "UPDATE site_sessions SET status='closed', ended_seq=?, "
                "end_reason='closed_locally' WHERE session_id=?",
                (seq, session_id),
            )
            payload = {"operation_id": operation_id, "session_id": session_id,
                       "ok": True, "released": True, "session_status": "closed"}
        conn.execute(
            "INSERT INTO site_operations(operation_id, op_type, package_id, "
            "session_id, response_json, seq) VALUES (?, 'close', ?, ?, ?, ?)",
            (operation_id, sess["package_id"], session_id,
             json.dumps(payload, ensure_ascii=False), seq),
        )
        return payload


# ---------------------------------------------------------------- 时钟


def advance_clock(conn, to_iso: str | None = None,
                  delta_seconds: float | None = None) -> dict:
    with transaction(conn):
        current = get_clock(conn)
        if to_iso is not None:
            target = normalize_ts(to_iso)
        elif delta_seconds is not None:
            target = normalize_ts(
                datetime.fromisoformat(current) + timedelta(seconds=delta_seconds))
        else:
            raise SiteError("missing_target", "需要提供 to 或 delta_seconds", 422)
        if target < current:
            raise SiteError("clock_cannot_go_back", "逻辑时钟不能回拨", 409)
        if target == current:
            return {"now": current, "advanced": False, "terminated": []}
        conn.execute("UPDATE clock SET now_iso=? WHERE id=1", (target,))
        seq = _emit(conn, "site_clock.advanced", {"from": current, "to": target})
        terminated = _sweep_expired(conn, target)
        return {"now": target, "advanced": True, "terminated": terminated,
                "seq": seq}


# ---------------------------------------------------------------- 提前还席


def return_package(conn, package_id: str, operation_id: str) -> dict:
    """永久停用包并终止其会话，导出包内中心预签的交回凭据（可重送）。"""
    with transaction(conn):
        at = get_clock(conn)
        _sweep_expired(conn, at)

        dup = conn.execute(
            "SELECT * FROM site_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if dup is not None:
            if dup["op_type"] != "return" or dup["package_id"] != package_id:
                raise SiteError("operation_id_conflict",
                                "同一 operation_id 已用于不同操作", 409)
            return json.loads(dup["response_json"])

        pkg = conn.execute(
            "SELECT * FROM imported_packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if pkg is None:
            raise SiteError("package_not_found", "包未导入本站", 404)
        if pkg["status"] == "returned":
            # 已交回：凭据仍可重送，但包不能重新启用
            payload = json.loads(pkg["return_credential_json"])
            return {"package_id": package_id, "returned": False,
                    "note": "包已处于交回状态，返回原交回凭据供重送",
                    "return_credential": payload}

        # 终止该包下全部仍活跃的本地会话
        terminated = []
        for r in conn.execute(
                "SELECT session_id FROM site_sessions "
                "WHERE package_id=? AND status='active'", (package_id,)).fetchall():
            seq = next_seq(conn)
            conn.execute(
                "UPDATE site_sessions SET status='returned_terminated', ended_seq=?, "
                "end_reason='package_returned' WHERE session_id=?",
                (seq, r["session_id"]),
            )
            _emit(conn, "site_session.returned_terminated",
                  {"session_id": r["session_id"], "package_id": package_id}, seq=seq)
            terminated.append(r["session_id"])

        # 交回凭据必须随包导入时保存；本站不具备签发能力
        cred = json.loads(pkg["return_credential_json"]) \
            if pkg["return_credential_json"] else None
        if cred is None:
            raise SiteError("return_credential_missing",
                            "包内没有中心预签的交回凭据，无法还席", 500)

        seq = _emit(conn, "site_package.returned", {
            "package_id": package_id, "terminated_sessions": terminated})
        conn.execute(
            "UPDATE imported_packages SET status='returned', returned_seq=? "
            "WHERE package_id=?",
            (seq, package_id),
        )
        payload = {"package_id": package_id, "returned": True,
                   "terminated_sessions": terminated,
                   "return_credential": cred, "at": at}
        conn.execute(
            "INSERT INTO site_operations(operation_id, op_type, package_id, "
            "session_id, response_json, seq) VALUES (?, 'return', ?, NULL, ?, ?)",
            (operation_id, package_id,
             json.dumps(payload, ensure_ascii=False), seq),
        )
        return payload


# ---------------------------------------------------------------- 查询


def list_packages(conn) -> list[dict]:
    at = get_clock(conn)
    out = []
    for r in conn.execute(
            "SELECT * FROM imported_packages ORDER BY imported_seq").fetchall():
        item = {k: r[k] for k in r.keys() if k != "return_credential_json"}
        if r["status"] == "active":
            item["derived_status"] = "expired" if at >= r["expire_at"] else "active"
            item["active_sessions"] = {
                "standard": _active_session_count(conn, r["package_id"], "standard"),
                "premium": _active_session_count(conn, r["package_id"], "premium"),
            }
        else:
            item["derived_status"] = r["status"]
        out.append(item)
    return out


def list_sessions(conn, package_id: str | None = None) -> list[dict]:
    if package_id:
        rows = conn.execute(
            "SELECT * FROM site_sessions WHERE package_id=? ORDER BY created_seq",
            (package_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM site_sessions ORDER BY created_seq").fetchall()
    return [dict(r) for r in rows]
