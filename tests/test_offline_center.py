"""中心侧离线借席单元测试（标准库 unittest，环境无 pytest 时亦可运行）。

运行： PYTHONPATH=/workspace python3 -m unittest tests.test_offline_center -v
"""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

from app import crypto, db, offline, service

EPOCH = "2026-01-01T00:00:00Z"
SIGN_KEY = "test-signing-key-0123456789abcdef"


class CenterCase(unittest.TestCase):
    def setUp(self):
        os.environ["LICENSE_SIGNING_KEY"] = SIGN_KEY
        self.tmp = tempfile.mkdtemp(prefix="offline_center_")
        self.conn = db.connect(str(Path(self.tmp) / "center.db"))
        db.init_db(self.conn)
        self.conn.execute("UPDATE clock SET now_iso=? WHERE id=1",
                          (db.normalize_ts(EPOCH),))
        # 固化签名密钥（环境变量优先）
        with db.transaction(self.conn):
            self.key = crypto.get_or_create_center_key(self.conn)

    def tearDown(self):
        self.conn.close()

    def ent(self, org, tier, seats, start=EPOCH, end="2027-01-01T00:00:00Z"):
        return service.create_entitlement(self.conn, org, tier, seats, start, end)

    def issue(self, org="acme", site="site-A", std=0, pre=0,
              expire="2026-02-01T00:00:00Z", rid="req-1"):
        return offline.issue_package(self.conn, org, site, std, pre, expire, rid)

    # ------------------------------------------------------------ 签发基础

    def test_issue_success_and_reservation_blocks_online(self):
        self.ent("acme", "premium", 1)
        out = self.issue(pre=1, rid="r1")
        self.assertTrue(out["issued"])
        # 预留生效：同一份高级额度不能再给中心在线会话
        admit = service.admit_session(self.conn, "acme", "premium", "a1")
        self.assertFalse(admit["admitted"])
        self.assertEqual(admit["reason"], "no_premium_capacity")
        # 标准会话也不能挪用（只剩 0 个总席位）
        admit_std = service.admit_session(self.conn, "acme", "standard", "a2")
        self.assertFalse(admit_std["admitted"])

    def test_issue_idempotent_same_params_same_result(self):
        self.ent("acme", "standard", 2)
        first = self.issue(std=1, rid="r-idem")
        second = self.issue(std=1, rid="r-idem")
        self.assertEqual(first["package_id"], second["package_id"])
        self.assertEqual(first["envelope"], second["envelope"])
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM offline_packages WHERE request_id='r-idem'").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_issue_retry_changed_params_rejected(self):
        self.ent("acme", "standard", 5)
        self.issue(std=1, rid="r-chg")
        with self.assertRaises(service.ServiceError) as ctx:
            self.issue(std=2, rid="r-chg")
        self.assertEqual(ctx.exception.code, "request_id_param_mismatch")
        # 换站点同样拒绝
        with self.assertRaises(service.ServiceError):
            self.issue(std=1, site="site-B", rid="r-chg")

    def test_rejected_request_is_persisted_and_replays(self):
        # 没有任何权益：可行性失败
        out = self.issue(pre=1, rid="r-bad")
        self.assertFalse(out["issued"])
        again = self.issue(pre=1, rid="r-bad")
        self.assertEqual(again, out)
        self.conn.execute("SELECT COUNT(*) FROM offline_packages").fetchone()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM offline_packages").fetchone()[0], 0)

    def test_issue_must_not_displace_current_online_session(self):
        self.ent("acme", "premium", 1)
        online = service.admit_session(self.conn, "acme", "premium", "online-1")
        self.assertTrue(online["admitted"])
        # 唯一高级席已被在线会话占用：签发不能挤掉它
        out = self.issue(pre=1, rid="r-disp")
        self.assertFalse(out["issued"])
        self.assertEqual(out["reason"], "would_displace_online_session")
        # 在线会话仍然活跃
        detail = service.get_session_detail(self.conn, online["session_id"])
        self.assertEqual(detail["status"], "active")

    def test_issue_standard_can_use_spare_premium_without_blocking_online(self):
        # 2 高级席：1 在线高级会话占用 1；借 1 标准席可落在富余高级席，
        # 且在线会话不受影响（离线预留不得挡住本可同时容纳的在线会话）
        self.ent("acme", "premium", 2)
        online = service.admit_session(self.conn, "acme", "premium", "online-1")
        out = self.issue(std=1, rid="r-spare")
        self.assertTrue(out["issued"])
        detail = service.get_session_detail(self.conn, online["session_id"])
        self.assertEqual(detail["status"], "active")
        # 此时总余量为 0：新在线标准会话进不来
        self.assertFalse(
            service.admit_session(self.conn, "acme", "standard", "s2")["admitted"])

    def test_premium_seats_only_backed_by_premium(self):
        # 只有标准权益：高级席借席必须失败
        self.ent("acme", "standard", 5)
        out = self.issue(pre=1, rid="r-preonly")
        self.assertFalse(out["issued"])
        self.assertEqual(out["reason"], "offline_premium_cannot_be_honored")

    # ------------------------------------------------------------ 多包与跨期

    def test_overlapping_packages_checked_together(self):
        self.ent("acme", "standard", 2)
        self.assertTrue(self.issue(std=1, expire="2026-03-01T00:00:00Z", rid="p1")["issued"])
        # 重叠期再借 2 席：同一时刻需要 3 席，只有 2
        out = self.issue(std=2, expire="2026-02-15T00:00:00Z", rid="p2")
        self.assertFalse(out["issued"])
        self.assertEqual(out["reason"], "offline_capacity_cannot_be_honored")
        # 不重叠（第一个包到期后才生效不行——签发即生效；改成短窗口在 p1 之后不行，
        # 因为 issued_at 固定为当前）。这里验证同容量接续窗口的包在 p1 到期点之后
        # 的需求不叠加：借 2 席、窗口完全落在 p1 到期之后是不允许的（签发即生效），
        # 改为验证 p1 交回后可再借（交回测试覆盖）。

    def test_entitlements_can_relay_across_borrow_window(self):
        """不要求同一份权益覆盖整个借期：两份接续权益共同兑现即可。"""
        self.ent("acme", "standard", 1, EPOCH, "2026-02-01T00:00:00Z")
        self.ent("acme", "standard", 1, "2026-02-01T00:00:00Z", "2027-01-01T00:00:00Z")
        out = self.issue(std=1, expire="2026-03-01T00:00:00Z", rid="relay")
        self.assertTrue(out["issued"])

    def test_gap_in_entitlement_coverage_rejects(self):
        self.ent("acme", "standard", 1, EPOCH, "2026-02-01T00:00:00Z")
        # 2 月 10 日才接续，中间 9 天空窗
        self.ent("acme", "standard", 1, "2026-02-10T00:00:00Z", "2027-01-01T00:00:00Z")
        out = self.issue(std=1, expire="2026-03-01T00:00:00Z", rid="gap")
        self.assertFalse(out["issued"])

    # ------------------------------------------------------------ 权益守卫

    def test_modify_entitlement_guard_rejects_then_allows_with_relay(self):
        self.ent("acme", "premium", 2)
        pkg = self.issue(pre=2, expire="2026-03-01T00:00:00Z", rid="g1")
        self.assertTrue(pkg["issued"])
        ent_id = self.conn.execute(
            "SELECT ent_id FROM entitlements WHERE org='acme'").fetchone()[0]
        # 降到 1 席：无法兑现 2 个高级预留 → 整笔拒绝
        with self.assertRaises(service.ServiceError) as ctx:
            service.modify_entitlement(self.conn, ent_id, seats=1)
        self.assertIn("offline", ctx.exception.code)
        # 加一份接续/并行高级权益后，修改可被接替 → 允许
        self.ent("acme", "premium", 1)
        out = service.modify_entitlement(self.conn, ent_id, seats=1)
        self.assertIsInstance(out["evicted"], list)

    def test_revoke_entitlement_guard(self):
        self.ent("acme", "standard", 2)
        self.assertTrue(self.issue(std=1, rid="g2")["issued"])
        ent_id = self.conn.execute(
            "SELECT ent_id FROM entitlements WHERE org='acme'").fetchone()[0]
        with self.assertRaises(service.ServiceError):
            service.revoke_entitlement(self.conn, ent_id, "合同终止")
        # 权益仍在
        status = self.conn.execute(
            "SELECT status FROM entitlements WHERE ent_id=?", (ent_id,)).fetchone()[0]
        self.assertEqual(status, "active")

    def test_modify_allowed_when_other_entitlement_covers_remainder(self):
        first = self.ent("acme", "standard", 1, EPOCH, "2026-02-01T00:00:00Z")
        # 另一份已生效的权益可在撤销后立即接替整个剩余借期
        self.ent("acme", "standard", 1, EPOCH, "2027-01-01T00:00:00Z")
        self.assertTrue(
            self.issue(std=1, expire="2026-03-01T00:00:00Z", rid="g3")["issued"])
        first_ent = first["entitlement"]["ent_id"]
        # 撤销第一份：另一份权益已生效，剩余借期仍可兑现 → 允许
        service.revoke_entitlement(self.conn, first_ent, "提前终止首份")
        # 包仍然是 active 未交回，承诺未被破坏
        pkg_row = self.conn.execute(
            "SELECT status FROM offline_packages WHERE request_id='g3'").fetchone()
        self.assertEqual(pkg_row["status"], "active")

    def test_revoke_blocked_when_relay_starts_too_late(self):
        """接替权益若在撤销之后才生效，中间空窗无法兑现，撤销仍应拒绝。"""
        self.ent("acme", "standard", 1, EPOCH, "2026-02-01T00:00:00Z")
        self.ent("acme", "standard", 1, "2026-01-31T00:00:00Z", "2027-01-01T00:00:00Z")
        self.assertTrue(
            self.issue(std=1, expire="2026-03-01T00:00:00Z", rid="g3b")["issued"])
        ent_id = self.conn.execute(
            "SELECT ent_id FROM entitlements "
            "WHERE expire_at LIKE '2026-02-01%' LIMIT 1").fetchone()[0]
        with self.assertRaises(service.ServiceError):
            service.revoke_entitlement(self.conn, ent_id, "提前终止")

    # ------------------------------------------------------------ 交回

    def _return_credential(self, issue_out):
        return issue_out["return_credential"]

    def test_return_releases_quota_and_replay_is_idempotent(self):
        self.ent("acme", "premium", 1)
        pkg = self.issue(pre=1, rid="ret1")
        # 交回前在线高级会话进不来
        self.assertFalse(
            service.admit_session(self.conn, "acme", "premium", "b1")["admitted"])

        cred = self._return_credential(pkg)
        out = offline.return_package(self.conn, cred, "op-ret")
        self.assertTrue(out["returned"])
        self.assertTrue(out["released"])

        # 重送：不重复释放
        replay = offline.return_package(self.conn, copy.deepcopy(cred), "op-ret-2")
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM offline_returns").fetchone()[0], 1)

        # 额度重新可分配
        admitted = service.admit_session(self.conn, "acme", "premium", "b2")
        self.assertTrue(admitted["admitted"])

    def test_package_file_is_not_a_return_credential(self):
        self.ent("acme", "standard", 1)
        pkg = self.issue(std=1, rid="ret2")
        # 拿包信封当交回凭据 → 类型不符拒绝
        with self.assertRaises(service.ServiceError) as ctx:
            offline.return_package(self.conn, pkg["envelope"])
        self.assertEqual(ctx.exception.code, "invalid_return_credential")

    def test_tampered_credential_rejected(self):
        self.ent("acme", "standard", 1)
        pkg = self.issue(std=1, rid="ret3")
        cred = copy.deepcopy(self._return_credential(pkg))
        cred["payload"]["return_secret"] = "forged"
        with self.assertRaises(service.ServiceError) as ctx:
            offline.return_package(self.conn, cred)
        self.assertEqual(ctx.exception.code, "invalid_return_credential")
        # 签名本身被改
        cred2 = copy.deepcopy(self._return_credential(pkg))
        cred2["signature"] = "00" * 32
        with self.assertRaises(service.ServiceError):
            offline.return_package(self.conn, cred2)

    def test_return_after_expiry_records_but_does_not_double_count(self):
        self.ent("acme", "standard", 1)
        pkg = self.issue(std=1, expire="2026-01-15T00:00:00Z", rid="ret4")
        service.advance_clock(self.conn, to_iso="2026-01-16T00:00:00Z")
        # 到期后预留已不占容量
        self.assertTrue(
            service.admit_session(self.conn, "acme", "standard", "after-exp")["admitted"])
        out = offline.return_package(self.conn, self._return_credential(pkg))
        self.assertTrue(out["returned"])
        self.assertFalse(out["released"])

    # ------------------------------------------------------------ 到期释放

    def test_expiry_frees_reservation_along_timeline(self):
        self.ent("acme", "standard", 1)
        self.issue(std=1, expire="2026-01-15T00:00:00Z", rid="exp1")
        self.assertFalse(
            service.admit_session(self.conn, "acme", "standard", "x1")["admitted"])
        service.advance_clock(self.conn, to_iso="2026-01-15T00:00:00Z")
        # 左闭右开：截止时刻包立即失效，席位回来
        admitted = service.admit_session(self.conn, "acme", "standard", "x2")
        self.assertTrue(admitted["admitted"])

    def test_envelope_verifies_but_tamper_fails(self):
        self.ent("acme", "standard", 1)
        pkg = self.issue(std=1, rid="sig1")
        # 合法信封可验
        payload = crypto.verify(self.key, pkg["envelope"])
        self.assertEqual(payload["package_id"], pkg["package_id"])
        # 篡改席位数字
        bad = copy.deepcopy(pkg["envelope"])
        bad["payload"]["standard_seats"] = 99
        with self.assertRaises(crypto.InvalidSignature):
            crypto.verify(self.key, bad)

    # ------------------------------------------------------------ 在线再平衡优先级

    def test_rebalance_priority_preserved_under_reservation(self):
        """预留扣减后，在线会话仍按「高级优先、同类先到先得」再平衡。"""
        # 3 高级席：离线预留 1，在线可用 2
        self.ent("acme", "premium", 3)
        self.assertTrue(self.issue(pre=1, rid="rb1")["issued"])
        # 一个标准会话先借走高级席（标准可借高级）
        std_first = service.admit_session(self.conn, "acme", "standard", "rb-s1")
        self.assertTrue(std_first["admitted"])
        # 两个高级会话先后到达
        pre_1 = service.admit_session(self.conn, "acme", "premium", "rb-p1")
        pre_2 = service.admit_session(self.conn, "acme", "premium", "rb-p2")
        self.assertTrue(pre_1["admitted"] and pre_2["admitted"])
        # 在线 2 高级 + 1 借高级的标准 = 3，刚好打满在线余量
        statuses = {s["session_id"]: s["status"]
                    for s in service.list_org_sessions(self.conn, "acme")}
        self.assertEqual(statuses[pre_1["session_id"]], "active")
        self.assertEqual(statuses[pre_2["session_id"]], "active")
        # 第三个高级会话：容量不足被拒，且不会穿透到离线预留
        pre_3 = service.admit_session(self.conn, "acme", "premium", "rb-p3")
        self.assertFalse(pre_3["admitted"])
        self.assertEqual(pre_3["reason"], "no_premium_capacity")
        # 预留仍然完整：离线包还是 active
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM offline_packages WHERE request_id='rb1'"
            ).fetchone()["status"], "active")

    def test_multiple_overlapping_packages_pool_reservations(self):
        self.ent("acme", "standard", 3)
        p1 = self.issue(std=1, expire="2026-03-01T00:00:00Z", rid="mp1")
        p2 = self.issue(std=1, expire="2026-02-01T00:00:00Z", rid="mp2")
        self.assertTrue(p1["issued"] and p2["issued"])
        # 两包重叠期合计预留 2，标准池只剩 1 给在线
        a1 = service.admit_session(self.conn, "acme", "standard", "mp-a1")
        a2 = service.admit_session(self.conn, "acme", "standard", "mp-a2")
        self.assertTrue(a1["admitted"])
        self.assertFalse(a2["admitted"])
        # 推进到短包到期：预留降到 1，在线余量回到 2
        service.advance_clock(self.conn, to_iso="2026-02-01T00:00:00Z")
        a3 = service.admit_session(self.conn, "acme", "standard", "mp-a3")
        self.assertTrue(a3["admitted"])


if __name__ == "__main__":
    unittest.main()
