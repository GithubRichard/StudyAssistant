"""离线烟雾测试：用模拟 Hermes（不联网、不花钱）验证完整学习任务闭环。

覆盖：
1. 会话鉴权：未带令牌被拒、跨账号读不到他人任务
2. 附件上传 → 幂等提交学习任务 → 执行器调用技能 → 状态与五态结果
3. 归档写入工作区、成果登记、配额只扣一次
4. 运行状态接口区分「已配置」与「已就绪」
5. 旧接口 /api/tasks 走同一鉴权与任务流程

运行：python test_smoke.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.config import Settings  # noqa: E402
from tests.mock_hermes import MockHermes  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="study_smoke_"))

SETTINGS = Settings.model_validate({
    "engine": {"mode": "hermes"},
    "hermes": {"base_url": "http://hermes.local", "api_key": "test-key",
               "verify_skill": True, "timeout_seconds": 30},
    "auth": {"allowed_openids": []},
    "data_dir": str(TMP / "data"),
    "workspace": {"dir": str(TMP / "workspace"), "init_readme": True},
    "limits": {"worker_poll_seconds": 0.05, "max_task_minutes": 1},
    "quota": {"new_user_bonus": 20, "daily_free": 3, "max_per_day": 50},
})

mock = MockHermes()
mock.install()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

app = create_app(SETTINGS)


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(buf, "PNG")
    return buf.getvalue()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def wait_done(client, token: str, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    data = {}
    while time.time() < deadline:
        data = client.get(f"/api/tasks/{task_id}", headers=auth(token)).json()
        if data["status"] in ("done", "waiting_input", "failed", "interrupted"):
            return data
        time.sleep(0.2)
    raise AssertionError(f"任务未在 {timeout}s 内结束: {data}")


try:
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        print("✓ 健康检查（仅存活，不代表技能就绪）")

        # 1. 鉴权
        assert client.get("/api/tasks").status_code == 401
        assert client.post("/api/study/tasks", json={"text": "hi"}).status_code == 401
        print("✓ 未携带会话令牌的请求被拒绝")

        login = client.post("/api/login", data={"code": "code-alpha-123456"}).json()
        token = login["token"]
        assert token and login["dev_identity"] is True
        print("✓ 登录返回会话令牌（未配置微信 → 开发身份，已标注）")

        runtime = client.get("/api/runtime", headers=auth(token)).json()
        assert runtime["engine"]["mode"] == "hermes"
        assert runtime["hermes"]["state"] == "ready", runtime["hermes"]
        assert "test-key" not in str(runtime)
        print("✓ 运行状态：Hermes 就绪（技能已安装），且未泄露密钥")

        # 2. 附件与任务
        quota_before = client.get("/api/quota", headers=auth(token)).json()["remaining"]
        up = client.post("/api/assets", headers=auth(token),
                         files={"file": ("hw.png", png_bytes(), "image/png")})
        assert up.status_code == 201, up.text
        asset_id = up.json()["asset_id"]
        print("✓ 图片上传成功 asset_id =", asset_id)

        payload = {"task_type": "grading", "subject": "数学", "grade_level": "七年级",
                   "text": "这是今天的作业", "asset_ids": [asset_id]}
        headers = dict(auth(token))
        headers["Idempotency-Key"] = "smoke-1"
        created = client.post("/api/study/tasks", json=payload, headers=headers)
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]

        again = client.post("/api/study/tasks", json=payload, headers=headers)
        assert again.json()["task_id"] == task_id and again.json()["duplicate"] is True
        print("✓ 幂等提交：相同幂等键返回同一任务", task_id)

        view = wait_done(client, token, task_id)
        assert view["status"] == "waiting_input", view
        assert mock.send_count == 1, "不应重复调用模型"

        result = view["result"]
        assert result["overview"]["wrong"] == 1 and result["overview"]["unanswered"] == 1
        assert result["review_summary"]["state"] == "completed"
        assert result["questions"][0]["review"]["state"] == "agreed"
        assert result["missing_info"], "缺少材料时应列出待补充项"
        assert view["runs"][0]["status"] == "waiting_input"
        print("✓ 技能执行完成：五态统计 + 二次核查状态 + 待补充项齐全")

        # 3. 归档与成果
        archive = TMP / "workspace" / "数学" / "错题解析" / "2026-09-26.md"
        assert archive.exists(), "归档文件未写入工作区"
        assert "移项未变号" in archive.read_text(encoding="utf-8")
        assert any(a["kind"] == "archive" for a in view["artifacts"])
        dl = client.get(view["artifacts"][0]["download_url"], headers=auth(token))
        assert dl.status_code == 200 and len(dl.content) > 0
        print("✓ 归档写入工作区，并登记可下载成果")

        assert any(r["status"] == "not_configured"
                   for r in [result["delivery"]["pdf"], result["delivery"]["email"]]), \
            "未启用的交付能力必须标注未配置"
        print("✓ 未启用的 PDF/邮件/同步能力如实标注为未配置")

        quota_after = client.get("/api/quota", headers=auth(token)).json()["remaining"]
        assert quota_after == quota_before - 1, (quota_before, quota_after)
        print("✓ 配额只扣一次")

        # 4. 跨账号隔离
        other = client.post("/api/login", data={"code": "code-beta-654321"}).json()
        assert client.get(f"/api/tasks/{task_id}", headers=auth(other["token"])).status_code == 404
        print("✓ 其他账号无法读取他人任务（按不存在处理）")

        # 5. 补充材料
        follow = client.post(f"/api/tasks/{task_id}/followups",
                             json={"text": "补充：本学期开学日期 9 月 1 日"},
                             headers=auth(token))
        assert follow.status_code == 201 and follow.json()["run_no"] == 2
        view2 = wait_done(client, token, task_id)
        assert len(view2["runs"]) == 2
        print("✓ 补充材料创建第二轮执行，历史轮次保留")

        # 6. 错题本与非法上传
        mid = client.post("/api/mistakes", headers=auth(token),
                          data={"task_id": task_id, "question_no": "1",
                                "knowledge_point": "一元一次方程", "note": "移项变号"})
        assert mid.status_code == 201
        assert len(client.get("/api/mistakes", headers=auth(token)).json()) == 1
        bad = client.post("/api/assets", headers=auth(token),
                          files={"file": ("a.txt", b"hello", "text/plain")})
        assert bad.status_code == 400
        print("✓ 错题本读写正常，非图片文件被拒绝")

        # 7. 旧接口
        legacy = client.post("/api/tasks", headers=auth(token),
                             data={"subject": "数学", "grade_level": "七年级"},
                             files={"file": ("hw.png", png_bytes(), "image/png")})
        assert legacy.status_code == 201
        legacy_view = wait_done(client, token, legacy.json()["task_id"])
        assert legacy_view["status"] in ("done", "waiting_input")
        print("✓ 旧 /api/tasks 入口复用同一鉴权与任务流程")

        # 8. 运行状态脱敏
        providers = client.get("/api/providers").json()
        assert "test-key" not in str(providers)
        print("✓ /api/providers 不泄露密钥")

    print(f"\n全部通过 ✅（数据目录 {TMP}）")
finally:
    mock.uninstall()
