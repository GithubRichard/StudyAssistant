/* 离线执行真正的页面函数，验证 SVG 显示、失败提示和补图按钮。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const context = {
  esc: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"),
  STATUS_LABEL: { uncertain: "存疑" }, REVIEW_STATE_LABEL: {},
  reviewSummaryHtml: () => "", location: { reload() {} }, setTimeout() {},
};
vm.createContext(context);
const bodyStart = source.indexOf("function resultBodyHtml(");
const bodyEnd = source.indexOf("function followupCardHtml(", bodyStart);
vm.runInContext(source.slice(bodyStart, bodyEnd), context);
const buttonStart = source.indexOf("function bindDiagramButton(");
const buttonEnd = source.indexOf("async function pagePractice(", buttonStart);
vm.runInContext(source.slice(buttonStart, buttonEnd), context);

const svg = "<svg><rect width='10' height='10'/></svg>";
const q = { id: "q1", no: "20(1)", stem: "正方形", status: "uncertain", diagram_svg: svg };
assert.ok(context.resultBodyHtml({ subject: "数学", result: {} }, [q], {}).includes(svg));
const failed = { ...q, diagram_svg: "", diagram: { status: "failed", message: "额度不足<script>" } };
const html = context.resultBodyHtml({ subject: "数学", result: {} }, [failed], {});
assert.ok(html.includes("示意图生成失败：额度不足&lt;script&gt;"));
assert.ok(!html.includes("<script>"));

async function click(response, question = failed) {
  let inserted = 0;
  const btn = { disabled: false }, msg = {};
  const bar = { querySelector: (selector) => selector === "#genDiagrams" ? btn : msg };
  context.document = { createElement: () => bar };
  context.S = { api: async () => response };
  const app = { querySelector: () => ({ firstChild: null, insertBefore: () => inserted++ }) };
  context.bindDiagramButton(app, { id: "t1", subject: "数学", result: { questions: [question] } }, () => true);
  if (inserted) await btn.onclick({ target: btn });
  return { inserted, btn, msg };
}

(async () => {
  let r = await click({ generated: 0, failures: ["20(1): 绘图输出被截断"] });
  assert.equal(r.inserted, 1); // 没有 uid、短题干也可补图
  assert.ok(r.msg.textContent.includes("截断"));
  assert.equal(r.btn.disabled, false);
  r = await click({ generated: 0, message: "无需绘图", failures: [] });
  assert.equal(r.msg.textContent, "无需绘图");
  r = await click({ generated: 1, results: [{ status: "failed" }] });
  assert.ok(r.msg.textContent.includes("已生成 1 张，1 题失败"));
  r = await click({});
  assert.ok(r.msg.textContent.includes("未返回绘图结果"));
  r = await click({}, { ...failed, diagram: { status: "skipped" } });
  assert.equal(r.inserted, 0);
  console.log("Diagram web regression tests passed");
})().catch((err) => { console.error(err); process.exitCode = 1; });
