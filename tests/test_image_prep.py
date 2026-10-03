"""图片预处理模块测试：放大/缩小/切分/异常兜底。"""
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from PIL import Image

from app import image_prep


def _jpeg(w, h, color=(200, 200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


def _size(b: bytes):
    return Image.open(io.BytesIO(b)).size


class PrepareTest(unittest.TestCase):
    def test_clockwise_angle_and_manual_override(self):
        image = Image.new("RGB", (40, 60), "white")
        for x in range(15):
            for y in range(15):
                image.putpixel((x, y), (255, 0, 0))
        buf = io.BytesIO()
        image.save(buf, "PNG")
        for angle, point in [(0, (5, 5)), (90, (54, 5)), (180, (34, 54)), (270, (5, 34))]:
            with self.subTest(angle=angle), patch.object(image_prep, "_detect_text_rotation") as osd:
                out, _, info = image_prep.prepare_extract_image(
                    buf.getvalue(), "image/png", 0, 0, confirmed_rotation=angle)
                red, green, blue = Image.open(io.BytesIO(out)).getpixel(point)
                self.assertGreater(red, 200)
                self.assertLess(green, 60)
                self.assertFalse(info["orientation_check_required"])
                osd.assert_not_called()

    def test_upscale_to_min_long_side(self):
        out, mime, info = image_prep.prepare_extract_image(
            _jpeg(400, 300), "image/jpeg", min_long_side=2048, max_long_side=4096)
        self.assertEqual(mime, "image/jpeg")
        w, h = _size(out)
        self.assertEqual(max(w, h), 2048)
        # 宽高比保持
        self.assertAlmostEqual(w / h, 400 / 300, places=2)

    def test_downscale_over_max(self):
        out, _, _info = image_prep.prepare_extract_image(
            _jpeg(5000, 4000), "image/jpeg", min_long_side=2048, max_long_side=4096)
        w, h = _size(out)
        self.assertEqual(max(w, h), 4096)

    def test_no_change_when_in_range(self):
        out, _, _info = image_prep.prepare_extract_image(
            _jpeg(3000, 2000), "image/jpeg", min_long_side=2048, max_long_side=4096)
        self.assertEqual(_size(out), (3000, 2000))

    def test_min_zero_disables_upscale(self):
        out, _, _info = image_prep.prepare_extract_image(
            _jpeg(400, 300), "image/jpeg", min_long_side=0, max_long_side=0)
        self.assertEqual(_size(out), (400, 300))

    def test_fail_open_on_garbage(self):
        bad = b"not-an-image"
        out, mime, info = image_prep.prepare_extract_image(bad, "image/jpeg")
        self.assertEqual(out, bad)
        self.assertEqual(mime, "image/jpeg")

    def test_fail_open_on_empty(self):
        out, mime, info = image_prep.prepare_extract_image(b"", "image/jpeg")
        self.assertEqual(out, b"")

    def test_info_reports_final_size_and_no_exif_rotation(self):
        out, mime, info = image_prep.prepare_extract_image(
            _jpeg(400, 300), "image/jpeg", min_long_side=2048, max_long_side=4096)
        self.assertEqual((info["width"], info["height"]), _size(out))
        self.assertIsNone(info["exif_orientation"])
        self.assertFalse(info["exif_rotated"])

    def test_info_reports_exif_rotation(self):
        # EXIF orientation=6：需顺时针转 90 度
        img = Image.new("RGB", (400, 300), (200, 200, 200))
        exif = img.getexif()
        exif[0x0112] = 6
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        out, mime, info = image_prep.prepare_extract_image(
            buf.getvalue(), "image/jpeg", min_long_side=0, max_long_side=0)
        self.assertEqual(info["exif_orientation"], 6)
        self.assertTrue(info["exif_rotated"])
        # 转正后宽高互换
        self.assertEqual((info["width"], info["height"]), (300, 400))

    def test_auto_rotates_when_osd_finds_sideways_text(self):
        with patch.object(image_prep, "_detect_text_rotation", return_value={
                "status": "rotated", "rotation": 90, "confidence": 4.2,
                "error": ""}):
            out, _mime, info = image_prep.prepare_extract_image(
                _jpeg(1600, 1200), "image/jpeg", min_long_side=0, max_long_side=0)
        self.assertEqual(_size(out), (1200, 1600))
        self.assertEqual(info["text_rotation_degrees"], 90)
        self.assertEqual(info["orientation_status"], "rotated")
        self.assertFalse(info["orientation_check_required"])

    def test_low_orientation_confidence_requires_confirmation(self):
        with patch.object(image_prep, "_detect_text_rotation", return_value={
                "status": "uncertain", "rotation": 90, "confidence": 0.5,
                "error": "orientation confidence below threshold"}):
            _out, _mime, info = image_prep.prepare_extract_image(
                _jpeg(1600, 1200), "image/jpeg", min_long_side=0, max_long_side=0)
        self.assertEqual(info["orientation_status"], "uncertain")
        self.assertTrue(info["orientation_check_required"])


class TilesTest(unittest.TestCase):
    def test_grid_2x2(self):
        tiles = image_prep.make_zoom_tiles(_jpeg(800, 600), "image/jpeg", page=1,
                                           grid=2, tile_min_long_side=1600)
        self.assertEqual(len(tiles), 4)
        labels = [t[2] for t in tiles]
        self.assertEqual(labels, ["图1-局部(第1行第1列)", "图1-局部(第1行第2列)",
                                  "图1-局部(第2行第1列)", "图1-局部(第2行第2列)"])
        for b, mime, _ in tiles:
            self.assertEqual(mime, "image/jpeg")
            w, h = _size(b)
            self.assertGreaterEqual(max(w, h), 1600)

    def test_grid_3(self):
        tiles = image_prep.make_zoom_tiles(_jpeg(900, 900), "image/jpeg", page=2,
                                           grid=3, tile_min_long_side=0)
        self.assertEqual(len(tiles), 9)
        self.assertEqual(tiles[0][2], "图2-局部(第1行第1列)")

    def test_fail_open_on_garbage(self):
        self.assertEqual(image_prep.make_zoom_tiles(b"xx", "image/jpeg", page=1), [])


if __name__ == "__main__":
    unittest.main()
