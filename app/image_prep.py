"""转写前图片预处理：让模型"看"得更清楚。

两件事：
1. prepare_extract_image：EXIF 自动旋转、转 RGB、放大到最小长边、
   超限缩小、轻度锐化/对比度提升。失败时原样返回（fail-open）。
2. make_zoom_tiles：把一页切成 grid×grid 重叠局部图并放大，
   供提取阶段对字迹存疑的题做第二遍复核。视觉模型通常会把输入图
   缩放到固定尺寸（如长边 ~1568px），整页发送时手写字会被压得很小；
   局部图覆盖 1/grid 的区域，同样缩放后字迹的有效分辨率约为 grid 倍。
"""
from __future__ import annotations

import io
import logging
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


def prepare_extract_image(image_bytes: bytes, mime: str,
                          min_long_side: int = 2048,
                          max_long_side: int = 4096,
                          jpeg_quality: int = 90) -> Tuple[bytes, str]:
    """转写前预处理。任何异常都原样返回输入，不阻断流程。"""
    if not _PIL_OK or not image_bytes:
        return image_bytes, mime
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)  # 手机照片自动摆正
        if img.mode != "RGB":
            img = img.convert("RGB")
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
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:  # noqa: BLE001
        log.warning("图片预处理失败，原样发送: %s", e)
        return image_bytes, mime


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
