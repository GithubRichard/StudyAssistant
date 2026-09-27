"""工作区测试：初始化保护、附件校验、归档追加与幂等、路径越界与成果真实性。"""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from PIL import Image

from app.config import Settings
from app.workspace import (CONFLICT_NAME_RE, WorkspaceError, apply_archive,
                           archive_artifact, collect_artifacts, ensure_workspace,
                           is_inside_allowed, run_output_dir, safe_archive_path,
                           store_asset, write_conflict_record)


def make_settings(tmp: str, **overrides) -> Settings:
    data = {
        "engine": {"mode": "hermes"},
        "hermes": {"base_url": "http://127.0.0.1:8642", "api_key": "k"},
        "data_dir": str(Path(tmp) / "data"),
        "workspace": {"dir": str(Path(tmp) / "workspace"), "init_readme": True},
    }
    data.update(overrides)
    return Settings.model_validate(data)


def png_bytes(size=(120, 80), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class InitTest(unittest.TestCase):
    def test_creates_skeleton_and_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            result = ensure_workspace(settings)
            root = Path(result["root"])
            self.assertTrue((root / "数学" / "错题解析").is_dir())
            self.assertTrue((root / "README.md").exists())
            self.assertTrue(result["readme_created"])

    def test_never_overwrites_existing_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            root = Path(settings.workspace_dir)
            root.mkdir(parents=True)
            (root / "README.md").write_text("用户自己的规范", encoding="utf-8")
            result = ensure_workspace(settings)
            self.assertFalse(result["readme_created"])
            self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "用户自己的规范")


class AssetTest(unittest.TestCase):
    def test_store_rejects_non_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            with self.assertRaises(WorkspaceError):
                store_asset(settings, "u1", b"hello", "a.txt")

    def test_store_rejects_oversized(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp, max_image_mb=1)
            with self.assertRaises(WorkspaceError):
                store_asset(settings, "u1", b"x" * (2 * 1024 * 1024), "big.jpg")

    def test_store_reports_heic_hint_without_decoder(self):
        from app import workspace
        header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            original = workspace.HEIF_SUPPORTED
            workspace.HEIF_SUPPORTED = False
            try:
                with self.assertRaises(WorkspaceError) as ctx:
                    store_asset(settings, "u1", header, "IMG_0001.HEIC")
            finally:
                workspace.HEIF_SUPPORTED = original
            self.assertIn("HEIC", str(ctx.exception))

    def test_store_accepts_heic_when_decoder_available(self):
        try:
            import pillow_heif
        except ImportError:
            self.skipTest("未安装 pillow-heif，跳过 HEIC 解码验证")
        pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.new("RGB", (120, 80), (10, 120, 200)).save(buf, "HEIF")
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            asset = store_asset(settings, "u1", buf.getvalue(), "IMG_0001.HEIC")
            self.assertEqual(asset["mime"], "image/jpeg")
            self.assertTrue(Path(asset["path"]).exists())

    def test_store_converts_to_jpeg_and_resizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp, max_image_px=100)
            asset = store_asset(settings, "u1", png_bytes((400, 200)), "hw.png")
            self.assertEqual(asset["mime"], "image/jpeg")
            self.assertEqual(asset["width"], 100)
            self.assertTrue(Path(asset["path"]).exists())
            self.assertEqual(len(asset["sha256"]), 64)


class ArchiveTest(unittest.TestCase):
    def test_safe_path_accepts_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            target = safe_archive_path(settings, "数学/错题解析/2026-09-26.md")
            self.assertIsNotNone(target)
            self.assertEqual(target.name, "2026-09-26.md")

    def test_rejects_traversal_and_unknown_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            for bad in ["../../etc/passwd", "/etc/passwd",
                        "数学/其他目录/2026-09-26.md",
                        "数学/错题解析/../x.md",
                        "数学/错题解析/notadate.md",
                        "数学/错题解析/2026-09-26.txt",
                        "数学/2026-09-26.md"]:
                self.assertIsNone(safe_archive_path(settings, bad), bad)

    def test_append_is_idempotent_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            root = Path(settings.workspace_dir)
            (root / "数学" / "错题解析").mkdir(parents=True)
            target = root / "数学" / "错题解析" / "2026-09-26.md"
            target.write_text("# 已有记录\n旧内容\n", encoding="utf-8")

            task = {"id": "t1"}
            run = {"run_no": 1}
            result = {"archive": {"suggested_path": "数学/错题解析/2026-09-26.md",
                                 "content_markdown": "新追加内容"}}
            first = asyncio.run(apply_archive(settings, task, run, result))
            self.assertEqual(first["status"], "generated")
            text = target.read_text(encoding="utf-8")
            self.assertIn("旧内容", text)
            self.assertIn("新追加内容", text)

            second = asyncio.run(apply_archive(settings, task, run, result))
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(target.read_text(encoding="utf-8").count("新追加内容"), 1)

    def test_invalid_path_reports_failure_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            result = {"archive": {"suggested_path": "../../evil.md",
                                 "content_markdown": "x"}}
            out = asyncio.run(apply_archive(settings, {"id": "t"}, {"run_no": 1}, result))
            self.assertEqual(out["status"], "failed")
            self.assertFalse((Path(tmp).parent / "evil.md").exists())


class ArtifactTest(unittest.TestCase):
    def test_only_real_files_inside_output_dir_are_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            out = run_output_dir(settings, "t1", 1)
            (out / "report.pdf").write_bytes(b"%PDF-1.4 fake")
            (out / "notes.txt").write_text("ignored", encoding="utf-8")
            (out / "empty.pdf").write_bytes(b"")
            found = collect_artifacts(settings, "t1", 1)
            names = sorted(Path(f["path"]).name for f in found)
            self.assertEqual(names, ["report.pdf"])
            self.assertEqual(found[0]["kind"], "pdf")

    def test_outside_workspace_download_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            self.assertFalse(is_inside_allowed(settings, "/etc/passwd"))
            archive = {"status": "generated", "path": "/etc/passwd"}
            self.assertIsNone(archive_artifact(settings, "t1", archive))

    def test_real_archive_file_is_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            target = Path(settings.workspace_dir) / "数学" / "错题解析" / "2026-09-26.md"
            target.parent.mkdir(parents=True)
            target.write_text("内容", encoding="utf-8")
            row = archive_artifact(settings, "t1", {"status": "generated", "path": str(target)})
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "archive")


class OriginalSkeletonTest(unittest.TestCase):
    """原题边界：目录骨架与 .gitignore 落地，原图不落工作区。"""

    def test_original_dirs_and_gitignore_are_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            result = ensure_workspace(settings)
            root = Path(result["root"])
            self.assertTrue((root / "数学" / "原题").is_dir())
            self.assertTrue((root / "数学" / "原题" / str(date.today().year)).is_dir())
            self.assertTrue((root / "数学" / "原题" / ".gitkeep").exists())
            gitignore = root / ".gitignore"
            self.assertTrue(gitignore.exists())
            self.assertIn("原题", gitignore.read_text(encoding="utf-8"))
            self.assertTrue(result["gitignore_created"])

            # 已存在的 .gitignore 不被覆盖
            again = ensure_workspace(settings)
            self.assertFalse(again["gitignore_created"])

    def test_uploaded_asset_stays_out_of_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            ensure_workspace(settings)
            asset = store_asset(settings, "u1", png_bytes(), "page1.png")
            ws_root = Path(settings.workspace_dir).resolve()
            self.assertNotIn(ws_root, Path(asset["path"]).resolve().parents)

    def test_conflict_record_names_never_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            ensure_workspace(settings)
            when = datetime(2026, 9, 26, 10, 30, 0)
            first = write_conflict_record(settings, "# 冲突记录", when=when)
            second = write_conflict_record(settings, "# 冲突记录", when=when)
            self.assertNotEqual(first, second)
            self.assertTrue((Path(settings.workspace_dir) / first).exists())
            self.assertTrue(CONFLICT_NAME_RE.match(Path(first).name))
            self.assertTrue(CONFLICT_NAME_RE.match(Path(second).name))

    def test_archive_marks_question_uids_for_ledger_linking(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            result_payload = {
                "archive": {"suggested_path": "数学/错题解析/2026-09-26.md",
                            "content_markdown": "## 来源：模拟作业\n\n- 第 1 题：移项未变号"},
                "questions": [{"uid": "q-abc"}, {"uid": "q-def"}, {}],
            }
            out = asyncio.run(apply_archive(settings, {"id": "t1"}, {"run_no": 1},
                                            result_payload))
            self.assertEqual(out["status"], "generated")
            self.assertEqual(out["questions"], ["q-abc", "q-def"])
            text = Path(out["path"]).read_text(encoding="utf-8")
            self.assertIn("<!-- questions: q-abc,q-def -->", text)


if __name__ == "__main__":
    unittest.main()
