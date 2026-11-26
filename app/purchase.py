"""组织购权：下单、对账推进、承诺交付、取消、发放权益。

核心不变式
----------
1. 两库分离：订单/权益在本服务库；资金状态只由独立收款方裁决。每次收款调用都
   先落「要领用的意图」，再在**另一个事务**里根据收款方结果推进状态。绝不同事务
   提交两边；任何调用结果（含超时/响应丢失）都逐条留痕。
2. 承诺交付只在收款方确认「已足额扣款」或「存在足额冻结、资金一直可扣」之后。
3. 承诺不可取消。取消与承诺在同一个写锁事务里判定，只有一方能成功。
4. 权益只在**实际扣款确认**（captured 且金额/币种与套餐一致）后发放一次；
   ent_effective_at/ent_expire_at 在发放事务里由逻辑时钟一次性锁定，之后重试、
   恢复、重复对账都不会重算。
5. 一旦接受取消，即使随后查到扣款成功也绝不发权益：必须关闭未扣款交易或原额
   退款，且只有收款方确认后才把订单标为已结清；不确定就保持 cancel_pending。
6. 收款方卡住/不可查询只影响本订单的推进；所有 HTTP 调用都在 SQLite 事务之外，
   不会阻塞同库中无关组织的会话准入。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from . import db
from .db import new_id, next_seq, normalize_ts
from .payment_client import PaymentClient, PaymentRejected, PaymentUnavailable
from .service import ServiceError, create_entitlement_locked

STAGE_PROCESSING = "processing"   # 仍在处理（收款结果未知 / 待承诺）
STAGE_PROMISED = "promised"       # 已承诺交付（确认足额扣款或足额冻结保障）
STAGE_DELIVERED = "delivered"     # 已发放权益（扣款已确认）
STAGE_CANCELED = "canceled"       # 已接受取消且确认无款终结（未发权益）
STAGE_FAILED = "failed"           # 支付明确拒绝，且未接受取消（未发权益）

CANCEL_UNSETTLED = ("none", "pending")


@dataclass(slots=True)
class OrderContext:
    conn: sqlite3.Connection
    payment: PaymentClient


# ---------------------------------------------------------------- 套餐


def create_package(conn: sqlite3.Connection, code: str, price_cents: int, currency: str,
                   tier: str, seats: int, duration_seconds: int,
                   charge_mode: str = "direct") -> dict:
    if tier not in ("standard", "premium"):
        raise ServiceError("invalid_tier", "tier 必须是 standard 或 premium", 422)
    if price_cents <= 0:
        raise ServiceError("invalid_price", "价格必须为正", 422)
    if seats < 0 or duration_seconds <= 0:
        raise ServiceError("invalid_package", "席位/时长非法", 422)
    if charge_mode not in ("direct", "freeze"):
        raise ServiceError("invalid_charge_mode", "charge_mode 必须是 direct 或 freeze", 422)
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM packages WHERE code=?", (code,)).fetchone():
            raise ServiceError("package_exists", "套餐已存在", 409)
        seq = next_seq(conn)
        conn.execute(
            "INSERT INTO packages(code, price_cents, currency, tier, seats, duration_seconds, "
            "charge_mode, created_seq) VALUES (?,?,?,?,?,?,?,?)",
            (code, price_cents, currency, tier, seats, duration_seconds, charge_mode, seq),
        )
    return {"package": dict(conn.execute(
        "SELECT * FROM packages WHERE code=?", (code,)).fetchone())}


def list_packages(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM packages ORDER BY created_seq")]


# ---------------------------------------------------------------- 小工具


def _read(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM purchase_orders WHERE order_id=?", (order_id,)).fetchone()


def _emit(conn: sqlite3.Connection, order: sqlite3.Row, kind: str, detail: dict, *,
          seq: int | None = None) -> int:
    if seq is None:
        seq = next_seq(conn)
    conn.execute(
        "INSERT INTO events(seq, kind, org, at_iso, detail_json) VALUES (?,?,?,?,?)",
        (seq, kind, order["org"], db.get_clock(conn),
         json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )
    return seq


def _touch(conn: sqlite3.Connection, order_id: str, seq: int, **changes: Any) -> None:
    if changes:
        keys = ", ".join(f"{k}=?" for k in changes)
        conn.execute(
            f"UPDATE purchase_orders SET {keys}, updated_seq=? WHERE order_id=?",
            (*changes.values(), seq, order_id),
        )
    else:
        conn.execute(
            "UPDATE purchase_orders SET updated_seq=? WHERE order_id=?", (seq, order_id))


def _record(conn: sqlite3.Connection, order: sqlite3.Row, op_type: str, outcome: str,
            detail: dict, seq: int, charge_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO purchase_money_ops(order_id, op_type, merchant_order_id, charge_id, "
        "outcome, detail_json, seq, at_iso) VALUES (?,?,?,?,?,?,?,?)",
        (order["order_id"], op_type, order["merchant_order_id"], charge_id, outcome,
         json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str), seq,
         db.get_clock(conn)),
    )


def _call(ctx: OrderContext, fn: Callable[[], dict | None]) -> tuple[str, dict]:
    """调用收款方：返回 (ok|rejected|unknown, payload)。超时绝不是失败或成功。"""
    try:
        result = fn()
        return "ok", result or {}
    except PaymentRejected as exc:
        return "rejected", {"error": exc.code, **(exc.detail or {})}
    except PaymentUnavailable as exc:
        return "unknown", {"error": exc.reason}


def _map_state(stub_state: str | None) -> str:
    return {"frozen": "frozen", "captured": "captured",
            "voided": "voided", "refunded": "refunded"}.get(stub_state or "", "unknown")


SETTLED_MONEY = ("voided", "refunded")


def _charge_fields(charge: dict, order: sqlite3.Row | None = None) -> dict:
    """把收款方观察到的交易事实写成订单列（金额/币种一并记录，供承诺时核对）。

    已本地终结的订单（取消已结清）不会被一个迟到的创建/查询响应改写成「又扣款了」：
    这类订单的资金状态只以结清事实为准。
    """
    state = _map_state(charge.get("state"))
    out: dict[str, Any] = {}
    if order is not None and (
            (order["status"] == "canceled" and _cancel_settled(order))
            or order["charge_state"] in SETTLED_MONEY):
        # 只更新金额/币种等核对信息，不改终态资金状态
        out["charge_state"] = order["charge_state"]
    else:
        out["charge_state"] = state
    if charge.get("charge_id"):
        out["charge_id"] = charge["charge_id"]
    if isinstance(charge.get("amount_cents"), int):
        out["charge_amount_cents"] = charge["amount_cents"]
    if charge.get("currency"):
        out["charge_currency"] = charge["currency"]
    return out


def _amount_matches(order: sqlite3.Row) -> bool:
    """收款方确认的金额/币种必须与本订单套餐价格完全一致，才算足额。"""
    return (order["charge_amount_cents"] == order["price_cents"]
            and order["charge_currency"] == order["currency"])


def _confirms(order: sqlite3.Row, charge: dict, *, captured: bool) -> bool:
    """确认「足额」且状态达到 captured（或 frozen 足额冻结保障）。"""
    want_state = "captured" if captured else "frozen"
    return (
        charge.get("state") == want_state
        and charge.get("amount_cents") == order["price_cents"]
        and charge.get("currency") == order["currency"]
    )


# ---------------------------------------------------------------- 下单


def place_order(conn: sqlite3.Connection, payment: PaymentClient, org: str,
                package_code: str, request_id: str) -> dict:
    """下单，并在同一请求里尝试第一步收款。返回订单对外视图。

    事务边界：
      A. 本库：幂等检查 + 写入订单 + 标记 create 意图（commit）
      B. 收款方：创建交易（HTTP，结果可能 Unknown）
      C. 本库：记录 B 并推进（commit）
    两边绝不共用事务。
    """
    if not org or not package_code or not request_id:
        raise ServiceError("invalid_order", "org/package_code/request_id 必填", 422)

    with db.transaction(conn):
        existing = conn.execute(
            "SELECT * FROM purchase_orders WHERE org=? AND request_id=?",
            (org, request_id)).fetchone()
        if existing is not None:
            if existing["package_code"] != package_code:
                raise ServiceError(
                    "request_id_package_conflict",
                    "同组织同 request_id 已用于不同套餐，拒绝", 409)
            order_id = existing["order_id"]  # 复用：同一订单，不重复扣款
        else:
            pkg = conn.execute(
                "SELECT * FROM packages WHERE code=?", (package_code,)).fetchone()
            if pkg is None:
                raise ServiceError("package_not_found", "套餐不存在", 404)
            order_id = new_id("ord")
            seq = next_seq(conn)
            conn.execute(
                "INSERT INTO purchase_orders(order_id, org, request_id, package_code, "
                "merchant_order_id, price_cents, currency, tier, seats, duration_seconds, "
                "charge_mode, status, charge_state, create_attempted, created_seq, updated_seq) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'processing', 'unknown', 1, ?, ?)",
                (order_id, org, request_id, pkg["code"], "M-" + order_id,
                 pkg["price_cents"], pkg["currency"], pkg["tier"], pkg["seats"],
                 pkg["duration_seconds"], pkg["charge_mode"], seq, seq),
            )
            conn.execute(
                "INSERT INTO events(seq, kind, org, at_iso, detail_json) "
                "VALUES (?, 'purchase.order_placed', ?, ?, ?)",
                (seq, org, db.get_clock(conn), json.dumps(
                    {"order_id": order_id, "package_code": pkg["code"],
                     "merchant_order_id": "M-" + order_id, "request_id": request_id},
                    ensure_ascii=False, sort_keys=True)),
            )

    advance_order(conn, payment, order_id)
    return get_order(conn, order_id)


# ---------------------------------------------------------------- 对账推进


def advance_order(conn: sqlite3.Connection, payment: PaymentClient, order_id: str) -> dict:
    """显式对账/推进。可重复调用、可在进程重启后调用、可被两个进程同时调用。

    每次只依据「已保存的订单 + 收款方可观察事实」推进；所有 HTTP 调用都在
    SQLite 事务之外完成，并各自用短事务记录结果，因此收款方卡住不会占写锁。
    """
    ctx = OrderContext(conn, payment)
    if _read(conn, order_id) is None:
        raise ServiceError("order_not_found", "订单不存在", 404)

    # 同一次对账里，每类外部操作最多尝试一次：结果未知就留给下一次显式对账，
    # 避免一个卡住的收款方把一次请求拖成多轮超时。
    tried: set[str] = set()
    for _round in range(6):
        order = _read(conn, order_id)
        if order is None:
            raise ServiceError("order_not_found", "订单不存在", 404)
        if order["status"] in ("failed", "canceled", "delivered") and \
                order["cancel_settlement"] != "pending":
            break
        changed = False

        # (1) 创建交易（响应可能丢失；收款方按商户单号去重）
        if ("create" not in tried and not order["create_finished"]
                and order["status"] in ("processing", "cancel_pending")):
            tried.add("create")
            changed = _try_create(ctx, order)

        # (2) 刷新交易状态（恢复丢失的创建响应、查询恢复、冻结后扣款之前）
        order = _read(conn, order_id)
        if (order is not None and "refresh" not in tried and order["charge_id"]
                and order["charge_state"] not in ("voided", "refunded")
                and order["status"] == "processing"):
            tried.add("refresh")
            changed = _refresh_charge(ctx, order) or changed

        # (3) 取消已接受但未结清：关闭未扣款交易 / 原额退款
        order = _read(conn, order_id)
        if order is not None and order["status"] == "cancel_pending" \
                and "settle" not in tried:
            tried.add("settle")
            changed = _settle_cancel(ctx, order) or changed

        # (4) 已承诺但尚未扣款（冻结模式）：尝试扣款
        order = _read(conn, order_id)
        if (order is not None and "capture" not in tried and order["status"] == "promised"
                and order["charge_state"] == "frozen"):
            tried.add("capture")
            _try_capture(ctx, order)
            changed = True

        # (5) 纯本地推进（承诺 / 发放）：写锁短事务，承诺与取消的竞争在此决出
        order = _read(conn, order_id)
        if order is not None:
            changed = _local_transition(ctx, order) or changed
            if order["status"] == "promised":
                changed = _local_transition(ctx, _read(conn, order_id)) or changed

        if not changed:
            break
    return get_order(conn, order_id)


def _try_create(ctx: OrderContext, order: sqlite3.Row) -> None:
    conn = ctx.conn
    with db.transaction(conn):
        order = _read(conn, order["order_id"])
        if order is None or order["create_finished"]:
            return False
        seq = next_seq(conn)
        _emit(conn, order, "purchase.create_attempted",
              {"order_id": order["order_id"],
               "merchant_order_id": order["merchant_order_id"],
               "attempt": order["create_attempted"] + 1}, seq=seq)
        _touch(conn, order["order_id"], seq,
               create_attempted=order["create_attempted"] + 1)

    outcome, payload = _call(ctx, lambda: ctx.payment.create_charge(
        merchant_order_id=order["merchant_order_id"],
        amount_cents=order["price_cents"],
        currency=order["currency"],
        mode=order["charge_mode"],
    ))
    charge = payload.get("charge") or {}

    with db.transaction(conn):
        order = _read(conn, order["order_id"])
        seq = next_seq(conn)
        _record(conn, order, "create", outcome, payload, seq,
                charge_id=charge.get("charge_id"))
        if outcome == "ok" and charge:
            if order["status"] == "canceled" and _cancel_settled(order):
                # 迟到的创建响应：以结清事实为准，不改写资金状态
                _touch(conn, order["order_id"], seq, create_finished=1,
                       create_outcome="ok", last_error=None)
                return True
            _emit(conn, order, "purchase.charge_created",
                  {"order_id": order["order_id"], "charge_id": charge.get("charge_id"),
                   "state": charge.get("state")}, seq=seq)
            _touch(conn, order["order_id"], seq, create_finished=1,
                   create_outcome="ok", **_charge_fields(charge, order), last_error=None)
        elif outcome == "rejected":
            # 明确拒绝：订单 failed（若已接受取消则保持 cancel_pending 去结清）
            _emit(conn, order, "purchase.create_rejected",
                  {"order_id": order["order_id"], "error": payload.get("error")}, seq=seq)
            _touch(conn, order["order_id"], seq, create_finished=1,
                   create_outcome="rejected", charge_state="rejected",
                   status=("cancel_pending" if order["status"] == "cancel_pending"
                           else "failed"),
                   last_error=payload.get("error"))
            return True
        else:
            # 未知：保持 processing；交易可能已存在，下次按商户单号继续对账
            _emit(conn, order, "purchase.create_unknown",
                  {"order_id": order["order_id"], "error": payload.get("error")}, seq=seq)
            _touch(conn, order["order_id"], seq, last_error=payload.get("error"))
            return False
        return True


def _refresh_charge(ctx: OrderContext, order: sqlite3.Row) -> bool:
    conn = ctx.conn
    charge_id = order["charge_id"]
    outcome, payload = _call(ctx, lambda: ctx.payment.get_charge(charge_id))
    charge = payload.get("charge") or {}
    with db.transaction(conn):
        order = _read(conn, order["order_id"])
        seq = next_seq(conn)
        _record(conn, order, "query_charge", outcome, payload, seq, charge_id=charge_id)
        if outcome == "ok" and charge:
            _emit(conn, order, "purchase.charge_observed",
                  {"order_id": order["order_id"], "charge_id": charge.get("charge_id"),
                   "state": charge.get("state")}, seq=seq)
            _touch(conn, order["order_id"], seq, **_charge_fields(charge, order),
                   last_error=None)
            return True
        _touch(conn, order["order_id"], seq,
               last_error=(payload.get("error") if outcome != "ok" else order["last_error"]))
        return False


def _try_capture(ctx: OrderContext, order: sqlite3.Row) -> None:
    conn = ctx.conn
    outcome, payload = _call(ctx, lambda: ctx.payment.capture(order["charge_id"]))
    charge = payload.get("charge") or {}
    with db.transaction(conn):
        order = _read(conn, order["order_id"])
        seq = next_seq(conn)
        _record(conn, order, "capture", outcome, payload, seq, charge_id=order["charge_id"])
        if outcome == "ok" and charge:
            _touch(conn, order["order_id"], seq, **_charge_fields(charge, order),
                   last_error=None if _confirms(order, charge, captured=True)
                   else "capture_mismatch")
        else:
            _touch(conn, order["order_id"], seq, last_error=payload.get("error"))


def _settle_cancel(ctx: OrderContext, order: sqlite3.Row) -> bool:
    """取消已接受：关闭未扣款交易 / 原额退款；只有确认后才结清。"""
    conn = ctx.conn

    if order["charge_id"] is None:
        if not order["create_attempted"]:
            return _finish_cancel(ctx, order, "no_charge")
        # 创建可能已发生：按商户单号对账
        outcome, payload = _call(
            ctx, lambda: ctx.payment.find_charge(order["merchant_order_id"]))
        charge = payload.get("charge") or payload or {}
        with db.transaction(conn):
            order = _read(conn, order["order_id"])
            seq = next_seq(conn)
            _record(conn, order, "query_merchant", outcome, payload, seq,
                    charge_id=charge.get("charge_id"))
            if outcome == "unknown":
                _touch(conn, order["order_id"], seq, last_error=payload.get("error"))
                return False
            if outcome == "rejected" or not charge:
                return _finish_cancel_locked(conn, order, "no_charge")
            _touch(conn, order["order_id"], seq, **_charge_fields(charge, order))
        order = _read(conn, order["order_id"])
    if order["status"] != "cancel_pending":
        return False

    if order["charge_state"] == "captured":
        # 已扣款：原额退款。取消已接受，绝不因查到扣款而发放权益。
        outcome, payload = _call(ctx, lambda: ctx.payment.refund(order["charge_id"]))
        charge = payload.get("charge") or {}
        with db.transaction(conn):
            order = _read(conn, order["order_id"])
            seq = next_seq(conn)
            _record(conn, order, "refund", outcome, payload, seq, charge_id=order["charge_id"])
            if outcome == "ok" and charge.get("state") == "refunded":
                _touch(conn, order["order_id"], seq, **_charge_fields(charge, order))
                return _finish_cancel_locked(conn, order, "refund_confirmed")
            if outcome == "rejected" and charge:
                # 例如「已扣款，不能关闭」：以收款方事实纠正本地状态，下一轮改走退款
                _touch(conn, order["order_id"], seq, **_charge_fields(charge, order),
                       cancel_settlement="pending",
                       last_error=payload.get("error"))
            else:
                _touch(conn, order["order_id"], seq, cancel_settlement="pending",
                       last_error=payload.get("error") or outcome)
            return True
    if order["charge_state"] == "frozen":
        outcome, payload = _call(ctx, lambda: ctx.payment.void(order["charge_id"]))
        charge = payload.get("charge") or {}
        with db.transaction(conn):
            order = _read(conn, order["order_id"])
            seq = next_seq(conn)
            _record(conn, order, "void", outcome, payload, seq, charge_id=order["charge_id"])
            if outcome == "ok" and charge.get("state") == "voided":
                _touch(conn, order["order_id"], seq, **_charge_fields(charge, order))
                return _finish_cancel_locked(conn, order, "void_confirmed")
            if outcome == "rejected" and charge:
                _touch(conn, order["order_id"], seq, **_charge_fields(charge, order),
                       cancel_settlement="pending",
                       last_error=payload.get("error"))
            else:
                _touch(conn, order["order_id"], seq, cancel_settlement="pending",
                       last_error=payload.get("error") or outcome)
            return True
    if order["charge_state"] == "voided":
        return _finish_cancel(ctx, order, "void_confirmed")
    if order["charge_state"] == "refunded":
        return _finish_cancel(ctx, order, "refund_confirmed")
    if order["charge_state"] == "rejected":
        return _finish_cancel(ctx, order, "no_charge")

    # none / unknown：查询一次，明确不存在才无款结清
    if order["charge_id"] is None:
        return _finish_cancel(ctx, order, "no_charge")
    outcome, payload = _call(ctx, lambda: ctx.payment.get_charge(order["charge_id"]))
    charge = payload.get("charge") or {}
    with db.transaction(conn):
        order = _read(conn, order["order_id"])
        seq = next_seq(conn)
        _record(conn, order, "query_charge", outcome, payload, seq,
                charge_id=order["charge_id"])
        if outcome == "ok" and charge:
            _touch(conn, order["order_id"], seq, **_charge_fields(charge, order),
                   cancel_settlement="pending")
        else:
            # 查询失败/未知：取消未结清，保持 pending，绝不当成无款
            _touch(conn, order["order_id"], seq, cancel_settlement="pending",
                   last_error=payload.get("error"))
        return True


def _finish_cancel_locked(conn: sqlite3.Connection, order: sqlite3.Row,
                          settlement: str) -> bool:
    """调用方已持有写事务：把取消结清落到订单。"""
    if order["status"] != "cancel_pending":
        return False
    seq = next_seq(conn)
    _emit(conn, order, "purchase.canceled",
          {"order_id": order["order_id"], "settlement": settlement,
           "cancel_request_id": order["cancel_request_id"], "ent_id": None}, seq=seq)
    _touch(conn, order["order_id"], seq, status="canceled",
           cancel_settlement=settlement, last_error=None)
    return True


def _finish_cancel(ctx: OrderContext, order: sqlite3.Row, settlement: str) -> bool:
    with db.transaction(ctx.conn):
        order = _read(ctx.conn, order["order_id"])
        return _finish_cancel_locked(ctx.conn, order, settlement)


def _local_transition(ctx: OrderContext, order: sqlite3.Row) -> bool:
    """纯本地状态推进：processing -> promised；promised -> delivered。"""
    conn = ctx.conn
    with db.transaction(conn):
        order = _read(conn, order["order_id"])
        if order is None:
            return False

        if order["status"] == "processing":
            if (order["charge_state"] in ("captured", "frozen")
                    and _amount_matches(order)):
                basis = "captured" if order["charge_state"] == "captured" \
                    else "frozen_guarantee"
                seq = next_seq(conn)
                _emit(conn, order, "purchase.promised",
                      {"order_id": order["order_id"], "basis": basis,
                       "merchant_order_id": order["merchant_order_id"],
                       "charge_id": order["charge_id"],
                       "amount_cents": order["price_cents"]}, seq=seq)
                _touch(conn, order["order_id"], seq, status="promised",
                       delivery_basis=basis, promised_seq=seq,
                       promised_at_iso=db.get_clock(conn), last_error=None)
                return True
            return False

        if (order["status"] == "promised" and order["charge_state"] == "captured"
                and _amount_matches(order)):
            return _deliver(ctx, order)
    return False


def _deliver(ctx: OrderContext, order: sqlite3.Row) -> bool:
    """实际扣款确认后发放**一份**权益；窗口从发放时逻辑时间起算并锁定。"""
    conn = ctx.conn
    if order["issued_ent_id"]:
        return False
    at = db.get_clock(conn)
    effective_at = at
    expire_at = normalize_ts(
        datetime.fromisoformat(at) + timedelta(seconds=order["duration_seconds"]))
    result = create_entitlement_locked(
        conn, order["org"], order["tier"], order["seats"], effective_at, expire_at,
        source="purchase",
        detail={"order_id": order["order_id"],
                "merchant_order_id": order["merchant_order_id"],
                "charge_id": order["charge_id"]},
    )
    seq = next_seq(conn)
    _emit(conn, order, "purchase.delivered",
          {"order_id": order["order_id"], "ent_id": result["ent_id"],
           "ent_effective_at": effective_at, "ent_expire_at": expire_at,
           "charge_id": order["charge_id"],
           "evicted": result["evicted"]}, seq=seq)
    _touch(conn, order["order_id"], seq, status="delivered",
           issued_ent_id=result["ent_id"], ent_effective_at=effective_at,
           ent_expire_at=expire_at, ent_at_iso=at, delivered_seq=seq)
    return True


# ---------------------------------------------------------------- 取消


def request_cancel(conn: sqlite3.Connection, payment: PaymentClient, order_id: str,
                   cancel_request_id: str) -> dict:
    """接受取消（只有未承诺订单可被接受），随后立即尝试结清（可能需多轮对账）。"""
    if not cancel_request_id:
        raise ServiceError("invalid_cancel", "cancel_request_id 必填", 422)
    with db.transaction(conn):
        order = _read(conn, order_id)
        if order is None:
            raise ServiceError("order_not_found", "订单不存在", 404)
        status = order["status"]
        if status in ("promised", "delivered"):
            raise ServiceError("cancel_after_promise",
                               "订单已承诺交付，不能取消；服务将继续履约", 409)
        if status == "canceled" and cancel_settlement(order) != "pending":
            return get_order(conn, order_id)
        if status == "canceled":
            return get_order(conn, order_id)  # 已接受取消，等对账
        seq = next_seq(conn)
        _emit(conn, order, "purchase.cancel_accepted",
              {"order_id": order_id, "cancel_request_id": cancel_request_id}, seq=seq)
        _touch(conn, order_id, seq, status="cancel_pending",
               cancel_request_id=cancel_request_id, cancel_accepted_seq=seq,
               cancel_accepted_at=db.get_clock(conn), cancel_settlement="pending")
    advance_order(conn, payment, order_id)
    return get_order(conn, order_id)


def cancel_settlement(order: sqlite3.Row) -> str:
    return order["cancel_settlement"]


# ---------------------------------------------------------------- 查询


def _order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
    row = _read(conn, order_id)
    if row is None:
        raise ServiceError("order_not_found", "订单不存在", 404)
    return row


def get_order(conn: sqlite3.Connection, order_id: str) -> dict:
    return order_view(conn, _order(conn, order_id))


def _money_ops(conn: sqlite3.Connection, order_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM purchase_money_ops WHERE order_id=? ORDER BY id", (order_id,)
    ).fetchall()
    return [{
        "op_type": r["op_type"],
        "merchant_order_id": r["merchant_order_id"],
        "charge_id": r["charge_id"],
        "outcome": r["outcome"],
        "detail": json.loads(r["detail_json"]),
        "seq": r["seq"],
        "at": r["at_iso"],
    } for r in rows]


def _cancel_settled(order: sqlite3.Row) -> bool:
    return order["cancel_settlement"] in ("void_confirmed", "refund_confirmed", "no_charge")


def order_view(conn: sqlite3.Connection, order: sqlite3.Row) -> dict:
    status = order["status"]
    stage = {
        "processing": STAGE_PROCESSING,
        "cancel_pending": STAGE_PROCESSING,
        "promised": STAGE_PROMISED,
        "delivered": STAGE_DELIVERED,
        "canceled": STAGE_CANCELED,
        "failed": STAGE_FAILED,
    }[status]

    cancel_accepted = order["cancel_accepted_seq"] is not None
    if not cancel_accepted:
        settlement = "not_accepted"
    elif _cancel_settled(order):
        settlement = order["cancel_settlement"]
    else:
        settlement = "pending"

    money = order["charge_state"]
    entitlement = None
    if order["issued_ent_id"]:
        ent_row = conn.execute(
            "SELECT * FROM entitlements WHERE ent_id=? AND version=1",
            (order["issued_ent_id"],)).fetchone()
        if ent_row is not None:
            entitlement = {
                "ent_id": ent_row["ent_id"],
                "tier": ent_row["tier"],
                "seats": ent_row["seats"],
                "effective_at": ent_row["effective_at"],
                "expire_at": ent_row["expire_at"],
                "status": ent_row["status"],
            }

    return {
        "order_id": order["order_id"],
        "org": order["org"],
        "request_id": order["request_id"],
        "package_code": order["package_code"],
        "merchant_order_id": order["merchant_order_id"],
        "amount_cents": order["price_cents"],
        "currency": order["currency"],
        "charge_mode": order["charge_mode"],
        "stage": stage,
        "status": status,
        "processing": status in ("processing", "cancel_pending"),
        "promised": status in ("promised", "delivered"),
        "delivered": status == "delivered",
        "failed": status == "failed",
        "promised_at": order["promised_at_iso"],
        "promised_seq": order["promised_seq"],
        "delivery_basis": order["delivery_basis"],
        "money": {
            "state": money,
            "charge_id": order["charge_id"],
            "has_charge": order["charge_id"] is not None,
            "captured": money == "captured",
            "unknown": money in ("unknown", "none"),
        },
        "cancel": {
            "accepted": cancel_accepted,
            "request_id": order["cancel_request_id"],
            "settlement": settlement,
            "settled": bool(cancel_accepted and _cancel_settled(order)),
        },
        "entitlement": entitlement,
        "ent_window": {
            "effective_at": order["ent_effective_at"],
            "expire_at": order["ent_expire_at"],
            "at": order["ent_at_iso"],
        } if order["ent_effective_at"] else None,
        "money_ops": _money_ops(conn, order["order_id"]),
        "last_error": order["last_error"],
        "created_seq": order["created_seq"],
        "updated_seq": order["updated_seq"],
    }


def list_orders(conn: sqlite3.Connection, org: str | None = None) -> list[dict]:
    if org:
        rows = conn.execute("SELECT * FROM purchase_orders WHERE org=? ORDER BY created_seq",
                            (org,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM purchase_orders ORDER BY created_seq").fetchall()
    return [order_view(conn, r) for r in rows]
