"""归属键的签名、验签与三级派生。"""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from core.auth import create_access_token
from dependencies.owner import (
    ANON_COOKIE,
    ANON_HEADER,
    OwnerContext,
    _issue,
    _sign,
    _verify,
    get_owner,
)


# ---------- 纯函数：签名与验签 ----------


def test_issue_produces_verifiable_value():
    raw = _issue()
    assert _verify(raw) is not None
    assert len(raw.split(".")) == 2


def test_issue_is_unique_per_call():
    assert _issue() != _issue()


def test_owner_key_length_within_model_limit():
    """projects.owner_key 的模型上限是 64 字符。"""
    assert len(f"anon:{_issue()}") <= 64


def test_verify_rejects_tampered_nonce():
    raw = _issue()
    nonce, sig = raw.split(".")
    tampered = f"{'0' * len(nonce)}.{sig}"
    assert _verify(tampered) is None


def test_verify_rejects_tampered_signature():
    raw = _issue()
    nonce, sig = raw.split(".")
    flipped = "0" if sig[0] != "0" else "1"
    assert _verify(f"{nonce}.{flipped}{sig[1:]}") is None


def test_verify_rejects_client_made_key():
    """客户端自造 anon-xxx 必须无效——这正是旧 getOwnerKey 的问题。"""
    assert _verify("anon-abcdefgh-1234567890") is None
    assert _verify("") is None
    assert _verify(None) is None


def test_verify_rejects_wrong_number_of_parts():
    assert _verify("no-dot-here") is None
    assert _verify("a.b.c") is None


# ---------- 依赖：三级派生 ----------


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe(owner: OwnerContext = Depends(get_owner)):
        return {"owner_key": owner.owner_key, "anon_key": owner.anon_key}

    return app


async def _call(headers=None, cookies=None) -> dict:
    # cookie 挂在 client 上而非逐请求传：httpx 已弃用 per-request cookies，
    # 逐请求传会为每个用例刷一条 DeprecationWarning。
    async with AsyncClient(
        transport=ASGITransport(app=_probe_app()),
        base_url="http://test",
        cookies=cookies or None,
    ) as http_client:
        response = await http_client.get("/probe", headers=headers or {})
        return response.json()


async def test_anonymous_gets_server_issued_key():
    body = await _call()
    assert body["owner_key"].startswith("anon:")
    assert body["anon_key"] is not None
    assert _verify(body["anon_key"]) is not None


async def test_cookie_is_honoured():
    raw = _issue()
    body = await _call(cookies={ANON_COOKIE: raw})
    assert body["owner_key"] == f"anon:{raw}"
    assert body["anon_key"] == raw


async def test_header_is_honoured():
    raw = _issue()
    body = await _call(headers={ANON_HEADER: raw})
    assert body["owner_key"] == f"anon:{raw}"


async def test_forged_cookie_falls_through_to_new_identity():
    body = await _call(cookies={ANON_COOKIE: "anon-forged-value"})
    assert body["owner_key"] != "anon:anon-forged-value"
    assert body["owner_key"].startswith("anon:")


async def test_login_subject_wins_over_cookie():
    """登录身份必须优先于匿名 cookie，否则登录后看不到自己的项目。"""
    token = create_access_token({"sub": "user-123", "email": "a@b.c"})
    body = await _call(
        headers={"Authorization": f"Bearer {token}"},
        cookies={ANON_COOKIE: _issue()},
    )
    assert body["owner_key"] == "user:user-123"
    assert body["anon_key"] is None


async def test_invalid_token_falls_back_to_anonymous():
    body = await _call(headers={"Authorization": "Bearer not-a-jwt"})
    assert body["owner_key"].startswith("anon:")
