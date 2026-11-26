"""组织购权的并发与隔离：承诺 vs 取消、双进程对账、无关组织不被阻塞。"""
import multiprocessing
import threading
import time

import pytest

from app import db, purchase, service
from tests.conftest import EPOCH

PKG = "pkg"
PKG_FREEZE = "pkg-freeze"


def _setup(conn, payments):
    purchase.create_package(conn, PKG, 9900, "CNY", "premium", 2, 30 * 86400)
    purchase.create_package(conn, PKG_FREEZE, 9900, "CNY", "premium", 2, 30 * 86400,
                            charge_mode="freeze")


def _fresh(path: str, payments):
    c = db.connect(path)
    db.init_db(c)
    c.execute("UPDATE clock SET now_iso=? WHERE id=1", (db.normalize_ts(EPOCH),))
    return c


def test_promise_vs_cancel_only_one_wins(tmp_path, payments):
    """承诺交付与取消竞争：写锁串行化，只有一方能成功，结果可核对。"""
    path = str(tmp_path / "race.db")
    c0 = _fresh(path, payments)
    _setup(c0, payments)
    # 冻结成功但本地事务尚未推进（创建响应丢失），订单处于处理中
    payments.control({"action": "drop", "op": "create", "merchant_order_id": None})
    o = purchase.place_order(c0, payments, "acme", PKG_FREEZE, "req-1")
    order_id = o["order_id"]
    assert o["stage"] == "processing"
    c0.close()

    conn_a = db.connect(path)
    conn_b = db.connect(path)
    barrier = threading.Barrier(2)
    outcomes: dict = {}

    def do_promise():
        barrier.wait()
        outcomes["promise"] = purchase.advance_order(conn_a, payments, order_id)

    def do_cancel():
        barrier.wait()
        try:
            outcomes["cancel"] = purchase.request_cancel(conn_b, payments, order_id, "c-1")
        except service.ServiceError as e:
            outcomes["cancel_error"] = e.code

    t1 = threading.Thread(target=do_promise)
    t2 = threading.Thread(target=do_cancel)
    t1.start(); t2.start(); t1.join(); t2.join()
    conn_a.close(); conn_b.close()

    check = db.connect(path)
    final = purchase.get_order(check, order_id)
    if "cancel_error" in outcomes and outcomes["cancel_error"] == "cancel_after_promise":
        # 承诺先赢：订单已承诺，取消被明确拒绝，服务继续履约
        assert final["promised"] is True
        assert final["cancel"]["accepted"] is False
        advance = purchase.advance_order(check, payments, order_id)
        assert advance["stage"] == "delivered"
    else:
        # 取消先赢：订单已取消、无权益；承诺结果必须是「处理中」而非已承诺
        assert final["stage"] in ("canceled", "processing")
        if final["stage"] == "canceled":
            assert final["cancel"]["settled"] is True
            assert final["entitlement"] is None
            assert outcomes["promise"]["promised"] is False
    check.close()


def test_two_processes_reconcile_same_order_single_charge_and_entitlement(tmp_path, payments):
    """两个真实 OS 进程同时对账同一订单：只扣一次、只发一份权益。"""
    path = str(tmp_path / "proc.db")
    c0 = _fresh(path, payments)
    _setup(c0, payments)
    payments.control({"action": "drop", "op": "create", "merchant_order_id": None})
    o = purchase.place_order(c0, payments, "acme", PKG, "req-1")
    order_id = o["order_id"]
    c0.close()

    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    p1 = ctx.Process(target=_reconcile_worker,
                     args=(path, payments.base_url, order_id, queue))
    p2 = ctx.Process(target=_reconcile_worker,
                     args=(path, payments.base_url, order_id, queue))
    p1.start(); p2.start(); p1.join(30); p2.join(30)
    assert p1.exitcode == 0 and p2.exitcode == 0

    check = db.connect(path)
    final = purchase.get_order(check, order_id)
    assert final["stage"] == "delivered"
    ent_ids = check.execute(
        "SELECT ent_id FROM entitlements WHERE org='acme'").fetchall()
    assert len(ent_ids) == 1  # 两个进程都跑了，但只发一份
    stub = payments.stub_state()
    assert len(stub["charges"]) == 1
    assert stub["charges"][0]["captures"] == 1
    check.close()


def _reconcile_worker(db_path: str, base_url: str, order_id: str, queue):
    from app.payment_client import PaymentClient
    c = db.connect(db_path)
    try:
        payout = purchase.advance_order(c, PaymentClient(base_url, timeout=2.0), order_id)
        queue.put(payout["stage"])
    finally:
        c.close()


def test_stuck_payment_does_not_block_unrelated_org_admission(conn, payments, tmp_path):
    """收款方卡住时，无关组织的会话准入仍能及时完成。"""
    _setup(conn, payments)
    # 另一个组织已有可用权益
    service.create_entitlement(conn, "quiet-org", "standard", 2,
                               "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

    payments.control({"hang_seconds": 3.0, "op": "create"})
    started = threading.Event()
    done = threading.Event()

    def slow_purchase():
        c = db.connect(str(tmp_path / "test.db"))
        try:
            started.set()
            purchase.place_order(c, payments, "slow-org", PKG, "req-1")
        finally:
            done.set()
            c.close()

    t = threading.Thread(target=slow_purchase, daemon=True)
    t.start()
    started.wait(5)
    time.sleep(0.2)

    # 收款方仍卡住时，另一组织准入必须在很短时间内成功（不等收款方）
    t0 = time.monotonic()
    result = service.admit_session(conn, "quiet-org", "standard", "quiet-req-1")
    elapsed = time.monotonic() - t0
    assert result["admitted"] is True
    assert elapsed < 1.0, f"无关组织准入被收款方拖住 {elapsed:.2f}s"

    done.wait(10)
