"""并发竞争测试：SQLite 写锁串行化 + 事件 seq 可核对提交先后。"""
import multiprocessing
import threading

from tests.conftest import admit, make_ent

from app import db, service


def test_concurrent_admit_only_one_wins_last_seat(conn, tmp_path):
    """10 个独立连接并发申请最后一份额度：恰好 1 个成功。"""
    db_path = str(tmp_path / "race.db")
    c0 = db.connect(db_path)
    db.init_db(c0)
    c0.execute("UPDATE clock SET now_iso=? WHERE id=1",
               (db.normalize_ts("2026-01-01T00:00:00Z"),))
    service.create_entitlement(c0, "race", "standard", 1,
                               "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    c0.close()

    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(10)

    def worker(i: int):
        c = db.connect(db_path)
        try:
            barrier.wait()
            results.append(service.admit_session(c, "race", "standard", f"r-{i}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    admitted = [r for r in results if r["admitted"]]
    rejected = [r for r in results if not r["admitted"]]
    assert len(admitted) == 1
    assert len(rejected) == 9
    assert len({r["session_id"] for r in admitted}) == 1

    check = db.connect(db_path)
    active = [s for s in service.list_org_sessions(check, "race") if s["status"] == "active"]
    assert len(active) == 1
    # 被拒会话也都以 revoked 终态留痕，而不是悄悄消失
    rejected_rows = [s for s in service.list_org_sessions(check, "race")
                     if s["status"] == "revoked"]
    assert len(rejected_rows) == 9
    check.close()


def _process_admit_worker(db_path: str, request_id: str, queue):
    c = db.connect(db_path)
    try:
        queue.put(service.admit_session(c, "race2", "standard", request_id))
    finally:
        c.close()


def test_two_real_processes_contend_for_last_seat(tmp_path):
    """两个真实 OS 进程同时申请最后一份额度。"""
    db_path = str(tmp_path / "proc.db")
    c0 = db.connect(db_path)
    db.init_db(c0)
    c0.execute("UPDATE clock SET now_iso=? WHERE id=1",
               (db.normalize_ts("2026-01-01T00:00:00Z"),))
    service.create_entitlement(c0, "race2", "standard", 1,
                               "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    c0.close()

    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    p1 = ctx.Process(target=_process_admit_worker, args=(db_path, "req-1", queue))
    p2 = ctx.Process(target=_process_admit_worker, args=(db_path, "req-2", queue))
    p1.start(); p2.start()
    p1.join(30); p2.join(30)
    assert p1.exitcode == 0 and p2.exitcode == 0

    outcomes = [queue.get_nowait() for _ in range(2)]
    assert sorted(r["admitted"] for r in outcomes) == [False, True]

    check = db.connect(db_path)
    events = service.list_events(check, org="race2", limit=50)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs, reverse=True)  # 全局事件 seq 单调、可核对
    check.close()


def test_renew_vs_revoke_race_has_verifiable_order(tmp_path):
    """续用与撤销竞争：两种结局都合法，但必须与事件 seq 的先后一致。"""
    db_path = str(tmp_path / "renew_revoke.db")
    c0 = db.connect(db_path)
    db.init_db(c0)
    c0.execute("UPDATE clock SET now_iso=? WHERE id=1",
               (db.normalize_ts("2026-01-01T00:00:00Z"),))
    made = service.create_entitlement(c0, "o", "standard", 1,
                                      "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    ent_id = made["entitlement"]["ent_id"]
    a = service.admit_session(c0, "o", "standard", "r1")
    sid, secret = a["session_id"], a["credential"]["secret"]
    c0.close()

    conn_a = db.connect(db_path)
    conn_b = db.connect(db_path)
    renew_out: dict = {}
    barrier = threading.Barrier(2)

    def do_renew():
        barrier.wait()
        renew_out.update(service.renew_session(conn_a, sid, secret, "op-race"))

    def do_revoke():
        barrier.wait()
        service.revoke_entitlement(conn_b, ent_id, "竞争撤销")

    t1 = threading.Thread(target=do_renew)
    t2 = threading.Thread(target=do_revoke)
    t1.start(); t2.start()
    t1.join(); t2.join()
    conn_a.close(); conn_b.close()

    check = db.connect(db_path)
    # 会话最终一定被撤销（唯一权益已收回）
    assert service.get_session_detail(check, sid)["status"] == "revoked"

    op_seq = check.execute(
        "SELECT seq FROM operations WHERE operation_id='op-race'").fetchone()[0]
    revoke_seq = check.execute(
        "SELECT ended_seq FROM entitlements WHERE ent_id=? AND status='revoked'",
        (ent_id,)).fetchone()[0]

    if op_seq < revoke_seq:
        # 续用先提交：成功换发，随后撤销
        assert renew_out["ok"] is True
        assert renew_out["credential"]["generation"] == 2
    else:
        # 撤销先提交：续用必然失败
        assert renew_out["ok"] is False
        assert renew_out["error"] == "session_revoked"
    check.close()


def test_concurrent_same_operation_id_returns_single_result(tmp_path):
    db_path = str(tmp_path / "same_op.db")
    c0 = db.connect(db_path)
    db.init_db(c0)
    c0.execute("UPDATE clock SET now_iso=? WHERE id=1",
               (db.normalize_ts("2026-01-01T00:00:00Z"),))
    service.create_entitlement(c0, "o2", "standard", 1,
                               "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    a = service.admit_session(c0, "o2", "standard", "r1")
    sid, secret = a["session_id"], a["credential"]["secret"]
    c0.close()

    conn_a = db.connect(db_path)
    conn_b = db.connect(db_path)
    outs: list[dict] = []
    barrier = threading.Barrier(2)

    def renew(c):
        barrier.wait()
        outs.append(service.renew_session(c, sid, secret, "same-op"))

    t1 = threading.Thread(target=renew, args=(conn_a,))
    t2 = threading.Thread(target=renew, args=(conn_b,))
    t1.start(); t2.start()
    t1.join(); t2.join()
    conn_a.close(); conn_b.close()

    assert outs[0] == outs[1]
    check = db.connect(db_path)
    # 只产生了一代新凭据
    creds = check.execute(
        "SELECT COUNT(*) FROM credentials WHERE session_id=?", (sid,)).fetchone()[0]
    assert creds == 2
    check.close()
