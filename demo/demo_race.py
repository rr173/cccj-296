"""双进程竞争演示：两个真实 OS 进程同时申请最后一份额度。

流程：启动 uvicorn（子进程）→ 建 1 席权益 → 两个工作进程同时 POST 准入
→ 校验恰好一个成功、事件 seq 给出可核对的提交先后 → 关闭服务。
直接运行：  python3 -m demo.demo_race
"""
from __future__ import annotations

import json
import multiprocessing
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

API_BASE = "http://127.0.0.1:{port}"


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("服务未在预期时间内启动")


def _http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _worker(port: int, request_id: str, queue) -> None:
    # 每个工作进程是独立解释器，独立 HTTP 连接，模拟真实两进程
    try:
        out = _http("POST", f"{API_BASE.format(port=port)}/sessions/admit",
                    {"org": "race-org", "tier": "standard", "request_id": request_id})
        queue.put((os.getpid(), request_id, out))
    except Exception as e:  # noqa: BLE001
        queue.put((os.getpid(), request_id, {"error": repr(e)}))


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="admission_race_"))
    db_path = workdir / "race.db"
    env = dict(os.environ, LICENSE_DB=str(db_path),
               LICENSE_EPOCH="2026-01-01T00:00:00Z")

    # 选一个空闲端口
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_port(port)
        base = API_BASE.format(port=port)
        print(f"服务已启动: {base}（数据库 {db_path}）")

        _http("POST", f"{base}/entitlements", {
            "org": "race-org", "tier": "standard", "seats": 1,
            "effective_at": "2026-01-01T00:00:00Z",
            "expire_at": "2027-01-01T00:00:00Z",
        })
        print("已创建标准权益 1 席；两个进程同时发起准入…")

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        p1 = ctx.Process(target=_worker, args=(port, "req-A", queue))
        p2 = ctx.Process(target=_worker, args=(port, "req-B", queue))
        p1.start(); p2.start()
        p1.join(20); p2.join(20)
        assert p1.exitcode == 0 and p2.exitcode == 0

        outcomes = [queue.get_nowait() for _ in range(2)]
        outcomes.sort(key=lambda x: x[1])
        winner = loser = None
        for pid, rid, out in outcomes:
            if "error" in out:
                print(f"  进程 pid={pid} request_id={rid}: 调用失败 {out['error']}")
                raise AssertionError("工作进程调用失败")
            print(f"  进程 pid={pid} request_id={rid}: admitted={out['admitted']} "
                  f"session={out['session_id']} status={out['status']}"
                  + (f" reason={out.get('reason')}" if not out['admitted'] else ""))
            if out["admitted"]:
                winner = (pid, rid, out)
            else:
                loser = (pid, rid, out)

        assert winner is not None and loser is not None
        print(f"\n结论: {winner[1]} 先拿到写锁并占用唯一席位；"
              f"{loser[1]} 的会话以 revoked 终态留痕（原因 {loser[2].get('reason')}）")

        events = _http("GET", f"{base}/events?org=race-org&limit=10")["events"]
        print("\n事件流（seq 小 = 先提交，可核对的提交先后）:")
        for e in sorted(events, key=lambda x: x["seq"]):
            print(f"  seq={e['seq']:<3} {e['kind']:<22} {e['detail']}")

        sessions = _http("GET", f"{base}/orgs/race-org/sessions")["sessions"]
        active = [s for s in sessions if s["status"] == "active"]
        print(f"\n校验: 活跃会话数={len(active)}（期望 1），"
              f"留痕会话总数={len(sessions)}（期望 2，被拒者仍可追溯）")
        assert len(active) == 1 and len(sessions) == 2
        print("演示通过。")
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    main()
