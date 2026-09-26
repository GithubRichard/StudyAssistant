"""会话鉴权与资源归属校验。

要点：
- 登录成功后签发随机不透明令牌，数据库只保存 SHA-256 摘要。
- 业务接口一律以会话身份为准，不再相信客户端自称的 openid。
- 默认家庭私用：可用 `auth.allowed_openids` 白名单限定账号；为空表示不限制（仅适合本地开发）。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any, Dict, Iterable, Optional

log = logging.getLogger(__name__)

TOKEN_BYTES = 32
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 天


class AuthError(Exception):
    """鉴权失败。status_code 供接口层直接映射为 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


WEB_OPENID_PREFIX = "web:"


def verify_password(given: str, expected: str) -> bool:
    """网页版密码校验：恒定时间比较，避免按字符逐位试探。"""
    if not expected:
        return False
    return secrets.compare_digest((given or "").encode("utf-8"), expected.encode("utf-8"))


def web_openid(user: str) -> str:
    """网页账号身份：加上前缀，避免与微信 openid 混淆，也不会命中微信白名单。"""
    name = (user or "").strip() or "family"
    name = "".join(ch for ch in name if ch.isalnum() or ch in "-_")[:32] or "family"
    return WEB_OPENID_PREFIX + name


def is_authorized(openid: str, allowed: Iterable[str] | None) -> bool:
    """白名单为空表示不做账号限制（本地开发）；配置后只允许名单内账号。"""
    allowed = list(allowed or [])
    if not allowed:
        return True
    return openid in allowed


async def issue_session(db_path: str, openid: str, allowed: Iterable[str] | None,
                        ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Dict[str, Any]:
    """校验账号授权后签发会话，返回明文令牌（仅此一次可见）。"""
    from . import db  # 延迟导入，避免循环依赖

    if not is_authorized(openid, allowed):
        raise AuthError("该账号未获得使用授权，请联系管理员添加白名单", 403)

    token = new_token()
    now = time.time()
    session = {
        "session_id": secrets.token_hex(8),
        "openid": openid,
        "token_hash": hash_token(token),
        "created_at": now,
        "expires_at": now + ttl_seconds,
        "last_seen_at": now,
    }
    await db.create_session(db_path, session)
    return {
        "token": token,
        "session_id": session["session_id"],
        "openid": openid,
        "expires_at": session["expires_at"],
    }


async def authenticate(db_path: str, token: str) -> Dict[str, Any]:
    """校验令牌并返回会话信息；过期或不存在一律拒绝。"""
    from . import db

    if not token or len(token) < 16:
        raise AuthError("缺少有效的会话凭证，请重新登录")
    session = await db.get_session_by_token(db_path, hash_token(token))
    if not session:
        raise AuthError("会话已失效，请重新登录")
    if float(session["expires_at"]) <= time.time():
        await db.delete_session(db_path, session["session_id"])
        raise AuthError("会话已过期，请重新登录")
    await db.touch_session(db_path, session["session_id"])
    return session


def ensure_owner(owner_openid: str, session: Dict[str, Any]) -> None:
    """资源和会话归属必须一致；不一致时按「不存在」处理，避免泄露他人资源是否存在。"""
    if owner_openid != session.get("openid"):
        raise AuthError("无权访问该资源", 404)


def extract_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""
