"""微信相关：登录(code2session)、内容安全、订阅消息。

全部是"配好即用、不配也不报错"的设计：没配 appid/secret 时
返回 None / 跳过，服务照常跑（开发模式）。
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

_token_cache: dict = {"token": "", "expire_at": 0}


async def code2session(code: str, cfg) -> str | None:
    """用小程序 wx.login() 拿到的 code 换 openid。未配置时返回 None。"""
    if not cfg.appid or not cfg.secret:
        return None
    url = "https://api.weixin.qq.com/sns/jscode2session"
    params = {"appid": cfg.appid, "secret": cfg.secret,
              "js_code": code, "grant_type": "authorization_code"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
    data = resp.json()
    openid = data.get("openid")
    if not openid:
        log.warning("code2session 失败: %s", data)
    return openid


async def get_access_token(cfg) -> str | None:
    if not cfg.appid or not cfg.secret:
        return None
    if _token_cache["expire_at"] > time.time() + 60:
        return _token_cache["token"]
    url = "https://api.weixin.qq.com/cgi-bin/token"
    params = {"grant_type": "client_credential", "appid": cfg.appid, "secret": cfg.secret}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
    data = resp.json()
    token = data.get("access_token")
    if token:
        _token_cache.update(token=token,
                            expire_at=time.time() + int(data.get("expires_in", 7000)))
    return token


async def img_sec_check(image_bytes: bytes, cfg) -> bool:
    """图片内容安全检查。未开启/未配置时直接返回 True（跳过）。"""
    if not cfg.seccheck_enabled:
        return True
    token = await get_access_token(cfg)
    if not token:
        return True
    url = f"https://api.weixin.qq.com/wxa/img_sec_check?access_token={token}"
    files = {"media": ("homework.jpg", image_bytes, "image/jpeg")}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, files=files)
    data = resp.json()
    ok = data.get("errcode") == 0
    if not ok:
        log.warning("内容安全检查未通过: %s", data)
    return ok


async def send_subscribe_message(openid: str, task_id: str, summary: str, cfg) -> bool:
    """批改完成通知（一次性订阅消息）。TODO: 在 run_grading 成功后调用。"""
    token = await get_access_token(cfg)
    if not token or not cfg.subscribe_template_id:
        return False
    url = f"https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token={token}"
    payload = {
        "touser": openid,
        "template_id": cfg.subscribe_template_id,
        "page": f"pages/result/result?task_id={task_id}",
        "data": {
            "thing1": {"value": "作业批改完成"},
            "thing2": {"value": summary[:20]},
        },
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
    ok = resp.json().get("errcode") == 0
    if not ok:
        log.warning("订阅消息发送失败: %s", resp.text[:300])
    return ok
