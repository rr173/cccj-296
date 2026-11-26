"""中心侧「离线借席」：签发、交回、可行性检查与权益变更守卫。

核心承诺
--------
1. 离线包在整个借期 [issued_at, expire_at) 内都必须能兑现——可行性按区间
   断点逐点检查，允许不同权益在不同时段接替，不要求一份权益覆盖整个借期。
2. 同组织多个借期重叠的包合在一起检查。
3. 签发不能挤掉当前在线会话：除全区间可行性外，还要在当前时刻做一次带预留的
   安置试算，试算会撤销任何现有会话即拒绝。
4. 未交回的包是硬性预留：在线再平衡先扣预留再安置在线会话；高级预留只由高级
   权益支撑，标准预留可由标准或富余高级权益支撑。
5. 权益修改/撤销若使任一未交回包在剩余借期无法兑现，整笔拒绝；有其他权益能
   接替则允许（放行后再按原优先级再平衡在线会话）。
6. 交回凭据可重送，但每个包至多释放一次；包文件或普通关会话回执不是凭据。
所有写操作走 BEGIN IMMEDIATE，与准入/权益改动在同一把全库写锁上串行化。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta

from . import crypto, db
from .db import new_id, normalize_ts
from .service import (
    ServiceError,
    _active_entitlements,
    _active_sessions,
    _emit,
    _open_slot_for,
    _plan_placement,
)

PACKAGE_KIND = "offline-seat-package"
RETURN_KIND = "offline-return-credential"


class _Box:
    """下标访问的轻量行，用于把候选包/模拟权益送进纯函数检查。"""

    def __init__(self, mapping: dict):
        self._d = mapping

    def __getitem__(self, key):
        return self._d[key]


class _SimEnt:
    """试算用的权益行（与 sqlite3.Row 的键访问接口兼容）。"""

    def __init__(self, row: sqlite3.Row, **overrides):
        self._d = {k: row[k] for k in row.keys()}
        self._d.update(overrides)

    def __getitem__(self, key):
        return self._d[key]

    def keys(self):
        return self._d.keys()


# ---------------------------------------------------------------- 查询辅助


def _outstanding_packages(conn: sqlite3.Connection, org: str,
                          at: str) -> list[sqlite3.Row]:
    """未交回且窗口与 at 之后时间线有交集的包（at 时刻已到期的不再约束）。"""
    return conn.execute(
        "SELECT * FROM offline_packages WHERE org = ? AND status = 'active' "
        "AND expire_at > ? ORDER BY issued_at, package_id",
        (org, at),
    ).fetchall()


def _reservation_at(rows, t: str) -> tuple[int, int]:
    std = pre = 0
    for r in rows:
        if r["issued_at"] <= t < r["expire_at"]:
            std += r["standard_seats"]
            pre += r["premium_seats"]
    return std, pre


def _series_feasible(ents, packages, not_before: str | None = None,
                     sample_after: str | None = None) -> tuple[bool, str | None]:
    """逐断点检查：预留的高级席/总席在借期内每个时段都能兑现。

    高级席只能由高级权益支撑；标准席可由标准权益与富余高级权益共同支撑。
    权益与包窗口都是左闭右开，容量只在边界处变化，检查全部边界点即充分。
    只考察借期实际覆盖的时间范围 [lo, hi)：范围之外没有预留需求，
    权益在那里是否生效与离线承诺无关。
    not_before：裁剪掉该时刻之前的历史借期；
    sample_after：额外在「该时刻之后紧接着的瞬间」采样一次——权益变更在该
    时刻生效，变更前的容量由旧权益保证，变更后必须立刻仍能兑现预留。
    """
    from datetime import datetime

    starts = [r["issued_at"] for r in packages]
    ends = [r["expire_at"] for r in packages]
    lo = max(not_before, min(starts)) if not_before else min(starts)
    hi = max(ends)
    if lo >= hi:
        return True, None

    boundaries = {normalize_ts(t) for t in starts + ends}
    for e in ents:
        boundaries.add(e["effective_at"])
        boundaries.add(e["expire_at"])
    if sample_after is not None:
        eps = (datetime.fromisoformat(normalize_ts(sample_after))
               + timedelta(microseconds=1)).isoformat()
        boundaries.add(normalize_ts(eps))

    for t in sorted(boundaries):
        if t < lo or t >= hi:
            continue
        need_std, need_pre = _reservation_at(packages, t)
        # 该断点不落在任何包窗口内（包间空隙）：无预留需求，无需容量
        if need_std == 0 and need_pre == 0:
            continue
        active_ents = [e for e in ents
                       if e["effective_at"] <= t < e["expire_at"]]
        premium_cap = sum(e["seats"] for e in active_ents if e["tier"] == "premium")
        standard_cap = sum(e["seats"] for e in active_ents if e["tier"] == "standard")
        if need_pre > premium_cap:
            return False, "offline_premium_cannot_be_honored"
        if need_std + need_pre > premium_cap + standard_cap:
            return False, "offline_capacity_cannot_be_honored"
    return True, None


def _online_fit_plan(conn: sqlite3.Connection, org: str,
                     reserve_std: int, reserve_pre: int):
    """在当前时刻带预留做一次纯试算：返回安置计划（不写库）。"""
    at = db.get_clock(conn)
    ents = _active_entitlements(conn, org, at)
    sessions = _active_sessions(conn, org)
    current_ents: dict[str, str] = {}
    for s in sessions:
        slot = _open_slot_for(conn, s["session_id"])
        if slot is not None:
            current_ents[s["session_id"]] = slot["ent_id"]
    return _plan_placement(ents, sessions, current_ents, reserve_std, reserve_pre)


# ---------------------------------------------------------------- 权益变更守卫


def guard_entitlement_change(conn: sqlite3.Connection, org: str,
                             replace_row_id: int, sim) -> None:
    """权益修改/撤销落库前调用：离线承诺无法继续兑现则抛错，整笔事务回滚。

    replace_row_id 是被改/被撤权益当前 active 版本的 row_id；
    sim 为变更后的形态（_SimEnt），撤销传 None 表示该容量消失。
    只保护当前逻辑时间起的剩余借期；放行后在线会话按原优先级正常再平衡。
    """
    at = db.get_clock(conn)
    # 只保护当前仍在借期内的未交回包：已到期的预留已自动失效，不再构成约束
    packages = [p for p in _outstanding_packages(conn, org, at)
                if p["issued_at"] <= at < p["expire_at"]]
    if not packages:
        return

    rows = conn.execute(
        "SELECT * FROM entitlements WHERE org = ? AND status = 'active' ORDER BY row_id",
        (org,),
    ).fetchall()
    future_ents = []
    for e in rows:
        if e["row_id"] == replace_row_id:
            if sim is not None:
                future_ents.append(sim)
        else:
            future_ents.append(e)

    # 变更在当前时刻生效：要求变更之后的剩余借期仍可兑现
    ok, code = _series_feasible(future_ents, packages, not_before=at,
                                sample_after=at)
    if not ok:
        raise ServiceError(
            code,
            "该变更会使尚未交回的离线包在剩余借期内无法兑现，已整笔拒绝",
            409,
        )


# ---------------------------------------------------------------- 签发


def issue_package(conn: sqlite3.Connection, org: str, site_id: str,
                  standard_seats: int, premium_seats: int,
                  expire_at: str, request_id: str) -> dict:
    if standard_seats < 0 or premium_seats < 0:
        raise ServiceError("invalid_seats", "席位数不能为负", 422)
    if standard_seats + premium_seats == 0:
        raise ServiceError("invalid_seats", "离线包至少包含一个席位", 422)
    if not site_id or not request_id:
        raise ServiceError("invalid_param", "site_id 与 request_id 不能为空", 422)

    with db.transaction(conn):
        # 幂等：同组织同 request_id
        dup = conn.execute(
            "SELECT * FROM offline_requests WHERE org=? AND request_id=?",
            (org, request_id),
        ).fetchone()
        if dup is not None:
            if (dup["site_id"] != site_id
                    or dup["standard_seats"] != standard_seats
                    or dup["premium_seats"] != premium_seats
                    or dup["expire_at"] != normalize_ts(expire_at)):
                raise ServiceError(
                    "request_id_param_mismatch",
                    "同一 request_id 已用于不同的离线包参数",
                    409,
                )
            return json.loads(dup["response_json"])

        at = db.get_clock(conn)
        exp = normalize_ts(expire_at)
        if exp <= at:
            return _reject(conn, org, site_id, standard_seats, premium_seats,
                           exp, request_id, "expire_at_must_be_future")

        package_id = new_id("offpkg")

        # 候选包叠加到既有未交回包上，做整个借期的可行性检查
        existing = _outstanding_packages(conn, org, at)
        candidate = _Box({
            "issued_at": at, "expire_at": exp,
            "standard_seats": standard_seats, "premium_seats": premium_seats,
        })
        ents = conn.execute(
            "SELECT * FROM entitlements WHERE org=? AND status='active' ORDER BY row_id",
            (org,),
        ).fetchall()
        ok, code = _series_feasible(list(ents), existing + [candidate], not_before=at)
        if not ok:
            return _reject(conn, org, site_id, standard_seats, premium_seats,
                           exp, request_id, code)

        # 签发不能挤掉当前在线会话：当前时刻带新预留试算
        reserve_std, reserve_pre = _reservation_at(existing, at)
        plan = _online_fit_plan(conn, org,
                                reserve_std + standard_seats,
                                reserve_pre + premium_seats)
        if plan.evicted:
            return _reject(conn, org, site_id, standard_seats, premium_seats,
                           exp, request_id, "would_displace_online_session")

        # 通过：生成交回秘密与预签凭据；交回凭据嵌入签名包内随包自包含，
        # 站点只负责提取与重送，本身不具备为任何包签发凭据的能力。
        key = crypto.get_or_create_center_key(conn)
        return_secret = secrets.token_urlsafe(32)
        return_credential = crypto.sign(key, {
            "kind": RETURN_KIND,
            "package_id": package_id,
            "org": org,
            "site_id": site_id,
            "return_secret": return_secret,
        })
        envelope = crypto.sign(key, {
            "kind": PACKAGE_KIND,
            "package_id": package_id,
            "org": org,
            "site_id": site_id,
            "standard_seats": standard_seats,
            "premium_seats": premium_seats,
            "issued_at": at,
            "expire_at": exp,
            "request_id": request_id,
            "return_credential": return_credential,
        })

        seq = _emit(conn, "offline_package.issued", org, {
            "package_id": package_id, "site_id": site_id,
            "standard_seats": standard_seats, "premium_seats": premium_seats,
            "issued_at": at, "expire_at": exp, "request_id": request_id,
        })
        conn.execute(
            "INSERT INTO offline_packages(package_id, org, site_id, standard_seats, "
            "premium_seats, issued_at, expire_at, request_id, return_secret_hash, "
            "status, created_seq) VALUES (?,?,?,?,?,?,?,?,?, 'active', ?)",
            (package_id, org, site_id, standard_seats, premium_seats, at, exp,
             request_id, crypto.secret_hash(return_secret), seq),
        )
        payload = {
            "issued": True,
            "package_id": package_id,
            "org": org,
            "site_id": site_id,
            "standard_seats": standard_seats,
            "premium_seats": premium_seats,
            "issued_at": at,
            "expire_at": exp,
            "request_id": request_id,
            "envelope": envelope,
            "return_credential": return_credential,
        }
        conn.execute(
            "INSERT INTO offline_requests(request_id, org, site_id, standard_seats, "
            "premium_seats, expire_at, first_status, response_json, created_seq) "
            "VALUES (?,?,?,?,?,?,'issued',?,?)",
            (request_id, org, site_id, standard_seats, premium_seats, exp,
             json.dumps(payload, ensure_ascii=False), seq),
        )
        return payload


def _reject(conn, org, site_id, standard_seats, premium_seats, exp,
            request_id, code) -> dict:
    seq = _emit(conn, "offline_package.issue_rejected", org, {
        "request_id": request_id, "site_id": site_id, "reason": code,
    })
    payload = {
        "issued": False,
        "org": org,
        "site_id": site_id,
        "standard_seats": standard_seats,
        "premium_seats": premium_seats,
        "expire_at": exp,
        "request_id": request_id,
        "reason": code,
    }
    conn.execute(
        "INSERT INTO offline_requests(request_id, org, site_id, standard_seats, "
        "premium_seats, expire_at, first_status, reject_code, response_json, created_seq) "
        "VALUES (?,?,?,?,?,?,'rejected', ?, ?, ?)",
        (request_id, org, site_id, standard_seats, premium_seats, exp, code,
         json.dumps(payload, ensure_ascii=False), seq),
    )
    return payload


# ---------------------------------------------------------------- 交回


def return_package(conn: sqlite3.Connection, envelope: dict,
                   operation_id: str | None = None) -> dict:
    """验证交回凭据并提前释放包额度。凭据可重送，释放只发生一次。"""
    with db.transaction(conn):
        key = crypto.get_or_create_center_key(conn)
        try:
            payload = crypto.verify(key, envelope)
        except crypto.InvalidSignature as exc:
            raise ServiceError("invalid_return_credential", str(exc), 409)
        if payload.get("kind") != RETURN_KIND:
            raise ServiceError("invalid_return_credential",
                               "凭据类型不是交回凭据", 409)
        package_id = payload.get("package_id")
        row = conn.execute(
            "SELECT * FROM offline_packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("package_not_found", "离线包不存在", 404)
        if crypto.secret_hash(payload.get("return_secret", "")) != row["return_secret_hash"]:
            raise ServiceError("invalid_return_credential",
                               "交回秘密与中心留痕不一致", 409)

        # 重送：返回第一次处理结果，不重复释放
        prior = conn.execute(
            "SELECT * FROM offline_returns WHERE package_id=?", (package_id,)
        ).fetchone()
        if prior is not None:
            out = json.loads(prior["response_json"])
            out["replayed"] = True
            return out

        at = db.get_clock(conn)
        already_expired = at >= row["expire_at"]
        seq = _emit(conn, "offline_package.returned", row["org"], {
            "package_id": package_id, "site_id": row["site_id"],
            "released": not already_expired,
            "operation_id": operation_id,
        })
        conn.execute(
            "UPDATE offline_packages SET status='returned', returned_seq=?, returned_at=? "
            "WHERE package_id=?",
            (seq, at, package_id),
        )
        payload_out = {
            "package_id": package_id,
            "org": row["org"],
            "site_id": row["site_id"],
            "returned": True,
            "released": not already_expired,
            "note": "包已到期，额度此前已随到期自动失效，本次仅登记交回"
                    if already_expired else "额度已提前释放，可重新分配",
            "at": at,
            "replayed": False,
        }
        conn.execute(
            "INSERT INTO offline_returns(package_id, operation_id, response_json, seq) "
            "VALUES (?,?,?,?)",
            (package_id, operation_id,
             json.dumps(payload_out, ensure_ascii=False), seq),
        )

        # 释放后立即再平衡在线会话
        from . import service  # 局部导入避免循环依赖
        result = service.reconcile(conn, row["org"])
        payload_out["evicted"] = result.evicted
        return payload_out


# ---------------------------------------------------------------- 查询


def list_org_packages(conn: sqlite3.Connection, org: str) -> list[dict]:
    at = db.get_clock(conn)
    rows = conn.execute(
        "SELECT * FROM offline_packages WHERE org=? ORDER BY created_seq", (org,)
    ).fetchall()
    out = []
    for r in rows:
        item = {k: r[k] for k in r.keys() if k != "return_secret_hash"}
        if r["status"] == "active":
            item["derived_status"] = "expired" if at >= r["expire_at"] else "active"
        else:
            item["derived_status"] = "returned"
        out.append(item)
    return out
