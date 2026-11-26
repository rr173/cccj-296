"""测试收款方（payment_stub）的 HTTP 客户端。

关键语义：**传输层失败、超时和 5xx 一律视为「结果未知」**，绝不是支付失败，
也绝不是退款/关闭成功。只有收款方明确返回 4xx 业务错误时才是确定结论。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from http.client import HTTPException
from urllib.parse import urlencode


class PaymentUnavailable(Exception):
    """无法确定收款方结果（超时、连接中断、5xx、响应不可解析）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PaymentRejected(Exception):
    """收款方给出了明确的业务拒绝（4xx，带错误码）。"""

    def __init__(self, code: str, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


class PaymentClient:
    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------ 底层调用

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.getcode()
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = _loads(raw)
            except Exception:  # noqa: BLE001 - 错误体不可解析不影响状态码语义
                payload = {}
            if exc.code >= 500:
                raise PaymentUnavailable(f"http_{exc.code}") from exc
            return exc.code, payload
        except (urllib.error.URLError, socket.timeout, TimeoutError, HTTPException,
                ConnectionError, OSError, ValueError) as exc:
            raise PaymentUnavailable(type(exc).__name__) from exc
        return status, _loads(raw)

    @staticmethod
    def _expect_charge(status: int, payload: dict) -> dict:
        if status in (200, 201):
            return payload
        if status >= 400:
            raise PaymentRejected(payload.get("error", f"http_{status}"), payload)
        raise PaymentUnavailable(f"unexpected_status_{status}")

    # ------------------------------------------------------------ 业务接口

    def create_charge(self, *, merchant_order_id: str, amount_cents: int,
                      currency: str, mode: str) -> dict:
        status, payload = self._call("POST", "/charges", {
            "merchant_order_id": merchant_order_id,
            "amount_cents": amount_cents,
            "currency": currency,
            "mode": mode,
        })
        return self._expect_charge(status, payload)

    def get_charge(self, charge_id: str) -> dict:
        status, payload = self._call("GET", f"/charges/{charge_id}")
        return self._expect_charge(status, payload)

    def find_charge(self, merchant_order_id: str) -> dict | None:
        """按商户订单号查询；明确 404 返回 None，网络问题抛 PaymentUnavailable。"""
        status, payload = self._call(
            "GET", "/charges?" + urlencode({"merchant_order_id": merchant_order_id}))
        if status == 200:
            return payload.get("charge", payload)
        if status == 404:
            return None
        if status >= 400:
            raise PaymentRejected(payload.get("error", f"http_{status}"), payload)
        raise PaymentUnavailable(f"unexpected_status_{status}")

    def capture(self, charge_id: str) -> dict:
        status, payload = self._call("POST", f"/charges/{charge_id}/capture", {})
        return self._expect_charge(status, payload)

    def void(self, charge_id: str) -> dict:
        status, payload = self._call("POST", f"/charges/{charge_id}/void", {})
        return self._expect_charge(status, payload)

    def refund(self, charge_id: str) -> dict:
        status, payload = self._call("POST", f"/charges/{charge_id}/refund", {})
        return self._expect_charge(status, payload)

    # ------------------------------------------------------------ 测试控制（仅测试收款方提供）

    def control(self, payload: dict) -> dict:
        status, response = self._call("POST", "/_control", payload)
        if status != 200:
            raise PaymentRejected(response.get("error", f"http_{status}"), response)
        return response

    def stub_state(self) -> dict:
        status, response = self._call("GET", "/_state")
        if status != 200:
            raise PaymentRejected(response.get("error", f"http_{status}"), response)
        return response


def _loads(raw: bytes) -> dict:
    if not raw:
        return {}
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else {"value": value}
