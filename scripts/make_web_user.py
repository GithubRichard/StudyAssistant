#!/usr/bin/env python3
"""生成网页版白名单账号片段（粘贴到 config.yaml 的 web.users 下）。

用法：
    python3 scripts/make_web_user.py --username kid1 --display-name 老大

脚本会提示输入密码（不回显），输出可直接粘贴的 YAML 片段。
密码只存 pbkdf2_sha256 哈希，不存明文；config.yaml 不进版本库。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password, web_openid  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="生成网页版白名单账号片段")
    ap.add_argument("--username", required=True, help="用户名（1~32 位字母数字与 -_）")
    ap.add_argument("--display-name", default="", help="显示名（默认与用户名相同）")
    args = ap.parse_args()

    name = (args.username or "").strip()
    if not name or len(name) > 32 or any(not (c.isalnum() or c in "-_") for c in name):
        ap.error("username 只允许 1~32 位字母数字与 -_")

    pw = getpass.getpass("请输入密码（输入时不回显）: ").strip()
    if not pw:
        ap.error("密码不能为空")
    pw2 = getpass.getpass("请再输入一次确认: ").strip()
    if pw != pw2:
        ap.error("两次输入的密码不一致")

    display = args.display_name.strip() or name
    print("\n# 把下面这段粘贴到 config.yaml 的 web.users: 下面")
    print(f'    - username: "{name}"')
    print(f'      display_name: "{display}"')
    print(f'      password_hash: "{hash_password(pw)}"')
    print(f"\n# 该账号登录后的数据身份为: {web_openid(name)}")


if __name__ == "__main__":
    main()
