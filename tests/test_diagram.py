"""数学错题示意图（SVG）测试。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

from app import diagram
from app.config import DiagramConfig, ProviderConfig
from app.providers import OpenAICompatibleProvider


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
            async def complete_text(self, system, user, max_tokens=0, timeout=None):
                assert "几何" in system
                assert "正方形" in user
                return FakeOutcome()

        svg = await diagram.generate_diagram_svg("甲乙两正方形边长a、b", FakeProvider())
        self.assertTrue(svg.startswith("<svg"))

    async def test_empty_stem_returns_empty(self):
        class FakeProvider:
            async def complete_text(self, system, user, max_tokens=0, timeout=None):
                raise AssertionError("不应被调用")

        self.assertEqual(await diagram.generate_diagram_svg("", FakeProvider()), "")
        self.assertEqual(await diagram.generate_diagram_svg("  ", FakeProvider()), "")

    async def test_provider_failure_is_fail_open(self):
        class BadProvider:
            async def complete_text(self, system, user, max_tokens=0, timeout=None):
                raise RuntimeError("boom")

        self.assertEqual(
            await diagram.generate_diagram_svg("正方形", BadProvider()), "")


RECT_JSON = '[{"t":"rect","x":50,"y":50,"w":100,"h":100,"fill":"none"}]'


class SequenceProvider:
    def __init__(self, responses, name="ds", cap=0, delay=0.0):
        self.name = name
        self.cfg = SimpleNamespace(max_output_tokens=cap)
        self.responses = list(responses)
        self.budgets = []
        self.timeouts = []
        self.delay = delay

    async def complete_text(self, system, user, max_tokens=0, timeout=None):
        self.budgets.append(max_tokens)
        self.timeouts.append(timeout)
        if self.delay:
            await asyncio.sleep(self.delay)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def outcome(text="", finish="stop", thinking=""):
    return SimpleNamespace(text=text, finish_reason=finish, thinking=thinking)


# 两档额度：用于覆盖「正文被截断 → 放大重试」等分档逻辑
TIERED = DiagramConfig(max_tokens=4000, retry_max_tokens=8000)


async def gen(stem, providers, cfg=TIERED):
    return await diagram.generate_diagram(stem, providers, cfg)


class DiagramConfigTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_is_single_large_budget_with_long_timeout(self):
        ds = SequenceProvider([outcome(RECT_JSON[:20], finish="length")])
        r = await diagram.generate_diagram(stem="正方形", providers=ds)
        # 默认一档 32000，正文截断也不放大重试（未配置 retry_max_tokens）
        self.assertEqual(ds.budgets, [32000])
        self.assertEqual(r.reason, "truncated")
        self.assertAlmostEqual(ds.timeouts[0], 300.0, delta=1.0)

    async def test_call_timeout_is_capped_by_remaining_deadline(self):
        ds = SequenceProvider([outcome(RECT_JSON)])
        cfg = DiagramConfig(timeout_seconds=300, deadline_seconds=50)
        await diagram.generate_diagram("正方形", ds, cfg)
        self.assertLessEqual(ds.timeouts[0], 50)

    async def test_zero_timeout_falls_back_to_provider_timeout(self):
        ds = SequenceProvider([outcome(RECT_JSON)])
        await diagram.generate_diagram("正方形", ds,
                                       DiagramConfig(timeout_seconds=0, deadline_seconds=0))
        self.assertEqual(ds.timeouts, [None])

    async def test_deadline_stops_slow_candidate_and_skips_the_rest(self):
        slow = SequenceProvider([outcome(RECT_JSON)], delay=1.0)
        backup = SequenceProvider([outcome(RECT_JSON)], name="backup")
        cfg = DiagramConfig(deadline_seconds=0.05)
        r = await diagram.generate_diagram("正方形", [slow, backup], cfg)
        self.assertEqual((r.status, r.reason), ("failed", "timeout"))
        self.assertEqual(backup.budgets, [])

    async def test_keeps_chain_order(self):
        # glm-5.3-flash 也是思考模型且更慢，不再把含 glm 的候选提前
        ds = SequenceProvider([outcome(RECT_JSON)], name="ds")
        glmf = SequenceProvider([outcome(RECT_JSON)], name="glmf")
        r = await diagram.generate_diagram(stem="正方形", providers=[ds, glmf])
        self.assertEqual(r.provider, "ds")
        self.assertEqual(glmf.budgets, [])

    def test_lazy_config_caps_deadline(self):
        self.assertEqual(diagram.lazy_config(DiagramConfig()).deadline_seconds,
                         diagram.LAZY_DEADLINE_SECONDS)
        self.assertEqual(diagram.lazy_config(
            DiagramConfig(deadline_seconds=0)).deadline_seconds, diagram.LAZY_DEADLINE_SECONDS)
        self.assertEqual(diagram.lazy_config(
            DiagramConfig(deadline_seconds=30)).deadline_seconds, 30)

    def test_rejects_invalid_values(self):
        for kwargs in ({"max_tokens": 0}, {"retry_max_tokens": -1},
                       {"timeout_seconds": -1}, {"deadline_seconds": -1}):
            with self.assertRaises(ValidationError):
                DiagramConfig(**kwargs)

    async def test_provider_uses_per_call_timeout(self):
        seen = []

        class FakeClient:
            def __init__(self, timeout):
                seen.append(timeout)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json, headers):
                return SimpleNamespace(status_code=200, json=lambda: {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

        prov = OpenAICompatibleProvider("ds", ProviderConfig(
            base_url="http://x", api_key="k", model="m", timeout=90))
        with mock.patch("app.providers.httpx.AsyncClient", FakeClient):
            await prov.complete_text("s", "u", max_tokens=10, timeout=300)
            await prov.complete_text("s", "u", max_tokens=10)
        self.assertEqual(seen, [300, 90])


class DiagramOutcomeTest(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_text_retries_with_larger_budget_before_fallback(self):
        ds = SequenceProvider([outcome(RECT_JSON[:20], finish="length"),
                               outcome(RECT_JSON)])
        backup = SequenceProvider([], name="backup")
        r = await gen("正方形", [ds, backup])
        self.assertEqual(ds.budgets, [4000, 8000])
        self.assertEqual(backup.budgets, [])
        self.assertEqual((r.status, r.provider, r.attempts), ("generated", "ds", 2))
        self.assertTrue(r.svg.startswith("<svg"))

    async def test_thinking_exhaustion_does_not_escalate_budget(self):
        # 2026-10-09 实测：4000/8000 两档思考都把额度用满、正文 0 字；
        # 首档不够时同一模型再放大没有依据，直接换下一个候选。
        ds = SequenceProvider([outcome(finish="length", thinking="很长的思考" * 50)])
        backup = SequenceProvider([outcome(RECT_JSON)], name="backup")
        r = await gen("正方形", [ds, backup])
        self.assertEqual(ds.budgets, [4000])
        self.assertEqual((r.status, r.provider, r.attempts), ("generated", "backup", 2))

    async def test_thinking_exhaustion_is_reported_when_no_candidate_works(self):
        ds = SequenceProvider([outcome(finish="length", thinking="很长的思考")])
        r = await gen("正方形", [ds])
        self.assertEqual((r.status, r.reason), ("failed", "thinking_exhausted"))
        self.assertEqual(ds.budgets, [4000])

    async def test_truncated_valid_json_is_not_accepted(self):
        ds = SequenceProvider([outcome(RECT_JSON, "length"), outcome(RECT_JSON, "length")])
        r = await gen("正方形", ds)
        self.assertEqual((r.status, r.reason, r.svg), ("failed", "truncated", ""))
        self.assertEqual(len(ds.budgets), 2)

    async def test_fallback_after_ds_retry_fails(self):
        ds = SequenceProvider([outcome(RECT_JSON[:20], finish="length"),
                               outcome(RECT_JSON[:20], finish="length")])
        backup = SequenceProvider([outcome(RECT_JSON)], name="backup")
        r = await gen("正方形", [ds, backup])
        self.assertEqual((r.status, r.provider, r.attempts), ("generated", "backup", 3))

    async def test_respects_provider_output_cap_without_duplicate_retry(self):
        ds = SequenceProvider([outcome(finish="length")], cap=1024)
        r = await gen("正方形", ds)
        self.assertEqual(ds.budgets, [1024])
        self.assertEqual(r.reason, "truncated")

    async def test_retry_budget_can_grow_only_to_cap(self):
        ds = SequenceProvider([outcome(RECT_JSON[:20], finish="length"),
                               outcome(RECT_JSON)], cap=6000)
        self.assertEqual((await gen("正方形", ds)).status, "generated")
        self.assertEqual(ds.budgets, [4000, 6000])

    async def test_reasoning_is_not_used_as_final_json(self):
        ds = SequenceProvider([outcome(thinking=RECT_JSON)])
        r = await gen("正方形", ds)
        self.assertEqual((r.status, r.reason), ("failed", "empty_output"))
        # 只有思考、没有正文不是额度问题，不再加倍重试
        self.assertEqual(ds.budgets, [4000])

    async def test_empty_output_without_reasoning_moves_to_backup(self):
        ds = SequenceProvider([outcome()])
        backup = SequenceProvider([outcome(RECT_JSON)], name="backup")
        self.assertEqual((await gen("正方形", [ds, backup])).provider,
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
            self.assertEqual((await gen("正方形", ds)).reason,
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
