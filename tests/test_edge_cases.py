"""补充边界：窗口修改、席位迁移、无心跳淘汰、零席位、多组织隔离。"""
from tests.conftest import active_ids, admit, make_ent

from app import service


def test_modify_window_expires_entitlement_via_clock(conn):
    """修改权益的失效时间，再推进时钟即可让它提前到期。"""
    out = make_ent(service, conn, "o1", "standard", 1,
                   start="2026-01-01T00:00:00Z", end="2027-01-01T00:00:00Z")
    ent_id = out["entitlement"]["ent_id"]
    s = admit(service, conn, "o1", "standard", "r1")
    assert s["admitted"] is True

    service.modify_entitlement(conn, ent_id, expire_at="2026-01-02T00:00:00Z")
    res = service.advance_clock(conn, to_iso="2026-01-02T00:00:00Z")
    assert [e["session_id"] for e in res["evicted"]] == [s["session_id"]]
    assert active_ids(service, conn, "o1") == []


def test_rebalance_moves_session_between_entitlements_not_silent_swap(conn):
    """再平衡产生席位迁移时，slot_history 完整记录迁移轨迹。"""
    e1 = make_ent(service, conn, "o2", "standard", 1)["entitlement"]["ent_id"]
    e2 = make_ent(service, conn, "o2", "premium", 1)["entitlement"]["ent_id"]
    std = admit(service, conn, "o2", "standard", "r-std")
    assert std["assigned_ent_id"] == e1

    # 标准权益降为 0 席：标准会话迁移到空闲的高级席位（标准两类都可用）
    service.modify_entitlement(conn, e1, seats=0)
    detail = service.get_session_detail(conn, std["session_id"])
    ents = [h["ent_id"] for h in detail["slot_history"]]
    assert ents == [e1, e2]
    assert detail["slot_history"][0]["release_reason"] == "rebalanced"
    assert detail["slot_history"][-1]["released_seq"] is None

    # 高级会话到来：标准会话被挤出，轨迹闭合且原因明确
    pre = admit(service, conn, "o2", "premium", "r-pre")
    assert pre["admitted"] is True
    detail = service.get_session_detail(conn, std["session_id"])
    assert detail["status"] == "revoked"
    assert detail["slot_history"][-1]["release_reason"] == "no_any_capacity"


def test_no_heartbeat_eviction_sessions_survive_clock_advance(conn):
    """不按心跳超时自动淘汰：时间推进本身不会动有容量的会话。"""
    make_ent(service, conn, "o3", "premium", 2,
             start="2026-01-01T00:00:00Z", end="2030-01-01T00:00:00Z")
    s1 = admit(service, conn, "o3", "premium", "r1")
    s2 = admit(service, conn, "o3", "premium", "r2")
    # 推进 30 天（权益仍生效）：没有任何心跳淘汰规则，会话原样保留
    res = service.advance_clock(conn, delta_seconds=30 * 24 * 3600)
    assert res["evicted"] == []
    assert set(active_ids(service, conn, "o3")) == {s1["session_id"], s2["session_id"]}


def test_zero_seat_entitlement_admits_nothing(conn):
    make_ent(service, conn, "o4", "standard", 0)
    res = admit(service, conn, "o4", "standard", "r1")
    assert res["admitted"] is False
    assert res["reason"] == "no_any_capacity"


def test_orgs_are_isolated(conn):
    make_ent(service, conn, "org-a", "standard", 1)
    make_ent(service, conn, "org-b", "standard", 1)
    a = admit(service, conn, "org-a", "standard", "r1")
    b = admit(service, conn, "org-b", "standard", "r2")
    assert a["admitted"] and b["admitted"]
    # org-a 满员不影响 org-b 之外的判断：org-a 再申请被拒，org-b 仍可关闭释放
    assert admit(service, conn, "org-a", "standard", "r3")["admitted"] is False
    assert service.list_org_sessions(conn, "org-b")[0]["session_id"] == b["session_id"]


def test_revoke_entitlement_returns_actual_evicted_with_reasons(conn):
    make_ent(service, conn, "o5", "premium", 1)
    make_ent(service, conn, "o5", "standard", 1)
    std = admit(service, conn, "o5", "standard", "r-s")     # 占标准席
    pre = admit(service, conn, "o5", "premium", "r-p")     # 占高级席
    pre_ent = service.get_session_detail(conn, pre["session_id"])["slot_history"][-1]["ent_id"]

    out = service.revoke_entitlement(conn, pre_ent, "商务纠纷")
    assert {e["session_id"] for e in out["evicted"]} == {pre["session_id"]}
    assert out["evicted"][0]["reason"] == "no_premium_capacity"
    # 标准会话安然无恙
    assert set(active_ids(service, conn, "o5")) == {std["session_id"]}

    # 撤销一个已撤销的权益版本 → 404
    try:
        service.revoke_entitlement(conn, pre_ent, "再次撤销")
        assert False
    except service.ServiceError as e:
        assert e.http_status == 404
