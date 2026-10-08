#!/usr/bin/env python3
"""Hermes 复查图片链路探测（运维诊断草稿，未提交、未推送）。

背景：2026-10-08 生产事故——复查走 Hermes 网关时图片没送达模型：
- 经 tencent-tokenhub 到 hy4：附图被替换成文字摘要
  "[The user attached an image. Here's what it contains:...]"；
- 经 zai 到 glm：直接 "image attachment failed"。
主流程（extract/number_verify）直连 provider 官方接口不受影响。

本脚本复现 app/hermes.py::review_questions 的请求包络
（POST {base_url}/v1/chat/completions，字段 model/provider/messages/
stream/temperature，头 X-Hermes-Session-Id），但只发一句话加一张测试图，
几秒内给出"图有没有送达模型"的结论。改完 Hermes 配置后跑一次即可验证，
不用等一次完整批改。

用法（在服务器上跑，环境里已有 HERMES_BASE_URL / HERMES_API_KEY，
例如先 source .env 或在 compose 环境里执行）：

    python3 scripts/probe-hermes-image.py --image /path/to/photo.jpg \\
        --model glm --provider zai

参数优先级：命令行 > 环境变量（HERMES_BASE_URL / HERMES_API_KEY /
HERMES_REVIEW_MODEL / HERMES_REVIEW_PROVIDER）。

只用标准库（urllib），无第三方依赖。密钥只进 Authorization 头，不打印。
失败不自动重试（与业务端纪律一致）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error

# 已知的链路故障信号（与 app/review.py::_IMAGE_LINK_FAILURE_HINTS 同源，
# 加上本次事故里网关侧的两条原文）
CAPTION_SIGNATURES = (
    "[The user attached an image",
    "Here's what it contains",
)
ATTACHMENT_FAILURE_SIGNATURES = (
    "image attachment failed",
    "attachment failed",
)
BLIND_HINTS = (
    "无法查看图片", "看不到图片", "没有收到图片", "未收到图片",
    "cannot see the image", "can't see the image", "unable to view",
    "no image was provided", "no image attached",
)

PROBE_TEXT = (
    "这是一次图片链路诊断，请如实回答三个问题，不要输出多余内容：\n"
    "1）你是否收到了原图本身（像素），而不是一段文字摘要或报错？只答\"是\"或\"否\"。\n"
    "2）如果收到了：用一句话描述图里的手写/印刷内容（证明你真的看到了）。\n"
    "3）如果没收到：把你实际看到的、代替图片的东西原文引用出来。\n"
)


def _mask(s: str) -> str:
    if not s:
        return "(empty)"
    return s[:4] + "..." + s[-4:] if len(s) > 12 else "****"


def load_image_data_url(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    ext = os.path.splitext(path)[1].lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    b64 = base64.b64encode(raw).decode("ascii")
    print(f"[probe] 图片 {path}：{len(raw)/1024:.0f}KB，{mime}", flush=True)
    return f"data:{mime};base64,{b64}"


def verdict(answer: str) -> str:
    low = answer.lower()
    if any(s.lower() in low for s in CAPTION_SIGNATURES):
        return ("FAIL(caption-substituted)：模型收到的是网关生成的文字摘要，"
                "原图没透传——查 Hermes 是否有自动 caption 降级中间件并关掉它")
    if any(s.lower() in low for s in ATTACHMENT_FAILURE_SIGNATURES):
        return ("FAIL(attachment-failed)：网关附件解析/下载失败——"
                "查大小限制、超时，或改传文件引用/URL 代替内联 base64")
    if any(h in answer for h in BLIND_HINTS):
        return ("FAIL(model-blind)：模型明确表示没拿到图——"
                "结合上面第 3 问的原文引用定位是网关替换还是路由丢弃")
    first_line = (answer.strip().splitlines() or [""])[0].strip()
    first_line = re.sub(r"^[0-9①-⑩][)）．.、:\s]*", "", first_line)
    if first_line.startswith("是"):
        return ("PASS(likely)：模型自称收到了原图——请人工核对第 2 问的描述"
                "是否与测试图内容相符，相符即链路修好")
    return "UNCERTAIN：模型回答不符合预期格式，请人工阅读上面的原文判定"


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes 图片链路探测")
    ap.add_argument("--image", required=True, help="测试图片路径（jpg/png/webp）")
    ap.add_argument("--model", default=os.environ.get("HERMES_REVIEW_MODEL", ""),
                    help="复查用 model（默认取 HERMES_REVIEW_MODEL）")
    ap.add_argument("--provider", default=os.environ.get("HERMES_REVIEW_PROVIDER", ""),
                    help="复查用 provider（默认取 HERMES_REVIEW_PROVIDER，可空）")
    ap.add_argument("--base-url", default=os.environ.get("HERMES_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("HERMES_API_KEY", ""))
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    if not args.base_url or not args.api_key:
        print("缺少 HERMES_BASE_URL / HERMES_API_KEY（参数或环境变量）", file=sys.stderr)
        return 2
    if not args.model:
        print("缺少 --model（或 HERMES_REVIEW_MODEL）", file=sys.stderr)
        return 2
    if not os.path.isfile(args.image):
        print(f"图片不存在：{args.image}", file=sys.stderr)
        return 2

    data_url = load_image_data_url(args.image)
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system",
             "content": "你是链路诊断助手，只按要求如实回答，不做多余推理。"},
            {"role": "user", "content": [
                {"type": "text", "text": PROBE_TEXT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "stream": False,
        "temperature": 0,
    }
    if args.provider:
        payload["provider"] = args.provider

    url = args.base_url.rstrip("/") + "/v1/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {args.api_key}",
                 "Content-Type": "application/json",
                 "X-Hermes-Session-Id": "probe-image-link"},
        method="POST")
    print(f"[probe] POST {url}", flush=True)
    print(f"[probe] model={args.model} provider={args.provider or '(空)'} "
          f"key={_mask(args.api_key)} timeout={args.timeout}s", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        print(f"[probe] HTTP {e.code}：{e.read()[:500]!r}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[probe] 请求失败（结果未确认，不自动重试）：{e}", file=sys.stderr)
        return 1
    print(f"[probe] HTTP {status}，响应 {len(body)} 字节", flush=True)

    try:
        data = json.loads(body)
    except ValueError:
        print("[probe] 响应不是合法 JSON", file=sys.stderr)
        return 1
    choices = data.get("choices") or []
    if not choices:
        print("[probe] 响应缺少 choices", file=sys.stderr)
        return 1
    answer = (choices[0].get("message") or {}).get("content") or ""
    print(f"[probe] 网关报告 model={data.get('model') or '(未报告)'} "
          f"provider={data.get('provider') or '(未报告)'}")
    print("=" * 60)
    print(answer.strip() or "(空回答)")
    print("=" * 60)
    print("[verdict]", verdict(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
