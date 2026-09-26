"""烟雾测试：用模拟模型（不花一分钱）验证
1. 配置加载与多模型 fallback 链
2. 任务全流程：上传 -> 后台批改 -> 轮询到 done -> 配额扣减
3. 错题本、/providers 接口

运行：python test_smoke.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="grader_test_"))
os.environ["CONFIG_PATH"] = str(ROOT / "config.example.yaml")  # 先用模板验证能解析

from PIL import Image, ImageDraw  # noqa: E402

# ---- 1. 验证配置模板能正常解析 ----
from app.config import load_settings, provider_chain  # noqa: E402

example = load_settings(str(ROOT / "config.example.yaml"))
assert example.llm.default_provider == "qwen"
assert "qwen" in example.llm.providers and "glm" in example.llm.providers
print("✓ config.example.yaml 解析通过，providers:", list(example.llm.providers))

# ---- 2. 验证多模型 fallback：第一个挂掉，自动切第二个 ----
from app import grading  # noqa: E402
from app.providers import BaseProvider, GradeOutcome, ProviderError  # noqa: E402


class FailProvider(BaseProvider):
    async def grade(self, *a, **k):
        raise ProviderError("模拟网络故障")


CANNED = ('{"total_questions": 2, "correct_count": 1, "questions": ['
          '{"no": "1", "student_answer": "x=2", "is_correct": false, '
          '"correct_answer": "x=3", "explanation": ["移项要变号"], "knowledge_point": "一元一次方程"},'
          '{"no": "2", "student_answer": "y=5", "is_correct": true, '
          '"correct_answer": "y=5", "explanation": [], "knowledge_point": "代入求值"}],'
          '"summary": "移项变号是失分点"}')


class OkProvider(BaseProvider):
    async def grade(self, *a, **k):
        return GradeOutcome(text=CANNED, input_tokens=3000,
                            output_tokens=500, provider=self.name, model="fake-vl")


real_make_provider = grading.providers.make_provider


def fake_make_provider(name, cfg):
    if name == "primary":
        return FailProvider(name, cfg)
    return OkProvider(name, cfg)


grading.providers.make_provider = fake_make_provider

test_settings = example.model_copy(deep=True)
test_settings.data_dir = str(TMP)
# 构造两个启用的 provider：primary（会挂）-> backup（成功）
pcfg = test_settings.llm.providers["qwen"].model_copy(deep=True)
pcfg.api_key = "fake-key"
test_settings.llm.providers = {"primary": pcfg, "backup": pcfg.model_copy(deep=True)}
test_settings.llm.providers["backup"].api_key = "fake-key-2"
test_settings.llm.default_provider = "primary"
test_settings.llm.fallback_order = ["primary", "backup"]

result, provider, model, itok, otok, cost = asyncio.run(
    grading.grade_image(b"fake", "image/jpeg", "数学", "七年级", test_settings))
assert provider == "backup", f"fallback 失败，实际用了 {provider}"
assert result.correct_count == 1 and result.total_questions == 2
assert result.questions[0].knowledge_point == "一元一次方程"
assert cost > 0
print(f"✓ 多模型 fallback 通过：primary 挂掉 -> 自动切 backup，结果校验通过，花费 {cost} 元")

# 非法 JSON 也会被过滤并换备胎
class BadJsonProvider(BaseProvider):
    async def grade(self, *a, **k):
        return GradeOutcome(text="我觉得这题做得不错", input_tokens=10,
                            output_tokens=10, provider=self.name, model="bad")


def fake_make_provider2(name, cfg):
    return BadJsonProvider(name, cfg) if name == "primary" else OkProvider(name, cfg)


grading.providers.make_provider = fake_make_provider2
result2, provider2, *_ = asyncio.run(
    grading.grade_image(b"fake", "image/jpeg", "数学", "七年级", test_settings))
assert provider2 == "backup"
print("✓ 非法 JSON 输出被过滤并切换备胎")

grading.providers.make_provider = real_make_provider

# ---- 3. 全流程：HTTP 上传 -> 后台批改 -> 轮询 -> 配额/错题本 ----
from fastapi.testclient import TestClient  # noqa: E402
from app.main import create_app  # noqa: E402

os.environ["CONFIG_PATH"] = str(ROOT / "config.example.yaml")
app = create_app(test_settings)

# 批改函数打桩（不调真实模型）
async def fake_grade_image(image_bytes, mime, subject, grade_level, settings):
    from app.grading import GradingResult
    return (GradingResult.model_validate(__import__("json").loads(CANNED)),
            "backup", "fake-vl", 3000, 500, 0.008)

grading.grade_image = fake_grade_image

img = Image.new("RGB", (800, 600), "white")
d = ImageDraw.Draw(img)
d.text((50, 50), "1. x+2=5  x=2", fill="black")
buf = io.BytesIO()
img.save(buf, "PNG")

with TestClient(app) as client:
    assert client.get("/healthz").json() == {"ok": True}

    r = client.post("/api/login", data={"code": "testcode123"})
    openid = r.json()["openid"]
    assert openid.startswith("dev_")
    print("✓ 登录（开发模式） openid =", openid)

    r = client.get("/api/quota", params={"openid": openid})
    before = r.json()["remaining"]
    assert before == 20 + 3, before

    r = client.post("/api/tasks", data={
        "openid": openid, "subject": "数学", "grade_level": "七年级",
    }, files={"file": ("hw.png", buf.getvalue(), "image/png")})
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    print("✓ 任务创建 task_id =", task_id)

    deadline = time.time() + 15
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/tasks/{task_id}").json()["status"]
        if status in ("done", "failed"):
            break
        time.sleep(0.5)
    assert status == "done", f"任务状态异常: {status}"
    data = client.get(f"/api/tasks/{task_id}").json()
    assert data["result"]["correct_count"] == 1
    assert data["provider"] == "backup" and data["cost_cny"] == 0.008
    print("✓ 后台批改完成，结果/花费已入库")

    r = client.get("/api/quota", params={"openid": openid})
    assert r.json()["remaining"] == before - 1
    print("✓ 配额扣减正确")

    r = client.post("/api/mistakes", data={
        "openid": openid, "task_id": task_id, "question_no": "1",
        "knowledge_point": "一元一次方程", "note": "移项没变号"})
    assert r.status_code == 201
    r = client.get("/api/mistakes", params={"openid": openid})
    assert len(r.json()) == 1 and r.json()[0]["question_no"] == "1"
    print("✓ 错题本写入/读取通过")

    r = client.get("/api/providers")
    assert r.json()["default"] == "primary"
    assert "api_key" not in r.text  # 密钥绝不外泄
    print("✓ /api/providers 不泄露密钥")

    # 非图片上传应被拒绝
    r = client.post("/api/tasks", data={"openid": openid},
                    files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    print("✓ 非法文件被拒绝")

print(f"\n全部通过 ✅（测试数据在 {TMP}）")
