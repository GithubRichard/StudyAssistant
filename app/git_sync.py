"""受控 Git 提交推送：只提交本次授权的工作区学习记录。

安全边界（与学习仓库约定一致）：
- 只 `git add -- <本次归档文件>`；禁止全量暂存、禁止 `git add -f` 绕过原题忽略规则；
- 不强制推送、不硬重置、不改 Git 配置、不跳过钩子、不自动 merge/rebase；
- 未启用、非 Git 仓库、detached HEAD、无上游、暂存区已有他人改动时都停止并如实上报；
- 只有明确检测到内容冲突或分支分歧（非快进）时才生成冲突记录，且注明是否已确认内容冲突；
- 认证失败、网络失败只如实报告原因，不冒充内容冲突、不声称推送成功。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import workspace
from .config import Settings

log = logging.getLogger(__name__)

# 推送失败原因分类关键字（git 输出不稳定，只做保守判定）
_CONTENT_CONFLICT_HINTS = ("CONFLICT", "conflict")
_DIVERGED_HINTS = ("non-fast-forward", "fetch first", "rejected", "behind",
                   "failed to push some refs")
_AUTH_HINTS = ("authentication failed", "could not read username",
               "permission denied", "invalid username or password",
               "terminal prompts disabled", "403")
_NETWORK_HINTS = ("could not resolve host", "connection refused", "connection timed out",
                  "unable to access", "network is unreachable", "operation timed out")

STATUSES = ("committed", "failed", "skipped", "not_configured")


def _result(status: str, **extra: Any) -> Dict[str, Any]:
    data = {"status": status, "committed": False, "pushed": False, "commit": "",
            "paths": [], "conflict_record": "", "reason": "", "branch": "",
            "upstream": ""}
    data.update(extra)
    return data


def _classify_push_failure(stderr: str) -> str:
    """把推送失败归类为 content_conflict / diverged / auth / network / unknown。"""
    text = (stderr or "").lower()
    if any(hint.lower() in text for hint in _CONTENT_CONFLICT_HINTS):
        return "content_conflict"
    if any(hint.lower() in text for hint in _DIVERGED_HINTS):
        return "diverged"
    if any(hint.lower() in text for hint in _AUTH_HINTS):
        return "auth"
    if any(hint.lower() in text for hint in _NETWORK_HINTS):
        return "network"
    return "unknown"


async def _run_git(root: Path, args: List[str], timeout: float) -> Tuple[int, str, str]:
    """执行 git 子命令；禁用交互式凭据提示，超时即终止并如实返回。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"      # 不做交互式输入，避免卡死
    env["GIT_ASKPASS"] = ""
    env.setdefault("GIT_PAGER", "cat")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    except FileNotFoundError:
        return -1, "", "git 不可用：未找到可执行文件"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"git {args[0] if args else ''} 执行超时（> {timeout:.0f}s）"
    return proc.returncode or 0, out.decode("utf-8", errors="replace"), \
        err.decode("utf-8", errors="replace")


def _relative(root: Path, path: str) -> Optional[str]:
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return None


def build_commit_message(task: Dict[str, Any], result: Dict[str, Any]) -> str:
    """提交说明：概括学科、日期与任务主题，避免与本次成果无关的信息。"""
    subject = task.get("subject") or result.get("subject") or "学习"
    task_type = task.get("task_type", "grading")
    kind = task.get("training_kind") or ""
    label = {
        "grading": "作业批改", "qa": "学习问答", "weekly_report": "周报分析",
        "training": "强化训练", "retest": "复测",
    }.get(task_type, task_type)
    if task_type == "training" and kind:
        from .scope import TRAINING_KIND_LABELS

        label = TRAINING_KIND_LABELS.get(kind, label)
    date = (result.get("scope") or {}).get("end_date") or datetime.now().strftime("%Y-%m-%d")
    return f"学习记录：{subject} {date} {label}"


async def sync_workspace(settings: Settings, *, task: Dict[str, Any], run: Dict[str, Any],
                         archive: Dict[str, Any], result: Dict[str, Any],
                         commit_message: str = "") -> Dict[str, Any]:
    """把本次归档结果提交并推送；返回真实状态，不夸大、不猜测。"""
    if not settings.git_sync_enabled:
        return _result("not_configured", reason="服务端未启用学习记录同步")

    if archive.get("status") != "generated" or not archive.get("path"):
        return _result("skipped", reason=f"本轮没有可提交的归档文件（{archive.get('status')}）")

    root = workspace.workspace_root(settings)
    relative = _relative(root, archive["path"])
    if relative is None:
        return _result("failed", reason="归档文件不在授权工作区内，已拒绝提交")

    timeout = settings.git.timeout_seconds
    code, out, err = await _run_git(root, ["rev-parse", "--is-inside-work-tree"], timeout)
    if code != 0 or out.strip() != "true":
        return _result("failed", reason=f"授权工作区不是 Git 仓库（{err.strip() or '无法确认'}）")

    code, out, err = await _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout)
    branch = out.strip()
    if code != 0 or not branch or branch == "HEAD":
        return _result("failed", reason="当前分支不可确认（可能处于 detached HEAD），已停止上传")

    code, out, err = await _run_git(
        root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout)
    upstream = out.strip()
    if code != 0 or not upstream:
        return _result("failed", branch=branch,
                       reason="当前分支未配置上游分支；不擅自新建分支或改远端，已停止上传")

    upstream_remote = upstream.split("/", 1)[0]
    if settings.git.remote and upstream_remote != settings.git.remote:
        return _result("failed", branch=branch, upstream=upstream,
                       reason=f"上游远端为 {upstream_remote}，与配置的 {settings.git.remote} 不一致，已停止上传")

    # 已有暂存改动且不是本次文件时暂停，避免夹带无关内容
    code, out, _ = await _run_git(root, ["diff", "--cached", "--name-only"], timeout)
    staged_before = [line.strip() for line in out.splitlines() if line.strip()]
    foreign = [path for path in staged_before if path != relative]
    if foreign:
        return _result("failed", branch=branch, upstream=upstream,
                       reason=("暂存区已存在本次成果之外的改动（"
                               + ", ".join(foreign[:5]) + "），已停止上传，请人工确认"))

    code, out, err = await _run_git(root, ["add", "--", relative], timeout)
    if code != 0:
        return _result("failed", branch=branch, upstream=upstream,
                       reason=f"暂存失败：{err.strip() or out.strip()}")

    code, out, _ = await _run_git(root, ["diff", "--cached", "--name-only"], timeout)
    staged = [line.strip() for line in out.splitlines() if line.strip()]
    if not staged:
        return _result("skipped", branch=branch, upstream=upstream,
                       reason="暂存区没有变化，未创建空提交")

    message = commit_message or build_commit_message(task, result)
    commit_args = ["commit", "-m", message]
    if settings.git.author_name and settings.git.author_email:
        commit_args += ["--author", f"{settings.git.author_name} <{settings.git.author_email}>"]
    code, out, err = await _run_git(root, commit_args, timeout)
    if code != 0:
        return _result("failed", branch=branch, upstream=upstream,
                       reason=f"提交失败：{err.strip() or out.strip()}")

    code, out, _ = await _run_git(root, ["rev-parse", "HEAD"], timeout)
    commit_hash = out.strip() if code == 0 else ""

    code, out, err = await _run_git(root, ["push", upstream_remote, branch], timeout)
    if code != 0:
        stderr = (err or out).strip()
        kind = _classify_push_failure(stderr)
        reason_map = {
            "content_conflict": "推送被拒：检测到内容冲突，已停止上传，未自动合并或变基",
            "diverged": "分支分歧／非快进，尚未确认内容冲突，已停止上传",
            "auth": "认证或权限失败，已停止上传（请自行配置凭据，服务端不改 Git 配置）",
            "network": "网络连接失败，已停止上传",
            "unknown": "推送失败，已停止上传",
        }
        conflict_record = ""
        if kind in ("content_conflict", "diverged"):
            conflict_record = _write_conflict_record(
                settings, task=task, archive=archive, relative=relative,
                branch=branch, upstream=upstream, commit_hash=commit_hash,
                stderr=stderr, kind=kind)
        log.warning("学习记录推送失败(%s): %s", kind, stderr[:300])
        return _result("failed", committed=True, pushed=False, commit=commit_hash,
                       paths=list(staged), branch=branch, upstream=upstream,
                       conflict_record=conflict_record,
                       reason=f"{reason_map[kind]}；git: {stderr[:300]}")

    log.info("学习记录已提交并推送: %s (%s)", commit_hash[:8], relative)
    return _result("committed", committed=True, pushed=True, commit=commit_hash,
                   paths=list(staged), branch=branch, upstream=upstream,
                   reason=f"已提交并推送到 {upstream}")


def _write_conflict_record(settings: Settings, *, task: Dict[str, Any],
                           archive: Dict[str, Any], relative: str, branch: str,
                           upstream: str, commit_hash: str, stderr: str,
                           kind: str) -> str:
    """按 README 要求生成冲突记录；未取得远端内容时如实注明，不编造。"""
    now = datetime.now()
    if kind == "content_conflict":
        conflict_type = "已确认的内容冲突（push 被拒且 git 输出包含冲突信息）"
    else:
        conflict_type = "分支分歧／非快进，尚未确认内容冲突（远端领先本身不等于内容冲突）"
    body = "\n".join([
        f"# 冲突记录 {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1. 发生时间与阶段",
        f"- 时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 阶段：推送（git push）阶段（学习记录已提交，未推送成功）",
        f"- 真实错误信息：{stderr.strip()[:500] or '（git 未返回 stderr）'}",
        "",
        "## 2. 冲突类型",
        f"- {conflict_type}",
        "",
        "## 3. 影响范围与版本",
        f"- 受影响文件：{relative}",
        f"- 本地分支：{branch}",
        f"- 远端跟踪分支：{upstream}",
        f"- 本地提交：{commit_hash or '（未取得提交号）'}",
        "- 远端版本：未取得（服务端不执行 fetch/merge，不猜测远端内容）",
        f"- 任务号：{task.get('id', '')}",
        "",
        "## 4. 双方内容与保留方式",
        f"- 本地内容：已保存到工作区文件 {relative}，并已提交为本地提交 {commit_hash or '（未取得）'}。",
        "- 远端内容：未取得，未编造；如需对比请在本机执行 git fetch 后人工查看。",
        "- 服务端未执行自动合并、变基、强制推送或硬重置，双方内容均未被覆盖。",
        "",
        "## 5. 当前状态与待处理事项",
        "- 已保存：是（工作区文件已写入）",
        "- 已提交：是（本地提交，未推送）",
        "- 已推送：否",
        "- 待人工处理：确认是否先 pull/合并远端改动，再推送本次提交；本记录不代表冲突已解决。",
        "",
    ])
    try:
        return workspace.write_conflict_record(settings, body, when=now)
    except workspace.WorkspaceError as e:
        log.error("冲突记录写入失败: %s", e)
        return ""
