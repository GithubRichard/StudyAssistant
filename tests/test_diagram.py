"""数学错题示意图（SVG）测试。"""
import unittest
from types import SimpleNamespace

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
            text = '[{"t":"rect","x":0,"y":0,"w":100,"h":100,"fill":"none"}]'

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


RECT_JSON = '[{"t":"rect","x":50,"y":50,"w":100,"h":100,"fill":"none"}]'


class SequenceProvider:
    def __init__(self, responses, name="ds", cap=0):
        self.name = name
        self.cfg = SimpleNamespace(max_output_tokens=cap)
        self.responses = list(responses)
        self.budgets = []

    async def complete_text(self, system, user, max_tokens=0):
        self.budgets.append(max_tokens)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def outcome(text="", finish="stop", thinking=""):
    return SimpleNamespace(text=text, finish_reason=finish, thinking=thinking)


class DiagramOutcomeTest(unittest.IsolatedAsyncioTestCase):
    async def test_ds_retries_with_larger_budget_before_fallback(self):
        ds = SequenceProvider([outcome(finish="length", thinking="reasoning"),
                               outcome(RECT_JSON)])
        backup = SequenceProvider([], name="backup")
        r = await diagram.generate_diagram("正方形", [ds, backup])
        self.assertEqual(ds.budgets, [4000, 8000])
        self.assertEqual(backup.budgets, [])
        self.assertEqual((r.status, r.provider, r.attempts), ("generated", "ds", 2))
        self.assertTrue(r.svg.startswith("<svg"))

    async def test_truncated_valid_json_is_not_accepted(self):
        ds = SequenceProvider([outcome(RECT_JSON, "length"), outcome(RECT_JSON, "length")])
        r = await diagram.generate_diagram("正方形", ds)
        self.assertEqual((r.status, r.reason, r.svg), ("failed", "truncated", ""))
        self.assertEqual(len(ds.budgets), 2)

    async def test_fallback_after_ds_retry_fails(self):
        ds = SequenceProvider([outcome(finish="length"), outcome(finish="length")])
        backup = SequenceProvider([outcome(RECT_JSON)], name="backup")
        r = await diagram.generate_diagram("正方形", [ds, backup])
        self.assertEqual((r.status, r.provider, r.attempts), ("generated", "backup", 3))

    async def test_respects_provider_output_cap_without_duplicate_retry(self):
        ds = SequenceProvider([outcome(finish="length")], cap=1024)
        r = await diagram.generate_diagram("正方形", ds)
        self.assertEqual(ds.budgets, [1024])
        self.assertEqual(r.reason, "truncated")

    async def test_retry_budget_can_grow_only_to_cap(self):
        ds = SequenceProvider([outcome(finish="length"), outcome(RECT_JSON)], cap=6000)
        self.assertEqual((await diagram.generate_diagram("正方形", ds)).status, "generated")
        self.assertEqual(ds.budgets, [4000, 6000])

    async def test_reasoning_is_not_used_as_final_json(self):
        ds = SequenceProvider([outcome(thinking=RECT_JSON), outcome(thinking=RECT_JSON)])
        r = await diagram.generate_diagram("正方形", ds)
        self.assertEqual((r.status, r.reason), ("failed", "empty_output"))
        self.assertEqual(ds.budgets, [4000, 8000])

    async def test_empty_output_without_reasoning_moves_to_backup(self):
        ds = SequenceProvider([outcome()])
        backup = SequenceProvider([outcome(RECT_JSON)], name="backup")
        self.assertEqual((await diagram.generate_diagram("正方形", [ds, backup])).provider,
                         "backup")
        self.assertEqual(ds.budgets, [4000])

    async def test_empty_array_skips_algebra(self):
        ds = SequenceProvider([outcome("[]")])
        r = await diagram.generate_diagram("解方程 2x+1=9", ds)
        self.assertEqual((r.status, r.reason), ("skipped", "no_geometry"))

    async def test_empty_array_for_geometry_reports_missing_information(self):
        ds = SequenceProvider([outcome("[]")])
        r = await diagram.generate_diagram("如图求阴影面积", ds)
        self.assertEqual((r.status, r.reason), ("failed", "insufficient_geometry"))
        self.assertIn("原图", r.message)

    async def test_invalid_json_is_failure_not_skipped(self):
        for text in ('[{"t":"rect"}', "没有输出 JSON", "{}"):
            ds = SequenceProvider([outcome(text)])
            self.assertEqual((await diagram.generate_diagram("正方形", ds)).reason,
                             "invalid_json")

    async def test_malformed_shapes_do_not_generate_blank_or_partial_svg(self):
        for text in ('[{"t":"rect"}]', '[{"t":"unknown"}]',
                     '[{"t":"text","x":1,"y":1,"s":"A"}]',
                     RECT_JSON[:-1] + ',{"t":"rect"}]'):
            r = await diagram.generate_diagram("正方形", SequenceProvider([outcome(text)]))
            self.assertEqual((r.reason, r.svg), ("invalid_shapes", ""))

    async def test_provider_exception_does_not_leak_response_to_page(self):
        r = await diagram.generate_diagram("正方形", SequenceProvider([RuntimeError("secret")]))
        self.assertEqual(r.reason, "provider_error")
        self.assertNotIn("secret", r.message)

    async def test_no_provider_is_failure(self):
        self.assertEqual((await diagram.generate_diagram("正方形", [])).reason, "no_provider")

    def test_labels_are_xml_escaped(self):
        svg = diagram.shapes_to_svg([
            {"t": "rect", "x":1, "y":1, "w":10, "h":10},
            {"t": "text", "x":1, "y":1, "s":"A&B<"},
        ])
        self.assertIn("A&amp;B&lt;", svg)
        self.assertTrue(diagram.sanitize_svg(svg))

    def test_rejects_active_svg_bypasses(self):
        for svg in (
            '<svg><foreignObject><div xmlns="http://www.w3.org/1999/xhtml"/></foreignObject></svg>',
            '<svg xmlns:s="urn:other"><s:script/></svg>',
            '<svg><image href="https://example.org/a.png"/></svg>',
            '<svg><rect><animate attributeName="x"/></rect></svg>',
            '<svg><rect fill="url(https://example.org/a.svg)"/></svg>',
        ):
            self.assertEqual(diagram.sanitize_svg(svg), "")


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
