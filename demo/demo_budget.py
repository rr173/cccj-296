"""预付预算本机演示：中心服务真实独立进程 + 显式逻辑时钟。

覆盖需求点名的两幕：
  1. 组织仍有并行席位，但预算用完 → 在线会话在余额耗尽那一刻被停用，
     终态与停用时间明确，补发额度也不复活。
  2. 推进途中席位失效 → 会话在席位失效的真实边界停用，不对失效后的时段扣费。

同时展示：走 HTTP 接口发放/查询（每笔额度的可用/已用/过期未用，以及用量
对应的会话、时间范围、额度来源）、同 request_id 重送不重复发放。

直接运行：  python3 -m demo.demo_budget
（无第三方依赖时使用 tools.run_center 的标准库 HTTP 包装，业务规则仍在服务层）
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"端口 {port} 未在预期时间内就绪")


def _http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode() or "{}")
        payload["_http_status"] = exc.code
        return payload


def _banner(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def _start_center(db_path: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = dict(os.environ, PYTHONPATH=str(ROOT), LICENSE_DB=str(db_path),
               LICENSE_EPOCH="2026-01-01T00:00:00Z", CENTER_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "-m", "tools.run_center"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _wait_port(port)
    return proc, f"http://127.0.0.1:{port}"


def _show_grants(api: str, org: str) -> None:
    out = _http("GET", f"{api}/orgs/{org}/budget")
    print(f"  计费开启: {out['billing_enabled']}  起点: {out['billable_since']}")
    for g in out["grants"]:
        print(f"  - 额度 {g['grant_id']}: 总额={g['amount']} 已用={g['used']} "
              f"可用={g['available']} 过期未用={g['expired_unused']} "
              f"到期={g['expire_at']}")
    print(f"  合计: {out['totals']}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="budget_demo_"))
    db_path = workdir / "center.db"
    proc, api = _start_center(db_path)
    try:
        print(f"中心服务已启动: {api}（数据库 {db_path}）")
        print(f"逻辑时钟起点: {_http('GET', f'{api}/clock')['now']}")

        # ------------------------------------------------------- 发放与幂等
        _banner("1. 运营发放额度：正整数、UTC 到期时间、request_id；重送不重复发放")
        org = "demo-org"
        first = _http("POST", f"{api}/budgets/grants", {
            "org": org, "amount": 30,
            "expire_at": "2026-02-01T00:00:00Z", "request_id": "grant-req-1"})
        print(f"  发放: granted={first['granted']} grant_id={first['grant_id']} "
              f"balance_after={first['balance_after']}")
        replay = _http("POST", f"{api}/budgets/grants", {
            "org": org, "amount": 30,
            "expire_at": "2026-02-01T00:00:00Z", "request_id": "grant-req-1"})
        print(f"  同 request_id 重送: 同一 grant_id={replay['grant_id'] == first['grant_id']}")
        bad = _http("POST", f"{api}/budgets/grants", {
            "org": org, "amount": 99,
            "expire_at": "2026-02-01T00:00:00Z", "request_id": "grant-req-1"})
        print(f"  改金额重送: {bad.get('_http_status')} {bad.get('error')}")

        # 席位与预算分开：充足席位，但预算只够一小段
        _http("POST", f"{api}/entitlements", {
            "org": org, "tier": "standard", "seats": 5,
            "effective_at": "2026-01-01T00:00:00Z",
            "expire_at": "2027-01-01T00:00:00Z"})
        s1 = _http("POST", f"{api}/sessions/admit",
                   {"org": org, "tier": "standard", "request_id": "admit-1"})
        s2 = _http("POST", f"{api}/sessions/admit",
                   {"org": org, "tier": "standard", "request_id": "admit-2"})
        print(f"  两个标准会话准入（席位 5 个，充足）: "
              f"{s1['session_id']}, {s2['session_id']}")

        # ------------------------------------------------- 预算不足停用
        _banner("2. 仍有席位，预算用完 → 付不起的在线会话在余额耗尽处停用")
        # 2 个标准会话 = 2 单位/微秒；30 单位只够 15 微秒
        out = _http("POST", f"{api}/clock/advance", {"delta_seconds": 0.000100})
        print(f"  推进 100µs: 扣费={out['charged_units']} 分段={out['segments']}")
        for ev in out["evicted"]:
            print(f"  - 停用 {ev['session_id']} 原因={ev['reason']} 停用时间={ev['at']}")
        for sid in (s1["session_id"], s2["session_id"]):
            d = _http("GET", f"{api}/sessions/{sid}")
            print(f"  会话 {sid}: status={d['status']} reason={d['end_reason']} "
                  f"ended_at={d['ended_at']} 已扣={d['budget_units']}")
        _show_grants(api, org)

        _banner("3. 补发额度不能复活已因预算停用的会话")
        extra = _http("POST", f"{api}/budgets/grants", {
            "org": org, "amount": 1000,
            "expire_at": "2026-03-01T00:00:00Z", "request_id": "grant-req-2"})
        print(f"  补发: granted={extra['granted']} balance_after={extra['balance_after']}")
        _http("POST", f"{api}/clock/advance", {"delta_seconds": 0.000010})
        still = _http("GET", f"{api}/sessions/{s1['session_id']}")
        print(f"  原会话仍是终态: status={still['status']} 已扣={still['budget_units']}（未再计费）")
        s3 = _http("POST", f"{api}/sessions/admit",
                   {"org": org, "tier": "standard", "request_id": "admit-3"})
        print(f"  新申请可重新进入: admitted={s3['admitted']} session={s3['session_id']}")

        # ------------------------------------------- 席位在推进途中失效
        _banner("4. 席位在推进途中失效 → 在失效边界停用，且不多扣费")
        org2 = "gap-org"
        _http("POST", f"{api}/budgets/grants", {
            "org": org2, "amount": 10 ** 9,
            "expire_at": "2027-01-01T00:00:00Z", "request_id": "gap-g1"})
        # 以当前逻辑时钟为基准：前 10µs 有 1 席；10µs 到 90µs 之间无席位；
        # 90µs 起恢复 1 席
        from datetime import datetime, timedelta

        def _us(n: int) -> str:
            now = datetime.fromisoformat(_http("GET", f"{api}/clock")["now"])
            return (now + timedelta(microseconds=n)).isoformat()

        _http("POST", f"{api}/entitlements", {
            "org": org2, "tier": "standard", "seats": 1,
            "effective_at": _http("GET", f"{api}/clock")["now"],
            "expire_at": _us(10)})
        _http("POST", f"{api}/entitlements", {
            "org": org2, "tier": "standard", "seats": 1,
            "effective_at": _us(90), "expire_at": _us(10 ** 9)})
        g = _http("POST", f"{api}/sessions/admit",
                  {"org": org2, "tier": "standard", "request_id": "gap-admit"})
        print(f"  会话准入: {g['session_id']}")
        out = _http("POST", f"{api}/clock/advance", {"delta_seconds": 0.000200})
        print(f"  一次推进 200µs（中途无席位、终点又有席位）: "
              f"扣费={out['charged_units']} 分段={out['segments']}")
        gd = _http("GET", f"{api}/sessions/{g['session_id']}")
        print(f"  会话终态: status={gd['status']} reason={gd['end_reason']} "
              f"ended_at={gd['ended_at']}")
        print(f"  只扣到失效边界: 已扣={gd['budget_units']}（期望 10，未对空档多扣费）")
        assert gd["budget_units"] == 10 and gd["status"] == "revoked"

        # ----------------------------------------------------- 用量核对
        _banner("5. 查询：每笔额度的可用/已用/过期未用，以及用量归属")
        _show_grants(api, org2)
        usage = _http("GET", f"{api}/orgs/{org2}/budget/usage")
        print(f"  用量合计: {usage['totals']}")
        for u in usage["usage"]:
            print(f"  - 会话 {u['session_id']} tier={u['tier']} "
                  f"[{u['start_at']} , {u['end_at']}) 单位={u['units']} "
                  f"额度来源={u['grant_id']}")
        agg = _http("GET", f"{api}/orgs/{org}/budget/usage")
        by_grant: dict = {}
        for u in agg["usage"]:
            key = u["grant_id"] or "(无额度)"
            by_grant[key] = by_grant.get(key, 0) + u["units"]
        print(f"  {org} 按额度来源汇总: {by_grant}")
        print("\n演示通过。")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
