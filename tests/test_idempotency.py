"""幂等、凭据代次、关闭语义测试。"""
from tests.conftest import active_ids, admit, make_ent

from app import service


def test_admit_request_id_replay_returns_first_result(conn):
    make_ent(service, conn, "o1", "standard", 1)
    first = admit(service, conn, "o1", "standard", "rid-1")
    again = admit(service, conn, "o1", "standard", "rid-1")
    assert again == first  # 完全相同的响应（含稳定会话 ID 与凭据）

    # 参数变化则拒绝
    try:
        service.admit_session(conn, "o1", "premium", "rid-1")
        assert False, "应当拒绝参数变化的重试"
    except service.ServiceError as e:
        assert e.code == "request_id_param_mismatch"

    # 只有一个会话被创建
    assert len(active_ids(service, conn, "o1")) == 1


def test_replay_points_to_original_session_after_revocation(conn):
    """第一次准入后来被撤销，同 request_id 重试仍指向原会话，不重新占用。"""
    make_ent(service, conn, "o2", "standard", 1)
    first = admit(service, conn, "o2", "standard", "rid-x")
    ent = conn.execute("SELECT ent_id FROM entitlements WHERE org='o2'").fetchone()[0]
    service.revoke_entitlement(conn, ent, "收回")
    assert service.get_session_detail(conn, first["session_id"])["status"] == "revoked"

    replay = admit(service, conn, "o2", "standard", "rid-x")
    assert replay["session_id"] == first["session_id"]
    assert replay["admitted"] is True          # 第一次的结果不变
    assert replay["credential"] == first["credential"]
    assert active_ids(service, conn, "o2") == []  # 没有借重试重新占位


def test_renew_rotates_credential_and_rejects_old_one(conn):
    make_ent(service, conn, "o3", "standard", 1)
    a = admit(service, conn, "o3", "standard", "r1")
    sid, secret = a["session_id"], a["credential"]["secret"]

    r1 = service.renew_session(conn, sid, secret, "op-1")
    assert r1["ok"] is True
    new_secret = r1["credential"]["secret"]
    assert new_secret != secret
    assert r1["credential"]["generation"] == 2

    # 旧凭据不能再发起任何操作
    r2 = service.renew_session(conn, sid, secret, "op-2")
    assert r2["ok"] is False and r2["error"] == "old_credential"

    # 新凭据正常工作
    r3 = service.renew_session(conn, sid, new_secret, "op-3")
    assert r3["ok"] is True and r3["credential"]["generation"] == 3

    # 同 operation_id 重试返回第一次的结果
    r1_replay = service.renew_session(conn, sid, new_secret, "op-1")
    assert r1_replay == r1

    detail = service.get_session_detail(conn, sid)
    gens = [c["generation"] for c in detail["credentials"]]
    assert gens == [1, 2, 3]
    assert [c["status"] for c in detail["credentials"]][:2] == ["consumed", "consumed"]


def test_unknown_credential_rejected_without_side_effects(conn):
    make_ent(service, conn, "o4", "standard", 1)
    a = admit(service, conn, "o4", "standard", "r1")
    out = service.renew_session(conn, a["session_id"], "not-a-real-secret", "op-x")
    assert out["ok"] is False and out["error"] == "invalid_credential"
    # 原凭据仍然有效
    ok = service.renew_session(conn, a["session_id"], a["credential"]["secret"], "op-y")
    assert ok["ok"] is True


def test_renew_revoked_or_closed_session_fails_forever(conn):
    make_ent(service, conn, "o5", "standard", 2)
    a1 = admit(service, conn, "o5", "standard", "r1")
    a2 = admit(service, conn, "o5", "standard", "r2")

    # 关闭后不能续用
    c = service.close_session(conn, a1["session_id"], a1["credential"]["secret"], "c1")
    assert c["ok"] is True and c["released"] is True
    r = service.renew_session(conn, a1["session_id"], a1["credential"]["secret"], "rn1")
    assert r["ok"] is False and r["error"] == "session_closed"

    # 撤销后不能续用
    ent = conn.execute("SELECT ent_id FROM entitlements WHERE org='o5'").fetchone()[0]
    service.modify_entitlement(conn, ent, seats=0)
    assert service.get_session_detail(conn, a2["session_id"])["status"] == "revoked"
    r2 = service.renew_session(conn, a2["session_id"], a2["credential"]["secret"], "rn2")
    assert r2["ok"] is False and r2["error"] == "session_revoked"

    # 扩容也不复活
    service.modify_entitlement(conn, ent, seats=5)
    assert active_ids(service, conn, "o5") == []
    r3 = service.renew_session(conn, a2["session_id"], a2["credential"]["secret"], "rn3")
    assert r3["ok"] is False


def test_close_releases_only_its_own_slot_and_is_idempotent(conn):
    make_ent(service, conn, "o6", "standard", 2)
    a1 = admit(service, conn, "o6", "standard", "r1")
    a2 = admit(service, conn, "o6", "standard", "r2")
    ent_of_1 = service.get_session_detail(conn, a1["session_id"])["slot_history"][-1]["ent_id"]

    c1 = service.close_session(conn, a1["session_id"], a1["credential"]["secret"], "close-1")
    assert c1["released_ent_id"] == ent_of_1
    assert active_ids(service, conn, "o6") == [a2["session_id"]]

    # 重复关闭（不同 operation_id）：幂等成功但不再释放
    c2 = service.close_session(conn, a1["session_id"], None, "close-2")
    assert c2["ok"] is True and c2["released"] is False

    # 同 operation_id 重试原样返回
    assert service.close_session(conn, a1["session_id"], None, "close-1") == c1

    # 释放出的席位可被新会话使用
    a3 = admit(service, conn, "o6", "standard", "r3")
    assert a3["admitted"] is True
    assert a3["assigned_ent_id"] == ent_of_1


def test_late_close_of_old_session_does_not_take_new_occupants_slot(conn):
    """晚到的旧会话关闭不能释放后来其它会话占用的额度。"""
    make_ent(service, conn, "o7", "standard", 1)
    old = admit(service, conn, "o7", "standard", "r-old")
    ent_id = conn.execute("SELECT ent_id FROM entitlements WHERE org='o7'").fetchone()[0]

    # 旧会话被撤销，席位后来给了新会话
    service.modify_entitlement(conn, ent_id, seats=0)
    service.modify_entitlement(conn, ent_id, seats=1)
    new = admit(service, conn, "o7", "standard", "r-new")
    assert new["admitted"] is True
    assert new["assigned_ent_id"] == ent_id

    # 旧会话的晚到关闭：不释放新会话占用的席位
    late = service.close_session(conn, old["session_id"], old["credential"]["secret"], "late")
    assert late["released"] is False
    assert active_ids(service, conn, "o7") == [new["session_id"]]
    detail = service.get_session_detail(conn, new["session_id"])
    assert detail["slot_history"][-1]["released_seq"] is None


def test_close_with_bad_credential_changes_nothing(conn):
    make_ent(service, conn, "o8", "standard", 1)
    a = admit(service, conn, "o8", "standard", "r1")
    out = service.close_session(conn, a["session_id"], "wrong", "c-bad")
    assert out["ok"] is False and out["error"] == "invalid_credential"
    assert active_ids(service, conn, "o8") == [a["session_id"]]
