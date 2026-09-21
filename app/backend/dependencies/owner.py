"""项目归属身份的服务端派生（设计文档 2026-09-20 S1.1）。

不变量 1 —— 归属由服务端派生，请求体永不参与：

    登录 JWT 的 sub > 签名 cookie > X-Atoms-Anon 请求头 > 服务端新签发

客户端提供的任何字段（含历史遗留的 ``owner_key``）不得影响归属。
匿名标识为 HMAC 签名（nonce.sig，共 49 字符），客户端无法伪造有效签名；
cookie + 请求头双通道传输，消除「线上网关吃掉 Set-Cookie 就静默失效」的单点。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth import AccessTokenError, decode_access_token
from core.config import settings

logger = logging.getLogger(__name__)

ANON_COOKIE = "atoms_anon"
ANON_HEADER = "X-Atoms-Anon"
ANON_MAX_AGE = 180 * 24 * 3600  # 180 天

bearer_scheme = HTTPBearer(auto_error=False)

# JWT_SECRET_KEY 未配置时的兜底签名密钥：保证匿名标识在重启后仍可验签，
# 避免老访客一夜之间「项目消失」。生产部署平台始终注入 JWT_SECRET_KEY，
# 该兜底仅覆盖本地/演示环境。
_FALLBACK_SECRET = "atoms-studio-anon-fallback-key"


def _secret() -> bytes:
    return (getattr(settings, "jwt_secret_key", None) or _FALLBACK_SECRET).encode()


@dataclass(frozen=True)
class OwnerContext:
    """一次请求解析出的归属身份。"""

    owner_key: str  # "user:<sub>" | "anon:<nonce>.<sig>"
    anon_key: Optional[str]  # 匿名身份时为 raw（nonce.sig）；登录身份时为 None


def _sign(nonce: str) -> str:
    return hmac.new(_secret(), nonce.encode(), hashlib.sha256).hexdigest()[:16]


def _verify(raw: str) -> Optional[str]:
    """校验匿名标识签名；通过返回 nonce，否则 None。"""
    parts = raw.split(".")
    if len(parts) != 2:
        return None
    nonce, sig = parts
    if len(nonce) != 32 or len(sig) != 16:
        return None
    if not hmac.compare_digest(_sign(nonce), sig):
        return None
    return nonce


def _issue() -> str:
    """签发新的匿名标识：nonce(32) + '.' + sig(16) = 49 字符。"""
    nonce = secrets.token_hex(16)
    return f"{nonce}.{_sign(nonce)}"


def _from_client_value(raw: Optional[str]) -> Optional[OwnerContext]:
    """验签客户端携带的匿名标识（cookie 或请求头），通过则复用其身份。"""
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > 49:
        return None
    if _verify(candidate) is None:
        return None
    return OwnerContext(owner_key=f"anon:{candidate}", anon_key=candidate)


async def get_owner(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> OwnerContext:
    """解析请求的归属身份。

    签发策略（§8 偏离 1）：所有 atoms 路由共用这一个依赖；凡解析不出有效
    标识的请求，一律视为新匿名会话并签发（幂等）。不采用「只在
    list_projects/create_project 下发」的写法——generate 也可能是入口
    （旧标签页直接提交），统一后无分支、不依赖前端调用顺序。

    JWT 解析失败（AccessTokenError / 密钥未配置）**不报错**，落到匿名分支，
    保证未登录用户始终可用（FR-014）。
    """
    # ① 登录 JWT
    if credentials and credentials.credentials:
        try:
            payload = decode_access_token(credentials.credentials)
            sub = payload.get("sub")
            if sub:
                return OwnerContext(owner_key=f"user:{sub}"[:64], anon_key=None)
        except AccessTokenError:
            pass
        except Exception:  # noqa: BLE001 - 任何 token 异常都不得阻断匿名访问
            logger.debug("access token 解析异常，回落到匿名身份")

    # ② 签名 cookie
    ctx = _from_client_value(request.cookies.get(ANON_COOKIE))
    if ctx:
        return ctx

    # ③ X-Atoms-Anon 请求头（双通道）
    ctx = _from_client_value(request.headers.get(ANON_HEADER))
    if ctx:
        return ctx

    # ④ 均无效：新签发，由路由层经 Set-Cookie + 响应体回传
    raw = _issue()
    return OwnerContext(owner_key=f"anon:{raw}", anon_key=raw)
