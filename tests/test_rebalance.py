"""核心再平衡规则测试：挪用、优先级、到期降额、撤销留痕。"""
from tests.conftest import active_ids, admit, make_ent

from app import service


def test_premium_entitlement_rejects_standard_borrow_blocking_premium(conn):
    """标准会话可借高级席位，但后来的高级申请必须能把席位要回来。"""
    make_ent(service, conn, "o1", "premium", 1)

    # 标准会话先占用唯一高级席位（挪用）
    std = admit(service, conn, "o1", "standard", "r-std")
    assert std["admitted"] is True
    assert std["assigned_ent_id"]  # 落在高级权益上
    assert active_ids(service, conn, "o1") == [std["session_id"]]

    # 高级申请到达：再平衡后标准会话被撤销，高级会话保留
    pre = admit(service, conn, "o1", "premium", "r-pre")
    assert pre["admitted"] is True
    sessions = {s["session_id"]: s for s in service.list_org_sessions(conn, "o1")}
    assert sessions[pre["session_id"]]["status"] == "active"
    assert sessions[std["session_id"]]["status"] == "revoked"
    detail = service.get_session_detail(conn, std["session_id"])
    assert detail["end_reason"] == "no_any_capacity"
    assert detail["slot_history"][-1]["release_reason"] == "no_any_capacity"


def test_premium_session_never_uses_standard_seats(conn):
    make_ent(service, conn, "o2", "standard", 5)
    res = admit(service, conn, "o2", "premium", "r1")
    assert res["admitted"] is False
    # 被拒会话仍是可追溯的终态记录
    detail = service.get_session_detail(conn, res["session_id"])
    assert detail["status"] == "revoked"
    assert detail["admit_first_status"] == "rejected"


def test_standard_prefers_standard_then_premium(conn):
    make_ent(service, conn, "o3", "standard", 1)
    make_ent(service, conn, "o3", "premium", 1)

    s1 = admit(service, conn, "o3", "standard", "r1")
    s2 = admit(service, conn, "o3", "standard", "r2")
    assert s1["assigned_ent_id"] != s2["assigned_ent_id"]
    # 第三个无处可去
    s3 = admit(service, conn, "o3", "standard", "r3")
    assert s3["admitted"] is False
    assert s3["reason"] == "no_any_capacity"


def test_eviction_priority_premium_first_then_start_time_then_id(conn):
    """容量收缩时：先撤标准；同类按开始先后，早的留下；并列用会话 ID 破序。"""
    make_ent(service, conn, "o4", "standard", 3)
    s_early = admit(service, conn, "o4", "standard", "r-a")["session_id"]
    s_mid = admit(service, conn, "o4", "standard", "r-b")["session_id"]
    s_late = admit(service, conn, "o4", "standard", "r-c")["session_id"]

    make_ent(service, conn, "o4", "premium", 1)
    p1 = admit(service, conn, "o4", "premium", "r-p1")["session_id"]

    # 标准权益降到 1 席：保留最早的标准会话
    std_ent = conn.execute(
        "SELECT ent_id FROM entitlements WHERE org='o4' AND tier='standard'").fetchone()[0]
    out = service.modify_entitlement(conn, std_ent, seats=1)
    evicted_ids = {e["session_id"] for e in out["evicted"]}
    assert evicted_ids == {s_mid, s_late}
    assert set(active_ids(service, conn, "o4")) == {s_early, p1}

    # 再撤销高级权益：高级会话被撤，标准会话不受影响
    pre_ent = conn.execute(
        "SELECT ent_id FROM entitlements WHERE org='o4' AND tier='premium'").fetchone()[0]
    out = service.revoke_entitlement(conn, pre_ent, "合同终止")
    assert [e["session_id"] for e in out["evicted"]] == [p1]
    assert active_ids(service, conn, "o4") == [s_early]


def test_expiry_via_clock_advance_reduces_capacity(conn):
    """时间推进使权益到期 -> 降额 -> 撤销，全程不依赖睡眠。"""
    make_ent(service, conn, "o5", "standard", 2,
             start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z")
    s1 = admit(service, conn, "o5", "standard", "r1")["session_id"]
    s2 = admit(service, conn, "o5", "standard", "r2")["session_id"]
    assert len(active_ids(service, conn, "o5")) == 2

    # 未来生效的新权益（1 席），在旧权益到期同日接续
    make_ent(service, conn, "o5", "standard", 1,
             start="2026-02-01T00:00:00Z", end="2027-01-01T00:00:00Z")

    out = service.advance_clock(conn, to_iso="2026-02-01T00:00:00Z")
    evicted = {e["session_id"]: e for e in out["evicted"]}
    # 左闭右开：02-01 00:00 旧权益已失效，只剩 1 席，较晚会话被撤
    assert set(evicted) == {s2}
    assert evicted[s2]["reason"] == "no_any_capacity"
    assert active_ids(service, conn, "o5") == [s1]


def test_future_entitlement_not_active_before_effective_time(conn):
    make_ent(service, conn, "o6", "standard", 1,
             start="2026-03-01T00:00:00Z", end="2027-01-01T00:00:00Z")
    res = admit(service, conn, "o6", "standard", "r1")
    assert res["admitted"] is False

    service.advance_clock(conn, to_iso="2026-03-01T00:00:00Z")
    res2 = admit(service, conn, "o6", "standard", "r2")
    assert res2["admitted"] is True


def test_overlapping_entitlements_same_tier_pool_together(conn):
    """同组织多份重叠权益的席位相加。"""
    make_ent(service, conn, "o7", "premium", 1)
    make_ent(service, conn, "o7", "premium", 1)
    p1 = admit(service, conn, "o7", "premium", "r1")
    p2 = admit(service, conn, "o7", "premium", "r2")
    assert p1["admitted"] and p2["admitted"]
    p3 = admit(service, conn, "o7", "premium", "r3")
    assert p3["admitted"] is False


def test_revoked_session_not_revived_when_capacity_returns(conn):
    make_ent(service, conn, "o8", "standard", 1)
    s1 = admit(service, conn, "o8", "standard", "r1")["session_id"]
    s2 = admit(service, conn, "o8", "standard", "r2")
    assert s2["admitted"] is False
    revoked_id = s2["session_id"]

    # 扩容后被撤会话不会复活，必须重新申请
    ent = conn.execute("SELECT ent_id FROM entitlements WHERE org='o8'").fetchone()[0]
    service.modify_entitlement(conn, ent, seats=5)
    detail = service.get_session_detail(conn, revoked_id)
    assert detail["status"] == "revoked"
    s3 = admit(service, conn, "o8", "standard", "r3")
    assert s3["admitted"] is True
    assert s3["session_id"] not in (s1, revoked_id)
