"""服务层：准入/续用/关闭/权益变更/时间推进的全部事务逻辑。

再平衡（reconcile）是系统的核心：给定组织在某个逻辑时刻的有效权益集合
与全部活跃会话，按
    高级先于标准；同类开始较早先于较晚；会话 ID 破同序
排序后贪心安置——
  1. 高级会话只能占高级席位；
  2. 标准会话先占标准席位，标准席位满后再借用高级席位。
这样不会因为历史上把标准会话放在高级额度上，而挤掉本可容纳的高级会话。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import db
from .db import new_id, next_seq, normalize_ts

# ---------------------------------------------------------------- 常量与异常


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


TIER_PRIORITY = {"premium": 0, "standard": 1}


# ---------------------------------------------------------------- 事件与序号


def _emit(conn: sqlite3.Connection, kind: str, org: str | None, detail: dict,
          seq: int | None = None) -> int:
    if seq is None:
        seq = next_seq(conn)
    conn.execute(
        "INSERT INTO events(seq, kind, org, at_iso, detail_json) VALUES (?,?,?,?,?)",
        (seq, kind, org, db.get_clock(conn), json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )
    return seq


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------- 查询辅助


def _active_entitlements(conn: sqlite3.Connection, org: str, at: str) -> list[sqlite3.Row]:
    """左闭右开区间 [effective_at, expire_at) 内在 at 时刻生效的权益。"""
    return conn.execute(
        "SELECT * FROM entitlements "
        "WHERE org = ? AND status = 'active' "
        "AND effective_at <= ? AND expire_at > ? "
        "ORDER BY row_id",
        (org, at, at),
    ).fetchall()


def _active_sessions(conn: sqlite3.Connection, org: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE org = ? AND status = 'active' ORDER BY created_seq, session_id",
        (org,),
    ).fetchall()


def _open_slot_for(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM slot_history WHERE session_id = ? AND released_seq IS NULL",
        (session_id,),
    ).fetchone()


def _dump_ent(row: sqlite3.Row) -> dict:
    return {
        "ent_id": row["ent_id"],
        "version": row["version"],
        "org": row["org"],
        "tier": row["tier"],
        "seats": row["seats"],
        "effective_at": row["effective_at"],
        "expire_at": row["expire_at"],
        "status": row["status"],
        "revoke_reason": row["revoke_reason"],
        "created_seq": row["created_seq"],
        "ended_seq": row["ended_seq"],
    }


# ---------------------------------------------------------------- 再平衡引擎


@dataclass
class ReconcileResult:
    retained: list[str]          # 仍是 active 的会话 ID
    evicted: list[dict]          # 本次撤销的会话及原因
    assignments: dict[str, str]  # 会话 ID -> 占用的 ent_id


def reconcile(conn: sqlite3.Connection, org: str) -> ReconcileResult:
    """按优先顺序重新确定组织内的活跃会话集合。

    在调用方已经打开的写事务内执行。每次撤销各取一个事件 seq；
    席位迁移只落 slot_history（其 assigned/released seq 同样可追溯）。
    """
    at = db.get_clock(conn)
    ents = _active_entitlements(conn, org, at)
    sessions = _active_sessions(conn, org)

    # 席位池：ent_id -> [容量, 已用]；按种类分桶
    premium_pools: dict[str, list[Any]] = {
        e["ent_id"]: [e["seats"], 0] for e in ents if e["tier"] == "premium"
    }
    standard_pools: dict[str, list[Any]] = {
        e["ent_id"]: [e["seats"], 0] for e in ents if e["tier"] == "standard"
    }
    # 保留会话现有的实际占用（每个活跃会话至多一条未释放记录）
    current_ents: dict[str, str] = {}
    for s in sessions:
        slot = _open_slot_for(conn, s["session_id"])
        if slot is not None:
            current_ents[s["session_id"]] = slot["ent_id"]

    ordered = sorted(
        sessions,
        key=lambda s: (TIER_PRIORITY[s["tier"]], s["created_seq"], s["session_id"]),
    )

    retained: list[str] = []
    evicted: list[dict] = []
    assignments: dict[str, str] = {}

    def take_from(pools: dict[str, list[Any]], preferred: str | None) -> str | None:
        # 优先继续占原有权益，减少无意义迁移
        if preferred and preferred in pools and pools[preferred][1] < pools[preferred][0]:
            pools[preferred][1] += 1
            return preferred
        for ent_id, (cap, used) in pools.items():
            if used < cap:
                pools[ent_id][1] = used + 1
                return ent_id
        return None

    for s in ordered:
        preferred = current_ents.get(s["session_id"])
        chosen: str | None = None
        if s["tier"] == "premium":
            chosen = take_from(premium_pools, preferred)
            if chosen is None:
                reason = "no_premium_capacity"
        else:
            chosen = take_from(standard_pools, preferred)
            if chosen is None:
                chosen = take_from(premium_pools, preferred)
            if chosen is None:
                reason = "no_any_capacity"

        if chosen is None:
            # 撤销：终态落库，释放原占用，写事件（每个撤销独立 seq）
            seq = next_seq(conn)
            old = current_ents.get(s["session_id"])
            conn.execute(
                "UPDATE sessions SET status='revoked', ended_seq=?, "
                "end_reason=?, current_cred_id=NULL WHERE session_id=?",
                (seq, reason, s["session_id"]),
            )
            if old is not None:
                conn.execute(
                    "UPDATE slot_history SET released_seq=?, release_reason=? "
                    "WHERE session_id=? AND released_seq IS NULL",
                    (seq, reason, s["session_id"]),
                )
            _emit(
                conn,
                "session.revoked",
                org,
                {
                    "session_id": s["session_id"],
                    "tier": s["tier"],
                    "reason": reason,
                    "previous_ent_id": old,
                },
                seq=seq,
            )
            evicted.append(
                {
                    "session_id": s["session_id"],
                    "tier": s["tier"],
                    "reason": reason,
                    "previous_ent_id": old,
                }
            )
            continue

        retained.append(s["session_id"])
        assignments[s["session_id"]] = chosen

        # 席位迁移或首次分配：只落 slot_history，其 seq 可在事件流/历史中核对
        if current_ents.get(s["session_id"]) != chosen:
            if s["session_id"] in current_ents:
                conn.execute(
                    "UPDATE slot_history SET released_seq=?, release_reason='rebalanced' "
                    "WHERE session_id=? AND released_seq IS NULL",
                    (next_seq(conn), s["session_id"]),
                )
            assign_seq = next_seq(conn)
            conn.execute(
                "INSERT INTO slot_history(session_id, ent_id, assigned_seq) VALUES (?,?,?)",
                (s["session_id"], chosen, assign_seq),
            )
        conn.execute(
            "UPDATE sessions SET last_ent_id=? WHERE session_id=?",
            (chosen, s["session_id"]),
        )

    return ReconcileResult(retained=retained, evicted=evicted, assignments=assignments)


# ---------------------------------------------------------------- 会话准入


def admit_session(conn: sqlite3.Connection, org: str, tier: str, request_id: str) -> dict:
    if tier not in ("standard", "premium"):
        raise ServiceError("invalid_tier", "tier 必须是 standard 或 premium", 422)

    with db.transaction(conn):
        # 幂等：同 request_id 重试
        dup = conn.execute(
            "SELECT * FROM admission_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if dup is not None:
            if dup["org"] != org or dup["tier"] != tier:
                raise ServiceError(
                    "request_id_param_mismatch",
                    "同一 request_id 已用于不同的 org/tier 参数",
                    409,
                )
            # 原样返回第一次的结果（即使会话后来已撤销，也指向原会话）
            return json.loads(dup["response_json"])

        at = db.get_clock(conn)
        session_id = new_id("sess")
        seq = _emit(
            conn,
            "session.admit_requested",
            org,
            {"request_id": request_id, "tier": tier},
        )
        conn.execute(
            "INSERT INTO sessions(session_id, org, tier, status, created_seq, admit_request_id) "
            "VALUES (?,?,?, 'active', ?, ?)",
            (session_id, org, tier, seq, request_id),
        )

        result = reconcile(conn, org)
        status_row = conn.execute(
            "SELECT status, end_reason FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()

        if session_id not in result.retained:
            # 容量不足：会话以 revoked 终态留痕，不返回凭据
            reason = status_row["end_reason"]
            payload = {
                "request_id": request_id,
                "org": org,
                "tier": tier,
                "admitted": False,
                "session_id": session_id,
                "status": "revoked",
                "reason": reason,
            }
            conn.execute(
                "INSERT INTO admission_requests(request_id, org, tier, first_session_id, "
                "first_status, reject_code, response_json, created_seq) "
                "VALUES (?,?,?,?, 'rejected', ?, ?, ?)",
                (request_id, org, tier, session_id, reason,
                 json.dumps(payload, ensure_ascii=False), seq),
            )
            return payload

        # 成功：签发第一代凭据
        secret = _new_secret()
        conn.execute(
            "INSERT INTO credentials(session_id, generation, secret_hash, status, issued_seq) "
            "VALUES (?, 1, ?, 'current', ?)",
            (session_id, _hash_secret(secret), seq),
        )
        cred_id = conn.execute(
            "SELECT cred_id FROM credentials WHERE session_id=? AND generation=1",
            (session_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE sessions SET current_cred_id=? WHERE session_id=?", (cred_id, session_id)
        )
        payload = {
            "request_id": request_id,
            "org": org,
            "tier": tier,
            "admitted": True,
            "session_id": session_id,
            "status": "active",
            "credential": {"generation": 1, "secret": secret},
            "assigned_ent_id": result.assignments[session_id],
            "at": at,
        }
        conn.execute(
            "INSERT INTO admission_requests(request_id, org, tier, first_session_id, "
            "first_status, response_json, created_seq) "
            "VALUES (?,?,?,?, 'admitted', ?, ?)",
            (request_id, org, tier, session_id,
             json.dumps(payload, ensure_ascii=False), seq),
        )
        return payload


# ---------------------------------------------------------------- 凭据续用


def _check_operation_replay(conn: sqlite3.Connection, operation_id: str, op_type: str,
                            session_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    if row["op_type"] != op_type or row["session_id"] != session_id:
        raise ServiceError("operation_id_conflict", "同一 operation_id 已用于不同操作", 409)
    return json.loads(row["response_json"])


def _record_operation(conn: sqlite3.Connection, operation_id: str, op_type: str,
                      session_id: str, cred_id: int | None, ok: bool,
                      error_code: str | None, payload: dict, seq: int) -> None:
    conn.execute(
        "INSERT INTO operations(operation_id, op_type, session_id, presented_cred_id, "
        "result, error_code, response_json, seq) VALUES (?,?,?,?,?,?,?,?)",
        (operation_id, op_type, session_id, cred_id, "ok" if ok else "failed",
         error_code, json.dumps(payload, ensure_ascii=False), seq),
    )


def renew_session(conn: sqlite3.Connection, session_id: str, credential_secret: str,
                  operation_id: str) -> dict:
    with db.transaction(conn):
        replay = _check_operation_replay(conn, operation_id, "renew", session_id)
        if replay is not None:
            return replay

        sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if sess is None:
            raise ServiceError("session_not_found", "会话不存在", 404)

        secret_hash = _hash_secret(credential_secret)
        cred = conn.execute(
            "SELECT * FROM credentials WHERE session_id=? AND secret_hash=?",
            (session_id, secret_hash),
        ).fetchone()

        seq = _emit(
            conn,
            "session.renew_attempted",
            sess["org"],
            {"session_id": session_id, "operation_id": operation_id},
        )

        if cred is None:
            payload = {"operation_id": operation_id, "session_id": session_id,
                       "ok": False, "error": "invalid_credential"}
            _record_operation(conn, operation_id, "renew", session_id, None, False,
                              "invalid_credential", payload, seq)
            return payload
        # 会话终态优先于凭据代次：已撤销/已关闭的会话，任何凭据都不能续用
        if sess["status"] != "active":
            payload = {"operation_id": operation_id, "session_id": session_id,
                       "ok": False, "error": f"session_{sess['status']}",
                       "session_status": sess["status"]}
            _record_operation(conn, operation_id, "renew", session_id, cred["cred_id"], False,
                              f"session_{sess['status']}", payload, seq)
            return payload
        if cred["status"] != "current":
            payload = {"operation_id": operation_id, "session_id": session_id,
                       "ok": False, "error": "old_credential",
                       "presented_generation": cred["generation"]}
            _record_operation(conn, operation_id, "renew", session_id, cred["cred_id"], False,
                              "old_credential", payload, seq)
            return payload

        # 成功：作废旧代、签发下一代
        new_generation = cred["generation"] + 1
        new_secret = _new_secret()
        conn.execute(
            "UPDATE credentials SET status='consumed', consumed_seq=? WHERE cred_id=?",
            (seq, cred["cred_id"]),
        )
        conn.execute(
            "INSERT INTO credentials(session_id, generation, secret_hash, status, issued_seq) "
            "VALUES (?,?,?, 'current', ?)",
            (session_id, new_generation, _hash_secret(new_secret), seq),
        )
        new_cred_id = conn.execute(
            "SELECT cred_id FROM credentials WHERE session_id=? AND generation=?",
            (session_id, new_generation),
        ).fetchone()[0]
        conn.execute(
            "UPDATE sessions SET current_cred_id=? WHERE session_id=?",
            (new_cred_id, session_id),
        )
        payload = {
            "operation_id": operation_id,
            "session_id": session_id,
            "ok": True,
            "credential": {"generation": new_generation, "secret": new_secret},
            "at": db.get_clock(conn),
        }
        _record_operation(conn, operation_id, "renew", session_id, cred["cred_id"], True,
                          None, payload, seq)
        return payload


# ---------------------------------------------------------------- 主动关闭


def close_session(conn: sqlite3.Connection, session_id: str, credential_secret: str | None,
                  operation_id: str) -> dict:
    with db.transaction(conn):
        replay = _check_operation_replay(conn, operation_id, "close", session_id)
        if replay is not None:
            return replay

        sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if sess is None:
            raise ServiceError("session_not_found", "会话不存在", 404)

        cred_id = None
        bad_cred = False
        old_cred = False
        if credential_secret:
            cred = conn.execute(
                "SELECT * FROM credentials WHERE session_id=? AND secret_hash=?",
                (session_id, _hash_secret(credential_secret)),
            ).fetchone()
            if cred is None:
                bad_cred = True
            else:
                cred_id = cred["cred_id"]
                old_cred = cred["status"] != "current"

        seq = _emit(
            conn,
            "session.close_attempted",
            sess["org"],
            {"session_id": session_id, "operation_id": operation_id},
        )

        # 凭据错误一律失败，不改变任何占用
        if bad_cred:
            payload = {"operation_id": operation_id, "session_id": session_id,
                       "ok": False, "error": "invalid_credential"}
            _record_operation(conn, operation_id, "close", session_id, None, False,
                              "invalid_credential", payload, seq)
            return payload

        if sess["status"] != "active":
            # 重复关闭 / 晚到的旧会话关闭：幂等成功但不释放任何额度（终态优先）
            payload = {
                "operation_id": operation_id,
                "session_id": session_id,
                "ok": True,
                "released": False,
                "session_status": sess["status"],
                "note": "会话已处于终态，不释放额度",
            }
            _record_operation(conn, operation_id, "close", session_id, cred_id, True,
                              None, payload, seq)
            return payload

        if old_cred:
            # 活跃会话上，旧凭据不能发起新操作
            payload = {"operation_id": operation_id, "session_id": session_id,
                       "ok": False, "error": "old_credential"}
            _record_operation(conn, operation_id, "close", session_id, cred_id, False,
                              "old_credential", payload, seq)
            return payload

        # 只释放它实际占用的那一份额度
        slot = _open_slot_for(conn, session_id)
        conn.execute(
            "UPDATE sessions SET status='closed', ended_seq=?, end_reason='closed_by_user', "
            "current_cred_id=NULL WHERE session_id=?",
            (seq, session_id),
        )
        if slot is not None:
            conn.execute(
                "UPDATE slot_history SET released_seq=?, release_reason='closed_by_user' "
                "WHERE id=?",
                (seq, slot["id"]),
            )
        if cred_id is not None:
            conn.execute(
                "UPDATE credentials SET status='consumed', consumed_seq=? WHERE cred_id=? "
                "AND status='current'",
                (seq, cred_id),
            )
        payload = {
            "operation_id": operation_id,
            "session_id": session_id,
            "ok": True,
            "released": slot is not None,
            "released_ent_id": slot["ent_id"] if slot is not None else None,
            "session_status": "closed",
        }
        _record_operation(conn, operation_id, "close", session_id, cred_id, True,
                          None, payload, seq)
        return payload


# ---------------------------------------------------------------- 权益管理


def _validate_window(effective_at: str, expire_at: str) -> tuple[str, str]:
    eff = normalize_ts(effective_at)
    exp = normalize_ts(expire_at)
    if exp <= eff:
        raise ServiceError("invalid_window", "expire_at 必须晚于 effective_at", 422)
    return eff, exp


def create_entitlement_locked(conn: sqlite3.Connection, org: str, tier: str, seats: int,
                              effective_at: str, expire_at: str, *,
                              source: str = "manual", detail: dict | None = None) -> dict:
    """在调用方已打开的写事务内创建一份权益并立即触发再平衡。

    组织购权的发放也走这里：权益窗口在**实际发放那一刻**由调用方用逻辑时钟算好，
    与订单发放记录在同一事务里落库，之后的重试/恢复不会再重算窗口。
    """
    if tier not in ("standard", "premium"):
        raise ServiceError("invalid_tier", "tier 必须是 standard 或 premium", 422)
    if seats < 0:
        raise ServiceError("invalid_seats", "seats 不能为负", 422)
    eff, exp = _validate_window(effective_at, expire_at)

    ent_id = new_id("ent")
    event_detail = {"ent_id": ent_id, "tier": tier, "seats": seats,
                    "effective_at": eff, "expire_at": exp, "source": source}
    if detail:
        event_detail.update(detail)
    seq = _emit(conn, "entitlement.created", org, event_detail)
    conn.execute(
        "INSERT INTO entitlements(ent_id, version, org, tier, seats, effective_at, "
        "expire_at, status, created_seq) VALUES (?, 1, ?, ?, ?, ?, ?, 'active', ?)",
        (ent_id, org, tier, seats, eff, exp, seq),
    )
    result = reconcile(conn, org)
    return {
        "ent_id": ent_id,
        "seq": seq,
        "entitlement": _dump_ent(
            conn.execute("SELECT * FROM entitlements WHERE ent_id=? AND version=1",
                         (ent_id,)).fetchone()),
        "evicted": result.evicted,
    }


def create_entitlement(conn: sqlite3.Connection, org: str, tier: str, seats: int,
                       effective_at: str, expire_at: str) -> dict:
    with db.transaction(conn):
        out = create_entitlement_locked(
            conn, org, tier, seats, effective_at, expire_at, source="manual")
        return {"entitlement": out["entitlement"], "evicted": out["evicted"]}


def modify_entitlement(conn: sqlite3.Connection, ent_id: str, *, seats: int | None = None,
                       effective_at: str | None = None, expire_at: str | None = None) -> dict:
    if seats is not None and seats < 0:
        raise ServiceError("invalid_seats", "seats 不能为负", 422)
    with db.transaction(conn):
        row = conn.execute(
            "SELECT * FROM entitlements WHERE ent_id=? AND status='active' "
            "ORDER BY version DESC LIMIT 1",
            (ent_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("entitlement_not_active", "权益不存在或已不在 active 状态", 404)

        new_seats = row["seats"] if seats is None else seats
        new_eff = row["effective_at"] if effective_at is None else normalize_ts(effective_at)
        new_exp = row["expire_at"] if expire_at is None else normalize_ts(expire_at)
        if new_exp <= new_eff:
            raise ServiceError("invalid_window", "expire_at 必须晚于 effective_at", 422)

        org = row["org"]
        seq = _emit(
            conn,
            "entitlement.modified",
            org,
            {"ent_id": ent_id, "old_version": row["version"],
             "changes": {"seats": new_seats, "effective_at": new_eff, "expire_at": new_exp}},
        )
        # 旧版本留痕；版本链由新版本行的 supersedes 指回旧 row_id
        conn.execute(
            "UPDATE entitlements SET status='superseded', ended_seq=? WHERE row_id=?",
            (seq, row["row_id"]),
        )
        new_version = row["version"] + 1
        conn.execute(
            "INSERT INTO entitlements(ent_id, version, org, tier, seats, effective_at, "
            "expire_at, status, supersedes, created_seq) "
            "VALUES (?,?,?,?,?,?,?, 'active', ?, ?)",
            (ent_id, new_version, org, row["tier"], new_seats, new_eff, new_exp,
             row["row_id"], seq),
        )
        result = reconcile(conn, org)
        return {"entitlement": _dump_ent(
            conn.execute("SELECT * FROM entitlements WHERE ent_id=? AND version=?",
                         (ent_id, new_version)).fetchone()),
            "evicted": result.evicted}


def revoke_entitlement(conn: sqlite3.Connection, ent_id: str, reason: str) -> dict:
    with db.transaction(conn):
        row = conn.execute(
            "SELECT * FROM entitlements WHERE ent_id=? AND status='active' "
            "ORDER BY version DESC LIMIT 1",
            (ent_id,),
        ).fetchone()
        if row is None:
            raise ServiceError("entitlement_not_active", "权益不存在或已不在 active 状态", 404)
        org = row["org"]
        seq = _emit(conn, "entitlement.revoked", org,
                    {"ent_id": ent_id, "reason": reason, "version": row["version"]})
        conn.execute(
            "UPDATE entitlements SET status='revoked', ended_seq=?, revoke_reason=? WHERE row_id=?",
            (seq, reason, row["row_id"]),
        )
        result = reconcile(conn, org)
        return {"entitlement_id": ent_id, "revoked": True, "evicted": result.evicted}


# ---------------------------------------------------------------- 时间推进


def advance_clock(conn: sqlite3.Connection, to_iso: str | None = None,
                  delta_seconds: float | None = None) -> dict:
    with db.transaction(conn):
        current = db.get_clock(conn)
        if to_iso is not None:
            target = normalize_ts(to_iso)
        elif delta_seconds is not None:
            target = normalize_ts(
                datetime.fromisoformat(current) + timedelta(seconds=delta_seconds))
        else:
            raise ServiceError("missing_target", "需要提供 to 或 delta_seconds", 422)
        if target < current:
            raise ServiceError("clock_cannot_go_back", "逻辑时钟不能回拨", 409)
        if target == current:
            return {"now": current, "advanced": False, "evicted": []}

        conn.execute("UPDATE clock SET now_iso=? WHERE id=1", (target,))
        seq = _emit(conn, "clock.advanced", None, {"from": current, "to": target})

        # 所有有活跃会话的组织都重新确定席位（权益到期/生效都可能改变容量）
        orgs = [r[0] for r in conn.execute(
            "SELECT DISTINCT org FROM sessions WHERE status='active'").fetchall()]
        all_evicted: list[dict] = []
        for org in orgs:
            all_evicted.extend(reconcile(conn, org).evicted)
        return {"now": target, "advanced": True, "evicted": all_evicted}


# ---------------------------------------------------------------- 追溯查询


def get_session_detail(conn: sqlite3.Connection, session_id: str) -> dict:
    sess = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if sess is None:
        raise ServiceError("session_not_found", "会话不存在", 404)
    slots = conn.execute(
        "SELECT * FROM slot_history WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall()
    admit = conn.execute(
        "SELECT * FROM admission_requests WHERE request_id=?",
        (sess["admit_request_id"],),
    ).fetchone()
    creds = conn.execute(
        "SELECT cred_id, generation, status, issued_seq, consumed_seq "
        "FROM credentials WHERE session_id=? ORDER BY generation", (session_id,)
    ).fetchall()
    return {
        "session_id": sess["session_id"],
        "org": sess["org"],
        "tier": sess["tier"],
        "status": sess["status"],
        "created_seq": sess["created_seq"],
        "ended_seq": sess["ended_seq"],
        "end_reason": sess["end_reason"],
        "admit_request_id": sess["admit_request_id"],
        "admit_first_status": admit["first_status"] if admit else None,
        "slot_history": [dict(s) for s in slots],
        "credentials": [dict(c) for c in creds],
    }


def list_org_sessions(conn: sqlite3.Connection, org: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sessions WHERE org=? ORDER BY created_seq", (org,)
    ).fetchall()
    out = []
    for r in rows:
        slot = _open_slot_for(conn, r["session_id"])
        item = dict(r)
        item["current_ent_id"] = slot["ent_id"] if slot else None
        out.append(item)
    return out


def list_entitlement_history(conn: sqlite3.Connection, ent_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM entitlements WHERE ent_id=? ORDER BY version", (ent_id,)
    ).fetchall()
    if not rows:
        raise ServiceError("entitlement_not_found", "权益不存在", 404)
    return [_dump_ent(r) for r in rows]


def list_events(conn: sqlite3.Connection, org: str | None = None, limit: int = 200) -> list[dict]:
    limit = max(1, min(limit, 1000))
    if org:
        rows = conn.execute(
            "SELECT * FROM events WHERE org=? ORDER BY seq DESC LIMIT ?", (org, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"seq": r["seq"], "kind": r["kind"], "org": r["org"], "at": r["at_iso"],
             "detail": json.loads(r["detail_json"])} for r in rows]
