"""授权学习工作区：初始化、附件处理、受控归档与成果登记。

安全边界：
- 所有写入路径都必须是「工作区 + 学科 + 允许的子目录 + 合法文件名」，拒绝 `..`、绝对路径与符号链接逃逸。
- 归档采用「读—合并—原子替换」，同日同学科追加而不覆盖，重复执行同一轮次不会重复写。
- 成果文件只有在授权目录内真实存在、类型与大小通过检查后才登记下载。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, UnidentifiedImageError

from .config import Settings

log = logging.getLogger(__name__)

ALLOWED_SUBDIRS = ("错题解析", "周报分析", "强化训练")
ORIGINAL_SUBDIR = "原题"                     # 原题资料仅本地保存，不入 Git、不进归档正文
DEFAULT_SUBJECTS = ("语文", "数学", "英语")
ARCHIVE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-[^/\\]{1,60})?\.md$")
SUBJECT_RE = re.compile(r"^[^/\\\s.]{1,20}$")
CONFLICT_NAME_RE = re.compile(r"^冲突记录-\d{4}-\d{2}-\d{2}-\d{6}(-\d+)?\.md$")

# 与学习仓库一致的忽略规则：原题目录内容不上传，仅放行目录骨架与 .gitkeep
GITIGNORE_CONTENT = """# 各学科「原题」目录下的原始试卷、照片与草稿：仅保存在本地，不上传远端。
# 只保留目录骨架（.gitkeep），使 clone 后仍能看到 原题/年份/周次 的结构。
/*/原题/**
!/*/原题/**/
!/*/原题/**/.gitkeep
"""

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
ARTIFACT_SUFFIXES = (".md", ".pdf")

_locks: Dict[str, asyncio.Lock] = {}


class WorkspaceError(Exception):
    pass


def _lock_for(path: Path) -> asyncio.Lock:
    key = str(path)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def workspace_root(settings: Settings) -> Path:
    root = Path(settings.workspace_dir).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def learning_rules_path() -> Path:
    """技能内置规范副本，用于初始化工作区 README。"""
    return Path(__file__).resolve().parent.parent / "hermes" / "skills" / \
        "leo-study-assistant" / "references" / "learning-rules.md"


def workspace_subjects(settings: Settings) -> List[str]:
    """工作区学科清单：取配置（可被设置页覆盖），过滤非法名并保底默认三科。"""
    names: List[str] = []
    for raw in list(settings.family.subjects) + list(DEFAULT_SUBJECTS):
        name = str(raw).strip()
        if name and SUBJECT_RE.match(name) and name not in names:
            names.append(name)
    return names


def _touch_gitkeep(directory: Path, created: List[str], root: Path) -> None:
    """空目录用 .gitkeep 占位，使 clone 后仍能看到目录骨架；已有文件不干预。"""
    try:
        has_files = any(entry.is_file() and entry.name != ".gitkeep"
                        for entry in directory.iterdir())
    except OSError:
        return
    keep = directory / ".gitkeep"
    if has_files or keep.exists():
        return
    keep.write_text("", encoding="utf-8")
    created.append(str(keep.relative_to(root)))


def ensure_workspace(settings: Settings) -> Dict[str, Any]:
    """创建工作区骨架；已存在的 README 与记录一律不动。

    - 学科目录：`学科/{错题解析,周报分析,强化训练}` + `学科/原题/<年份>/`
    - 根级 `.gitignore`：原题内容不上传（已存在时不覆盖）
    - 冲突记录由同步阶段按需生成到根目录（白名单见 `safe_conflict_path`）
    """
    root = workspace_root(settings)
    subjects = workspace_subjects(settings)
    created: List[str] = []
    root.mkdir(parents=True, exist_ok=True)
    for subject in subjects:
        for sub in ALLOWED_SUBDIRS:
            target = root / subject / sub
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                created.append(str(target.relative_to(root)))
            _touch_gitkeep(target, created, root)

        original_dir = root / subject / ORIGINAL_SUBDIR
        year_dir = original_dir / str(date.today().year)
        if not year_dir.exists():
            year_dir.mkdir(parents=True, exist_ok=True)
            created.append(str(year_dir.relative_to(root)))
        _touch_gitkeep(original_dir, created, root)
        _touch_gitkeep(year_dir, created, root)

    readme = root / "README.md"
    readme_created = False
    if settings.workspace.init_readme and not readme.exists():
        rules = learning_rules_path()
        if rules.exists():
            readme.write_text(rules.read_text(encoding="utf-8"), encoding="utf-8")
            readme_created = True
        else:
            log.warning("工作区规范文件缺失，未初始化 README: %s", rules)

    gitignore = root / ".gitignore"
    gitignore_created = False
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_CONTENT, encoding="utf-8")
        gitignore_created = True
    elif ORIGINAL_SUBDIR not in gitignore.read_text(encoding="utf-8", errors="ignore"):
        log.warning("工作区 .gitignore 未包含原题忽略规则，请人工确认: %s", gitignore)

    return {"root": str(root), "created": created, "readme_created": readme_created,
            "gitignore_created": gitignore_created, "subjects": subjects}


def run_output_dir(settings: Settings, task_id: str, run_no: int) -> Path:
    path = Path(settings.data_dir) / "runs" / task_id / f"run{run_no}"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


# ---------- 附件 ----------


def store_asset(settings: Settings, openid: str, raw: bytes, filename: str = "") -> Dict[str, Any]:
    """校验并保存上传图片，返回可入库的附件信息。"""
    limit = settings.max_image_mb * 1024 * 1024
    if len(raw) > limit:
        raise WorkspaceError(f"图片超过 {settings.max_image_mb}MB 上限")
    if not raw:
        raise WorkspaceError("上传内容为空")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise WorkspaceError("不是有效的图片文件") from e

    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    width, height = img.size
    scale = min(1.0, settings.max_image_px / max(width, height))
    if scale < 1:
        img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
        width, height = img.size
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    data = buf.getvalue()

    asset_id = uuid.uuid4().hex[:16]
    target_dir = Path(settings.upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = (target_dir / f"{asset_id}.jpg").resolve()
    path.write_bytes(data)

    return {
        "id": asset_id,
        "openid": openid,
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime": "image/jpeg",
        "bytes": len(data),
        "width": width,
        "height": height,
        "path": str(path),
        "created_at": time.time(),
    }


def load_asset_data_url(asset: Dict[str, Any]) -> str:
    """读取附件并转成内联 data URL（不外发服务器路径）。"""
    path = Path(asset["path"]).resolve()
    if not path.exists():
        raise WorkspaceError(f"附件文件缺失: {asset['id']}")
    import base64
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{asset.get('mime', 'image/jpeg')};base64,{b64}"


# ---------- 归档 ----------


def safe_archive_path(settings: Settings, relative: str) -> Optional[Path]:
    """校验归档建议路径；不合法返回 None（由调用方如实报告，而不是猜测目标位置）。"""
    if not relative:
        return None
    rel = relative.strip().lstrip("./")
    parts = Path(rel).parts
    if len(parts) != 3:
        return None
    subject, subdir, name = parts
    if not SUBJECT_RE.match(subject) or subdir not in ALLOWED_SUBDIRS:
        return None
    if not ARCHIVE_NAME_RE.match(name):
        return None
    root = workspace_root(settings)
    target = (root / subject / subdir / name).resolve()
    if root not in target.parents:
        return None
    return target


async def apply_archive(settings: Settings, task: Dict[str, Any], run: Dict[str, Any],
                        result: Dict[str, Any]) -> Dict[str, Any]:
    """把结果中的归档建议写入工作区；追加不覆盖，重复轮次不重复写。"""
    archive = result.get("archive") or {}
    markdown = (archive.get("content_markdown") or "").strip()
    question_uids = [str(q.get("uid") or "").strip()
                     for q in (result.get("questions") or [])]
    question_uids = [uid for uid in question_uids if uid]
    if not markdown:
        return {"status": "skipped", "path": "", "note": "结果未提供归档内容",
                "questions": question_uids}

    target = safe_archive_path(settings, archive.get("suggested_path", ""))
    if target is None:
        return {
            "status": "failed",
            "path": "",
            "note": f"归档路径不合法或越界，已拒绝写入：{archive.get('suggested_path')!r}",
            "questions": question_uids,
        }

    marker = f"<!-- task:{task['id']} run:{run['run_no']} -->"
    # 题目去重键写入注释：便于与台账/复测事件交叉核对，也方便人工定位历史判定的出处
    meta = f"\n<!-- questions: {','.join(question_uids)} -->" if question_uids else ""
    block = f"\n\n{markdown}{meta}\n\n{marker}\n"

    lock = _lock_for(target)
    async with lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if marker in existing:
            return {"status": "skipped", "path": str(target),
                    "note": "本轮归档已存在，未重复写入", "questions": question_uids}
        if target.exists() and not existing.endswith("\n"):
            existing += "\n"
        temp = target.with_suffix(target.suffix + f".tmp{uuid.uuid4().hex[:6]}")
        temp.write_text(existing + block, encoding="utf-8")
        os.replace(temp, target)

    return {"status": "generated", "path": str(target), "note": "已按追加规则写入工作区",
            "bytes": target.stat().st_size, "questions": question_uids}


# ---------- 复测登记追加 ----------

_RETEST_LABELS = {
    "retest_passed": "复测通过",
    "retest_failed": "复测未通过",
    "corrected": "已订正（待复测）",
}


async def append_retest_note(settings: Settings, entry: Dict[str, Any], event: Dict[str, Any],
                             when: Optional[datetime] = None) -> Dict[str, Any]:
    """把一次复测/订正结果追加到既有归档文件；追加不覆盖，重复登记不重复写。

    只写台账条目已关联、且仍在允许目录内的归档文件；找不到时如实说明，不新建记录。
    """
    rel = (entry.get("archive_path") or "").strip()
    target = safe_archive_path(settings, rel) if rel else None
    if target is None:
        return {"status": "skipped", "path": "",
                "note": "台账条目未关联可写入的归档文件，未追加复测记录"}
    if not target.exists():
        return {"status": "skipped", "path": str(target),
                "note": "关联的归档文件已不存在，未追加复测记录"}

    date_str = (event.get("occurred_date") or "").strip() or \
        (when or datetime.now()).strftime("%Y-%m-%d")
    label = _RETEST_LABELS.get(event.get("result", ""), event.get("result", "已登记"))
    marker = f"<!-- retest:{entry.get('id')}:{date_str} -->"
    lines = [
        f"### 复测登记（{date_str}）",
        (f"- 定位：{(entry.get('source') or '未记录来源')}"
         f" {entry.get('page') or ''} 第{entry.get('question_no') or '?'}题"),
        f"- 结果：{label}",
        f"- 孩子答案：{event.get('student_answer') or '未记录'}",
    ]
    if event.get("note"):
        lines.append(f"- 备注：{event['note']}")
    block = "\n\n" + "\n".join(lines) + f"\n\n{marker}\n"

    lock = _lock_for(target)
    async with lock:
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if marker in existing:
            return {"status": "skipped", "path": str(target), "note": "该次复测已记录，未重复追加"}
        if existing and not existing.endswith("\n"):
            existing += "\n"
        temp = target.with_suffix(target.suffix + f".tmp{uuid.uuid4().hex[:6]}")
        temp.write_text(existing + block, encoding="utf-8")
        os.replace(temp, target)
    return {"status": "generated", "path": str(target), "note": "已把复测结果追加到归档文件"}


# ---------- 冲突记录 ----------


def safe_conflict_path(settings: Settings, when: Optional[datetime] = None,
                       seq: int = 1) -> Path:
    """冲突记录路径（工作区根，带时间戳）；seq>1 时加序号，绝不覆盖已有记录。"""
    when = when or datetime.now()
    stamp = when.strftime("%Y-%m-%d-%H%M%S")
    name = f"冲突记录-{stamp}.md" if seq <= 1 else f"冲突记录-{stamp}-{seq}.md"
    return (workspace_root(settings) / name).resolve()


def write_conflict_record(settings: Settings, markdown: str,
                          when: Optional[datetime] = None) -> str:
    """把冲突记录写到工作区根，返回相对路径；重名自动加序号。

    冲突记录先保存在本地，不自动提交；内容由调用方按 README 要求组织。
    """
    content = markdown.strip() + "\n"
    root = workspace_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    for seq in range(1, 100):
        target = safe_conflict_path(settings, when, seq)
        try:
            exists = target.exists()
        except OSError as e:
            raise WorkspaceError(f"冲突记录路径不可用: {e}") from e
        if exists:
            continue
        temp = target.with_suffix(target.suffix + f".tmp{uuid.uuid4().hex[:6]}")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, target)
        return str(target.relative_to(root))
    raise WorkspaceError("冲突记录文件重名过多，未能写入；请人工整理根目录")


# ---------- 成果文件 ----------


def collect_artifacts(settings: Settings, task_id: str, run_no: int) -> List[Dict[str, Any]]:
    """扫描本轮输出目录，只登记真实存在、通过类型与大小校验的普通文件。"""
    base = (Path(settings.data_dir) / "runs" / task_id / f"run{run_no}").resolve()
    if not base.exists():
        return []
    found: List[Dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if base not in path.resolve().parents:
            continue
        if path.suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        size = path.stat().st_size
        if size <= 0 or size > MAX_ARTIFACT_BYTES:
            continue
        data = path.read_bytes()
        found.append({
            "id": uuid.uuid4().hex[:16],
            "task_id": task_id,
            "kind": "pdf" if path.suffix.lower() == ".pdf" else "report",
            "path": str(path),
            "bytes": size,
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": time.time(),
        })
    return found


def is_inside_allowed(settings: Settings, path: str) -> bool:
    """下载前的二次校验：文件必须位于数据目录或授权工作区内。"""
    target = Path(path).resolve()
    roots = [
        Path(settings.data_dir).resolve(),
        workspace_root(settings),
    ]
    return any(root == target or root in target.parents for root in roots)


def archive_artifact(settings: Settings, task_id: str, archive: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """归档文件登记为下载成果（仅在文件真实存在且校验通过时）。"""
    path_str = archive.get("path") or ""
    if archive.get("status") != "generated" or not path_str:
        return None
    target = Path(path_str).resolve()
    if not target.exists() or target.is_symlink():
        return None
    if not is_inside_allowed(settings, str(target)):
        return None
    return {
        "id": uuid.uuid4().hex[:16],
        "task_id": task_id,
        "kind": "archive",
        "path": str(target),
        "bytes": target.stat().st_size,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "created_at": time.time(),
    }


def summarize_delivery(settings: Settings, result: Dict[str, Any],
                       archive: Dict[str, Any],
                       git_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把结果自述的交付状态与真实情况对齐，绝不放大为成功。

    - PDF / 邮件：本轮未实现，开关关闭时一律标 not_configured，不接受模型自述；
    - Git：以服务端受控同步（`git_sync.sync_workspace`）的真实结果为准，
      未启用时不接受模型自述的 committed。
    """
    delivery = dict(result.get("delivery") or {})
    pdf = dict(delivery.get("pdf") or {"status": "not_configured", "note": ""})
    email = dict(delivery.get("email") or {"status": "not_configured", "note": ""})

    if not settings.delivery.pdf_enabled and pdf.get("status") == "generated":
        pdf = {"status": "not_configured", "note": "服务端未启用 PDF 生成，模型自述不作为成功依据"}
    if not settings.delivery.email_enabled and email.get("status") == "sent":
        email = {"status": "not_configured", "note": "服务端未启用邮件渠道，模型自述不作为成功依据"}
    if email.get("status") == "sent" and settings.delivery.email_enabled:
        email = {"status": "skipped",
                 "note": "服务端不代为发送邮件，请以工作区与本地文件为准"}

    if not settings.git_sync_enabled:
        git = {"status": "not_configured",
               "note": "服务端未启用学习记录同步（模型自述不作为成功依据）"}
    elif git_result:
        git = {
            "status": git_result.get("status", "failed"),
            "note": git_result.get("reason", ""),
            "committed": bool(git_result.get("committed")),
            "pushed": bool(git_result.get("pushed")),
            "commit": git_result.get("commit", ""),
            "conflict_record": git_result.get("conflict_record", ""),
        }
    else:
        git = {"status": "failed", "note": "已启用学习记录同步，但本轮未取得同步结果"}

    delivery_out = {
        "pdf": pdf,
        "email": email,
        "git": git,
        "archive": {
            "status": archive.get("status", "skipped"),
            "path": _rel(settings, archive.get("path", "")),
            "note": archive.get("note", ""),
            "questions": list(archive.get("questions") or []),
        },
    }
    return delivery_out


def workspace_relative_path(settings: Settings, path: str) -> str:
    """对外暴露工作区相对路径（不泄露服务器绝对路径）。"""
    return _rel(settings, path)


def _rel(settings: Settings, path: str) -> str:
    """对外只暴露工作区相对路径，避免泄露服务器绝对路径。"""
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(workspace_root(settings)))
    except ValueError:
        return Path(path).name


def task_materials_summary(settings: Settings, assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"id": a["id"], "bytes": a["bytes"], "width": a.get("width", 0), "height": a.get("height", 0)}
        for a in assets
    ]
