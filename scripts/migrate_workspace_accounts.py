#!/usr/bin/env python3
"""把工作区归档迁到账号目录，并改写数据库里指向旧路径的引用。

背景：工作区由「所有账号共用一棵树」改为「账号/学科/...」后，历史归档仍留在根级
学科目录下。只搬文件而不改引用，会让「记为已订正」「复测通过」与成果下载静默失效。

用法：
    # 1) 预演（默认不落盘，只打印计划）
    python3 scripts/migrate_workspace_accounts.py --account leo

    # 2) 停止服务后实际执行（改数据库前会自动备份）
    python3 scripts/migrate_workspace_accounts.py --account leo --apply

约定：
- 请先停掉服务（`docker compose stop grader`），避免执行器同时写盘；
- 绝不触碰 `README.md`、`.gitignore`、`冲突记录-*.md`、`.git` 等非学科目录；
- 不做任何 git 提交/推送，只在最后打印建议命令（提交时机由你决定）；
- 幂等：已搬走的目录、已是账号层的路径都会跳过。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import workspace as ws                     # noqa: E402
from app.config import load_settings                # noqa: E402
from app.migrations import backup_database          # noqa: E402

# 待改写的工作区相对路径列（值形如 `学科/子目录/文件名`，旧版为 3 段）
RELATIVE_COLUMNS = (("mistakes", "archive_path"), ("question_events", "archive_path"))
# 待改写的绝对路径列（值可能是工作区内路径，也可能是 data/ 下路径）
ABSOLUTE_COLUMNS = (("tasks", "archive_path"), ("artifacts", "path"))


def _rel_parts(value: str) -> List[str]:
    if not value:
        return []
    return list(Path(value.strip().lstrip("./")).parts)


def needs_account_prefix(parts: List[str]) -> bool:
    """旧版归档路径形态：`学科/子目录/文件名`（未含账号层）。"""
    return (len(parts) == 3 and bool(ws.SUBJECT_RE.match(parts[0]))
            and parts[1] in ws.ALLOWED_SUBDIRS)


def with_account(parts: List[str], account: str) -> str:
    return "/".join([account] + parts)


def plan_directory_moves(root: Path, account: str) -> Tuple[List[Tuple[Path, Path]], List[str]]:
    """规划 `学科/{子目录}` → `账号/学科/{子目录}` 的搬移；返回 (moves, 跳过说明)。"""
    moves: List[Tuple[Path, Path]] = []
    notes: List[str] = []
    for subject_dir in sorted(root.iterdir()):
        if not subject_dir.is_dir() or subject_dir.is_symlink():
            continue
        name = subject_dir.name
        if name.startswith(".") or name == account or not ws.SUBJECT_RE.match(name):
            continue
        for sub in list(ws.ALLOWED_SUBDIRS) + [ws.ORIGINAL_SUBDIR]:
            src = subject_dir / sub
            if not src.is_dir() or src.is_symlink():
                continue
            dst = root / account / name / sub
            if dst.exists():
                notes.append(f"跳过（目标已存在）：{src.relative_to(root)} → "
                             f"{dst.relative_to(root)}")
                continue
            moves.append((src, dst))
    return moves, notes


def plan_gitignore(root: Path, account: str) -> List[str]:
    """返回对 `.gitignore` 的动作说明（真正写入由 apply 阶段执行）。"""
    gi = root / ".gitignore"
    if not gi.exists():
        return [f"新建 {gi.name}（含账号层原题忽略规则）"]
    current = gi.read_text(encoding="utf-8", errors="ignore")
    if "/*/*/原题/**" in current:
        return [f"{gi.name} 已含账号层规则，无需改动"]
    if current == ws.LEGACY_GITIGNORE_CONTENT:
        return [f"{gi.name} 与服务端旧版逐字一致 → 升级为含账号层规则"]
    return [f"{gi.name} 不是服务端生成的内容 → 不自动改写，请人工补 `/*/*/原题/**` 规则"]


def plan_db(db_path: Path, ws_root: Path,
            account: str) -> Tuple[List[Tuple[str, object, str, str]], int]:
    """返回 [(table, pk, 旧值, 新值)] 与「已符合账号层而跳过的条数」。"""
    changes: List[Tuple[str, object, str, str]] = []
    already = 0
    if not db_path.exists():
        return changes, already
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for table, column in RELATIVE_COLUMNS:
            for pk, value in _select(conn, table, "id", column):
                parts = _rel_parts(value)
                if needs_account_prefix(parts):
                    changes.append((table, pk, str(value), with_account(parts, account)))
                else:
                    already += 1
        for table, column in ABSOLUTE_COLUMNS:
            for pk, value in _select(conn, table, "id", column):
                new = _migrate_absolute(value, ws_root, account)
                if new:
                    changes.append((table, pk, str(value), new))
                else:
                    already += 1
    finally:
        conn.close()
    return changes, already


def _select(conn: sqlite3.Connection, table: str, pk: str, column: str):
    try:
        rows = conn.execute(f"SELECT {pk}, {column} FROM {table} "
                            f"WHERE {column} IS NOT NULL AND {column} != ''").fetchall()
    except sqlite3.OperationalError:
        return []
    return [(row[0], str(row[1])) for row in rows]


def _migrate_absolute(value: str, ws_root: Path, account: str) -> Optional[str]:
    """工作区内、且仍是旧版 3 段布局的绝对路径才改写；其余（含 data/ 下文件）不动。"""
    if not value:
        return None
    try:
        rel = Path(value).resolve().relative_to(ws_root)
    except (ValueError, OSError):
        return None
    parts = list(rel.parts)
    if not needs_account_prefix(parts):
        return None
    return str(ws_root / account / rel)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="把工作区归档迁移到账号目录，并改写数据库中的旧路径引用")
    ap.add_argument("--account", required=True,
                    help="目标账号目录名（与 workspace/<账号> 一致，如 leo）")
    ap.add_argument("--workspace", default="", help="工作区目录（默认取 config.yaml 的 workspace.dir）")
    ap.add_argument("--db", default="", help="SQLite 路径（默认取 config.yaml 的 data_dir/app.db）")
    ap.add_argument("--apply", action="store_true", help="实际执行（默认只预演）")
    ap.add_argument("--dry-run", action="store_true", help="显式预演，优先级高于 --apply")
    ap.add_argument("--git-mv", dest="git_mv", action="store_true",
                    help="用 git mv 搬家（保留 rename 记录；目录内含未跟踪文件时会失败）")
    ap.add_argument("--no-git-mv", dest="git_mv", action="store_false",
                    help="用普通移动（默认；Git 按内容识别重命名）")
    ap.add_argument("--no-update-gitignore", dest="update_gitignore",
                    action="store_false", help="不改写 .gitignore")
    ap.set_defaults(git_mv=False, update_gitignore=True)
    args = ap.parse_args()

    account = (args.account or "").strip()
    if not ws.ACCOUNT_DIR_RE.match(account):
        ap.error("--account 只允许 1~40 个不含路径分隔符、空白与点号的字符")

    # 两个路径都显式给出时无需读配置（便于在迁移演练/离线环境里直接跑）
    settings = load_settings() if (not args.workspace or not args.db) else None
    root = (Path(args.workspace).expanduser().resolve() if args.workspace
            else ws.workspace_root(settings))
    db_path = Path(args.db).expanduser().resolve() if args.db else Path(settings.db_path)
    do_apply = args.apply and not args.dry_run
    if args.apply and args.dry_run:
        print("注意：同时给了 --apply 与 --dry-run，按预演处理。")

    mode = "执行" if do_apply else "预演（不落盘）"
    print(f"工作区：{root}")
    print(f"数据库：{db_path}")
    print(f"目标账号目录：{account}")
    print(f"模式：{mode}\n")

    if not root.is_dir():
        ap.error(f"工作区目录不存在：{root}")

    # ---------- 1. 目录搬移 ----------
    moves, notes = plan_directory_moves(root, account)
    print(f"[1/4] 目录搬移：{len(moves)} 项")
    for src, dst in moves:
        print(f"  {src.relative_to(root)}  →  {dst.relative_to(root)}")
    for note in notes:
        print(f"  {note}")
    moved: List[Tuple[Path, Path]] = []
    if do_apply:
        for src, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if args.git_mv:
                done = _git_mv(root, src, dst)
                if not done:
                    print(f"  git mv 失败，改为普通移动：{src.relative_to(root)}")
                    shutil.move(str(src), str(dst))
            else:
                shutil.move(str(src), str(dst))
            moved.append((src, dst))
        _remove_empty_subject_dirs(root, account)

    # ---------- 2. .gitignore ----------
    gi_actions = plan_gitignore(root, account)
    print(f"\n[2/4] .gitignore：")
    for action in gi_actions:
        print(f"  {action}")
    if do_apply and args.update_gitignore:
        _apply_gitignore(root)

    # ---------- 3. 数据库路径 ----------
    changes, already = plan_db(db_path, root, account)
    print(f"\n[3/4] 数据库路径改写：{len(changes)} 条（已符合账号层而跳过 {already} 条）")
    for table, pk, old, new in changes[:20]:
        print(f"  {table}#{pk}: {old}  →  {new}")
    if len(changes) > 20:
        print(f"  …… 其余 {len(changes) - 20} 条略")
    backup_path = ""
    if do_apply and changes:
        if db_path.exists():
            backup_path = backup_database(db_path)
            print(f"  已备份数据库：{backup_path}")
        _apply_db(db_path, changes)

    # ---------- 4. 收尾 ----------
    print("\n[4/4] 后续动作")
    if not do_apply:
        print("  这是预演；确认无误后加 --apply 实际执行（执行前请先停掉服务）。")
    else:
        print("  迁移完成。请检查工作区，然后按受控 Git 约定提交：")
        print("    git -C <工作区> status")
        print(f"    git -C <工作区> add -- {account} README.md .gitignore")
        print("  最后重启服务：docker compose up -d --build --force-recreate grader")


def _git_mv(root: Path, src: Path, dst: Path) -> bool:
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "mv", str(src), str(dst)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _remove_empty_subject_dirs(root: Path, account: str) -> None:
    for subject_dir in sorted(root.iterdir()):
        if not subject_dir.is_dir() or subject_dir.name == account:
            continue
        if not ws.SUBJECT_RE.match(subject_dir.name):
            continue
        try:
            if not any(subject_dir.iterdir()):
                subject_dir.rmdir()
        except OSError:
            pass


def _apply_gitignore(root: Path) -> None:
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(ws.GITIGNORE_CONTENT, encoding="utf-8")
        return
    current = gi.read_text(encoding="utf-8", errors="ignore")
    if "/*/*/原题/**" in current or current != ws.LEGACY_GITIGNORE_CONTENT:
        return
    gi.write_text(ws.GITIGNORE_CONTENT, encoding="utf-8")


def _apply_db(db_path: Path, changes: List[Tuple[str, object, str, str]]) -> None:
    """逐条精确更新：用「主键 + 旧值」定位，避免误伤并行修改。"""
    conn = sqlite3.connect(str(db_path))
    try:
        for table, pk, old, new in changes:
            column = "path" if table == "artifacts" else "archive_path"
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE id = ? AND {column} = ?",
                (new, pk, old))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    started = time.time()
    main()
    print(f"\n耗时 {time.time() - started:.2f}s")
