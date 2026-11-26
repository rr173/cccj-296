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

# 预付预算：每逻辑微秒消耗的最小额度单位（标准 1、高级 3）
TIER_UNITS = {"standard": 1, "premium": 3}


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


# ---------------------------------------------------------------- 离线预留查询


def reservation_totals(conn: sqlite3.Connection, org: str, at: str) -> tuple[int, int]:
    """未交回且在 at 时刻有效的全部离线包合计预留（同组织多包合在一起）。

    左闭右开：issued_at <= at < expire_at 且 status='active'。
    这些席位在整个借期内都不能再分配给在线会话或其他站点。
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(standard_seats), 0), COALESCE(SUM(premium_seats), 0) "
        "FROM offline_packages WHERE org = ? AND status = 'active' "
        "AND issued_at <= ? AND expire_at > ?",
        (org, at, at),
    ).fetchone()
    return row[0], row[1]


# ---------------------------------------------------------------- 再平衡引擎


@dataclass
class ReconcileResult:
    retained: list[str]          # 仍是 active 的会话 ID
    evicted: list[dict]          # 本次撤销的会话及原因
    assignments: dict[str, str]  # 会话 ID -> 占用的 ent_id


@dataclass
class PlacementPlan:
    retained: list[str]          # 可保持 active 的会话 ID
    evicted: list[dict]          # 需撤销的会话及原因
    assignments: dict[str, str]  # 会话 ID -> 占用的 ent_id


def _plan_placement(ents: list[sqlite3.Row], sessions: list[sqlite3.Row],
                    current_ents: dict[str, str],
                    reserve_standard: int, reserve_premium: int,
                    billing_balance: int | None = None) -> PlacementPlan:
    """纯函数：给定权益、会话与离线预留，算出安置结果，不做任何写库。

    离线承诺优先：先把预留席位作为「幻影占用」放进池里（高级预留只进高级池，
    标准预留先占标准池、再溢出到高级池），再按既有优先级贪心安置在线会话。
    可行性检查保证幻影一定放得下；放不下时钳制为池容量（绝不超发）。
    由于保留/撤销只取决于各类池的总余量，幻影在权益间如何分布不影响在线
    会话集合——离线席位不会挡住本可同时容纳的在线会话。

    billing_balance 不为 None 表示该组织已开启计费：逐个会话在分到席位后，
    还要检查「已选会话接下来 1 微秒的总费用」不超过当前可用额度；付不起的
    会话撤销并归还刚占的席位、不计入已用预算，继续考虑后面的会话。
    """
    premium_pools: dict[str, list[Any]] = {
        e["ent_id"]: [e["seats"], 0] for e in ents if e["tier"] == "premium"
    }
    standard_pools: dict[str, list[Any]] = {
        e["ent_id"]: [e["seats"], 0] for e in ents if e["tier"] == "standard"
    }

    total_premium = sum(p[0] for p in premium_pools.values())
    total_standard = sum(p[0] for p in standard_pools.values())
    r_premium = min(reserve_premium, total_premium)
    r_standard = min(reserve_standard, total_standard + max(0, total_premium - r_premium))

    def phantom_fill(pools: dict[str, list[Any]], amount: int) -> None:
        for ent_id in sorted(pools):
            cap, used = pools[ent_id]
            take = min(amount, cap - used)
            if take:
                pools[ent_id][1] = used + take
                amount -= take
            if amount == 0:
                break

    # 高级预留只能由高级权益支撑
    phantom_fill(premium_pools, r_premium)
    # 标准预留：先标准池，富余后再进高级池
    phantom_fill(standard_pools, r_standard)
    phantom_fill(premium_pools, max(0, r_standard - sum(
        min(r_standard, p[0]) for p in standard_pools.values())))

    ordered = sorted(
        sessions,
        key=lambda s: (TIER_PRIORITY[s["tier"]], s["created_seq"], s["session_id"]),
    )

    retained: list[str] = []
    evicted: list[dict] = []
    assignments: dict[str, str] = {}
    committed_cost = 0

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

        if chosen is not None and billing_balance is not None:
            need = TIER_UNITS[s["tier"]]
            if committed_cost + need > billing_balance:
                if chosen in premium_pools and premium_pools[chosen][1] > 0:
                    premium_pools[chosen][1] -= 1
                else:
                    standard_pools[chosen][1] -= 1
                chosen = None
                reason = "budget_exhausted"

        if chosen is None:
            evicted.append(
                {
                    "session_id": s["session_id"],
                    "tier": s["tier"],
                    "reason": reason,
                    "previous_ent_id": current_ents.get(s["session_id"]),
                }
            )
            continue
        if billing_balance is not None:
            committed_cost += TIER_UNITS[s["tier"]]
        retained.append(s["session_id"])
        assignments[s["session_id"]] = chosen

    return PlacementPlan(retained=retained, evicted=evicted, assignments=assignments)


def reconcile(conn: sqlite3.Connection, org: str) -> ReconcileResult:
    """按优先顺序重新确定组织内的活跃会话集合（先离线承诺、后在线会话）。

    在调用方已经打开的写事务内执行。每次撤销各取一个事件 seq；
    席位迁移只落 slot_history（其 assigned/released seq 同样可追溯）。
    已开启计费的组织还要逐会话验证「接下来 1 微秒付得起」，付不起的以
    budget_exhausted 终态撤销（见 _plan_placement 的 billing_balance 参数）。
    """
    from . import budget

    at = db.get_clock(conn)
    ents = _active_entitlements(conn, org, at)
    sessions = _active_sessions(conn, org)
    reserve_standard, reserve_premium = reservation_totals(conn, org, at)

    # 保留会话现有的实际占用（每个活跃会话至多一条未释放记录）
    current_ents: dict[str, str] = {}
    for s in sessions:
        slot = _open_slot_for(conn, s["session_id"])
        if slot is not None:
            current_ents[s["session_id"]] = slot["ent_id"]

    # 已开启计费的组织：预算余额参与安置（付不起的会话不占席位也不占预算）
    billable_since = budget.billing_since(conn, org)
    balance = budget.balance_at(conn, org, at) \
        if billable_since is not None and billable_since <= at else None
    plan = _plan_placement(ents, sessions, current_ents,
                           reserve_standard, reserve_premium, billing_balance=balance)
    evicted_ids = {e["session_id"] for e in plan.evicted}

    for ev in plan.evicted:
        seq = next_seq(conn)
        old = ev["previous_ent_id"]
        ev["at"] = at
        conn.execute(
            "UPDATE sessions SET status='revoked', ended_seq=?, ended_at=?, "
            "end_reason=?, current_cred_id=NULL WHERE session_id=?",
            (seq, at, ev["reason"], ev["session_id"]),
        )
        if old is not None:
            conn.execute(
                "UPDATE slot_history SET released_seq=?, release_reason=? "
                "WHERE session_id=? AND released_seq IS NULL",
                (seq, ev["reason"], ev["session_id"]),
            )
        _emit(
            conn,
            "session.revoked",
            org,
            {
                "session_id": ev["session_id"],
                "tier": ev["tier"],
                "reason": ev["reason"],
                "previous_ent_id": old,
                "at": at,
            },
            seq=seq,
        )

    for s in sessions:
        if s["session_id"] in evicted_ids:
            continue
        chosen = plan.assignments[s["session_id"]]
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

    return ReconcileResult(retained=plan.retained, evicted=plan.evicted,
                           assignments=plan.assignments)


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
            "UPDATE sessions SET status='closed', ended_seq=?, ended_at=?, "
            "end_reason='closed_by_user', current_cred_id=NULL WHERE session_id=?",
            (seq, db.get_clock(conn), session_id),
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
        # 离线承诺守卫：变更后若任一未交回包在剩余借期无法兑现，整笔拒绝
        from .offline import _SimEnt, guard_entitlement_change
        sim = _SimEnt(row, seats=new_seats, effective_at=new_eff, expire_at=new_exp,
                      version=row["version"] + 1)
        guard_entitlement_change(conn, org, row["row_id"], sim)

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
        # 离线承诺守卫：撤销会使未交回包无法兑现时整笔拒绝
        from .offline import guard_entitlement_change
        guard_entitlement_change(conn, org, row["row_id"], None)
        seq = _emit(conn, "entitlement.revoked", org,
                    {"ent_id": ent_id, "reason": reason, "version": row["version"]})
        conn.execute(
            "UPDATE entitlements SET status='revoked', ended_seq=?, revoke_reason=? WHERE row_id=?",
            (seq, reason, row["row_id"]),
        )
        result = reconcile(conn, org)
        return {"entitlement_id": ent_id, "revoked": True, "evicted": result.evicted}


# ---------------------------------------------------------------- 时间推进


def _capacity_signature(conn: sqlite3.Connection, org: str, at: str) -> tuple:
    """at 时刻席位容量指纹：有效权益集合 + 离线预留。变化点即真实边界。"""
    ents = _active_entitlements(conn, org, at)
    ent_sig = tuple(sorted((e["ent_id"], e["tier"], e["seats"]) for e in ents))
    return (ent_sig, reservation_totals(conn, org, at))


def _settle_boundaries(conn: sqlite3.Connection, org: str, lo: str, hi: str) -> list[dict]:
    """在真实边界 hi 处停用不再合格的会话（预算 + 席位）。

    本段内只对活跃时段扣费；到达边界后重新执行与准入完全一致的贪心规则：
    逐个会话先看离线承诺之外的席位，再看已选集合接下来 1 微秒的费用是否
    付得起。因此席位收缩、额度到期或余额耗尽都会在各自真实时刻停用会话，
    不会拖到推进的终点；已撤销会话不在候选集合内，补发额度也不会复活它。
    结束时间取该边界时刻本身（停用发生在边界，不晚于它）。
    """
    from . import budget

    sessions = _active_sessions(conn, org)
    if not sessions:
        return []
    sig_changed = _capacity_signature(conn, org, lo) != _capacity_signature(conn, org, hi)
    balance = budget.balance_at(conn, org, hi) \
        if budget.billing_since(conn, org) is not None else None
    cost = sum(TIER_UNITS[s["tier"]] for s in sessions)
    if not sig_changed and (balance is None or cost <= balance):
        return []
    # 结算发生在边界时刻 hi：先把逻辑时钟置于该点，再按规则停用
    boundary = db.get_clock(conn)
    conn.execute("UPDATE clock SET now_iso=? WHERE id=1", (hi,))
    try:
        return reconcile(conn, org).evicted
    finally:
        conn.execute("UPDATE clock SET now_iso=? WHERE id=1", (boundary,))


def advance_clock(conn: sqlite3.Connection, to_iso: str | None = None,
                  delta_seconds: float | None = None) -> dict:
    from . import budget

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
            return {"now": current, "advanced": False, "evicted": [],
                    "charged_units": 0, "segments": 0}

        billable_orgs = set(budget.billing_orgs_with_active_sessions(conn))
        # 有活跃会话的组织都要在真实边界（席位/额度/余额）处停用不合格会话
        active_orgs = [r[0] for r in conn.execute(
            "SELECT DISTINCT org FROM sessions WHERE status='active' ORDER BY org"
        ).fetchall()]
        all_evicted: list[dict] = []
        total_charged = 0
        segments = 0
        t = current
        while t < target:
            stops: set[str] = {target}
            for org in active_orgs:
                stops |= budget.boundary_times(conn, org, t, target)
                if org in billable_orgs:
                    k = budget.exhaustion_time(conn, org, t)
                    if k > 0:
                        stops.add(budget.add_us(t, k))
            stops = {s for s in stops if t < s <= target}
            nxt = min(stops)

            # 起点已付不起哪怕 1 微秒的计费会话先停用，避免产生任何欠费段
            for org in billable_orgs:
                if budget.exhaustion_time(conn, org, t) == 0:
                    all_evicted.extend(reconcile(conn, org).evicted)

            # 只对实际活跃的时段扣费（左闭右开 [t, nxt)）
            for org in billable_orgs:
                total_charged += sum(
                    r["units"] for r in budget.settle_span(conn, org, t, nxt))
            conn.execute("UPDATE clock SET now_iso=? WHERE id=1", (nxt,))
            segments += 1

            # 在真实边界停用不再合格的会话（不拖到推进的终点）
            for org in active_orgs:
                all_evicted.extend(_settle_boundaries(conn, org, t, nxt))
            t = nxt

        seq = _emit(conn, "clock.advanced", None,
                    {"from": current, "to": target, "segments": segments,
                     "charged_units": total_charged})
        return {"now": target, "advanced": True, "evicted": all_evicted,
                "charged_units": total_charged, "segments": segments, "seq": seq}


# ---------------------------------------------------------------- 追溯查询


def get_session_detail(conn: sqlite3.Connection, session_id: str) -> dict:
    from . import budget

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
    usage = budget.session_usage(conn, session_id)
    return {
        "session_id": sess["session_id"],
        "org": sess["org"],
        "tier": sess["tier"],
        "status": sess["status"],
        "created_seq": sess["created_seq"],
        "ended_seq": sess["ended_seq"],
        "ended_at": sess["ended_at"],
        "end_reason": sess["end_reason"],
        "admit_request_id": sess["admit_request_id"],
        "admit_first_status": admit["first_status"] if admit else None,
        "slot_history": [dict(s) for s in slots],
        "credentials": [dict(c) for c in creds],
        "budget_usage": usage,
        "budget_units": sum(u["units"] for u in usage),
    }


def list_org_sessions(conn: sqlite3.Connection, org: str) -> list[dict]:
    from . import budget

    rows = conn.execute(
        "SELECT * FROM sessions WHERE org=? ORDER BY created_seq", (org,)
    ).fetchall()
    out = []
    for r in rows:
        slot = _open_slot_for(conn, r["session_id"])
        item = dict(r)
        item["current_ent_id"] = slot["ent_id"] if slot else None
        item["budget_units"] = sum(
            u["units"] for u in budget.session_usage(conn, r["session_id"]))
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
