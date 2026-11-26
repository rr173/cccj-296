"""离线包与交回凭据的签名原语（HMAC-SHA256）。

信封格式：
    {"alg": "HS256", "payload": <业务对象>, "signature": <hex>}
签名覆盖 canonical(payload)：sort_keys、紧凑分隔符、ensure_ascii=False，
保证同内容跨进程/跨语言字节一致。篡改 payload 任意字段都会使验签失败。

密钥约定：中心在 service_meta 中持久化一把随机密钥（也可由环境变量
LICENSE_SIGNING_KEY 注入，演示时中心与站点借此获得同一把密钥）；
站点只做验签，不生成密钥。交回凭据在签发时由中心**预签**后嵌入包内，
站点进程本身不具备为其他包签发凭据的能力。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3

ALG = "HS256"
META_KEY = "hmac_signing_key"


class InvalidSignature(Exception):
    """信封签名不合法或结构不受支持。"""


def canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _normalize_key(raw: str) -> bytes:
    """环境变量里给 64 位 hex 就直接解码，否则对字符串做 SHA-256 定长派生。"""
    raw = raw.strip()
    try:
        if len(raw) == 64:
            return bytes.fromhex(raw)
    except ValueError:
        pass
    return hashlib.sha256(raw.encode("utf-8")).digest()


def get_or_create_center_key(conn: sqlite3.Connection) -> bytes:
    """中心侧：优先 service_meta，其次环境变量，否则生成并落库。

    必须在调用方已打开的写事务内调用。
    """
    row = conn.execute(
        "SELECT value FROM service_meta WHERE key=?", (META_KEY,)
    ).fetchone()
    if row is not None:
        return bytes.fromhex(row[0])
    env = os.environ.get("LICENSE_SIGNING_KEY")
    key = _normalize_key(env) if env else secrets.token_bytes(32)
    conn.execute(
        "INSERT INTO service_meta(key, value) VALUES(?, ?)",
        (META_KEY, key.hex()),
    )
    return key


def load_site_key() -> bytes:
    """站点侧：密钥只能随部署带外注入，环境变量缺失即拒绝启动。"""
    env = os.environ.get("LICENSE_SIGNING_KEY")
    if not env:
        raise RuntimeError(
            "站点缺少 LICENSE_SIGNING_KEY：无法验证中心签发的离线包")
    return _normalize_key(env)


def sign(key: bytes, payload: dict) -> dict:
    sig = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()
    return {"alg": ALG, "payload": payload, "signature": sig}


def verify(key: bytes, envelope: dict) -> dict:
    if not isinstance(envelope, dict) or envelope.get("alg") != ALG:
        raise InvalidSignature("信封 alg 不受支持")
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise InvalidSignature("信封结构不完整")
    expected = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature("签名校验失败：包内容可能已被篡改")
    return payload


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
