"""组织购权本机演示：独立收款方进程 + 服务进程。

覆盖需求里点名的两幕：
  A. 一次扣款结果未知（收款方已处理后响应丢失）→ 进程重启后从已保存订单恢复，
     最终确认扣款并发放权益，且只扣一次、只发一份。
  B. 取消后确认无款且未发权益；以及取消已接受却查到扣款 → 全额退款、仍不发权益。

直接运行：  python3 -m demo.demo_purchase
不依赖真实支付；收款方是 app.payment_stub（独立 SQLite）。
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
ORG = "demo-buyer"


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
            time.sleep(0.1)
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


def _start_stub(db_path: Path) -> tuple[subprocess.Popen, str, int]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.payment_stub", "--port", str(port), "--db", str(db_path)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _wait_port(port)
    return proc, f"http://127.0.0.1:{port}", port


def _start_service(db_path: Path, payment_url: str, port: int | None = None,
                   epoch: str = "2026-01-01T00:00:00Z") -> tuple[subprocess.Popen, str]:
    port = port or _free_port()
    env = dict(os.environ, LICENSE_DB=str(db_path), LICENSE_EPOCH=epoch,
               PAYMENT_BASE_URL=payment_url, PAYMENT_TIMEOUT="2.0")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _wait_port(port)
    return proc, f"http://127.0.0.1:{port}"


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _order_line(tag: str, order: dict) -> None:
    money = order["money"]
    ent = order["entitlement"]
    cancel = order["cancel"]
    print(f"  [{tag}] stage={order['stage']:<10} status={order['status']:<14} "
          f"money={money['state']:<8} charge={money['charge_id'] or '-'}")
    print(f"         cancel.accepted={cancel['accepted']} "
          f"settlement={cancel['settlement']} settled={cancel['settled']}  "
          f"ent={'✓ ' + ent['ent_id'] if ent else '✗ 无'}")


def _stub_ledger(stub_base: str) -> None:
    state = _http("GET", stub_base + "/_state")
    print("  独立收款方账本:")
    for charge in state["charges"]:
        print(f"    charge {charge['charge_id']} moid={charge['merchant_order_id']} "
              f"state={charge['state']} captures={charge['captures']} "
              f"refunds={charge['refunds']} amount={charge['amount_cents']}分")
    ops = state["ops"]
    print(f"    操作计数: {ops}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="purchase_demo_"))
    stub_db = workdir / "payment_stub.db"
    service_db = workdir / "service.db"
    print(f"工作目录: {workdir}")
    print(f"服务库:   {service_db}（订单与权益）")
    print(f"收款方库: {stub_db}（独立进程、独立持久化；两库不共享事务）")

    stub_proc, stub_base, stub_port = _start_stub(stub_db)
    service_proc, api = _start_service(service_db, stub_base)
    try:
        # ------------------------------------------------------ 套餐
        _banner("0. 预设套餐：固定价格 / 权益种类 / 席位 / 时长")
        _http("POST", api + "/packages", {
            "code": "pro-monthly", "price_cents": 12800, "currency": "CNY",
            "tier": "premium", "seats": 3, "duration_seconds": 30 * 86400,
            "charge_mode": "direct"})
        _http("POST", api + "/packages", {
            "code": "std-freeze", "price_cents": 5000, "currency": "CNY",
            "tier": "standard", "seats": 2, "duration_seconds": 10 * 86400,
            "charge_mode": "freeze"})
        for pkg in _http("GET", api + "/packages")["packages"]:
            print(f"  {pkg['code']}: {pkg['price_cents']}分 {pkg['currency']} "
                  f"tier={pkg['tier']} seats={pkg['seats']} "
                  f"{pkg['duration_seconds']}秒 mode={pkg['charge_mode']}")

        # ------------------------------------------------------ 场景 A 前置
        _banner("1. 正常下单：直接扣款 -> 承诺 -> 扣款确认 -> 发放权益")
        order = _http("POST", api + "/purchases", {
            "org": ORG, "package_code": "pro-monthly", "request_id": "buy-normal"})
        _order_line("normal", order)
        # 权益立即可用于现有会话准入
        admit = _http("POST", api + "/sessions/admit", {
            "org": ORG, "tier": "premium", "request_id": "sess-normal"})
        print(f"  会话准入: admitted={admit['admitted']} "
              f"占用权益={admit.get('assigned_ent_id')}")
        window = order["ent_window"]
        print(f"  权益有效期从实际发放时的逻辑时间起算: "
              f"[{window['effective_at']}, {window['expire_at']})")
        # 同组织重复相同下单 -> 同一订单，不重复扣款
        again = _http("POST", api + "/purchases", {
            "org": ORG, "package_code": "pro-monthly", "request_id": "buy-normal"})
        print(f"  同 request_id 重复下单: order_id 相同="
              f"{again['order_id'] == order['order_id']}, 阶段={again['stage']}")
        _stub_ledger(stub_base)

        # ------------------------------------------------------ 场景 A
        _banner("2. 扣款结果未知：收款方已处理后丢失响应 -> 杀进程 -> 重启后恢复")
        _http("POST", stub_base + "/_control", {"action": "drop", "op": "create"})
        unknown = _http("POST", api + "/purchases", {
            "org": ORG, "package_code": "pro-monthly", "request_id": "buy-unknown"})
        _order_line("未知", unknown)
        print("  服务只看到「结果未知」：订单保持处理中、无权益 —— 不把超时当支付失败")
        _stub_ledger(stub_base)

        print("\n  现在杀掉服务进程并重启，从已保存的订单继续…")
        service_proc.terminate()
        service_proc.wait(timeout=10)
        service_proc, api = _start_service(service_db, stub_base, epoch="2026-01-01T00:00:00Z")
        restart_order = _http("GET", api + "/purchases/" + unknown["order_id"])
        _order_line("重启后", restart_order)
        print("  显式触发对账（不要求后台调度）…")
        recovered = _http("POST", api + f"/purchases/{unknown['order_id']}/reconcile")
        _order_line("恢复", recovered)
        _stub_ledger(stub_base)
        ent_ids = {r["ent_id"] for r in
                   [e for e in _entitlements(service_db) if e["order_id"] == unknown["order_id"]]}
        print(f"  校验: 该订单扣款次数="
              f"{_charge_captures(stub_base, unknown['merchant_order_id'])}, "
              f"发放权益份数={len(ent_ids)}（期望各自 1）")
        # 已出结果的收款重试/恢复不重置权益有效期
        window_before = recovered["ent_window"]
        again = _http("POST", api + f"/purchases/{unknown['order_id']}/reconcile")
        print(f"  再次对账: 权益有效期不变="
              f"{again['ent_window'] == window_before}, 阶段={again['stage']}")

        # ------------------------------------------------------ 场景 B1
        _banner("3. 取消后确认无款且未发权益")
        # 收款方连交易都不接收（创建不可用）——订单只有「未知」，取消后对账确认无款
        _http("POST", stub_base + "/_control", {"creates_unavailable": True,
                                                "queries_unavailable": False})
        no_money = _http("POST", api + "/purchases", {
            "org": "demo-cancel-org", "package_code": "std-freeze",
            "request_id": "buy-cancel-1"})
        _order_line("未知", no_money)
        _http("POST", stub_base + "/_control", {"creates_unavailable": False})
        canceled = _http("POST", api + f"/purchases/{no_money['order_id']}/cancel",
                         {"cancel_request_id": "cancel-1"})
        _order_line("取消", canceled)
        print(f"  取消结清方式={canceled['cancel']['settlement']} "
              f"（订单显示为已结清的无款终结: {canceled['cancel']['settled']}）")
        _stub_ledger(stub_base)
        print(f"  该组织权益数="
              f"{len([e for e in _entitlements(service_db) if e['org'] == 'demo-cancel-org'])}"
              f"（期望 0）")

        # ------------------------------------------------------ 场景 B2
        _banner("4. 取消已接受后才发现扣款成功：全额退款，仍不发权益")
        _http("POST", stub_base + "/_control", {"action": "drop", "op": "create"})
        _http("POST", stub_base + "/_control", {"queries_unavailable": True})
        hidden = _http("POST", api + "/purchases", {
            "org": "demo-cancel-org", "package_code": "pro-monthly",
            "request_id": "buy-cancel-2"})
        _order_line("未知", hidden)
        cancel2 = _http("POST", api + f"/purchases/{hidden['order_id']}/cancel",
                        {"cancel_request_id": "cancel-2"})
        _order_line("取消", cancel2)
        print("  退款/查询结果还不确定：不显示为无款终结。恢复查询后继续对账…")
        _http("POST", stub_base + "/_control",
              {"queries_unavailable": False, "mutations_unavailable": False})
        final = _http("POST", api + f"/purchases/{hidden['order_id']}/reconcile")
        _order_line("结清", final)
        print(f"  结清方式={final['cancel']['settlement']}；即使查到扣款成功也不发权益")
        _stub_ledger(stub_base)
        print(f"  该组织权益数="
              f"{len([e for e in _entitlements(service_db) if e['org'] == 'demo-cancel-org'])}"
              f"（期望 0）")

        # ------------------------------------------------------ 场景 C
        _banner("5. 承诺后取消被明确拒绝，服务继续履约")
        _http("POST", stub_base + "/_control", {"mutations_unavailable": True})
        frozen = _http("POST", api + "/purchases", {
            "org": "demo-promise-org", "package_code": "std-freeze",
            "request_id": "buy-promise-1"})
        _order_line("冻结后", frozen)
        print(f"  冻结成功给出足额保障 -> 可承诺交付（basis={frozen['delivery_basis']}），"
              f"但扣款未发生 -> 无权益")
        refused = _http("POST", api + f"/purchases/{frozen['order_id']}/cancel",
                        {"cancel_request_id": "cancel-3"})
        print(f"  取消尝试: HTTP {refused.get('_http_status')} 错误={refused.get('error')}")
        _http("POST", stub_base + "/_control", {"mutations_unavailable": False})
        done = _http("POST", api + f"/purchases/{frozen['order_id']}/reconcile")
        _order_line("继续履约", done)

        # ------------------------------------------------------ 查询视角
        _banner("6. 查询：区分处理中/已承诺/已发放/取消结清，并可追查交易与权益")
        for o in _http("GET", api + "/purchases/" + unknown["order_id"]), final:
            print(f"\n  订单 {o['order_id']}（请求 {o['request_id']}）")
            print(f"    stage={o['stage']} promised={o['promised']} "
                  f"delivered={o['delivered']} processing={o['processing']}")
            print(f"    资金: state={o['money']['state']} charge={o['money']['charge_id']}")
            print(f"    取消: accepted={o['cancel']['accepted']} "
                  f"settlement={o['cancel']['settlement']} settled={o['cancel']['settled']}")
            print(f"    权益: {o['entitlement']['ent_id'] if o['entitlement'] else '无'}")
            print(f"    与收款方交互（{len(o['money_ops'])} 条，含 unknown）:")
            for op in o["money_ops"]:
                print(f"      {op['op_type']:<14} outcome={op['outcome']:<8} seq={op['seq']}")

        print("\n演示通过。")
    finally:
        for proc in (service_proc, stub_proc):
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
    print(f"\n数据库保留在: {workdir}")


# ---------------------------------------------------------------- 只读核对


def _entitlements(service_db: Path) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(service_db)
    try:
        rows = conn.execute(
            "SELECT ent_id, org, tier, seats, effective_at, expire_at FROM entitlements"
        ).fetchall()
        # order_id 从事件流里取（权益只由购权发放时才有关联）
        issued = conn.execute(
            "SELECT detail_json FROM events WHERE kind='purchase.delivered'").fetchall()
        by_ent = {}
        for (detail,) in issued:
            d = json.loads(detail)
            by_ent[d["ent_id"]] = d["order_id"]
        return [{
            "ent_id": r[0], "org": r[1], "tier": r[2], "seats": r[3],
            "effective_at": r[4], "expire_at": r[5],
            "order_id": by_ent.get(r[0]),
        } for r in rows]
    finally:
        conn.close()


def _charge_captures(stub_base: str, merchant_order_id: str) -> int:
    state = _http("GET", stub_base + "/_state")
    for c in state["charges"]:
        if c["merchant_order_id"] == merchant_order_id:
            return c["captures"]
    return 0


if __name__ == "__main__":
    main()
