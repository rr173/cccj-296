"""组织购权：下单/幂等/收款结果未知恢复/取消/冻结/并发。"""
import json
import multiprocessing
import threading
import time

import pytest

from app import db, purchase, service
from tests.conftest import EPOCH

PKG = "pkg-premium"
PKG_FREEZE = "pkg-freeze"


def _setup(conn, payments):
    purchase.create_package(conn, PKG, 9900, "CNY", "premium", 3, 30 * 86400)
    purchase.create_package(conn, PKG_FREEZE, 5000, "CNY", "standard", 2,
                            10 * 86400, charge_mode="freeze")
    return payments


def _ent(conn, org):
    return [r for r in service.list_org_sessions(conn, org) if r["status"] == "active"]


# ---------------------------------------------------------------- 套餐与下单


def test_package_only_preset_fields_and_order_locks_price(conn, payments):
    _setup(conn, payments)
    o = purchase.place_order(conn, payments, "acme", PKG, "req-1")
    assert o["amount_cents"] == 9900
    assert o["package_code"] == PKG
    assert o["stage"] == "delivered"
    # 价格来自套餐，下单参数无法改价
    with pytest.raises(service.ServiceError):
        purchase.place_order(conn, payments, "acme", "no-such-pkg", "req-2")


def test_same_org_same_request_returns_same_order_and_no_double_charge(conn, payments):
    _setup(conn, payments)
    first = purchase.place_order(conn, payments, "acme", PKG, "req-1")
    again = purchase.place_order(conn, payments, "acme", PKG, "req-1")
    assert again["order_id"] == first["order_id"]
    assert again["amount_cents"] == first["amount_cents"]
    # 收款方只存在一笔交易
    state = payments.stub_state()
    assert len(state["charges"]) == 1
    assert state["charges"][0]["captures"] == 1
    # 复用 request_id 换套餐则拒绝
    purchase.create_package(conn, "other", 100, "CNY", "standard", 1, 86400)
    with pytest.raises(service.ServiceError) as exc:
        purchase.place_order(conn, payments, "acme", "other", "req-1")
    assert exc.value.code == "request_id_package_conflict"


def test_entitlement_window_starts_at_delivery_logical_time(conn, payments):
    _setup(conn, payments)
    service.advance_clock(conn, to_iso="2026-03-01T00:00:00Z")
    o = purchase.place_order(conn, payments, "acme", PKG, "req-1")
    assert o["stage"] == "delivered"
    # 权益从实际发放时的服务逻辑时间起算（不是下单 wall clock，也不是套餐创建时间）
    assert o["entitlement"]["effective_at"].startswith("2026-03-01T00:00:00")
    assert o["entitlement"]["expire_at"].startswith("2026-03-31T00:00:00")
    # 新权益立即可用于现有会话准入
    s = service.admit_session(conn, "acme", "premium", "s-1")
    assert s["admitted"] is True
    assert s["assigned_ent_id"] == o["entitlement"]["ent_id"]


# ---------------------------------------------------------------- 结果未知与恢复


def test_lost_create_response_is_unknown_not_failure_then_recovers(conn, payments):
    _setup(conn, payments)
    payments.control({"action": "drop", "op": "create", "merchant_order_id": None})
    # 收款方已记录并扣款，但响应丢失：本服务只能看到「结果未知」
    o = purchase.place_order(conn, payments, "acme", PKG, "req-1")
    # 订单已持久化，且没有臆断失败、没有发权益
    orders = purchase.list_orders(conn, "acme")
    assert len(orders) == 1
    o = orders[0]
    assert o["stage"] == "processing"
    assert o["money"]["state"] == "unknown"
    assert o["entitlement"] is None
    # 收款方确实已经扣了款（测试确认「请求已处理后丢失响应」）
    assert payments.stub_state()["charges"][0]["state"] == "captured"

    # 进程退出后用已保存订单继续对账（这里用同一库模拟重启后的显式触发）
    recovered = purchase.advance_order(conn, payments, o["order_id"])
    assert recovered["stage"] == "delivered"
    assert recovered["money"]["captured"] is True
    assert recovered["entitlement"]["ent_id"]
    # 只扣一次、只发一份权益
    assert payments.stub_state()["charges"][0]["captures"] == 1
    assert len(purchase.list_orders(conn, "acme")) == 1


def test_queries_unavailable_keeps_processing_never_failure(conn, payments):
    _setup(conn, payments)
    # 创建响应丢失 + 随后收款方暂不可查询：只能保持「处理中」，不能判成失败
    payments.control({"action": "drop", "op": "create", "merchant_order_id": None})
    payments.control({"queries_unavailable": True})
    purchase.place_order(conn, payments, "acme", PKG, "req-1")
    o = purchase.list_orders(conn, "acme")[0]
    assert o["stage"] == "processing"
    assert o["money"]["state"] == "unknown"
    assert o["entitlement"] is None

    payments.control({"queries_unavailable": False})
    o2 = purchase.advance_order(conn, payments, o["order_id"])
    assert o2["stage"] == "delivered"
    assert o2["money"]["captured"] is True


def test_recovery_does_not_reset_entitlement_window(conn, payments):
    _setup(conn, payments)
    o = purchase.place_order(conn, payments, "acme", PKG, "req-1")
    assert o["stage"] == "delivered"
    window = o["ent_window"]
    # 之后重试、恢复、时钟推进，都不会改写已发放权益的有效期
    service.advance_clock(conn, delta_seconds=10 * 86400)
    again = purchase.advance_order(conn, payments, o["order_id"])
    assert again["ent_window"] == window
    assert again["entitlement"]["effective_at"] == window["effective_at"]
    assert again["entitlement"]["expire_at"] == window["expire_at"]


# ---------------------------------------------------------------- 冻结后扣款


def test_freeze_guarantees_promise_then_captures_and_delivers_once(conn, payments):
    _setup(conn, payments)
    payments.control({"mutations_unavailable": True})
    o = purchase.place_order(conn, payments, "acme", PKG_FREEZE, "req-1")
    # 冻结保障存在 -> 可承诺交付，但扣款还没发生 -> 不发权益
    assert o["stage"] == "promised"
    assert o["delivery_basis"] == "frozen_guarantee"
    assert o["money"]["state"] == "frozen"
    assert o["entitlement"] is None

    payments.control({"mutations_unavailable": False})
    o2 = purchase.advance_order(conn, payments, o["order_id"])
    assert o2["stage"] == "delivered"
    assert o2["money"]["captured"] is True
    assert o2["entitlement"]["seats"] == 2
    state = payments.stub_state()
    assert state["charges"][0]["captures"] == 1
    assert len(state["charges"]) == 1


def test_frozen_funds_remain_capturable_until_explicit_void(conn, payments):
    _setup(conn, payments)
    payments.control({"mutations_unavailable": True})
    o = purchase.place_order(conn, payments, "acme", PKG_FREEZE, "req-1")
    assert o["stage"] == "promised"
    # 冻结成功且未被商户解冻前，资金一直足额可扣
    payments.control({"mutations_unavailable": False})
    o2 = purchase.advance_order(conn, payments, o["order_id"])
    assert o2["money"]["captured"] is True


# ---------------------------------------------------------------- 取消


def test_cancel_after_promise_is_refused_and_keeps_fulfilling(conn, payments):
    _setup(conn, payments)
    payments.control({"mutations_unavailable": True})
    o = purchase.place_order(conn, payments, "acme", PKG_FREEZE, "req-1")
    assert o["stage"] == "promised"
    with pytest.raises(service.ServiceError) as exc:
        purchase.request_cancel(conn, payments, o["order_id"], "c-1")
    assert exc.value.code == "cancel_after_promise"
    # 仍继续履约：随后扣款并发放权益
    payments.control({"mutations_unavailable": False})
    o2 = purchase.advance_order(conn, payments, o["order_id"])
    assert o2["stage"] == "delivered"
    assert o2["cancel"]["accepted"] is False


def test_cancel_before_promise_on_frozen_voids_and_never_delivers(conn, payments):
    """冻结已成功但本地还没承诺（状态不明）时取消：关闭冻结交易、不发权益。"""
    _setup(conn, payments)
    payments.control({"action": "drop", "op": "create", "merchant_order_id": None})
    o = purchase.place_order(conn, payments, "acme", PKG_FREEZE, "req-1")
    assert o["stage"] == "processing"
    assert o["entitlement"] is None
    assert payments.stub_state()["charges"][0]["state"] == "frozen"

    o2 = purchase.request_cancel(conn, payments, o["order_id"], "c-1")
    assert o2["stage"] == "canceled"
    assert o2["cancel"]["settlement"] == "void_confirmed"
    assert o2["cancel"]["settled"] is True
    assert o2["entitlement"] is None
    assert payments.stub_state()["charges"][0]["state"] == "voided"
    # 关闭成功后迟到请求也不能再扣该单
    with pytest.raises(Exception):
        payments.capture(payments.stub_state()["charges"][0]["charge_id"])


def test_cancel_on_direct_charge_before_delivery_refunds_and_no_entitlement(conn, payments):
    """直接扣款已 captured 但尚未承诺/发放时取消 -> 全额退款、不发权益。"""
    _setup(conn, payments)
    # 让扣款成功但本地事务在拿到结果前「崩溃」：用丢响应模拟随机失败
    payments.control({"action": "drop", "op": "create", "merchant_order_id": None})
    purchase.place_order(conn, payments, "acme", PKG, "req-1")
    o = purchase.list_orders(conn, "acme")[0]
    assert o["stage"] == "processing"
    assert o["entitlement"] is None

    # 接受取消（尚未承诺）：应退款，且绝不因查到扣款而发权益
    o2 = purchase.request_cancel(conn, payments, o["order_id"], "c-1")
    assert o2["stage"] == "canceled"
    assert o2["cancel"]["settled"] is True
    assert o2["cancel"]["settlement"] == "refund_confirmed"
    assert o2["entitlement"] is None
    stub = payments.stub_state()["charges"][0]
    assert stub["state"] == "refunded"
    assert stub["refunds"] == 1
    # 收款方不再有任何可扣款交易
    assert service.list_org_sessions(conn, "acme") == []


def test_cancel_with_ambiguous_refund_stays_pending_not_no_charge(conn, payments):
    _setup(conn, payments)
    payments.control({"action": "drop", "op": "create", "merchant_order_id": None})
    purchase.place_order(conn, payments, "acme", PKG, "req-1")
    o = purchase.list_orders(conn, "acme")[0]
    # 退款请求已处理但响应丢失 + 收款方暂不可查询
    payments.control({"action": "drop", "op": "refund", "count": 1,
                      "merchant_order_id": None})
    payments.control({"queries_unavailable": True})
    o2 = purchase.request_cancel(conn, payments, o["order_id"], "c-1")
    assert o2["status"] == "cancel_pending"
    assert o2["cancel"]["settled"] is False
    assert o2["cancel"]["settlement"] == "pending"
    assert o2["entitlement"] is None

    # 收款方恢复查询后确认退款已完成 -> 结清
    payments.control({"queries_unavailable": False})
    o3 = purchase.advance_order(conn, payments, o["order_id"])
    assert o3["stage"] == "canceled"
    assert o3["cancel"]["settlement"] == "refund_confirmed"
    assert o3["entitlement"] is None
    assert payments.stub_state()["charges"][0]["state"] == "refunded"


