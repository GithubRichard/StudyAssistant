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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, UnidentifiedImageError

from .config import Settings

log = logging.getLogger(__name__)

ALLOWED_SUBDIRS = ("错题解析", "周报分析", "强化训练")
ARCHIVE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-[^/\\]{1,60})?\.md$")
SUBJECT_RE = re.compile(r"^[^/\\\s.]{1,20}$")

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


def ensure_workspace(settings: Settings) -> Dict[str, Any]:
    """创建工作区骨架；已存在的 README 与记录一律不动。"""
    root = workspace_root(settings)
    subjects = ("语文", "数学", "英语")
    created: List[str] = []
    root.mkdir(parents=True, exist_ok=True)
    for subject in subjects:
        for sub in ALLOWED_SUBDIRS:
            target = root / subject / sub
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                created.append(str(target.relative_to(root)))

    readme = root / "README.md"
    readme_created = False
    if settings.workspace.init_readme and not readme.exists():
        rules = learning_rules_path()
        if rules.exists():
            readme.write_text(rules.read_text(encoding="utf-8"), encoding="utf-8")
            readme_created = True
        else:
            log.warning("工作区规范文件缺失，未初始化 README: %s", rules)
    return {"root": str(root), "created": created, "readme_created": readme_created}


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
    if not markdown:
        return {"status": "skipped", "path": "", "note": "结果未提供归档内容"}

    target = safe_archive_path(settings, archive.get("suggested_path", ""))
    if target is None:
        return {
            "status": "failed",
            "path": "",
            "note": f"归档路径不合法或越界，已拒绝写入：{archive.get('suggested_path')!r}",
        }

    marker = f"<!-- task:{task['id']} run:{run['run_no']} -->"
    block = f"\n\n{markdown}\n\n{marker}\n"

    lock = _lock_for(target)
    async with lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if marker in existing:
            return {"status": "skipped", "path": str(target), "note": "本轮归档已存在，未重复写入"}
        if target.exists() and not existing.endswith("\n"):
            existing += "\n"
        temp = target.with_suffix(target.suffix + f".tmp{uuid.uuid4().hex[:6]}")
        temp.write_text(existing + block, encoding="utf-8")
        os.replace(temp, target)

    return {"status": "generated", "path": str(target), "note": "已按追加规则写入工作区", "bytes": target.stat().st_size}


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
                       archive: Dict[str, Any]) -> Dict[str, Any]:
    """把结果自述的交付状态与真实情况对齐，绝不放大为成功。"""
    delivery = dict(result.get("delivery") or {})
    pdf = dict(delivery.get("pdf") or {"status": "not_configured", "note": ""})
    email = dict(delivery.get("email") or {"status": "not_configured", "note": ""})
    git = dict(delivery.get("git") or {"status": "not_configured", "note": ""})

    if not settings.delivery.pdf_enabled and pdf.get("status") == "generated":
        pdf = {"status": "not_configured", "note": "服务端未启用 PDF 生成，模型自述不作为成功依据"}
    if not settings.delivery.email_enabled and email.get("status") == "sent":
        email = {"status": "not_configured", "note": "服务端未启用邮件渠道，模型自述不作为成功依据"}
    if not settings.delivery.git_enabled and git.get("status") == "committed":
        git = {"status": "not_configured", "note": "服务端未启用学习记录同步，模型自述不作为成功依据"}
    if git.get("status") in ("committed",) or email.get("status") == "sent":
        # 服务端当前不会代为执行外部副作用，出现这两种状态一律降级说明
        note = "服务端不代为提交或发送，请以工作区与本地文件为准"
        if settings.delivery.git_enabled:
            git = {"status": "skipped", "note": note}
        if settings.delivery.email_enabled:
            email = {"status": "skipped", "note": note}

    delivery_out = {
        "pdf": pdf,
        "email": email,
        "git": git,
        "archive": {
            "status": archive.get("status", "skipped"),
            "path": _rel(settings, archive.get("path", "")),
            "note": archive.get("note", ""),
        },
    }
    return delivery_out


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
