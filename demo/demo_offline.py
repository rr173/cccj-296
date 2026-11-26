"""离线借席端到端演示：两个真实独立进程（中心 + 站点），从借到还全流程。

与 demo_race.py 一样直接拉起子进程；两端各自独立 SQLite 文件、独立 HTTP 服务，
共用同一把显式推进的 UTC 逻辑时钟（不睡眠、不依赖 wall clock）。

演示脉络：
  0. 启动中心/站点两个进程，中心建权益
  1. 中心领包：幂等重试、改参数拒绝、容量约束（不能挤掉在线会话）
  2. 带包到站点：验签导入、篡改拒绝、重复导入不多开
  3. 断开中心：站点独立开/关会话，高级/标准席位规则与并发上限
  4. 借期内中心无法重复分配这份额度（在线会话被拒、第二个包也借不出）
  5. 站点重启：持久化存储保证不多开
  6. 提前还席：站点停用包并导出交回凭据；包文件/关会话回执不能当凭据
  7. 凭据送回中心：额度释放可重新分配；重送不重复释放；已交回包不能重启用
  8. 到期演示：时钟推过截止点，站点会话全部终止且不能再开

直接运行： PYTHONPATH=/workspace python3 -m demo.demo_offline
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
EPOCH = "2026-01-01T00:00:00Z"
SIGN_KEY = "demo-shared-hmac-key-0123456789abcdef"
ORG = "acme"
SITE = "site-A"


def line(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"端口 {port} 服务未启动")


def http(method: str, url: str, body: dict | None = None,
         expect_error: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        if expect_error:
            return exc.code, payload
        raise AssertionError(f"{method} {url} 意外失败: {payload}") from exc


class Demo:
    def __init__(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="offline_demo_"))
        self.center_db = self.workdir / "center.db"
        self.site_db = self.workdir / "site.db"
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.center_port = s.getsockname()[1]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.site_port = s.getsockname()[1]
        self.center_url = f"http://127.0.0.1:{self.center_port}"
        self.site_url = f"http://127.0.0.1:{self.site_port}"
        self.center_proc: subprocess.Popen | None = None
        self.site_proc: subprocess.Popen | None = None

    def _env(self, **extra) -> dict:
        env = dict(os.environ, PYTHONPATH=str(ROOT),
                   LICENSE_SIGNING_KEY=SIGN_KEY)
        env.update(extra)
        return env

    def start_center(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tools.run_center"],
            cwd=str(ROOT),
            env=self._env(LICENSE_DB=str(self.center_db), LICENSE_EPOCH=EPOCH,
                          CENTER_PORT=str(self.center_port)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wait_port(self.center_port)
        self.center_proc = proc
        return proc

    def start_site(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tools.run_site"],
            cwd=str(ROOT),
            env=self._env(SITE_DB=str(self.site_db), SITE_ID=SITE,
                          SITE_EPOCH=EPOCH, SITE_PORT=str(self.site_port)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        wait_port(self.site_port)
        self.site_proc = proc
        return proc

    @staticmethod
    def _stop_proc(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def start(self):
        self.start_center()
        self.start_site()
        print(f"工作目录: {self.workdir}")
        print(f"中心进程 db={self.center_db} 端口={self.center_port}")
        print(f"站点进程 db={self.site_db} 端口={self.site_port}（独立存储）")

    def stop(self):
        self._stop_proc(self.center_proc)
        self._stop_proc(self.site_proc)

    # 便捷封装
    def c(self, method, path, body=None, expect_error=False):
        return http(method, self.center_url + path, body, expect_error)

    def s(self, method, path, body=None, expect_error=False):
        return http(method, self.site_url + path, body, expect_error)


def main() -> None:
    demo = Demo()
    demo.start()
    try:
        _run(demo)
    finally:
        demo.stop()
    print("\n演示结束。")


def _run(demo: Demo) -> None:
    # ------------------------------------------------- 0. 准备权益
    line("0. 中心建权：acme 拥有 2 高级席 + 2 标准席（窗口覆盖全年）")
    demo.c("POST", "/entitlements", {
        "org": ORG, "tier": "premium", "seats": 2,
        "effective_at": EPOCH, "expire_at": "2027-01-01T00:00:00Z"})
    demo.c("POST", "/entitlements", {
        "org": ORG, "tier": "standard", "seats": 2,
        "effective_at": EPOCH, "expire_at": "2027-01-01T00:00:00Z"})
    print("  权益已创建；中心逻辑时钟 =", demo.c("GET", "/clock")[1]["now"])

    # 先占一个在线高级会话，验证签发不会挤掉它
    online = demo.c("POST", "/sessions/admit", {
        "org": ORG, "tier": "premium", "request_id": "online-p-1"})[1]
    print(f"  在线高级会话 {online['session_id']} 已准入（占用 1/2 高级席）")

    # ------------------------------------------------- 1. 中心领包
    line("1. 中心领包：1 高级席 + 1 标准席，截止 2026-02-01T00:00:00Z")
    issue_body = {
        "org": ORG, "site_id": SITE, "standard_seats": 1, "premium_seats": 1,
        "expire_at": "2026-02-01T00:00:00Z", "request_id": "issue-1"}
    status, pkg = demo.c("POST", "/offline/packages/issue", issue_body)
    assert pkg["issued"], pkg
    package_id = pkg["package_id"]
    print(f"  签发成功 package_id={package_id}")
    print(f"  生效={pkg['issued_at']} 截止={pkg['expire_at']}（左闭右开，截止即失效）")

    # 幂等：同 org 同 request_id 重试得到原结果
    _, again = demo.c("POST", "/offline/packages/issue", issue_body)
    assert again["package_id"] == package_id
    print("  同 request_id 重试：返回同一个包（幂等）")

    # 改参数拒绝
    changed = dict(issue_body, premium_seats=2)
    code, err = demo.c("POST", "/offline/packages/issue", changed, expect_error=True)
    assert code == 409 and err["error"] == "request_id_param_mismatch"
    print(f"  同 request_id 改参数 → 409 {err['error']}")

    # 容量约束：高级池 2 席 = 在线 1 + 已借 1，再借 1 高级席会挤掉在线会话 → 拒绝
    _, too_big = demo.c("POST", "/offline/packages/issue", {
        "org": ORG, "site_id": SITE, "standard_seats": 0, "premium_seats": 1,
        "expire_at": "2026-02-01T00:00:00Z", "request_id": "issue-2"})
    assert not too_big["issued"] and too_big["reason"] == "would_displace_online_session"
    print(f"  会挤掉在线会话的签发 → issued=false（{too_big['reason']}），在线会话仍在")

    # ------------------------------------------------- 2. 站点导入
    line("2. 包带到站点：验签导入；篡改包与重复导入的处理")
    _, imp = demo.s("POST", "/packages/import", {"envelope": pkg["envelope"]})
    assert imp["imported"]
    print(f"  导入成功：{imp['package_id']} 高级席={imp['premium_seats']} "
          f"标准席={imp['standard_seats']}")

    # 篡改
    tampered = json.loads(json.dumps(pkg["envelope"]))
    tampered["payload"]["premium_seats"] = 99
    code, err = demo.s("POST", "/packages/import",
                       {"envelope": tampered}, expect_error=True)
    assert err["error"] == "invalid_package_signature"
    print(f"  篡改席位数后导入 → {code} {err['error']}")

    # 重复导入不多开
    _, dup = demo.s("POST", "/packages/import", {"envelope": pkg["envelope"]})
    assert dup["imported"] is False
    print(f"  重复导入 → imported={dup['imported']}（不产生新额度）")

    # 签给别的站点的包不能用：合法签名包改 site_id 会破坏签名；
    # 这里用一个真实签发给 site-B 的包（第 4 节容量演示后签发），先做本地伪造测试
    forged = json.loads(json.dumps(pkg["envelope"]))
    forged["payload"]["site_id"] = "site-B"
    code, err = demo.s("POST", "/packages/import",
                       {"envelope": forged}, expect_error=True)
    assert err["error"] == "invalid_package_signature"
    print(f"  篡改包内 site_id → {code} {err['error']}（HMAC 覆盖整包，改任意字段即失效）")

    # ------------------------------------------------- 3. 断开中心
    line("3. 断开中心：站点独立开/关本地会话（此后不向中心发任何请求）")
    # 停掉中心进程，模拟工地断网
    demo._stop_proc(demo.center_proc)
    demo.center_proc = None
    print("  中心进程已停止，站点完全离线")

    p1 = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "premium", "request_id": "lp-1"})[1]
    s1 = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "standard", "request_id": "ls-1"})[1]
    assert p1["admitted"] and s1["admitted"]
    print(f"  离线准入：高级会话 {p1['session_id']}、标准会话 {s1['session_id']}")

    # 超额：高级第二席
    p2 = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "premium", "request_id": "lp-2"})[1]
    assert not p2["admitted"]
    # 标准第二席：标准席满，高级席也满 → 拒绝
    s2 = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "standard", "request_id": "ls-2"})[1]
    assert not s2["admitted"]
    print(f"  高级再开 → {p2['reason']}；标准再开 → {s2['reason']}（并发不能多开）")

    # 幂等重试不多开
    p1_replay = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "premium", "request_id": "lp-1"})[1]
    assert p1_replay["session_id"] == p1["session_id"]
    print("  同 request_id 重试返回原会话，不新增占用")

    # 关闭后席位可再用
    demo.s("POST", f"/sessions/{p1['session_id']}/close",
           {"operation_id": "close-lp1"})
    p3 = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "premium", "request_id": "lp-3"})[1]
    assert p3["admitted"]
    print(f"  关闭 {p1['session_id']} 后新高级会话 {p3['session_id']} 顶上")

    # ------------------------------------------------- 4. 中心无法重复分配
    line("4. 中心侧：借期内这份额度不能再分给在线会话或第二个站点")
    # 重启中心进程（数据库持久，包预留仍在）
    new_center = demo.start_center()
    print(f"  中心进程重启 pid={new_center.pid}，离线预留从持久库恢复")

    blocked = demo.c("POST", "/sessions/admit", {
        "org": ORG, "tier": "premium", "request_id": "online-blocked"})[1]
    assert not blocked["admitted"]
    print(f"  新在线高级会话 → admitted=false（{blocked['reason']}），"
          f"原在线会话 {online['session_id']} 不受影响")

    second_pkg = demo.c("POST", "/offline/packages/issue", {
        "org": ORG, "site_id": "site-C", "standard_seats": 1, "premium_seats": 1,
        "expire_at": "2026-02-01T00:00:00Z", "request_id": "issue-4"})[1]
    assert not second_pkg["issued"]
    print(f"  再签一个同规模包给 site-C → issued=false（{second_pkg['reason']}）")

    # 标准池余量：2 标准席中只被主包预留 1 个，剩 1 个仍可在线准入
    # （离线席位不得挡住本可同时容纳的在线会话）
    std_ok = demo.c("POST", "/sessions/admit", {
        "org": ORG, "tier": "standard", "request_id": "online-std"})[1]
    assert std_ok["admitted"]
    print(f"  未被预留的标准席仍可在线准入：{std_ok['session_id']}（离线不挡可容纳的会话）")
    std_blocked = demo.c("POST", "/sessions/admit", {
        "org": ORG, "tier": "standard", "request_id": "online-std-2"})[1]
    assert not std_blocked["admitted"]
    print(f"  再开第二个在线标准会话 → {std_blocked['reason']}（预留之外确无余量）")

    # 签给别的站点的包：中心仍可在有余量时签发，但站点按 site_id 拒绝
    other = demo.c("POST", "/offline/packages/issue", {
        "org": ORG, "site_id": "site-B", "standard_seats": 1, "premium_seats": 0,
        "expire_at": "2026-01-20T00:00:00Z", "request_id": "issue-3"})
    # 标准池此刻可能已满（预留 1 + 在线 1），签发失败也符合承诺；成功则验证站点拒收
    if other[1]["issued"]:
        code, err = demo.s("POST", "/packages/import",
                           {"envelope": other[1]["envelope"]}, expect_error=True)
        assert err["error"] == "package_site_mismatch"
        print(f"  签给 site-B 的真实包在本站导入 → {code} {err['error']}")
    else:
        print(f"  标准池无余量，给 site-B 再签 → issued=false（{other[1]['reason']}）")

    # ------------------------------------------------- 5. 站点重启
    line("5. 站点进程重启：独立持久化存储保证状态延续、不多开")
    site_proc = demo.site_proc
    demo._stop_proc(site_proc)
    demo.site_proc = None
    new_site = demo.start_site()
    print(f"  站点进程重启 pid={new_site.pid}，数据库 {demo.site_db} 原样挂载")

    pkgs = demo.s("GET", "/packages")[1]["packages"]
    assert len(pkgs) == 1 and pkgs[0]["derived_status"] == "active"
    active = [x for x in demo.s("GET", "/sessions")[1]["sessions"]
              if x["status"] == "active"]
    print(f"  重启后包仍 active，本地活跃会话数={len(active)}（s1 标准 + lp-3 高级）")
    assert len(active) == 2
    over = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "premium", "request_id": "lp-after-restart"})[1]
    assert not over["admitted"]
    print(f"  重启后高级席仍占满 → {over['reason']}（持久化防止多开）")

    # ------------------------------------------------- 6. 提前还席
    line("6. 提前还席：站点永久停用包、终止会话并导出交回凭据")
    ret = demo.s("POST", f"/packages/{package_id}/return",
                 {"operation_id": "return-1"})[1]
    assert ret["returned"]
    print(f"  包已停用，终止本地会话: {ret['terminated_sessions']}")
    credential = ret["return_credential"]
    print(f"  导出交回凭据: kind={credential['payload']['kind']} "
          f"package_id={credential['payload']['package_id']}")

    # 站点侧：已交回包不能重新启用/再开
    code, err = demo.s("POST", "/packages/import",
                       {"envelope": pkg["envelope"]}, expect_error=True)
    assert err["error"] == "package_already_returned"
    print(f"  重新导入已交回包 → {code} {err['error']}")
    dead = demo.s("POST", "/sessions/admit", {
        "package_id": package_id, "tier": "standard", "request_id": "after-ret"})[1]
    assert not dead["admitted"] and dead["reason"] == "package_returned"
    print(f"  已交回包上再开会话 → {dead['reason']}")

    # 包文件不是交回凭据
    code, err = demo.c("POST", "/offline/packages/return",
                       {"return_credential": pkg["envelope"]}, expect_error=True)
    assert err["error"] == "invalid_return_credential"
    print(f"  拿包文件当交回凭据送中心 → {code} {err['error']}")

    # ------------------------------------------------- 7. 凭据送回中心
    line("7. 交回凭据送回中心：额度释放可重新分配，重送不重复释放")
    out = demo.c("POST", "/offline/packages/return",
                 {"return_credential": credential, "operation_id": "center-ret-1"})[1]
    assert out["released"]
    print(f"  中心验证通过：released={out['released']}，{out['note']}")

    replay = demo.c("POST", "/offline/packages/return",
                    {"return_credential": credential, "operation_id": "center-ret-2"})[1]
    assert replay["replayed"]
    print(f"  凭据重送 → replayed={replay['replayed']}（不重复释放）")

    freed = demo.c("POST", "/sessions/admit", {
        "org": ORG, "tier": "premium", "request_id": "online-freed"})[1]
    assert freed["admitted"]
    print(f"  释放出的高级席重新分配给在线会话 {freed['session_id']}")

    # 篡改的凭据被拒
    bad = json.loads(json.dumps(credential))
    bad["payload"]["return_secret"] = "forged-secret"
    code, err = demo.c("POST", "/offline/packages/return",
                       {"return_credential": bad}, expect_error=True)
    assert err["error"] == "invalid_return_credential"
    print(f"  伪造交回秘密 → {code} {err['error']}")

    # ------------------------------------------------- 8. 到期演示（第二个包）
    line("8. 到期失效：新借一个短窗口包，时钟推过截止点观察站点行为")
    short = demo.c("POST", "/offline/packages/issue", {
        "org": ORG, "site_id": SITE, "standard_seats": 1, "premium_seats": 0,
        "expire_at": "2026-01-10T00:00:00Z", "request_id": "issue-5"})[1]
    assert short["issued"]
    demo.s("POST", "/packages/import", {"envelope": short["envelope"]})
    short_id = short["package_id"]
    demo.s("POST", "/sessions/admit", {
        "package_id": short_id, "tier": "standard", "request_id": "short-1"})
    print(f"  短窗口包 {short_id} 导入并开了 1 个标准会话，截止 2026-01-10T00:00:00Z")

    # 两端时钟同步推进到截止点（同一 UTC 时间线，显式推进不睡眠）
    adv_site = demo.s("POST", "/clock/advance",
                      {"to": "2026-01-10T00:00:00Z"})[1]
    demo.c("POST", "/clock/advance", {"to": "2026-01-10T00:00:00Z"})
    assert len(adv_site["terminated"]) == 1
    print(f"  站点时钟推进到截止点：终止会话 {adv_site['terminated']}")
    expired_admit = demo.s("POST", "/sessions/admit", {
        "package_id": short_id, "tier": "standard",
        "request_id": "short-after-exp"})[1]
    assert not expired_admit["admitted"]
    print(f"  到期后再开 → {expired_admit['reason']}（包内会话全部终态、不可新开）")

    # 中心侧到期也自动释放预留
    center_now = demo.c("GET", "/clock")[1]["now"]
    after = demo.c("POST", "/sessions/admit", {
        "org": ORG, "tier": "standard", "request_id": "online-after-exp"})[1]
    print(f"  中心时钟={center_now}，到期预留已不占容量，"
          f"在线标准会话 admitted={after['admitted']}")
    assert after["admitted"]

    # 到期后交回凭据仍可登记，但不会二次释放
    late_ret = demo.s("POST", f"/packages/{short_id}/return",
                      {"operation_id": "return-short"})[1]
    center_late = demo.c("POST", "/offline/packages/return",
                         {"return_credential": late_ret["return_credential"]})[1]
    assert center_late["returned"] and not center_late["released"]
    print(f"  到期后交回：returned={center_late['returned']} "
          f"released={center_late['released']}（仅登记，不重复计数）")

    line("全部断言通过：借、离线用、不可重复分配、还、重新分配、到期均符合承诺")


if __name__ == "__main__":
    main()
