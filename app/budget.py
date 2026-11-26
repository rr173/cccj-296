"""预付预算：按组织发放额度、对在线会话按逻辑时间扣费、在真实边界停用。

与席位完全分开：预算不接支付、不计离线站点用量，只控制中心在线会话。
组织第一笔额度发放即开启该组织的计费；未开启的组织沿用原有席位规则。

计费模型
--------
- 逻辑时钟每前进 1 微秒（µs），标准在线会话消耗 1 个最小额度单位，
  高级在线会话消耗 3 个。
- 扣费按「时间主序」：在同一微秒内，先按现有会话优先次序
  （高级先于标准 → 同类开始较早先于较晚 → 会话 ID 破同序）依次归属，
  再进入下一微秒。这样无论一次推进跨越多久、还是分成多段推进，
  只要中间没有其它操作，每笔用量归属到哪笔额度、合计多少都完全一致。
- 额度优先用最早到期的，同到期按发放先后；一笔用量可跨多笔额度扣。

边界处理
--------
推进时钟时按真实边界（额度到期、权益席位变化、余额耗尽）切段：在每段内
只对实际活跃的会话扣费，段末立即停用不再合格的会话，再进入下一段。
因此不会把整段推进拖到终点才一次性处理，也不会逐微秒循环模拟。

账本
----
budget_charges 每行是「会话 × 额度」在一个时间范围内的用量；grant_id 为
NULL 表示该段没有任何可用额度（欠费），用于完整对账。所有写入与时钟推进
在同一 SQLite 写事务内，重启或并发不会留下「时钟已推进但未记账」的半成品。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import db
from .db import new_id, next_seq, normalize_ts
from .service import (
    ServiceError,
    TIER_PRIORITY,
    TIER_UNITS,
    _active_sessions,
    _emit,
)

MAX_US_PER_DAY = 86_400_000_000


# ---------------------------------------------------------------- 时间工具


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def us_between(lo: str, hi: str) -> int:
    """精确的微秒差（不用浮点，避免长时间跨度丢精度）。"""
    d = _dt(hi) - _dt(lo)
    return d.days * MAX_US_PER_DAY + d.seconds * 1_000_000 + d.microseconds


def add_us(ts: str, n: int) -> str:
    return normalize_ts(_dt(ts) + timedelta(microseconds=n))


# ---------------------------------------------------------------- 计费状态


def billing_since(conn: sqlite3.Connection, org: str) -> str | None:
    row = conn.execute(
        "SELECT billable_since FROM billing_orgs WHERE org=?", (org,)
    ).fetchone()
    return row["billable_since"] if row else None


def _used_by_grant(conn: sqlite3.Connection, org: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT grant_id, COALESCE(SUM(units), 0) AS used FROM budget_charges "
        "WHERE org=? AND grant_id IS NOT NULL GROUP BY grant_id",
        (org,),
    ).fetchall()
    return {r["grant_id"]: int(r["used"]) for r in rows}


def balance_at(conn: sqlite3.Connection, org: str, at: str) -> int:
    """at 时刻仍可用的额度单位：仅统计 [issued_at, expire_at) 覆盖 at 的额度。"""
    rows = conn.execute(
        "SELECT grant_id, amount FROM budget_grants "
        "WHERE org=? AND issued_at <= ? AND expire_at > ?",
        (org, at, at),
    ).fetchall()
    used = _used_by_grant(conn, org)
    return sum(int(r["amount"]) - used.get(r["grant_id"], 0) for r in rows)


def _lots(conn: sqlite3.Connection, org: str, at: str) -> list[dict]:
    """按扣费顺序（最早到期 → 发放先后 → ID）返回 at 时刻可用的额度。"""
    rows = conn.execute(
        "SELECT grant_id, amount, expire_at, issued_at, created_seq "
        "FROM budget_grants WHERE org=? AND issued_at <= ? AND expire_at > ? "
        "ORDER BY expire_at, created_seq, grant_id",
        (org, at, at),
    ).fetchall()
    used = _used_by_grant(conn, org)
    return [
        {
            "grant_id": r["grant_id"],
            "amount": int(r["amount"]),
            "expire_at": r["expire_at"],
            "remaining": int(r["amount"]) - used.get(r["grant_id"], 0),
        }
        for r in rows
    ]


def billing_orgs_with_active_sessions(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT s.org FROM sessions s JOIN billing_orgs b ON b.org = s.org "
        "WHERE s.status = 'active' ORDER BY s.org"
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- 周期归属计算


def _pref(n: int, lo: int, hi: int, period: int) -> int:
    full, rem = divmod(n, period)
    return full * (hi - lo) + max(0, min(hi, rem) - lo)


def _count_resid(p: int, q: int, lo: int, hi: int, period: int) -> int:
    """[p,q) 中满足 k % period ∈ [lo,hi) 的整数个数。"""
    if q <= p or hi <= lo:
        return 0
    return _pref(q, lo, hi, period) - _pref(p, lo, hi, period)


def _first_resid(p: int, q: int, lo: int, hi: int, period: int) -> int | None:
    r = p % period
    if lo <= r < hi:
        return p
    k = p + (lo - r) % period
    return k if k < q else None


def _last_resid(p: int, q: int, lo: int, hi: int, period: int) -> int | None:
    qq = q - 1
    r = qq % period
    if lo <= r < hi:
        return qq
    k = qq - (r - (hi - 1)) % period
    return k if k >= p else None


# ---------------------------------------------------------------- 扣费


def settle_span(conn: sqlite3.Connection, org: str, a: str, b: str) -> list[dict]:
    """对 [a,b) 内实际活跃的会话按时间主序扣费；假定区间内无额度到期。

    先把每微秒内按优先顺序交错归属到各会话（标准 1、高级 3），再沿额度
    到期顺序切片：每笔额度覆盖从头数起的若干微秒，一笔用量可跨多笔额度。
    用整除与余数区间计数一次算出，绝不逐微秒模拟。返回本段扣费明细。
    """
    if b <= a:
        return []
    sessions = _active_sessions(conn, org)
    if not sessions:
        return []
    since = billing_since(conn, org)
    if since is None or since >= b:
        return []
    a = max(a, since)
    if b <= a:
        return []

    ordered = sorted(
        sessions,
        key=lambda s: (TIER_PRIORITY[s["tier"]], s["created_seq"], s["session_id"]),
    )
    rates = [TIER_UNITS[s["tier"]] for s in ordered]
    period = sum(rates)
    if period == 0:  # pragma: no cover - 活跃会话必有正费率
        return []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for r in rates:
        offsets.append((cursor, cursor + r))
        cursor += r

    dur = us_between(a, b)
    total_units = period * dur
    lots = _lots(conn, org, a)
    seq = next_seq(conn)

    rows: list[dict] = []

    def emit_rows(p: int, q: int, grant_id: str | None) -> None:
        for i, s in enumerate(ordered):
            lo, hi = offsets[i]
            n = _count_resid(p, q, lo, hi, period)
            if n <= 0:
                continue
            kf = _first_resid(p, q, lo, hi, period)
            kl = _last_resid(p, q, lo, hi, period)
            start_at = add_us(a, kf // period)
            end_at = add_us(a, kl // period + 1)
            rows.append({
                "session_id": s["session_id"],
                "tier": s["tier"],
                "grant_id": grant_id,
                "start_at": start_at,
                "end_at": end_at,
                "units": n,
            })

    p = 0
    for lot in lots:
        if p >= total_units:
            break
        cap = lot["remaining"]
        if cap <= 0:
            continue
        q = min(p + cap, total_units)
        if q <= p:
            continue
        emit_rows(p, q, lot["grant_id"])
        p = q

    if p < total_units:
        # 防御：正常情况下在真实边界处已停用，不会出现欠费段
        emit_rows(p, total_units, None)

    for row in rows:
        conn.execute(
            "INSERT INTO budget_charges(org, session_id, tier, grant_id, start_at, "
            "end_at, units, seq) VALUES (?,?,?,?,?,?,?,?)",
            (org, row["session_id"], row["tier"], row["grant_id"],
             row["start_at"], row["end_at"], row["units"], seq),
        )

    if rows:
        by_grant: dict[str | None, int] = {}
        by_session: dict[str, int] = {}
        for row in rows:
            by_grant[row["grant_id"]] = by_grant.get(row["grant_id"], 0) + row["units"]
            by_session[row["session_id"]] = by_session.get(row["session_id"], 0) + row["units"]
        _emit(conn, "budget.charged", org, {
            "from": a, "to": b,
            "units": sum(r["units"] for r in rows),
            "by_session": by_session,
            "by_grant": {k: v for k, v in by_grant.items() if k is not None},
            "unfunded_units": by_grant.get(None, 0),
        }, seq=seq)
    return rows


# ---------------------------------------------------------------- 推进时钟切段


def boundary_times(conn: sqlite3.Connection, org: str, after: str, before: str) -> set[str]:
    """(after, before) 内可能改变资格的真实边界（额度到期、权益/离线包窗口）。"""
    ts: set[str] = set()

    def add(v):
        if v is not None and after < v < before:
            ts.add(v)

    for r in conn.execute(
            "SELECT expire_at FROM budget_grants WHERE org=? AND expire_at > ? "
            "AND expire_at < ?", (org, after, before)).fetchall():
        add(r["expire_at"])
    for r in conn.execute(
            "SELECT effective_at, expire_at FROM entitlements "
            "WHERE org=? AND status='active'", (org,)).fetchall():
        add(r["effective_at"])
        add(r["expire_at"])
    for r in conn.execute(
            "SELECT issued_at, expire_at FROM offline_packages "
            "WHERE org=? AND status='active'", (org,)).fetchall():
        add(r["issued_at"])
        add(r["expire_at"])
    return ts


def exhaustion_time(conn: sqlite3.Connection, org: str, at: str) -> int:
    """当前活跃集合从 at 起还能付得起多少个完整微秒（余额只减不增时有效）。

    仅在「下一次扣费之前没有额度到期」的段内调用：余额线性下降，首次付不起的
    时刻可由整除一次算出（除数集合固定，周期扣费总额恒定），不需要逐微秒循环。
    """
    since = billing_since(conn, org)
    if since is None or since > at:
        return 0
    sessions = _active_sessions(conn, org)
    cost = sum(TIER_UNITS[s["tier"]] for s in sessions)
    if cost == 0:
        return 0
    return balance_at(conn, org, at) // cost


# ---------------------------------------------------------------- 发放


def _reject(conn, org, amount, exp, request_id, code) -> dict:
    seq = _emit(conn, "budget.grant_rejected", org, {
        "request_id": request_id, "amount": amount, "expire_at": exp, "reason": code})
    payload = {
        "granted": False,
        "org": org,
        "amount": amount,
        "expire_at": exp,
        "request_id": request_id,
        "reason": code,
    }
    conn.execute(
        "INSERT INTO budget_requests(request_id, org, amount, expire_at, first_status, "
        "reject_code, response_json, created_seq) "
        "VALUES (?,?,?,?,'rejected', ?, ?, ?)",
        (request_id, org, amount, exp, code,
         _json(payload), seq),
    )
    return payload


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def grant(conn: sqlite3.Connection, org: str, amount: int, expire_at: str,
          request_id: str) -> dict:
    """运营发放一笔正整数额度；同组织同 request_id 幂等。"""
    from . import service  # 局部导入避免模块加载期循环

    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ServiceError("invalid_amount", "额度必须是正整数", 422)
    if not org or not request_id:
        raise ServiceError("invalid_param", "org 与 request_id 不能为空", 422)
    exp = normalize_ts(expire_at)

    with db.transaction(conn):
        dup = conn.execute(
            "SELECT * FROM budget_requests WHERE org=? AND request_id=?",
            (org, request_id),
        ).fetchone()
        if dup is not None:
            if dup["amount"] != amount or dup["expire_at"] != exp:
                raise ServiceError(
                    "request_id_param_mismatch",
                    "同一 request_id 已用于不同的额度金额或到期时间",
                    409,
                )
            return json.loads(dup["response_json"])

        at = db.get_clock(conn)
        if exp <= at:
            return _reject(conn, org, amount, exp, request_id,
                           "expire_at_must_be_future")

        grant_id = new_id("grant")
        first = billing_since(conn, org) is None
        seq = _emit(conn, "budget.granted", org, {
            "grant_id": grant_id, "amount": amount, "issued_at": at,
            "expire_at": exp, "request_id": request_id,
            "billing_enabled": first,
        })
        conn.execute(
            "INSERT INTO budget_grants(grant_id, org, amount, issued_at, expire_at, "
            "request_id, created_seq) VALUES (?,?,?,?,?,?,?)",
            (grant_id, org, amount, at, exp, request_id, seq),
        )
        if first:
            conn.execute(
                "INSERT INTO billing_orgs(org, billable_since, enabled_seq) "
                "VALUES (?,?,?)", (org, at, seq))
            _emit(conn, "billing.enabled", org, {
                "org": org, "billable_since": at, "first_grant_id": grant_id})
            # 第一笔额度开启计费：已有在线会话只从此刻起计费，并立即按预算再平衡
            result = service.reconcile(conn, org)
        else:
            result = service.ReconcileResult(retained=[], evicted=[], assignments={})

        payload = {
            "granted": True,
            "grant_id": grant_id,
            "org": org,
            "amount": amount,
            "issued_at": at,
            "expire_at": exp,
            "request_id": request_id,
            "billing_enabled_since": billing_since(conn, org),
            "balance_after": balance_at(conn, org, at),
            "evicted": result.evicted,
        }
        conn.execute(
            "INSERT INTO budget_requests(request_id, org, amount, expire_at, "
            "first_status, response_json, created_seq) "
            "VALUES (?,?,?,?,'granted', ?, ?)",
            (request_id, org, amount, exp, _json(payload), seq),
        )
        return payload


# ---------------------------------------------------------------- 查询


def list_grants(conn: sqlite3.Connection, org: str) -> list[dict]:
    at = db.get_clock(conn)
    used = _used_by_grant(conn, org)
    rows = conn.execute(
        "SELECT * FROM budget_grants WHERE org=? ORDER BY issued_at, created_seq, grant_id",
        (org,),
    ).fetchall()
    out = []
    for r in rows:
        u = used.get(r["grant_id"], 0)
        left = max(0, int(r["amount"]) - u)
        expired = at >= r["expire_at"]
        out.append({
            "grant_id": r["grant_id"],
            "org": r["org"],
            "amount": int(r["amount"]),
            "used": u,
            "available": 0 if expired else left,
            "expired_unused": left if expired else 0,
            "issued_at": r["issued_at"],
            "expire_at": r["expire_at"],
            "request_id": r["request_id"],
            "created_seq": r["created_seq"],
            "derived_status": "expired" if expired else "active",
        })
    return out


def summary(conn: sqlite3.Connection, org: str) -> dict:
    at = db.get_clock(conn)
    grants = list_grants(conn, org)
    since = billing_since(conn, org)
    totals = {
        "amount": sum(g["amount"] for g in grants),
        "used": sum(g["used"] for g in grants),
        "available": sum(g["available"] for g in grants),
        "expired_unused": sum(g["expired_unused"] for g in grants),
    }
    return {
        "org": org,
        "now": at,
        "billing_enabled": since is not None,
        "billable_since": since,
        "unit_rate_per_microsecond": dict(TIER_UNITS),
        "totals": totals,
        "grants": grants,
    }


def usage(conn: sqlite3.Connection, org: str, session_id: str | None = None) -> list[dict]:
    sql = (
        "SELECT c.id, c.session_id, c.tier, c.grant_id, c.start_at, c.end_at, c.units, "
        "c.seq, s.status AS session_status "
        "FROM budget_charges c LEFT JOIN sessions s ON s.session_id = c.session_id "
        "WHERE c.org=?"
    )
    params: list = [org]
    if session_id is not None:
        sql += " AND c.session_id=?"
        params.append(session_id)
    sql += " ORDER BY c.start_at, c.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def session_usage(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT grant_id, start_at, end_at, units FROM budget_charges "
        "WHERE session_id=? ORDER BY start_at, id", (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def usage_totals(conn: sqlite3.Connection, org: str) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(units), 0) AS units, "
        "COALESCE(SUM(CASE WHEN grant_id IS NULL THEN units ELSE 0 END), 0) AS unfunded "
        "FROM budget_charges WHERE org=?", (org,),
    ).fetchone()
    return {"units": int(row["units"]), "unfunded_units": int(row["unfunded"])}
