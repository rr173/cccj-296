"""站点侧离线包使用单元测试（标准库 unittest）。

运行： PYTHONPATH=/workspace python3 -m unittest tests.test_offline_site -v
"""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

from app import crypto, db, offline, service, site_db, site_service

EPOCH = "2026-01-01T00:00:00Z"
SIGN_KEY = "test-signing-key-0123456789abcdef"
SITE = "site-A"


class SiteCase(unittest.TestCase):
    def setUp(self):
        os.environ["LICENSE_SIGNING_KEY"] = SIGN_KEY
        self.tmp = tempfile.mkdtemp(prefix="offline_site_")
        # 中心库
        self.center = db.connect(str(Path(self.tmp) / "center.db"))
        db.init_db(self.center)
        self.center.execute("UPDATE clock SET now_iso=? WHERE id=1",
                            (db.normalize_ts(EPOCH),))
        with db.transaction(self.center):
            self.key = crypto.get_or_create_center_key(self.center)
        # 站点库（独立文件）
        self.site = site_db.connect(str(Path(self.tmp) / "site.db"))
        site_db.init_db(self.site)
        self.site.execute("UPDATE clock SET now_iso=? WHERE id=1",
                          (db.normalize_ts(EPOCH),))

    def tearDown(self):
        self.center.close()
        self.site.close()

    def make_pkg(self, std=1, pre=1, site=SITE, expire="2026-02-01T00:00:00Z",
                 rid="req-1", org="acme"):
        service.create_entitlement(self.center, org, "standard",
                                   std + 1, EPOCH, "2027-01-01T00:00:00Z")
        service.create_entitlement(self.center, org, "premium",
                                   pre + 1, EPOCH, "2027-01-01T00:00:00Z")
        return offline.issue_package(self.center, org, site, std, pre, expire, rid)

    def import_pkg(self, issue_out):
        return site_service.import_package(
            self.site, copy.deepcopy(issue_out["envelope"]), SITE, self.key)

    # ------------------------------------------------------------ 导入安全

    def test_import_valid_package(self):
        pkg = self.make_pkg()
        out = self.import_pkg(pkg)
        self.assertTrue(out["imported"])
        self.assertEqual(out["package_id"], pkg["package_id"])

    def test_import_rejects_other_site(self):
        pkg = self.make_pkg(site="site-B", rid="r-other")
        with self.assertRaises(site_service.SiteError) as ctx:
            site_service.import_package(
                self.site, copy.deepcopy(pkg["envelope"]), SITE, self.key)
        self.assertEqual(ctx.exception.code, "package_site_mismatch")

    def test_import_rejects_tampered_envelope(self):
        pkg = self.make_pkg(rid="r-tamper")
        env = copy.deepcopy(pkg["envelope"])
        env["payload"]["premium_seats"] = 99
        with self.assertRaises(crypto.InvalidSignature):
            crypto.verify(self.key, env)
        with self.assertRaises(site_service.SiteError):
            site_service.import_package(self.site, env, SITE, self.key)

    def test_duplicate_import_does_not_double_capacity(self):
        pkg = self.make_pkg(std=1, pre=1, rid="r-dup")
        self.import_pkg(pkg)
        again = self.import_pkg(pkg)
        self.assertFalse(again["imported"])
        rows = self.site.execute(
            "SELECT COUNT(*) FROM imported_packages").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_import_expired_package_rejected(self):
        pkg = self.make_pkg(expire="2026-01-15T00:00:00Z", rid="r-exp-imp")
        site_service.advance_clock(self.site, to_iso="2026-01-16T00:00:00Z")
        with self.assertRaises(site_service.SiteError) as ctx:
            self.import_pkg(pkg)
        self.assertEqual(ctx.exception.code, "package_expired")

    # ------------------------------------------------------------ 准入规则

    def test_premium_session_uses_only_premium_seat(self):
        pkg = self.make_pkg(std=1, pre=1, rid="r-adm1")
        self.import_pkg(pkg)
        pid = pkg["package_id"]
        p1 = site_service.admit(self.site, pid, "premium", "p1")
        self.assertTrue(p1["admitted"])
        # 唯一高级席已占
        p2 = site_service.admit(self.site, pid, "premium", "p2")
        self.assertFalse(p2["admitted"])
        self.assertEqual(p2["reason"], "no_premium_capacity")
        # 标准席仍可用
        s1 = site_service.admit(self.site, pid, "standard", "s1")
        self.assertTrue(s1["admitted"])

    def test_standard_session_can_borrow_premium_seat(self):
        pkg = self.make_pkg(std=1, pre=1, rid="r-adm2")
        self.import_pkg(pkg)
        pid = pkg["package_id"]
        s1 = site_service.admit(self.site, pid, "standard", "s1")
        s2 = site_service.admit(self.site, pid, "standard", "s2")
        self.assertTrue(s1["admitted"] and s2["admitted"])  # 第二個借高級席
        s3 = site_service.admit(self.site, pid, "standard", "s3")
        self.assertFalse(s3["admitted"])
        # 高级会话仍受高级席保护：标准会话占了高级席时，高级申请失败而不是挤掉标准
        p1 = site_service.admit(self.site, pid, "premium", "p1")
        self.assertFalse(p1["admitted"])

    def test_admit_idempotent_same_request_id(self):
        pkg = self.make_pkg(rid="r-adm3")
        self.import_pkg(pkg)
        first = site_service.admit(self.site, pkg["package_id"], "standard", "idem")
        second = site_service.admit(self.site, pkg["package_id"], "standard", "idem")
        self.assertEqual(first["session_id"], second["session_id"])
        n = self.site.execute(
            "SELECT COUNT(*) FROM site_sessions WHERE request_id='idem'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_admit_request_id_mismatch_rejected(self):
        pkg = self.make_pkg(rid="r-adm4")
        self.import_pkg(pkg)
        site_service.admit(self.site, pkg["package_id"], "standard", "mix")
        with self.assertRaises(site_service.SiteError):
            site_service.admit(self.site, pkg["package_id"], "premium", "mix")

    def test_close_releases_seat_and_duplicate_close_is_safe(self):
        pkg = self.make_pkg(std=1, pre=0, rid="r-adm5")
        self.import_pkg(pkg)
        pid = pkg["package_id"]
        s1 = site_service.admit(self.site, pid, "standard", "c1")
        out = site_service.close(self.site, s1["session_id"], "op-close-1")
        self.assertTrue(out["released"])
        # 席位释放后新会话可进
        s2 = site_service.admit(self.site, pid, "standard", "c2")
        self.assertTrue(s2["admitted"])
        # 重复关闭不释放别人的额度
        dup = site_service.close(self.site, s1["session_id"], "op-close-2")
        self.assertFalse(dup["released"])
        active = [s for s in site_service.list_sessions(self.site, pid)
                  if s["status"] == "active"]
        self.assertEqual(len(active), 1)

    # ------------------------------------------------------------ 到期

    def test_expiry_terminates_sessions_and_blocks_new_ones(self):
        pkg = self.make_pkg(expire="2026-01-15T00:00:00Z", rid="r-adm6")
        self.import_pkg(pkg)
        pid = pkg["package_id"]
        s1 = site_service.admit(self.site, pid, "standard", "e1")
        site_service.admit(self.site, pid, "premium", "e2")
        out = site_service.advance_clock(self.site, to_iso="2026-01-15T00:00:00Z")
        # 左闭右开：截止时刻全部终止
        self.assertEqual(len(out["terminated"]), 2)
        statuses = {s["session_id"]: s["status"]
                    for s in site_service.list_sessions(self.site, pid)}
        self.assertTrue(all(v == "expired" for v in statuses.values()))
        # 不能再开新会话
        again = site_service.admit(self.site, pid, "standard", "e3")
        self.assertFalse(again["admitted"])
        self.assertEqual(again["reason"], "package_expired")

    # ------------------------------------------------------------ 提前还席

    def test_return_terminates_sessions_and_exports_credential(self):
        pkg = self.make_pkg(rid="r-ret1")
        self.import_pkg(pkg)
        pid = pkg["package_id"]
        s1 = site_service.admit(self.site, pid, "standard", "t1")
        out = site_service.return_package(self.site, pid, "op-return-1")
        self.assertTrue(out["returned"])
        self.assertEqual(out["terminated_sessions"], [s1["session_id"]])
        # 导出的凭据就是中心预签的交回凭据，可独立验签
        payload = crypto.verify(self.key, out["return_credential"])
        self.assertEqual(payload["kind"], "offline-return-credential")
        self.assertEqual(payload["package_id"], pid)
        # 会话终态
        sess = self.site.execute(
            "SELECT status FROM site_sessions WHERE session_id=?",
            (s1["session_id"],)).fetchone()
        self.assertEqual(sess["status"], "returned_terminated")

    def test_returned_package_cannot_be_reenabled_or_admit(self):
        pkg = self.make_pkg(rid="r-ret2")
        self.import_pkg(pkg)
        pid = pkg["package_id"]
        site_service.return_package(self.site, pid, "op-return-2")
        # 重新导入被拒
        with self.assertRaises(site_service.SiteError) as ctx:
            self.import_pkg(pkg)
        self.assertEqual(ctx.exception.code, "package_already_returned")
        # 再开会话被拒
        out = site_service.admit(self.site, pid, "standard", "after-ret")
        self.assertFalse(out["admitted"])
        self.assertEqual(out["reason"], "package_returned")

    def test_return_credential_is_resendable(self):
        pkg = self.make_pkg(rid="r-ret3")
        self.import_pkg(pkg)
        pid = pkg["package_id"]
        first = site_service.return_package(self.site, pid, "op-return-3")
        # 再次还席：返回同一份凭据供重送
        again = site_service.return_package(self.site, pid, "op-return-4")
        self.assertEqual(again["return_credential"], first["return_credential"])

    def test_package_envelope_is_not_return_credential(self):
        """普通包文件不能当交回凭据：它的 kind 不是 return-credential。"""
        pkg = self.make_pkg(rid="r-ret4")
        self.import_pkg(pkg)
        with self.assertRaises(service.ServiceError):
            offline.return_package(self.center, copy.deepcopy(pkg["envelope"]))

    # ------------------------------------------------------------ 端到端交回

    def test_full_borrow_use_return_cycle_frees_center_quota(self):
        # 中心只有 1 个高级席
        service.create_entitlement(self.center, "acme", "premium", 1,
                                   EPOCH, "2027-01-01T00:00:00Z")
        pkg = offline.issue_package(
            self.center, "acme", SITE, 0, 1, "2026-02-01T00:00:00Z", "e2e")
        self.assertTrue(pkg["issued"])
        # 借席期间中心在线高级会话进不来
        self.assertFalse(
            service.admit_session(self.center, "acme", "premium", "blocked")["admitted"])

        self.import_pkg(pkg)
        site_service.admit(self.site, pkg["package_id"], "premium", "local-1")
        ret = site_service.return_package(
            self.site, pkg["package_id"], "e2e-return")
        # 凭据送回中心，额度释放
        center_out = offline.return_package(
            self.center, copy.deepcopy(ret["return_credential"]), "e2e-op")
        self.assertTrue(center_out["released"])
        # 同一份额度现在可以重新分配
        admitted = service.admit_session(self.center, "acme", "premium", "freed")
        self.assertTrue(admitted["admitted"])
        # 重送交回凭据不会二次释放
        replay = offline.return_package(
            self.center, copy.deepcopy(ret["return_credential"]), "e2e-op-2")
        self.assertTrue(replay["replayed"])


if __name__ == "__main__":
    unittest.main()
