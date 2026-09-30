"""转写前图片预处理：校正文字方向并让模型"看"得更清楚。

1. prepare_extract_image：EXIF 自动旋转，再用 Tesseract OSD 检测纸面文字的
   90°倍数旋转；置信度不足时在 info 中要求上游阻断批改。随后转 RGB、缩放、
   轻度锐化/对比度提升。返回 (bytes, mime, info)。
2. make_zoom_tiles：把一页切成 grid×grid 重叠局部图并放大，
   供提取阶段对字迹存疑的题做第二遍复核。视觉模型通常会把输入图
   缩放到固定尺寸（如长边 ~1568px），整页发送时手写字会被压得很小；
   局部图覆盖 1/grid 的区域，同样缩放后字迹的有效分辨率约为 grid 倍。
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
from typing import List, Tuple

log = logging.getLogger(__name__)

try:
    from PIL import Image, ImageEnhance, ImageOps
    _PIL_OK = True
except Exception:  # noqa: BLE001
    _PIL_OK = False

try:
    import pillow_heif  # noqa: F401
    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pass


# Tesseract OSD confidence is not a probability. On the uploaded sideways exam
# it reports 4.62 after a modest upscale; values below 2 tend to be ambiguous.
_MIN_ORIENTATION_CONFIDENCE = 2.0
_ORIENTATION_TIMEOUT_SECONDS = 8


def _detect_text_rotation(img) -> dict:
    """Ask Tesseract OSD how many degrees the page text must be rotated.

    Pillow's positive ``Image.rotate`` angle is counter-clockwise, which is the
    direction Tesseract's ``Rotate`` field asks us to apply. Return a status
    rather than guessing when OCR is unavailable or its confidence is low.
    """
    if not _PIL_OK:
        return {"status": "unavailable", "rotation": 0, "confidence": None,
                "error": "Pillow unavailable"}
    try:
        probe = img.copy()
        long_side = max(probe.size)
        if long_side < 2048:
            scale = 2048 / long_side
            probe = probe.resize((round(probe.width * scale),
                                  round(probe.height * scale)), Image.LANCZOS)
        if probe.mode != "L":
            probe = probe.convert("L")
        buf = io.BytesIO()
        probe.save(buf, format="PNG")
        completed = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "0"],
            input=buf.getvalue(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=_ORIENTATION_TIMEOUT_SECONDS,
            check=False,
        )
        output = completed.stdout.decode("utf-8", errors="replace")
        rotation_match = re.search(r"(?m)^Rotate:\s*(0|90|180|270)\s*$", output)
        confidence_match = re.search(
            r"(?m)^Orientation confidence:\s*([0-9]+(?:\.[0-9]+)?)\s*$", output)
        if completed.returncode != 0 or not rotation_match or not confidence_match:
            return {"status": "uncertain", "rotation": 0, "confidence": None,
                    "error": output[-300:] or "Tesseract OSD returned no orientation"}
        rotation = int(rotation_match.group(1))
        confidence = float(confidence_match.group(1))
        if confidence < _MIN_ORIENTATION_CONFIDENCE:
            return {"status": "uncertain", "rotation": rotation,
                    "confidence": confidence,
                    "error": "orientation confidence below threshold"}
        return {"status": "rotated" if rotation else "upright",
                "rotation": rotation, "confidence": confidence, "error": ""}
    except FileNotFoundError:
        return {"status": "uncertain", "rotation": 0, "confidence": None,
                "error": "tesseract executable not installed"}
    except subprocess.TimeoutExpired:
        return {"status": "uncertain", "rotation": 0, "confidence": None,
                "error": "orientation detection timed out"}
    except Exception as e:  # noqa: BLE001
        return {"status": "uncertain", "rotation": 0, "confidence": None,
                "error": str(e)[:300]}


def prepare_extract_image(image_bytes: bytes, mime: str,
                          min_long_side: int = 2048,
                          max_long_side: int = 4096,
                          jpeg_quality: int = 90) -> Tuple[bytes, str, dict]:
    """转写前预处理。返回方向检测状态，由批改编排器决定是否继续。

    返回 (bytes, mime, info)：info = {"width": 最终宽, "height": 最终高,
    "exif_orientation": EXIF 方向值（无则为 None）, "exif_rotated": 是否因
    EXIF 旋转/翻转, "text_rotation_degrees": 自动旋转角度,
    "orientation_confidence": OSD 方向置信度, "orientation_check_required": 是否
    因方向无法确认而必须阻断批改}。
    """
    info = {"width": 0, "height": 0,
            "exif_orientation": None, "exif_rotated": False,
            "text_rotation_degrees": 0, "orientation_confidence": None,
            "orientation_status": "unavailable",
            "orientation_check_required": False,
            "orientation_error": ""}
    if not image_bytes:
        return image_bytes, mime, info
    if not _PIL_OK:
        info.update({"orientation_status": "uncertain",
                     "orientation_check_required": True,
                     "orientation_error": "Pillow unavailable"})
        return image_bytes, mime, info
    try:
        img = Image.open(io.BytesIO(image_bytes))
        try:
            orientation = img.getexif().get(0x0112)
        except Exception:  # noqa: BLE001
            orientation = None
        info["exif_orientation"] = orientation
        # orientation 缺失或为 1（正常）时 exif_transpose 不做任何事
        info["exif_rotated"] = orientation not in (None, 1)
        img = ImageOps.exif_transpose(img)  # 手机照片自动摆正
        if img.mode != "RGB":
            img = img.convert("RGB")
        orientation_result = _detect_text_rotation(img)
        info["orientation_status"] = orientation_result["status"]
        info["text_rotation_degrees"] = orientation_result["rotation"]
        info["orientation_confidence"] = orientation_result["confidence"]
        info["orientation_error"] = orientation_result["error"]
        info["orientation_check_required"] = orientation_result["status"] == "uncertain"
        if orientation_result["status"] == "rotated":
            img = img.rotate(orientation_result["rotation"], expand=True)
        w, h = img.size
        long_side = max(w, h)
        target = long_side
        if min_long_side > 0 and long_side < min_long_side:
            target = min_long_side
        elif max_long_side > 0 and long_side > max_long_side:
            target = max_long_side
        if target != long_side:
            scale = target / long_side
            img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        # 轻度锐化 + 对比度，手写笔画更清晰；强度保守，避免噪点被放大
        img = ImageEnhance.Sharpness(img).enhance(1.35)
        img = ImageEnhance.Contrast(img).enhance(1.08)
        info["width"], info["height"] = img.size
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality)
        return buf.getvalue(), "image/jpeg", info
    except Exception as e:  # noqa: BLE001
        # Invalid/mock bytes (or unsupported image formats) cannot be oriented;
        # preserve the historic fail-open behavior for callers that do not
        # supply a decodable image. Valid images with an OSD failure are marked
        # orientation_check_required and blocked by the grading pipeline.
        log.warning("图片预处理失败，原样发送: %s", e)
        return image_bytes, mime, info


def make_zoom_tiles(image_bytes: bytes, mime: str, page: int,
                    grid: int = 2, overlap: float = 0.12,
                    tile_min_long_side: int = 1600,
                    jpeg_quality: int = 88) -> List[Tuple[bytes, str, str]]:
    """把一页切成 grid×grid 重叠局部图并放大。

    返回 [(bytes, mime, label)]，label 如 "图1-局部(第1行第2列)"。
    失败返回 []（调用方跳过复核，不阻断流程）。
    """
    if not _PIL_OK or not image_bytes or grid < 2:
        return []
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        # 每块的理论尺寸 + 向外扩展 overlap，避免字迹正好被切在边界上
        cell_w, cell_h = w / grid, h / grid
        pad_w, pad_h = cell_w * overlap, cell_h * overlap
        tiles: List[Tuple[bytes, str, str]] = []
        for r in range(grid):
            for c in range(grid):
                x0 = max(0, int(c * cell_w - pad_w))
                y0 = max(0, int(r * cell_h - pad_h))
                x1 = min(w, int((c + 1) * cell_w + pad_w))
                y1 = min(h, int((r + 1) * cell_h + pad_h))
                tile = img.crop((x0, y0, x1, y1))
                tw, th = tile.size
                if tile_min_long_side > 0 and max(tw, th) < tile_min_long_side:
                    scale = tile_min_long_side / max(tw, th)
                    tile = tile.resize((round(tw * scale), round(th * scale)),
                                       Image.LANCZOS)
                tile = ImageEnhance.Sharpness(tile).enhance(1.35)
                buf = io.BytesIO()
                tile.save(buf, format="JPEG", quality=jpeg_quality)
                label = f"图{page}-局部(第{r + 1}行第{c + 1}列)"
                tiles.append((buf.getvalue(), "image/jpeg", label))
        return tiles
    except Exception as e:  # noqa: BLE001
        log.warning("局部图切分失败，跳过放大复核: %s", e)
        return []
