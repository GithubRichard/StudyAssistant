"""问问题聊天 API（#/learn?tab=qa）：多轮对话，SSE 流式返回。

鉴权：沿用网页登录会话，全部接口按 openid 隔离。
AI：引导式学习助手（先给提示、用户明确要答案才给），复用 provider_chain，
取最近 10 轮上下文；纯问答，不带作业/台账上下文。
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import AsyncIterator, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import db
from .api import Session, get_settings
from .config import Settings, provider_chain
from .providers import make_provider

log = logging.getLogger("studyassistant.qa")
router = APIRouter(prefix="/api/qa", tags=["qa"])

_QA_SYSTEM = """你是中学生的课后学习助手，用户在问作业或学习上的问题。
教学策略（必须遵守）：
1. 先给思路提示、问启发性问题，引导学生自己思考，不要一上来就给完整答案或解题全过程。
2. 只有当学生明确说"直接告诉我答案""直接讲""给我完整解答"时，才给出完整解答。
3. 讲解分步骤、用中文；数学公式用纯文本（如 x^2、√2、1/2），不要用 LaTeX。
4. 回答简洁，一次只讲一个关键点；学生追问再展开下一步。
5. 不确定的地方如实说不知道，不要编造；不要猜测学生没给的条件。
6. 语气耐心、鼓励，像家教一样。"""

_HISTORY_TURNS = 10          # 上下文轮数（user+assistant 算一轮）
_MAX_IMAGES = 4              # 单条消息最多图片数
_QA_MAX_TOKENS = 2000


class CreateSessionBody(BaseModel):
    title: str = ""


class SendMessageBody(BaseModel):
    content: str = ""
    images: List[str] = Field(default_factory=list)  # data URL 列表


def _openid(ctx: dict) -> str:
    return ctx.get("openid", "")


async def _get_owned_session(s: Settings, openid: str,
                             session_id: str) -> dict:
    sess = await db.get_qa_session(s.db_path, session_id)
    if not sess or sess.get("openid") != openid:
        raise HTTPException(404, "会话不存在")
    return sess


def _history_text(rows: List[dict]) -> str:
    """最近 N 轮历史拼成 user_prompt（图片只留占位，不重传）。"""
    parts = []
    for r in rows:
        role = "学生" if r.get("role") == "user" else "助手"
        text = (r.get("content") or "").strip()
        imgs = r.get("images") or []
        if imgs:
            text = (text + f" [附{len(imgs)}张图片]" if text else f"[附{len(imgs)}张图片]")
        if text:
            parts.append(f"{role}：{text}")
    return "\n".join(parts)


def _decode_data_url(data_url: str):
    """data:image/jpeg;base64,.... -> (bytes, mime)，失败返回 None。"""
    try:
        head, b64 = data_url.split(",", 1)
        mime = head.split(";")[0].split(":")[1]
        if not mime.startswith("image/"):
            return None
        return base64.b64decode(b64), mime
    except (ValueError, IndexError, base64.binascii.Error):
        return None


@router.get("/sessions")
async def list_sessions(ctx: dict = Session):
    s = get_settings()
    rows = await db.list_qa_sessions(s.db_path, _openid(ctx))
    return {"sessions": rows}


@router.post("/sessions")
async def create_session(body: CreateSessionBody, ctx: dict = Session):
    s = get_settings()
    sid = uuid.uuid4().hex[:16]
    await db.create_qa_session(s.db_path, sid, _openid(ctx),
                               body.title.strip()[:40])
    return {"id": sid}


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, ctx: dict = Session):
    s = get_settings()
    await _get_owned_session(s, _openid(ctx), session_id)
    rows = await db.list_qa_messages(s.db_path, session_id)
    return {"messages": [
        {"id": r["id"], "role": r["role"], "content": r["content"],
         "images": r["images"], "created_at": r["created_at"]}
        for r in rows
    ]}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, ctx: dict = Session):
    s = get_settings()
    await _get_owned_session(s, _openid(ctx), session_id)
    await db.delete_qa_session(s.db_path, session_id)
    return {"deleted": session_id}


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: str, body: SendMessageBody,
                       ctx: dict = Session):
    """保存用户消息，SSE 流式返回 AI 回复。

    事件：data: {"t":"delta","text":"..."} / {"t":"done","message_id":"..."}
         / {"t":"error","message":"..."}
    """
    s = get_settings()
    openid = _openid(ctx)
    sess = await _get_owned_session(s, openid, session_id)

    content = (body.content or "").strip()
    images = [u for u in (body.images or []) if isinstance(u, str)
              and u.startswith("data:image/")][: _MAX_IMAGES]
    if not content and not images:
        raise HTTPException(400, "消息内容为空")

    user_msg_id = uuid.uuid4().hex[:16]
    await db.add_qa_message(s.db_path, user_msg_id, session_id, "user",
                            content, images)
    # 首条消息自动生成标题
    if not (sess.get("title") or "").strip():
        title = (content[:20] if content else "图片问题")
        await db.touch_qa_session(s.db_path, session_id, title=title)

    history = await db.list_qa_messages(s.db_path, session_id, limit=200)
    # 去掉刚存的这条，取之前最近 10 轮
    history = [r for r in history if r["id"] != user_msg_id]
    history = history[-_HISTORY_TURNS * 2:]
    hist_text = _history_text(history)
    user_prompt = (f"以下是之前的对话（供参考）：\n{hist_text}\n\n"
                   f"学生现在问：{content}" if hist_text
                   else f"学生问：{content}")

    chain = provider_chain(s)
    if not chain:
        raise HTTPException(500, "无可用模型")

    async def gen() -> AsyncIterator[str]:
        def ev(obj: dict) -> str:
            return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

        full: List[str] = []
        assistant_msg_id = uuid.uuid4().hex[:16]
        ok = False
        last_err = ""
        for name in chain:
            cfg = s.llm.providers.get(name)
            if cfg is None:
                continue
            prov = make_provider(name, cfg)
            try:
                if images:
                    decoded = [d for d in (_decode_data_url(u) for u in images)
                               if d]
                    if not decoded:
                        last_err = "图片解码失败"
                        continue
                    outcome = await prov.grade_multi(
                        decoded, _QA_SYSTEM, user_prompt,
                        max_tokens=_QA_MAX_TOKENS)
                    text = (outcome.text or "").strip()
                    # 非流式：切块经 SSE 发出，复用同一套前端渲染
                    for i in range(0, len(text), 60):
                        chunk = text[i:i + 60]
                        full.append(chunk)
                        yield ev({"t": "delta", "text": chunk})
                else:
                    async for chunk in prov.stream_text(
                            _QA_SYSTEM, user_prompt, max_tokens=_QA_MAX_TOKENS):
                        if chunk:
                            full.append(chunk)
                            yield ev({"t": "delta", "text": chunk})
                ok = True
                break
            except Exception as e:  # noqa: BLE001 - 换下一个 provider
                last_err = f"{name}: {e}"
                log.warning("问答 %s 调用失败，换备胎：%s", name, e)
                continue

        if not ok:
            yield ev({"t": "error", "message": f"AI 暂时不可用（{last_err}），请稍后重试"})
            return
        text = "".join(full).strip()
        if not text:
            yield ev({"t": "error", "message": "AI 返回为空，请重试"})
            return
        await db.add_qa_message(s.db_path, assistant_msg_id, session_id,
                                "assistant", text)
        yield ev({"t": "done", "message_id": assistant_msg_id})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
