"""本机演示：标准/高级额度挪用、到期降额、旧凭据、重复关闭。

直接运行：  python3 -m demo.demo_scenarios
不依赖外部服务、不睡眠；时间推进全部走逻辑时钟。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app import db, service

ORG = "demo-org"


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _show_active(conn) -> None:
    rows = service.list_org_sessions(conn, ORG)
    for r in rows:
        print(f"  - {r['session_id']}  tier={r['tier']:<8} status={r['status']:<8} "
              f"ent={r['current_ent_id'] or '-'}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="admission_demo_"))
    conn = db.connect(str(workdir / "demo.db"))
    db.init_db(conn)
    conn.execute("UPDATE clock SET now_iso=? WHERE id=1",
                 (db.normalize_ts("2026-01-01T00:00:00Z"),))
    print(f"数据库文件: {workdir / 'demo.db'}")
    print(f"逻辑时钟起点: {db.get_clock(conn)}")

    # ----------------------------------------------------------- 场景 1：挪用
    _line("场景 1：标准会话挪用高级席位，后到的高级申请能把席位要回来")
    pre = service.create_entitlement(conn, ORG, "premium", 1,
                                     "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    pre_ent = pre["entitlement"]["ent_id"]
    print(f"创建高级权益 {pre_ent}（1 席），暂无标准权益")

    std = service.admit_session(conn, ORG, "standard", "demo-req-std")
    print(f"标准会话 {std['session_id']} 准入成功，实际占用高级席位 "
          f"{std['assigned_ent_id']}（标准会话两类额度都可用）")

    new_pre = service.admit_session(conn, ORG, "premium", "demo-req-pre")
    print(f"高级会话 {new_pre['session_id']} 申请准入 → 再平衡：")
    print(f"  高级会话保留: {new_pre['admitted']}，标准会话被明确撤销（不是查询里消失）")
    _show_active(conn)
    std_detail = service.get_session_detail(conn, std["session_id"])
    print(f"  被撤标准会话终态: status={std_detail['status']}, "
          f"reason={std_detail['end_reason']}")

    # 同 request_id 重试仍指向原会话，不会借重试重新占位
    replay = service.admit_session(conn, ORG, "standard", "demo-req-std")
    print(f"  原 request_id 重试: session_id 不变={replay['session_id'] == std['session_id']}, "
          f"仍无活跃标准会话={all(s['status'] != 'active' for s in service.list_org_sessions(conn, ORG) if s['tier']=='standard')}")

    # ------------------------------------------------------- 场景 2：到期降额
    _line("场景 2：时间推进使权益到期，自动降额并按优先级撤销（无睡眠）")
    std_ent = service.create_entitlement(
        conn, ORG, "standard", 2,
        "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")["entitlement"]["ent_id"]
    print(f"创建标准权益 {std_ent}（2 席），2026-02-01 到期（左闭右开）")
    s1 = service.admit_session(conn, ORG, "standard", "demo-req-s1")
    s2 = service.admit_session(conn, ORG, "standard", "demo-req-s2")
    print(f"两个标准会话准入: {s1['session_id']}（早）、{s2['session_id']}（晚）")

    # 接续权益只有 1 席
    next_ent = service.create_entitlement(
        conn, ORG, "standard", 1,
        "2026-02-01T00:00:00Z", "2027-01-01T00:00:00Z")["entitlement"]["ent_id"]
    print(f"创建接续标准权益 {next_ent}（1 席），2026-02-01 生效")

    out = service.advance_clock(conn, to_iso="2026-02-01T00:00:00Z")
    print(f"时钟推进到 {out['now']}，本次实际撤销:")
    for e in out["evicted"]:
        print(f"  - {e['session_id']} tier={e['tier']} 原因={e['reason']}")
    print("当前会话:")
    _show_active(conn)

    # ------------------------------------------------------- 场景 3：旧凭据
    _line("场景 3：续用换发下一代凭据，旧凭据不能再发起新操作")
    r = service.renew_session(conn, s1["session_id"], s1["credential"]["secret"], "demo-op-renew-1")
    gen2 = r["credential"]["secret"]
    print(f"首次续用成功: 第 1 代 → 第 {r['credential']['generation']} 代")

    old = service.renew_session(conn, s1["session_id"], s1["credential"]["secret"], "demo-op-renew-old")
    print(f"拿旧凭据再续用: ok={old['ok']} error={old['error']}")

    replay_renew = service.renew_session(conn, s1["session_id"], gen2, "demo-op-renew-1")
    print(f"同 operation_id 重试: 返回第一次结果={replay_renew == r}")

    r2 = service.renew_session(conn, s1["session_id"], gen2, "demo-op-renew-2")
    print(f"用新代凭据续用: ok={r2['ok']} 新一代={r2['credential']['generation']}")

    # ------------------------------------------------------- 场景 4：重复关闭
    _line("场景 4：主动关闭只释放自己那份额度，重复/晚到关闭不释放别人的额度")
    ent_before = service.get_session_detail(conn, s1["session_id"])["slot_history"][-1]["ent_id"]
    c1 = service.close_session(conn, s1["session_id"], r2["credential"]["secret"], "demo-op-close-1")
    print(f"首次关闭: released={c1['released']} 释放的权益={c1['released_ent_id']}（正是它实际占用的 {ent_before}）")

    c2 = service.close_session(conn, s1["session_id"], None, "demo-op-close-2")
    print(f"重复关闭: ok={c2['ok']} released={c2['released']}（不再释放任何额度）")
    print(f"同 operation_id 重试返回相同结果="
          f"{service.close_session(conn, s1['session_id'], None, 'demo-op-close-1') == c1}")

    # 新会话可以顶上释放出的席位
    s3 = service.admit_session(conn, ORG, "standard", "demo-req-s3")
    print(f"新会话 {s3['session_id']} 准入成功，拿到释放出的席位: "
          f"{s3['assigned_ent_id'] == ent_before}")

    # 晚到关闭：此前场景 1 被撤销的标准会话
    late = service.close_session(conn, std["session_id"], std["credential"]["secret"], "demo-op-late")
    print(f"晚到的旧会话关闭: released={late['released']}，新会话席位不受影响: "
          f"{service.get_session_detail(conn, s3['session_id'])['slot_history'][-1]['released_seq'] is None}")

    # ----------------------------------------------------------- 追溯
    _line("追溯：权益版本历史、会话结局与全局事件流")
    print(f"权益 {std_ent} 的版本历史:")
    for v in service.list_entitlement_history(conn, std_ent):
        print(f"  v{v['version']} seats={v['seats']} status={v['status']} "
              f"窗口=[{v['effective_at']}, {v['expire_at']})")
    print(f"\n会话 {s2['session_id']} 的完整结局:")
    d = service.get_session_detail(conn, s2["session_id"])
    print(f"  status={d['status']} reason={d['end_reason']} 原始申请={d['admit_request_id']} "
          f"首次结果={d['admit_first_status']}")
    print(f"  席位轨迹: {[(h['ent_id'], h['release_reason']) for h in d['slot_history']]}")
    print("\n最近 8 条事件（seq 即可核对的提交先后）:")
    for e in service.list_events(conn, org=ORG, limit=8):
        print(f"  seq={e['seq']:<3} {e['kind']:<22} {e['detail']}")

    conn.close()
    print(f"\n演示结束，数据库保留在: {workdir / 'demo.db'}")


if __name__ == "__main__":
    main()
