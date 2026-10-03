#!/usr/bin/env python3
"""按 session 拆分 thinking 日志；无 session 的模型调用各自成文件。"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re
import sys

HEADER = re.compile(r"^(?:.*?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.*?)?【(模型思考过程|任务阶段)】(.*)$")
SESSION = re.compile(r"(?:^|\s)session(?:_id)?=([^\s]+)")


def split_log(source: Path, output: Path) -> tuple[int, int, int]:
    """保留原始行；已有输出目录拒绝覆盖。返回文件数、调用数、残缺段数。"""
    groups: dict[str, list[str]] = {}
    active: str | None = None
    calls = incomplete = 0
    records = 0
    # newline='' 保留 CRLF；严格解码，避免静默丢失字符。
    with source.open(encoding="utf-8-sig", newline="") as stream:
        for line in stream:
            header = HEADER.match(line.rstrip("\r\n"))
            if header:
                if active is not None:
                    incomplete += 1
                records += 1
                calls += header.group(1) == "模型思考过程"
                session = SESSION.search(header.group(2))
                active = "session:" + session.group(1) if session else f"call:{records:06d}"
                groups.setdefault(active, []).append(line)
            elif active is not None:
                groups[active].append(line)
                if line.strip() in ("【思考过程结束】", "【任务阶段结束】"):
                    active = None
            else:
                groups.setdefault("unassigned", []).append(line)
    if active is not None:
        incomplete += 1
    if not records:
        raise ValueError("没有找到【模型思考过程】起始标记，请检查输入文件。")
    output.mkdir(parents=True, exist_ok=False)
    for index, (key, lines) in enumerate(groups.items(), 1):
        label = re.sub(r"[^\w.-]+", "_", key, flags=re.UNICODE)[:100]
        with (output / f"{index:04d}_{label}.txt").open(
            "x", encoding="utf-8", newline=""
        ) as target:
            target.writelines(lines)
    return len(groups), calls, incomplete


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=Path("data/logs/thinking.log"))
    parser.add_argument("-o", "--output", type=Path, help="新建输出目录；不覆盖已有目录")
    args = parser.parse_args()
    output = args.output or args.source.parent / (
        args.source.name + "_sessions_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    )
    try:
        files, calls, incomplete = split_log(args.source, output)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"切分失败：{exc}", file=sys.stderr)
        return 1
    print(f"已将 {calls} 段模型调用拆分为 {files} 个文件：{output}")
    print("相同 session 合并；无 session 各段独立；段外内容保存在 unassigned 文件。")
    if incomplete:
        print(f"警告：{incomplete} 段缺少结束标记，已保留原文。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
