"""预付预算：发放幂等、按逻辑时间扣费、真实边界停用、账目可核对、并发不重复。

运行： PYTHONPATH=/workspace python3 -m unittest tests.test_budget -v
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from app import budget, db, service

EPOCH = "2026-01-01T00:00:00Z"
US = 0.000001          # 1 逻辑微秒（秒）
LONG = "2027-01-01T00:00:00Z"


class BudgetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="budget_")
        self.conn = db.connect(str(Path(self.tmp) / "b.db"))
        db.init_db(self.conn)
        self.conn.execute("UPDATE clock SET now_iso=? WHERE id=1",
                          (db.normalize_ts(EPOCH),))

    def tearDown(self):
        self.conn.close()

    # ------------------------------------------------------------ 辅助

    def ent(self, org="acme", tier="standard", seats=5,
            start=EPOCH, end=LONG):
        return service.create_entitlement(self.conn, org, tier, seats, start, end)

    def grant(self, amount, expire="2026-02-01T00:00:00Z", rid="g1", org="acme"):
        return budget.grant(self.conn, org, amount, expire, rid)

    def admit(self, tier="standard", rid="r1", org="acme"):
        return service.admit_session(self.conn, org, tier, rid)

    def sessions(self, org="acme"):
        return service.list_org_sessions(self.conn, org)

    def advance_us(self, n, **kw):
        return service.advance_clock(self.conn, delta_seconds=n * US, **kw)

    # ------------------------------------------------------------ 发放

    def test_grant_is_positive_and_idempotent(self):
        self.ent()
        out = self.grant(100, rid="g-a")
        self.assertTrue(out["granted"])
        self.assertEqual(out["balance_after"], 100)
        # 同组织同 request_id 原样重试
        again = self.grant(100, rid="g-a")
        self.assertEqual(again["grant_id"], out["grant_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM budget_grants").fetchone()[0], 1)

    def test_grant_changed_amount_or_expiry_rejected(self):
        self.ent()
        self.grant(100, rid="g-a")
        for kwargs in ({"amount": 200}, {"expire": "2026-03-01T00:00:00Z"}):
            args = {"amount": 100, "expire": "2026-02-01T00:00:00Z", "rid": "g-a"}
            args.update(kwargs)
            with self.assertRaises(service.ServiceError) as ctx:
                self.grant(**args)
            self.assertEqual(ctx.exception.code, "request_id_param_mismatch")

    def test_non_positive_amount_is_invalid(self):
        for bad in (0, -5):
            with self.assertRaises(service.ServiceError) as ctx:
                self.grant(bad, rid=f"bad{bad}")
            self.assertEqual(ctx.exception.code, "invalid_amount")

    def test_expired_grant_target_is_rejected_and_replayed(self):
        out = self.grant(10, expire="2026-01-01T00:00:00Z", rid="g-past")
        self.assertFalse(out["granted"])
        self.assertEqual(out["reason"], "expire_at_must_be_future")
        self.assertEqual(self.grant(10, expire="2026-01-01T00:00:00Z", rid="g-past"), out)

    # ------------------------------------------------------------ 计费开启

    def test_unbilled_org_keeps_old_rules(self):
        """没有开启计费的组织继续按原规则使用，不产生任何扣费。"""
        self.ent(seats=2)
        s = self.admit()
        self.assertTrue(s["admitted"])
        self.advance_us(1000)
        self.assertEqual(budget.usage_totals(self.conn, "acme")["units"], 0)
        # 席位仍按原规则，会话保留
        self.assertEqual(self.sessions()[0]["status"], "active")
        self.assertIsNone(budget.billing_since(self.conn, "acme"))

    def test_first_grant_enables_billing_from_that_moment(self):
        """已有在线会话只从计费开启时算起，开启前的空闲时间不计费。"""
        self.ent(seats=2)
        s = self.admit()
        DB = 10 ** 6
        service.advance_clock(self.conn, delta_seconds=100)   # 开启前 100 秒
        self.grant(DB, rid="g1")
        self.assertEqual(budget.billing_since(self.conn, "acme"),
                         db.get_clock(self.conn))
        self.advance_us(5)
        usage = budget.session_usage(self.conn, s["session_id"])
        # 只算了开启后的 5 微秒，标准会话 5 单位
        self.assertEqual(sum(u["units"] for u in usage), 5)
        self.assertEqual(service.get_session_detail(
            self.conn, s["session_id"])["budget_units"], 5)

    # ------------------------------------------------------------ 扣费规则

    def test_rates_and_earliest_expiry_first(self):
        """标准 1/µs、高级 3/µs；先扣最早到期，跨多笔额度。"""
        self.ent(tier="standard", seats=5)
        self.ent(tier="premium", seats=5)
        g1 = self.grant(4, expire="2026-02-01T00:00:00Z", rid="g1")
        g2 = self.grant(100, expire="2026-03-01T00:00:00Z", rid="g2")
        pre = self.admit("premium", "r-pre")     # 3 单位/微秒
        self.advance_us(3)                        # 一共 9 单位：g1 扣 4，g2 扣 5
        grants = {g["grant_id"]: g for g in budget.list_grants(self.conn, "acme")}
        self.assertEqual(grants[g1["grant_id"]]["used"], 4)
        self.assertEqual(grants[g2["grant_id"]]["used"], 5)
        usage = budget.session_usage(self.conn, pre["session_id"])
        self.assertEqual(sum(u["units"] for u in usage), 9)
        # 跨额度：同一会话的用量拆在两笔额度上
        self.assertEqual({u["grant_id"] for u in usage},
                         {g1["grant_id"], g2["grant_id"]})

    def test_within_microsecond_priority_attribution(self):
        """同一微秒内按既有优先次序归属：先高级后标准。"""
        self.ent(tier="standard", seats=5)
        self.ent(tier="premium", seats=5)
        g1 = self.grant(3, expire="2026-02-01T00:00:00Z", rid="g1")
        g2 = self.grant(100, expire="2026-03-01T00:00:00Z", rid="g2")
        pre = self.admit("premium", "r-pre")     # 3 单位/微秒
        std = self.admit("standard", "r-std")    # 1 单位/微秒
        self.advance_us(1)                        # 一微秒共 4 单位
        # g1 只有 3：全部归高级会话（高级先于标准），标准会话从 g2 扣 1
        pre_usage = budget.session_usage(self.conn, pre["session_id"])
        std_usage = budget.session_usage(self.conn, std["session_id"])
        self.assertEqual([(u["grant_id"], u["units"]) for u in pre_usage],
                         [(g1["grant_id"], 3)])
        self.assertEqual([(u["grant_id"], u["units"]) for u in std_usage],
                         [(g2["grant_id"], 1)])
        # 扣费不改变任何会话的 active 状态（余额仍剩 99）
        for s in (pre, std):
            self.assertEqual(service.get_session_detail(
                self.conn, s["session_id"])["status"], "active")

    # ------------------------------------------------------------ 预算不足停用

    def test_budget_stops_online_session_even_with_spare_seats(self):
        """仍有并行席位，预算用完也要停掉付不起的在线会话（明确终态与时间）。"""
        self.ent(tier="standard", seats=5)       # 席位充足
        self.grant(10, rid="g1")
        s = self.admit()
        self.assertTrue(s["admitted"])
        out = self.advance_us(20)                # 10 单位只够 10 微秒
        self.assertEqual(len(out["evicted"]), 1)
        detail = service.get_session_detail(self.conn, s["session_id"])
        self.assertEqual(detail["status"], "revoked")
        self.assertEqual(detail["end_reason"], "budget_exhausted")
        self.assertEqual(detail["ended_at"],
                         db.normalize_ts("2026-01-01T00:00:00.000010+00:00"))
        self.assertEqual(detail["budget_units"], 10)
        # 停用时间正好在余额耗尽处，不是推进终点；不再续用
        renew = service.renew_session(
            self.conn, s["session_id"], s["credential"]["secret"], "rn")
        self.assertFalse(renew["ok"])
        self.assertEqual(renew["error"], "session_revoked")
        # 没有借出超出余额的任何单位
        self.assertEqual(budget.usage_totals(self.conn, "acme")["units"], 10)
        self.assertEqual(budget.usage_totals(self.conn, "acme")["unfunded_units"], 0)
        # 补发额度也不复活
        self.grant(100, rid="g2")
        self.assertEqual(service.get_session_detail(
            self.conn, s["session_id"])["status"], "revoked")

    def test_admission_requires_paying_next_microsecond(self):
        """新准入同样要付得起接下来 1 微秒；付不起的不占席位/预算，后面的仍可考虑。

        准入按既有优先顺序逐个考虑：高级先于标准，因此付不起的高级申请
        不会挡住后面付得起的标准会话。
        """
        self.ent(tier="standard", seats=3)
        self.ent(tier="premium", seats=3)
        self.grant(2, rid="g1")
        s1 = self.admit("standard", "r1")       # 1 单位/µs
        s2 = self.admit("premium", "r2")        # 需 3/µs，付不起，被挡下
        s3 = self.admit("standard", "r3")       # 累计 2/µs，仍付得起
        s4 = self.admit("standard", "r4")       # 3/µs > 2，付不起
        self.assertTrue(s1["admitted"])
        self.assertFalse(s2["admitted"])
        self.assertEqual(s2["reason"], "budget_exhausted")
        self.assertTrue(s3["admitted"])
        self.assertFalse(s4["admitted"])
        self.assertEqual(s4["reason"], "budget_exhausted")
        # 被拒的高级申请没有占住席位：补额度后高级申请仍可进入
        self.grant(10, rid="g2")
        s5 = self.admit("premium", "r5")
        self.assertTrue(s5["admitted"])

    def test_close_releases_only_future_usage(self):
        """主动关闭不改变已经发生的扣费，只影响未来用量。"""
        self.ent(seats=2)
        self.grant(100, rid="g1")
        s1 = self.admit("standard", "r1")
        s2 = self.admit("standard", "r2")
        self.advance_us(10)                      # 两会话共 20 单位
        service.close_session(self.conn, s1["session_id"],
                              s1["credential"]["secret"], "c1")
        self.assertEqual(service.get_session_detail(
            self.conn, s1["session_id"])["budget_units"], 10)
        self.advance_us(10)                      # 只剩 s2，共再 10 单位
        total = budget.usage_totals(self.conn, "acme")["units"]
        self.assertEqual(total, 30)
        self.assertEqual(service.get_session_detail(
            self.conn, s2["session_id"])["budget_units"], 20)

    # ------------------------------------------------------------ 边界与等价性

    def test_expiry_boundary_stops_session_exactly_then(self):
        self.ent(seats=2)
        self.grant(1000, expire="2026-01-01T00:00:00.000005+00:00", rid="g1")
        self.grant(1000, expire="2026-02-01T00:00:00Z", rid="g2")
        s = self.admit()
        self.advance_us(10)
        detail = service.get_session_detail(self.conn, s["session_id"])
        # 短额度在 5µs 到期，余额仍够（还有 g2），所以会话不因预算停用
        self.assertEqual(detail["status"], "active")
        grants = {g["grant_id"]: g for g in budget.list_grants(self.conn, "acme")}
        short = [g for g in grants.values()
                 if g["expire_at"].startswith("2026-01-01T00:00:00.000005")][0]
        self.assertEqual(short["used"], 5)
        self.assertEqual(short["expired_unused"], 995)

    def test_seat_gap_does_not_revive_across_long_advance(self):
        """跨过无席位的空档：即使终点又有席位，原会话也不能继续运行。"""
        self.ent(seats=1, end="2026-01-01T00:00:00.000010+00:00")
        self.ent(seats=1, start="2026-01-01T00:00:00.000090+00:00", end=LONG)
        self.grant(10 ** 9, expire=LONG, rid="g1")
        s = self.admit()
        self.assertTrue(s["admitted"])
        out = self.advance_us(100)
        detail = service.get_session_detail(self.conn, s["session_id"])
        self.assertEqual(detail["status"], "revoked")
        self.assertEqual(detail["end_reason"], "no_any_capacity")
        # 停用发生在权益到期那一刻，不是推进终点
        self.assertEqual(detail["ended_at"],
                         db.normalize_ts("2026-01-01T00:00:00.000010+00:00"))
        # 新席位空档里没有对已停用会话多扣费：只扣到停用时刻
        self.assertEqual(sum(u["units"] for u in
                             budget.session_usage(self.conn, s["session_id"])), 10)
        self.assertEqual(out["charged_units"], 10)

    def test_one_shot_vs_split_advance_identical(self):
        """一次跨多天与分段推进（中间无其它操作）终态、停用时间、扣费一致。"""

        def run(split: bool):
            conn = db.connect(str(Path(tempfile.mkdtemp()) / "eq.db"))
            db.init_db(conn)
            conn.execute("UPDATE clock SET now_iso=? WHERE id=1",
                         (db.normalize_ts(EPOCH),))
            service.create_entitlement(conn, "o", "standard", 5, EPOCH, LONG)
            service.create_entitlement(conn, "o", "premium", 2, EPOCH, LONG)
            budget.grant(conn, "o", 1000, "2026-01-15T00:00:00Z", "g1")
            budget.grant(conn, "o", 5000, "2026-02-01T00:00:00Z", "g2")
            for i, t in enumerate(["standard", "premium", "standard", "premium"]):
                service.admit_session(conn, "o", t, f"r{i}")
            # 01-10 席位收缩到 1 标准、0 高级，02-05 恢复
            service.create_entitlement(
                conn, "o", "standard", 0, "2026-01-10T00:00:00Z", "2026-02-05T00:00:00Z")
            if split:
                for t in ("2026-01-03T00:00:00Z", "2026-01-10T00:00:00Z",
                          "2026-01-20T00:00:00Z", "2026-02-05T00:00:00Z"):
                    service.advance_clock(conn, to_iso=t)
            else:
                service.advance_clock(conn, to_iso="2026-02-05T00:00:00Z")
            state = [(s["status"], s["end_reason"], s["ended_at"])
                     for s in sorted(service.list_org_sessions(conn, "o"),
                                     key=lambda s: s["created_seq"])]
            usage = [(u["tier"], u["start_at"], u["end_at"], u["units"],
                      self._grant_key(u["grant_id"])) for u in
                     sorted(budget.usage(conn, "o"), key=lambda u: (
                         u["start_at"], u["tier"], u["end_at"], u["units"]))]
            grants = [(g["amount"], g["used"], g["expire_at"], g["issued_at"])
                      for g in sorted(budget.list_grants(conn, "o"),
                                      key=lambda g: (g["issued_at"], g["expire_at"]))]
            totals = budget.usage_totals(conn, "o")
            summary = budget.summary(conn, "o")["totals"]
            conn.close()
            return state, usage, totals, summary, grants

        one = run(False)
        split = run(True)
        self.assertEqual(one[0], split[0])                 # 终态与停用时间
        self.assertEqual(one[2], split[2])                 # 扣费合计
        self.assertEqual(one[3], split[3])                 # 每笔额度已用/过期
        self.assertEqual(one[4], split[4])
        # 账目允许不同分段，但「会话 × 额度」的用量归属必须一致
        self.assertEqual(one[1], split[1])

    @staticmethod
    def _grant_key(grant_id):
        # 两次运行各自生成随机 grant_id；按发放定义把 ID 归一成序号
        return None if grant_id is None else grant_id[:6] == "grant_"

    def test_unbilled_org_not_charged_while_billable_org_is(self):
        """同一时钟推进：计费组织按时长扣费，未计费组织不多扣。"""
        for org in ("bill", "free"):
            self.ent(org=org, seats=1)
        budget.grant(self.conn, "bill", 10 ** 9, LONG, "g")
        sb = service.admit_session(self.conn, "bill", "standard", "b1")
        sf = service.admit_session(self.conn, "free", "standard", "f1")
        self.advance_us(30)
        self.assertEqual(budget.usage_totals(self.conn, "bill")["units"], 30)
        self.assertEqual(budget.usage_totals(self.conn, "free")["units"], 0)
        # 都不会因此停用（余额/席位充足）
        self.assertEqual(service.get_session_detail(
            self.conn, sb["session_id"])["status"], "active")
        self.assertEqual(service.get_session_detail(
            self.conn, sf["session_id"])["status"], "active")

    def test_seat_gap_without_budget_still_boundary_stops(self):
        """未开启计费的组织也按真实边界停用跨空档的会话（不改变原席位规则）。"""
        self.ent(seats=1, end="2026-01-01T00:00:00.000010+00:00")
        self.ent(seats=1, start="2026-01-01T00:00:00.000090+00:00", end=LONG)
        s = self.admit()
        self.assertTrue(s["admitted"])
        self.advance_us(100)
        detail = service.get_session_detail(self.conn, s["session_id"])
        self.assertEqual(detail["status"], "revoked")
        self.assertEqual(detail["ended_at"],
                         db.normalize_ts("2026-01-01T00:00:00.000010+00:00"))
        self.assertEqual(budget.usage_totals(self.conn, "acme")["units"], 0)

    def test_no_per_microsecond_loop_for_long_advance(self):
        """跨多天推进不能逐微秒模拟：段数应与边界数量同阶，而不是微秒数。"""
        self.ent(seats=5)
        self.grant(10 ** 12, expire="2026-02-01T00:00:00Z", rid="g1")
        for i in range(3):
            self.admit("standard", f"r{i}")
        out = service.advance_clock(self.conn, delta_seconds=86400 * 3)
        self.assertTrue(out["advanced"])
        self.assertLess(out["segments"], 100)      # 何止 3 天 = 2.6e11 微秒
        self.assertEqual(budget.usage_totals(self.conn, "acme")["units"],
                         3 * 86400 * 3 * 10 ** 6)

    # ------------------------------------------------------------ 查询

    def test_summary_reports_available_used_and_expired_unused(self):
        self.ent(seats=2)
        g1 = self.grant(10, expire="2026-01-01T00:00:00.000020+00:00", rid="g1")
        self.grant(50, expire="2026-02-01T00:00:00Z", rid="g2")
        self.admit()
        self.advance_us(30)
        summary = budget.summary(self.conn, "acme")
        grants = {g["grant_id"]: g for g in summary["grants"]}
        # 短额度用满 10 后到期；长额度扣了 20
        self.assertEqual(grants[g1["grant_id"]]["used"], 10)
        self.assertEqual(grants[g1["grant_id"]]["available"], 0)
        self.assertEqual(grants[g1["grant_id"]]["expired_unused"], 0)
        self.assertEqual(summary["totals"]["used"], 30)
        self.assertEqual(summary["totals"]["available"], 30)
        self.assertTrue(summary["billing_enabled"])

    def test_expired_unused_is_written_off(self):
        self.ent(seats=1)
        g = self.grant(100, expire="2026-01-01T00:00:00.000010+00:00", rid="g1")
        self.admit()
        self.advance_us(20)
        row = {x["grant_id"]: x for x in budget.list_grants(self.conn, "acme")}[
            g["grant_id"]]
        self.assertEqual(row["used"], 10)
        self.assertEqual(row["expired_unused"], 90)
        self.assertEqual(row["available"], 0)


class BudgetConcurrencyCase(unittest.TestCase):
    """多进程/多连接并发、请求重送、重启不重复发放或扣费。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="budget_conc_")
        self.db_path = str(Path(self.dir) / "c.db")
        c = db.connect(self.db_path)
        db.init_db(c)
        c.execute("UPDATE clock SET now_iso=? WHERE id=1", (db.normalize_ts(EPOCH),))
        service.create_entitlement(c, "o", "standard", 10, EPOCH, LONG)
        c.close()

    def test_concurrent_same_grant_request_id_creates_one_grant(self):
        results: list[dict] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker():
            c = db.connect(self.db_path)
            try:
                barrier.wait()
                results.append(budget.grant(c, "o", 500, "2026-02-01T00:00:00Z", "same"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(len({r["grant_id"] for r in results}), 1)
        check = db.connect(self.db_path)
        self.assertEqual(
            check.execute("SELECT COUNT(*) FROM budget_grants").fetchone()[0], 1)
        # 重送（重启后新连接）仍返回同一发放
        again = budget.grant(check, "o", 500, "2026-02-01T00:00:00Z", "same")
        self.assertEqual(again["grant_id"], results[0]["grant_id"])
        check.close()

    def test_concurrent_advance_charges_once(self):
        c = db.connect(self.db_path)
        budget.grant(c, "o", 10 ** 12, LONG, "g1")
        for i in range(3):
            service.admit_session(c, "o", "standard", f"r{i}")
        c.close()

        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def worker():
            cc = db.connect(self.db_path)
            try:
                barrier.wait()
                # 各推进 1 秒到同一目标：写锁串行化，重复目标成为无操作
                service.advance_clock(cc, to_iso="2026-01-01T00:00:01Z")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                cc.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        check = db.connect(self.db_path)
        # 时钟只前进 1 秒总量（后到者目标已达成或回拨被拒/无操作），
        # 即 1e6 微秒 × 3 标准会话 = 3e6 单位，绝不重复计费
        self.assertEqual(budget.usage_totals(check, "o")["units"], 3 * 10 ** 6)
        self.assertEqual(db.get_clock(check),
                         db.normalize_ts("2026-01-01T00:00:01+00:00"))
        check.close()

    def test_two_processes_same_grant_request_issue_once(self):
        """两个真实 OS 进程同时用同一 request_id 发放：只产生一笔、返回同一 ID。"""
        import multiprocessing

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        p1 = ctx.Process(target=_process_grant, args=(self.db_path, queue))
        p2 = ctx.Process(target=_process_grant, args=(self.db_path, queue))
        p1.start(); p2.start()
        p1.join(30); p2.join(30)
        self.assertEqual(p1.exitcode, 0)
        self.assertEqual(p2.exitcode, 0)
        out = [queue.get_nowait() for _ in range(2)]
        self.assertEqual(len({r["grant_id"] for r in out}), 1)
        check = db.connect(self.db_path)
        self.assertEqual(
            check.execute("SELECT COUNT(*) FROM budget_grants").fetchone()[0], 1)
        check.close()

    def test_two_processes_advance_same_target_charge_once(self):
        """两个进程同时把时钟推进到同一目标：只扣一次费、只记一次账。"""
        import multiprocessing

        c = db.connect(self.db_path)
        budget.grant(c, "o", 10 ** 12, LONG, "g1")
        for i in range(2):
            service.admit_session(c, "o", "standard", f"r{i}")
        c.close()
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        p1 = ctx.Process(target=_process_advance, args=(self.db_path, queue))
        p2 = ctx.Process(target=_process_advance, args=(self.db_path, queue))
        p1.start(); p2.start()
        p1.join(30); p2.join(30)
        self.assertEqual((p1.exitcode, p2.exitcode), (0, 0))
        [queue.get_nowait() for _ in range(2)]
        check = db.connect(self.db_path)
        totals = budget.usage_totals(check, "o")
        self.assertEqual(totals["units"], 2 * 10 ** 6)
        self.assertEqual(totals["unfunded_units"], 0)
        clock = db.get_clock(check)
        self.assertEqual(clock, db.normalize_ts("2026-01-01T00:00:01+00:00"))
        # 重启后再次推进同一目标不会重复记账
        out = service.advance_clock(check, to_iso="2026-01-01T00:00:01Z")
        self.assertFalse(out["advanced"])
        self.assertEqual(budget.usage_totals(check, "o")["units"], 2 * 10 ** 6)
        check.close()


def _process_grant(db_path: str, queue) -> None:
    c = db.connect(db_path)
    try:
        queue.put(budget.grant(c, "o", 500, "2026-02-01T00:00:00Z", "same"))
    finally:
        c.close()


def _process_advance(db_path: str, queue) -> None:
    c = db.connect(db_path)
    try:
        queue.put(service.advance_clock(c, to_iso="2026-01-01T00:00:01Z"))
    finally:
        c.close()


if __name__ == "__main__":
    unittest.main()
