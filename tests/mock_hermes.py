"""模拟 Hermes Agent API：所有离线验证都使用它，不产生任何外部调用。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

SKILL_NAME = "leo-study-assistant"

LEARNING_RESULT: Dict[str, Any] = {
    "schema_version": 2,
    "task_type": "grading",
    "subject": "数学",
    "grade_level": "七年级",
    "scope": {"start_date": "2026-09-20", "end_date": "2026-09-26", "sources": ["模拟作业 P12"]},
    "overview": {"summary": "移项变号仍需巩固"},
    "questions": [
        {
            "id": "sim-p12-q1",
            "no": "1",
            "source": "模拟作业",
            "page": "P12",
            "stem": "解方程 2x+1=9",
            "student_answer": "x=5",
            "status": "wrong",
            "correct_answer": "x=4",
            "steps": ["2x=8", "x=4"],
            "error_rule": "移项时忘记变号",
            "knowledge_point": "一元一次方程",
            "evidence": "原图 P12 第 1 题",
            "review": {"state": "agreed", "note": "核查未发现异议", "basis": "由 2x=8 得 x=4"},
            "final_decision": "kept_wrong",
            "final_decision_basis": "复核原作答后维持原判定",
        },
        {
            "id": "sim-p12-q2",
            "no": "2",
            "source": "模拟作业",
            "page": "P12",
            "status": "unanswered",
            "final_decision": "pending",
        },
    ],
    "sections": [{"title": "做得好的题", "body": "第 3 题思路完整。"}],
    "missing_info": ["本学期开学日期未提供"],
    "parent_tips": ["每天 10 分钟重做移项变式题"],
    "review_summary": {
        "state": "completed", "scope": 1, "disagreed": 0, "unverified": 0,
        "note": "仅核查已判错题，未发现异议",
    },
    "archive": {
        "suggested_path": "数学/错题解析/2026-09-26.md",
        "action": "append",
        "content_markdown": "- 第 1 题：移项未变号（x=5，应为 x=4）",
    },
    "delivery": {
        "pdf": {"status": "not_configured", "note": "未安装 PDF 能力"},
        "email": {"status": "not_configured", "note": "未配置邮件渠道"},
        "git": {"status": "not_configured", "note": "未配置记录同步"},
    },
}


def completion_payload(result: Optional[Dict[str, Any]] = None,
                       text_prefix: str = "已完成分析。\n") -> Dict[str, Any]:
    body = result if result is not None else LEARNING_RESULT
    content = text_prefix + "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": "hermes-agent",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1234, "completion_tokens": 567},
    }


class MockHermes:
    """可配置的模拟服务，记录收到的请求便于断言。"""

    def __init__(self, *, skills: Optional[List[str]] = None,
                 result: Optional[Dict[str, Any]] = None,
                 fail_mode: str = "") -> None:
        self.skills = skills if skills is not None else [SKILL_NAME]
        self.result = result
        self.fail_mode = fail_mode
        self.requests: List[httpx.Request] = []
        self.send_count = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if self.fail_mode == "unreachable":
            raise httpx.ConnectError("mock: 连不上", request=request)

        if path == "/health":
            # 复现真实环境：/health 在该版本上返回 5xx，但 /v1/health 正常
            if self.fail_mode == "health_500":
                return httpx.Response(500, json={"error": "degraded"})
            return httpx.Response(200, json={"status": "ok"})

        if path == "/v1/health":
            return httpx.Response(200, json={"status": "ok"})

        if path == "/v1/skills":
            if self.fail_mode == "skills_500":
                # 复现真实环境：/v1/skills 内部 500，但网关本身是活的
                return httpx.Response(500, json={
                    "error": {"message": "Failed to enumerate skills",
                              "type": "server_error"}})
            if self.fail_mode == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"skills": [{"name": s} for s in self.skills]})

        if path == "/v1/capabilities":
            return httpx.Response(200, json={"endpoints": ["/v1/chat/completions"]})

        if path == "/v1/chat/completions":
            self.send_count += 1
            if self.fail_mode == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            if self.fail_mode == "server_error":
                return httpx.Response(500, json={"error": "boom"})
            if self.fail_mode == "timeout":
                raise httpx.ReadTimeout("mock: 读取超时", request=request)
            if self.fail_mode == "bad_json":
                return httpx.Response(200, json={
                    "model": "hermes-agent",
                    "choices": [{"message": {"content": "我觉得这题做得不错"}}],
                    "usage": {},
                })
            if self.fail_mode == "invalid_result":
                broken = dict(self.result or LEARNING_RESULT)
                broken["questions"] = [dict(q) for q in broken["questions"]]
                broken["questions"][0]["status"] = "wrong"
                broken["questions"][0]["error_rule"] = "粗心"
                return httpx.Response(200, json=completion_payload(broken))
            return httpx.Response(200, json=completion_payload(self.result))

        return httpx.Response(404, json={"error": f"unknown path {path}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def fake_build_client(self, hermes_client) -> httpx.AsyncClient:
        """替换 HermesClient._build_client 使用的构造器。"""
        cfg = hermes_client.cfg
        return httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {cfg.api_key}",
                     "Content-Type": "application/json"},
            timeout=httpx.Timeout(cfg.timeout_seconds, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport(),
        )

    def install(self) -> None:
        """把模拟传输注入 HermesClient（进程内生效）。"""
        from app.hermes import HermesClient

        self._original = HermesClient._build_client

        def _patched(client_self):
            return self.fake_build_client(client_self)

        HermesClient._build_client = _patched

    def uninstall(self) -> None:
        from app.hermes import HermesClient

        if getattr(self, "_original", None):
            HermesClient._build_client = self._original
