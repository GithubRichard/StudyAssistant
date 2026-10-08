"""数学错题示意图（SVG）测试。"""
import unittest

from app import diagram


class SanitizeSvgTest(unittest.TestCase):
    def test_valid_svg_passes(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect x="10" y="10" width="100" height="100" fill="none" stroke="black"/></svg>'
        self.assertEqual(diagram.sanitize_svg(svg), svg)

    def test_extracts_svg_from_text(self):
        raw = '好的，这是示意图：<svg width="400" height="300"><circle cx="50" cy="50" r="20"/></svg>希望有帮助'
        out = diagram.sanitize_svg(raw)
        self.assertTrue(out.startswith("<svg"))
        self.assertTrue(out.endswith("</svg>"))

    def test_rejects_script(self):
        self.assertEqual(
            diagram.sanitize_svg('<svg><script>alert(1)</script></svg>'), "")

    def test_rejects_event_handler(self):
        self.assertEqual(
            diagram.sanitize_svg('<svg><rect onclick="alert(1)" width="10"/></svg>'), "")

    def test_rejects_non_svg(self):
        self.assertEqual(diagram.sanitize_svg("没有图形"), "")
        self.assertEqual(diagram.sanitize_svg("<html><body></body></html>"), "")

    def test_rejects_invalid_xml(self):
        self.assertEqual(diagram.sanitize_svg("<svg><rect></svg>"), "")

    def test_empty(self):
        self.assertEqual(diagram.sanitize_svg(""), "")


class IsMathSubjectTest(unittest.TestCase):
    def test_math(self):
        self.assertTrue(diagram.is_math_subject("数学"))
        self.assertTrue(diagram.is_math_subject("初中数学"))

    def test_non_math(self):
        self.assertFalse(diagram.is_math_subject("英语"))
        self.assertFalse(diagram.is_math_subject(""))
        self.assertFalse(diagram.is_math_subject(None))


if __name__ == "__main__":
    unittest.main()


class GenerateDiagramTest(unittest.IsolatedAsyncioTestCase):
    async def test_generates_and_sanitizes(self):
        class FakeOutcome:
            text = '这是示意图：<svg width="400" height="300"><rect x="0" y="0" width="100" height="100"/></svg>'

        class FakeProvider:
            async def complete_text(self, system, user, max_tokens=0):
                assert "几何" in system
                assert "正方形" in user
                return FakeOutcome()

        svg = await diagram.generate_diagram_svg("甲乙两正方形边长a、b", FakeProvider())
        self.assertTrue(svg.startswith("<svg"))

    async def test_empty_stem_returns_empty(self):
        class FakeProvider:
            async def complete_text(self, system, user, max_tokens=0):
                raise AssertionError("不应被调用")

        self.assertEqual(await diagram.generate_diagram_svg("", FakeProvider()), "")
        self.assertEqual(await diagram.generate_diagram_svg("  ", FakeProvider()), "")

    async def test_provider_failure_is_fail_open(self):
        class BadProvider:
            async def complete_text(self, system, user, max_tokens=0):
                raise RuntimeError("boom")

        self.assertEqual(
            await diagram.generate_diagram_svg("正方形", BadProvider()), "")


class ShouldAttemptDiagramTest(unittest.TestCase):
    def test_math_subject(self):
        self.assertTrue(diagram.should_attempt_diagram("数学", "解方程"))
        self.assertTrue(diagram.should_attempt_diagram("初中数学", "x=1"))

    def test_empty_subject_with_geometry(self):
        self.assertTrue(diagram.should_attempt_diagram("", "图1中两正方形底边共线"))
        self.assertTrue(diagram.should_attempt_diagram("", "求阴影面积"))

    def test_empty_subject_without_geometry(self):
        self.assertFalse(diagram.should_attempt_diagram("", "exciting"))
        self.assertFalse(diagram.should_attempt_diagram("", ""))

    def test_other_subject(self):
        self.assertFalse(diagram.should_attempt_diagram("英语", "图1中两正方形"))
        self.assertFalse(diagram.should_attempt_diagram("语文", "阅读理解"))
