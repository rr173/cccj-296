"""离线借席并发测试：多连接/真双进程竞争下额度承诺不破。

运行： PYTHONPATH=/workspace python3 -m unittest tests.test_offline_concurrency -v
"""
from __future__ import annotations

import copy
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path

from app import crypto, db, offline, service

EPOCH = "2026-01-01T00:00:00Z"
SIGN_KEY = "test-signing-key-0123456789abcdef"
os.environ.setdefault("LICENSE_SIGNING_KEY", SIGN_KEY)


def _fresh_db(db_path: str):
    c = db.connect(db_path)
    db.init_db(c)
    c.execute("UPDATE clock SET now_iso=? WHERE id=1", (db.normalize_ts(EPOCH),))
    with db.transaction(c):
        crypto.get_or_create_center_key(c)
    service.create_entitlement(c, "race", "premium", 1,
                               EPOCH, "2027-01-01T00:00:00Z")
    return c


class OfflineConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="offline_conc_")
        self.db_path = str(Path(self.tmp) / "race.db")
        c0 = _fresh_db(self.db_path)
        c0.close()

    def test_concurrent_issue_only_one_wins_single_seat(self):
        """两个连接同时借唯一高级席：恰好一个成功，预留绝不超发。"""
        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def worker(rid: str):
            c = db.connect(self.db_path)
            try:
                barrier.wait()
                results.append(offline.issue_package(
                    c, "race", f"site-{rid}", 0, 1,
                    "2026-02-01T00:00:00Z", rid))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(f"r-{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        issued = [r for r in results if r["issued"]]
        rejected = [r for r in results if not r["issued"]]
        self.assertEqual(len(issued), 1)
        self.assertEqual(len(rejected), 1)

        check = db.connect(self.db_path)
        active_pkgs = check.execute(
            "SELECT COUNT(*) FROM offline_packages WHERE status='active'").fetchone()[0]
        self.assertEqual(active_pkgs, 1)
        # 唯一席位已被预留：在线高级会话进不来
        admit = service.admit_session(check, "race", "premium", "online-x")
        self.assertFalse(admit["admitted"])
        check.close()

    def test_return_and_issue_race_quota_reassigned_once(self):
        """交回释放与新签发竞争：最终只有一个持有者占用该席位。"""
        c0 = db.connect(self.db_path)
        first = offline.issue_package(
            c0, "race", "site-1", 0, 1, "2026-02-01T00:00:00Z", "first")
        self.assertTrue(first["issued"])
        credential = copy.deepcopy(first["return_credential"])
        c0.close()

        conn_a = db.connect(self.db_path)
        conn_b = db.connect(self.db_path)
        barrier = threading.Barrier(2)
        out: dict = {}

        def do_return():
            barrier.wait()
            out["return"] = offline.return_package(conn_a, credential, "ret-op")

        def do_issue():
            barrier.wait()
            out["issue"] = offline.issue_package(
                conn_b, "race", "site-2", 0, 1,
                "2026-02-01T00:00:00Z", "second")

        t1 = threading.Thread(target=do_return)
        t2 = threading.Thread(target=do_issue)
        t1.start(); t2.start()
        t1.join(); t2.join()
        conn_a.close(); conn_b.close()

        self.assertTrue(out["return"]["returned"])
        # 无论顺序如何：交回成功后新签发一定成功；若签发先到则它被拒，
        # 交回后再签一次必然成功。最终活跃包数恒为 1。
        check = db.connect(self.db_path)
        if not out["issue"]["issued"]:
            again = offline.issue_package(
                check, "race", "site-2", 0, 1,
                "2026-02-01T00:00:00Z", "second-retry")
            self.assertTrue(again["issued"])
        active = check.execute(
            "SELECT package_id FROM offline_packages WHERE status='active'").fetchall()
        self.assertEqual(len(active), 1)
        check.close()

    def test_modify_race_cannot_break_offline_promise(self):
        """权益降额与离线签发竞争：两种顺序都不能让未交回包失去兑现保障。"""
        conn_a = db.connect(self.db_path)
        conn_b = db.connect(self.db_path)
        barrier = threading.Barrier(2)
        outcomes: dict = {}

        def do_issue():
            barrier.wait()
            outcomes["issue"] = offline.issue_package(
                conn_a, "race", "site-9", 0, 1,
                "2026-02-01T00:00:00Z", "guard-race")

        def do_modify():
            barrier.wait()
            ent_id = conn_b.execute(
                "SELECT ent_id FROM entitlements WHERE org='race'").fetchone()[0]
            try:
                service.modify_entitlement(conn_b, ent_id, seats=0)
                outcomes["modify"] = "allowed"
            except service.ServiceError as exc:
                outcomes["modify"] = exc.code

        t1 = threading.Thread(target=do_issue)
        t2 = threading.Thread(target=do_modify)
        t1.start(); t2.start()
        t1.join(); t2.join()
        conn_a.close(); conn_b.close()

        check = db.connect(self.db_path)
        pkg = check.execute(
            "SELECT COUNT(*) FROM offline_packages WHERE status='active'").fetchone()[0]
        if pkg:
            # 包签出去了：降额必须被守卫拒绝，承诺仍在
            self.assertIn("offline", outcomes["modify"])
            seats = check.execute(
                "SELECT seats FROM entitlements WHERE status='active'").fetchone()[0]
            self.assertEqual(seats, 1)
        else:
            # 降额先提交：签发必须被拒（容量无法兑现）
            self.assertFalse(outcomes["issue"]["issued"])
        check.close()


def _process_worker(db_path: str, rid: str, queue):
    os.environ["LICENSE_SIGNING_KEY"] = SIGN_KEY
    c = db.connect(db_path)
    try:
        queue.put(offline.issue_package(
            c, "race2", f"site-{rid}", 0, 1,
            "2026-02-01T00:00:00Z", rid))
    finally:
        c.close()


class OfflineTwoProcessCase(unittest.TestCase):
    def test_two_real_processes_contend_for_single_offline_seat(self):
        tmp = tempfile.mkdtemp(prefix="offline_proc_")
        db_path = str(Path(tmp) / "proc.db")
        c0 = db.connect(db_path)
        db.init_db(c0)
        c0.execute("UPDATE clock SET now_iso=? WHERE id=1",
                   (db.normalize_ts(EPOCH),))
        with db.transaction(c0):
            crypto.get_or_create_center_key(c0)
        service.create_entitlement(c0, "race2", "premium", 1,
                                   EPOCH, "2027-01-01T00:00:00Z")
        c0.close()

        # spawn 启动干净解释器：不继承父进程已打开的 SQLite 连接/WAL 状态，
        # 与演示中两个独立 OS 进程的部署形态一致
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        p1 = ctx.Process(target=_process_worker, args=(db_path, "preq-1", queue))
        p2 = ctx.Process(target=_process_worker, args=(db_path, "preq-2", queue))
        p1.start(); p2.start()
        p1.join(30); p2.join(30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)

        outcomes = [queue.get_nowait() for _ in range(2)]
        self.assertEqual(sorted(r["issued"] for r in outcomes), [False, True])

        check = db.connect(db_path)
        active = check.execute(
            "SELECT COUNT(*) FROM offline_packages WHERE status='active'").fetchone()[0]
        self.assertEqual(active, 1)
        check.close()


if __name__ == "__main__":
    unittest.main()
