"use strict";
/* 学习助手 · 网页版 v1
 * IA：学习（今日学习台+提交）/ 复习（跨天未解决台账）/ 历史 / 我的
 */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const fmtDT = (ts) => {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fmtD = (ts) => {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

const HEIC_RE = /\.(heic|heif)$/i;
/* HEIC 预览：Chrome 等浏览器无法直接渲染，退回文字占位；真正的解码在服务端完成。 */
function renderThumbs(box, files) {
  box.innerHTML = "";
  files.slice(0, 20).forEach((f) => {
    if (HEIC_RE.test(f.name || "") || /heic|heif/i.test(f.type || "")) {
      const ph = document.createElement("div");
      ph.className = "img-preview-fallback";
      ph.textContent = "HEIC\n提交后自动转换";
      box.appendChild(ph);
      return;
    }
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.onerror = () => {
      const ph = document.createElement("div");
      ph.className = "img-preview-fallback";
      ph.textContent = "预览不可用\n提交后自动转换";
      img.replaceWith(ph);
    };
    box.appendChild(img);
  });
}

let toastTimer = 0;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

function confirmDialog(title, body) {
  return new Promise((resolve) => {
    const mask = $("#modal");
    $("#modalTitle").textContent = title;
    $("#modalBody").innerHTML = body;
    const box = $("#modalActions");
    box.innerHTML = "";
    const cancel = document.createElement("button");
    cancel.className = "btn ghost";
    cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.className = "btn primary";
    ok.textContent = "确定";
    const done = (v) => { mask.hidden = true; box.innerHTML = ""; resolve(v); };
    cancel.onclick = () => done(false);
    ok.onclick = () => done(true);
    box.append(cancel, ok);
    mask.hidden = false;
  });
}

/* ---------------- 状态与 API ---------------- */
const S = {
  token: localStorage.getItem("sa_token") || "",
  meta: null,
  user: JSON.parse(localStorage.getItem("sa_user") || "null"),
  pollTimer: 0,

  async api(path, opts = {}) {
    const headers = {};
    if (S.token) headers["Authorization"] = "Bearer " + S.token;
    let body = opts.body;
    if (body !== undefined && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch("/api" + path, {
      method: opts.method || "GET", headers, body,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* 非 JSON */ }
    if (!res.ok) {
      throw { status: res.status, message: data.detail || data.message || `请求失败（${res.status}）` };
    }
    return data;
  },

  saveSession(token, user) {
    S.token = token;
    S.user = user;
    localStorage.setItem("sa_token", token);
    localStorage.setItem("sa_user", JSON.stringify(user));
  },
  logout() {
    S.token = "";
    S.user = null;
    localStorage.removeItem("sa_token");
    localStorage.removeItem("sa_user");
  },
};

/* ---------------- 路由 ---------------- */
function parseHash() {
  const h = location.hash || "#/home";
  const [pathPart, queryPart] = h.slice(2).split("?");
  const segs = pathPart.split("/").filter(Boolean);
  const query = {};
  (queryPart || "").split("&").forEach((kv) => {
    if (!kv) return;
    const [k, v] = kv.split("=");
    query[decodeURIComponent(k)] = decodeURIComponent(v || "");
  });
  return { name: segs[0] || "home", param: segs[1] || "", query };
}

function go(name, param = "", query = {}) {
  const qs = Object.entries(query)
    .filter(([, v]) => v !== "" && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
  location.hash = `#/${name}${param ? "/" + encodeURIComponent(param) : ""}${qs ? "?" + qs : ""}`;
}

const STATE_LABEL = {
  pending_correction: "待订正",
  corrected_pending_retest: "待复测",
  retest_failed: "复测未通过",
  retest_passed: "已通过",
};
const TASK_TYPE_LABEL = { grading: "批改", qa: "问问题", training: "训练", weekly_report: "周报", retest: "复测" };
const TRAINING_KIND_LABEL = { topic: "按知识点", monthly: "月考", midterm: "期中", final: "期末" };

let renderSeq = 0;

async function render() {
  clearTimeout(S.pollTimer);
  const seq = ++renderSeq;
  const app = $("#app");
  const alive = () => seq === renderSeq;
  try {
    const r = parseHash();
    if (r.name !== "login" && !S.token) { go("login"); return; }
    if (r.name === "login" && S.token) { go("home"); return; }
    if (!S.meta) S.meta = await S.api("/web/meta").catch(() => null);

    const pages = {
      login: pageLogin, home: pageHome, learn: pageLearn, task: pageTask,
      result: pageResult, practice: pagePractice, review: pageReview,
      history: pageHistory, mine: pageMine,
    };
    // 复习详情复用 review 路由（带 param 即为详情）
    const fn = pages[r.name] || pageHome;
    await fn(app, r, alive);
  } catch (e) {
    if (!alive()) return;
    if (e && e.status === 401) {
      S.logout();
      toast("登录已过期，请重新登录");
      go("login");
      return;
    }
    app.innerHTML = `<div class="page"><div class="card"><div class="card-title">出错了</div>
      <p class="muted">${esc(e && e.message || "未知错误")}</p>
      <button class="btn primary" onclick="location.reload()">重试</button></div></div>`;
  }
}

function shell(active, title, content) {
  const tabs = [
    ["home", "学习", "📚"],
    ["review", "复习", "📝"],
    ["history", "历史", "🕘"],
    ["mine", "我的", "👤"],
  ];
  return `
  <header class="topbar"><div class="topbar-title">${esc(title || (S.meta && S.meta.title) || "学习助手")}</div>
    ${S.user ? `<div class="topbar-user">${esc(S.user.display_name || S.user.username)}</div>` : ""}
  </header>
  <main class="main">${content}</main>
  <nav class="tabbar">${tabs.map(([n, label, icon]) =>
    `<a href="#/${n}" class="tab${active === n ? " on" : ""}"><span class="tab-icon">${icon}</span>${label}</a>`
  ).join("")}</nav>`;
}

/* ---------------- 登录 ---------------- */
async function pageLogin(app) {
  const meta = await S.api("/web/meta").catch(() => null);
  S.meta = meta;
  const title = (meta && meta.title) || "学习助手";
  document.title = title;
  const usable = meta && meta.enabled && meta.configured;
  app.innerHTML = `
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-title">${esc(title)}</div>
      <div class="login-sub">一个账号对应一个孩子，学习记录按账号隔离</div>
      ${usable ? `
      <form id="loginForm">
        <label class="field"><span>用户名</span>
          <input name="username" autocomplete="username" required maxlength="32" placeholder="请输入用户名"></label>
        <label class="field"><span>密码</span>
          <input name="password" type="password" autocomplete="current-password" required placeholder="请输入密码"></label>
        <button class="btn primary block" type="submit">登录</button>
      </form>` : `
      <div class="notice">服务端尚未配置网页账号（web.users），请联系管理员先配置好再登录。</div>`}
    </div>
  </div>`;
  const form = $("#loginForm");
  if (form) form.onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const btn = $("button[type=submit]", form);
    btn.disabled = true;
    try {
      const data = new URLSearchParams();
      data.set("username", fd.get("username"));
      data.set("password", fd.get("password"));
      const res = await fetch("/api/web/login", { method: "POST", body: data });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw { status: res.status, message: body.detail || "登录失败" };
      S.saveSession(body.token, { username: body.username, display_name: body.display_name });
      S.meta = await S.api("/web/meta").catch(() => null);
      go("home");
    } catch (e) {
      toast(e.message || "登录失败");
      btn.disabled = false;
    }
  };
}

/* ---------------- 学习：今日学习台 ---------------- */
async function pageHome(app, r, alive) {
  document.title = "今日学习台";
  app.innerHTML = shell("home", "今日学习台", `<div class="page"><div class="loading">加载中…</div></div>`);
  const ov = await S.api("/web/overview");
  if (!alive()) return;
  const todos = ov.todos || {};
  const acc = ov.week_accuracy;
  const accHtml = acc
    ? `<div class="acc-num">${(acc.rate * 100).toFixed(1)}<span class="acc-pct">%</span></div>
       <div class="muted">近 7 天 · 共批改 ${acc.checked} 题
       ${acc.delta === null || acc.delta === undefined ? "（上周无数据）"
         : acc.delta >= 0 ? ` · 较上周 <span class="up">+${(acc.delta * 100).toFixed(1)}%</span>`
         : ` · 较上周 <span class="down">${(acc.delta * 100).toFixed(1)}%</span>`}</div>`
    : `<div class="acc-num muted">暂无数据</div><div class="muted">最近 7 天还没有批改记录</div>`;

  const todoCards = [
    ["pending_correction", "待订正", todos.pending_correction || 0],
    ["corrected_pending_retest", "待复测", todos.pending_retest || 0],
    ["retest_failed", "复测未通过", todos.retest_failed || 0],
  ].map(([st, label, n]) => `
    <a class="todo-card" href="#/review?state=${st}">
      <div class="todo-num">${n}</div><div class="todo-label">${label}</div>
    </a>`).join("");

  const causeHtml = (ov.top_causes || []).length
    ? `<ol class="cause-list">${(ov.top_causes || []).map((c) =>
        `<li><span class="cause-name">${esc(c.cause)}</span><span class="cause-count">${c.count} 次</span></li>`).join("")}</ol>`
    : `<div class="muted">近 30 天暂无错因统计</div>`;

  app.innerHTML = shell("home", "今日学习台", `
  <div class="page">
    <div class="card">
      <div class="card-title">今日待办</div>
      <div class="todo-row">${todoCards}</div>
    </div>
    <div class="entry-row">
      <a class="entry-card" href="#/learn?tab=grading"><div class="entry-icon">📸</div><div>拍作业</div></a>
      <a class="entry-card" href="#/learn?tab=qa"><div class="entry-icon">❓</div><div>问问题</div></a>
      <a class="entry-card" href="#/learn?tab=training"><div class="entry-icon">🎯</div><div>生成训练</div></a>
    </div>
    <div class="card">
      <div class="card-title">近 7 天正确率（全学科）</div>
      ${accHtml}
    </div>
    <div class="card">
      <div class="card-title">近 30 天高频错因 TOP3</div>
      ${causeHtml}
    </div>
  </div>`);
}

/* ---------------- 学习：提交 ---------------- */
const LEARN_TABS = [
  ["grading", "拍作业", "上传作业照片，自动批改并记入错题台账"],
  ["qa", "问问题", "拍下不会的题，讲思路、给提示"],
  ["training", "生成训练", "按知识点或考试范围生成练习题"],
];
const DEFAULT_SUBJECTS = ["数学", "语文", "英语", "物理", "化学", "生物", "历史", "地理", "政治"];
// 学科留空交给模型按材料判断：试卷/题目本身就能推断出学科，不必让用户先选
const AUTO_SUBJECT = "自动识别（按试卷判断）";

async function pageLearn(app, r, alive) {
  document.title = "提交";
  const tab = LEARN_TABS.some(([k]) => k === r.query.tab) ? r.query.tab : "grading";
  app.innerHTML = shell("home", "提交", `<div class="page"><div class="loading">加载中…</div></div>`);
  const settings = await S.api("/settings").catch(() => null);
  if (!alive()) return;
  const subjects = (settings && settings.subjects && settings.subjects.length)
    ? settings.subjects : DEFAULT_SUBJECTS;
  const tabInfo = LEARN_TABS.find(([k]) => k === tab);
  // 材料类任务（拍作业/问问题）默认不指定学科，由模型看试卷推断；训练默认沿用第一个学科
  const subjectOptions = [{ value: "", label: AUTO_SUBJECT },
    ...subjects.map((s) => ({ value: s, label: s }))];
  const defaultSubjectIndex = tab === "training" ? 1 : 0;

  app.innerHTML = shell("home", "提交", `
  <div class="page">
    <div class="seg">${LEARN_TABS.map(([k, label]) =>
      `<a href="#/learn?tab=${k}" class="seg-item${k === tab ? " on" : ""}">${label}</a>`).join("")}</div>
    <div class="card">
      <div class="card-title">${tabInfo[1]}</div>
      <p class="muted">${tabInfo[2]}</p>
      <form id="taskForm">
        <label class="field"><span>学科${tab === "training" ? "" : "（默认按试卷自动识别）"}</span>
          <select name="subject">${subjectOptions.map((o, i) =>
            `<option value="${esc(o.value)}"${i === defaultSubjectIndex ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label>
        ${tab === "training" ? `
        <label class="field"><span>训练类型</span>
          <select name="training_kind">
            <option value="topic">按知识点</option>
            <option value="monthly">月考范围</option>
            <option value="midterm">期中范围</option>
            <option value="final">期末范围</option>
          </select></label>
        <label class="field"><span>考试范围（可选）</span>
          <input name="exam_scope" maxlength="200" placeholder="如：第 3 章 一元二次方程"></label>
        <div class="field-row">
          <label class="field"><span>范围开始</span><input name="scope_start" type="date"></label>
          <label class="field"><span>范围结束</span><input name="scope_end" type="date"></label>
        </div>` : ""}
        <label class="field"><span>文字说明${tab === "grading" ? "（可选）" : ""}</span>
          <textarea name="text" rows="3" placeholder="${tab === "qa" ? "把问题写清楚，比如哪一步卡住了" : tab === "training" ? "想练哪些知识点？越具体越好" : "补充说明（可选）"}"></textarea></label>
        <label class="field"><span>图片（可多选，支持 iPhone 的 HEIC）</span>
          <input name="images" type="file" accept="image/*,.heic,.heif" multiple></label>
        <div id="imgPreview" class="img-preview"></div>
        <div id="submitMsg" class="muted"></div>
        <button class="btn primary block" type="submit" id="submitBtn">${tab === "training" ? "生成训练题" : "提交"}</button>
      </form>
    </div>
  </div>`);

  const fileInput = $("input[name=images]");
  fileInput.onchange = () => {
    renderThumbs($("#imgPreview"), Array.from(fileInput.files || []));
  };

  $("#taskForm").onsubmit = async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const btn = $("#submitBtn");
    const msg = $("#submitMsg");
    const text = form.text.value.trim();
    const files = Array.from(form.images.files || []);
    if (!text && !files.length) { toast("请填写文字说明或上传图片"); return; }
    btn.disabled = true;
    try {
      const assetIds = [];
      for (let i = 0; i < files.length; i++) {
        msg.textContent = `上传图片 ${i + 1}/${files.length}…`;
        const fd = new FormData();
        fd.append("file", files[i]);
        const up = await S.api("/assets", { method: "POST", body: fd });
        assetIds.push(up.asset_id);
      }
      msg.textContent = "创建任务…";
      const payload = {
        task_type: tab,
        subject: form.subject.value,
        text,
        asset_ids: assetIds,
      };
      if (tab === "training") {
        payload.training_kind = form.training_kind.value;
        payload.exam_scope = form.exam_scope.value.trim();
        payload.scope_start = form.scope_start.value;
        payload.scope_end = form.scope_end.value;
      }
      const task = await S.api("/study/tasks", { method: "POST", body: payload });
      toast("已提交，正在处理…");
      go("task", task.id);
    } catch (e) {
      toast(e.message || "提交失败");
      msg.textContent = "";
      btn.disabled = false;
    }
  };
}

/* ---------------- 任务状态页（轮询） ---------------- */
const TERMINAL = ["done", "failed", "waiting_input", "interrupted"];

async function pageTask(app, r, alive) {
  const id = r.param;
  document.title = "处理中";
  app.innerHTML = shell("home", "处理中", `<div class="page"><div class="loading">加载中…</div></div>`);

  const poll = async () => {
    if (!alive() || parseHash().param !== id) return;
    try {
      const task = await S.api(`/tasks/${encodeURIComponent(id)}`);
      if (!alive() || parseHash().param !== id) return;
      if (task.status === "done") {
        if ((task.task_type || "grading") === "training") go("practice", id);
        else go("result", id);
        return;
      }
      if (task.status === "failed" || task.status === "interrupted") {
        renderTaskState(task);
        return;
      }
      renderTaskState(task);
      S.pollTimer = setTimeout(poll, 2000);
    } catch (e) {
      if (e && e.status === 401) throw e;
      app.innerHTML = shell("home", "处理中", `<div class="page"><div class="card">
        <div class="card-title">读取任务失败</div><p class="muted">${esc(e.message || "")}</p>
        <button class="btn primary" onclick="location.reload()">重试</button></div></div>`);
    }
  };

  const renderTaskState = (task) => {
    const needInput = task.status === "waiting_input";
    // 结果已产出（含等待补充材料）：先展示结果，补充材料作为附加项放最下方
    if (needInput && task.result && !task.result_unknown) {
      renderResultView(app, task, {
        title: "批改结果",
        followup: true,
        footer: false,
        onSubmitted: () => poll(),
      });
      return;
    }
    app.innerHTML = shell("home", needInput ? "需要补充材料" : "处理中", `
    <div class="page">
      <div class="card center">
        <div class="spinner"></div>
        <div class="card-title">${needInput ? "需要补充材料" : "正在处理…"}</div>
        <p class="muted">${esc(task.subject || "")} · ${esc(TASK_TYPE_LABEL[task.task_type] || task.task_type || "")}</p>
        ${task.error ? `<p class="error-text">${esc(task.error)}</p>` : ""}
        ${needInput
          ? `<p class="muted small">结果还没有产出，补充材料后会重新处理</p>`
          : `<p class="muted">完成后会自动跳转</p>`}
      </div>
      ${needInput ? followupCardHtml(task, false) : ""}
    </div>`);
    if (needInput) bindFollowupForm(app, id, () => poll());
  };

  await poll();
}

/* ---------------- 批改结果页 ---------------- */
const STATUS_LABEL = {
  correct: "答对", wrong: "做错", uncertain: "存疑",
  unanswered: "未作答", unprocessed: "未处理",
};

function sortQuestionsForResult(questions) {
  const rank = { wrong: 0, uncertain: 1, unanswered: 2, unprocessed: 3, correct: 4 };
  return [...questions].sort((a, b) => (rank[a.status] ?? 5) - (rank[b.status] ?? 5));
}

/* 结果主体：顶部只有统计，下面只列错题与存疑题；答对的题只在统计里体现数量 */
function resultBodyHtml(task, questions, ledgerByUid) {
  const overview = (task.result || {}).overview || {};
  const wrongCount = questions.filter((q) => q.status === "wrong").length;
  const uncertainCount = questions.filter((q) => q.status === "uncertain").length;
  const correctCount = questions.filter((q) => q.status === "correct").length;
  const focusQs = questions.filter((q) => q.status !== "correct");

  const qCard = (q) => {
    const entry = q.uid ? ledgerByUid[q.uid] : null;
    return `
    <div class="card q-card q-${esc(q.status)}" data-qid="${esc(q.id)}">
      <div class="q-head">
        <span class="q-no">${esc(q.no || "")}</span>
        <span class="q-status st-${esc(q.status)}">${esc(STATUS_LABEL[q.status] || q.status)}</span>
      </div>
      ${q.stem ? `<div class="q-stem">${esc(q.stem)}</div>` : ""}
      ${q.student_answer ? `<div class="q-row"><span class="q-label">我的作答</span><div>${esc(q.student_answer)}</div></div>` : ""}
      ${q.correct_answer ? `<div class="q-row"><span class="q-label">正确答案</span><div class="q-correct">${esc(q.correct_answer)}</div></div>` : ""}
      ${q.error_rule ? `<div class="q-row"><span class="q-label">错因</span><div>${esc(q.error_rule)}</div></div>` : ""}
      ${q.knowledge_point ? `<div class="q-row"><span class="q-label">知识点</span><div>${esc(q.knowledge_point)}</div></div>` : ""}
      ${(q.steps || []).length ? `<div class="q-row"><span class="q-label">解析</span><div>${(q.steps || []).map((s) => `<p>${esc(s)}</p>`).join("")}</div></div>` : ""}
      ${q.status === "wrong" || q.status === "uncertain" ? `
      <div class="q-actions">
        ${entry
          ? `<button class="btn ghost small" data-dispute="${entry.id}">我觉得判错了</button>`
          : `<span class="muted small">该题未记入台账</span>`}
      </div>` : ""}
    </div>`;
  };

  const emptyText = questions.length ? "🎉 全部答对，没有错题" : "这次没有需要跟进的错题";

  return `
    <div class="card">
      <div class="card-title">${esc(task.subject || "")} · 批改结果</div>
      <div class="result-stats">
        <span class="stat bad">做错 ${wrongCount}</span>
        <span class="stat warn">存疑 ${uncertainCount}</span>
        <span class="stat good">答对 ${correctCount}</span>
      </div>
      ${overview.summary ? `<p>${esc(overview.summary)}</p>` : ""}
      <div class="muted small">错题与存疑题已记入复习台账，可跨天跟进订正与复测</div>
    </div>
    ${focusQs.length ? `<div class="section-title">错题与存疑题（${focusQs.length}）</div>
      ${focusQs.map(qCard).join("")}` : `<div class="card center"><p>${emptyText}</p></div>`}`;
}

/* 补充材料卡片：结果之后的附加项，只在任务等待补充材料时出现 */
function followupCardHtml(task, hasResult) {
  const missing = ((task.result || {}).missing_info || [])
    .map((m) => (m || "").trim()).filter(Boolean);
  return `
    <div class="card" id="followupCard">
      <div class="card-title">补充材料</div>
      <p class="muted small">${hasResult
        ? "结果已经出来了，但还缺下面这些信息；补充后会自动重新处理。"
        : "这次还没有产出可用结果，缺下面这些信息；补充后会重新处理。"}</p>
      ${missing.length ? `<ul class="missing-list">${missing.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}
      <form id="followupForm">
        <label class="field"><span>补充说明</span><textarea name="text" rows="3" placeholder="补充缺失的信息"></textarea></label>
        <label class="field"><span>补充图片（可多选，支持 iPhone 的 HEIC）</span>
          <input name="images" type="file" accept="image/*,.heic,.heif" multiple></label>
        <div id="followupPreview" class="img-preview"></div>
        <div id="followupMsg" class="muted"></div>
        <button class="btn primary block" type="submit">提交补充材料</button>
      </form>
    </div>`;
}

function bindFollowupForm(app, taskId, onSubmitted) {
  const form = $("#followupForm", app);
  if (!form) return;
  const msg = $("#followupMsg", app);
  const input = form.images;
  const preview = $("#followupPreview", app);
  if (input && preview) {
    input.onchange = () => renderThumbs(preview, Array.from(input.files || []));
  }
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = $("button[type=submit]", form);
    const text = form.text.value.trim();
    const files = Array.from((input && input.files) || []);
    if (!text && !files.length) { toast("请填写补充说明或上传图片"); return; }
    btn.disabled = true;
    try {
      const assetIds = [];
      for (let i = 0; i < files.length; i++) {
        if (msg) msg.textContent = `上传图片 ${i + 1}/${files.length}…`;
        const fd = new FormData();
        fd.append("file", files[i]);
        const up = await S.api("/assets", { method: "POST", body: fd });
        assetIds.push(up.asset_id);
      }
      if (msg) msg.textContent = "提交中…";
      await S.api(`/tasks/${encodeURIComponent(taskId)}/followups`, {
        method: "POST",
        body: { text, asset_ids: assetIds },
      });
      toast("已提交补充材料");
      if (msg) msg.textContent = "";
      if (typeof onSubmitted === "function") onSubmitted();
    } catch (e) {
      toast(e.message || "提交失败");
      if (msg) msg.textContent = "";
      btn.disabled = false;
    }
  };
}

function bindDisputeActions(app) {
  $$("[data-dispute]", app).forEach((btn) => {
    btn.onclick = async () => {
      const entryId = btn.getAttribute("data-dispute");
      const ok = await confirmDialog("标记异议",
        "<p>这条判分你觉得有问题？点确定后会记一笔「异议」事件，方便之后核对。<b>台账状态不会改变</b>，该订正还是要订正。</p>");
      if (!ok) return;
      btn.disabled = true;
      try {
        await S.api(`/ledger/${entryId}/events`, {
          method: "POST",
          body: { result: "disputed", note: "学生认为判分有误" },
        });
        toast("已记录你的异议");
        btn.textContent = "已标记异议";
      } catch (e) {
        toast(e.message || "标记失败");
        btn.disabled = false;
      }
    };
  });
}

/**
 * 渲染一份批改结果：统计 + 错题/存疑题（答对的题不逐条展示）。
 * opts.followup=true 时在结果之后追加补充材料卡片（任务在等补充材料）。
 */
function renderResultView(app, task, opts = {}) {
  const questions = sortQuestionsForResult(((task.result || {}).questions) || []);
  const ledgerByUid = {};
  (task.ledger || []).forEach((e) => { if (e.question_uid) ledgerByUid[e.question_uid] = e; });

  app.innerHTML = shell("home", opts.title || "批改结果", `
  <div class="page">
    ${resultBodyHtml(task, questions, ledgerByUid)}
    ${opts.followup ? followupCardHtml(task, true) : ""}
    ${opts.footer === false ? "" : `<a class="btn ghost block" href="#/history">返回任务历史</a>`}
  </div>`);

  bindDisputeActions(app);
  if (opts.followup) bindFollowupForm(app, task.id, opts.onSubmitted);
}

async function pageResult(app, r, alive) {
  const id = r.param;
  document.title = "批改结果";
  app.innerHTML = shell("home", "批改结果", `<div class="page"><div class="loading">加载中…</div></div>`);
  const task = await S.api(`/tasks/${encodeURIComponent(id)}`);
  if (!alive()) return;
  if (!task.result) {
    // 结果缺失或格式无法识别：如实说明，不假装「全部答对」
    app.innerHTML = shell("home", "批改结果", `<div class="page"><div class="card">
      <div class="card-title">没有可展示的结果</div>
      <p class="muted">${task.result_unknown
        ? "结果格式无法识别，已如实标注，未写入学习记录。"
        : "这次任务还没有产出结果。"}</p>
      ${task.error ? `<p class="error-text">${esc(task.error)}</p>` : ""}
      <a class="btn ghost block" href="#/history">返回任务历史</a></div></div>`);
    return;
  }
  renderResultView(app, task, { title: "批改结果" });
}

/* ---------------- 做题页（一题一屏） ---------------- */
async function pagePractice(app, r, alive) {
  const id = r.param;
  document.title = "做题";
  app.innerHTML = shell("home", "做题", `<div class="page"><div class="loading">加载中…</div></div>`);
  const task = await S.api(`/tasks/${encodeURIComponent(id)}`);
  if (!alive()) return;
  const questions = (task.result && task.result.questions) || [];
  if (!questions.length) {
    app.innerHTML = shell("home", "做题", `<div class="page"><div class="card center">
      <p>这份训练还没有题目</p><a class="btn primary" href="#/learn?tab=training">重新生成</a></div></div>`);
    return;
  }
  let idx = 0;
  const marks = {};   // qid -> "right" | "wrong"
  const recorded = {}; // qid -> entry_id（已记入台账）

  const draw = () => {
    const q = questions[idx];
    const total = questions.length;
    const marked = marks[q.id];
    app.innerHTML = shell("home", "做题", `
    <div class="page practice">
      <div class="practice-progress">
        <span>第 ${idx + 1} / ${total} 题</span>
        <div class="progress-bar"><div class="progress-fill" style="width:${((idx + 1) / total * 100).toFixed(0)}%"></div></div>
      </div>
      <div class="card q-card">
        <div class="q-head"><span class="q-no">${esc(q.no || `第${idx + 1}题`)}</span>
          ${q.knowledge_point ? `<span class="muted small">${esc(q.knowledge_point)}</span>` : ""}</div>
        <div class="q-stem large">${esc(q.stem || "")}</div>
        ${marked ? `<div class="mark-banner ${marked === "right" ? "good" : "bad"}">
          ${marked === "right" ? "✓ 你标记为：做对了" : "✗ 你标记为：做错了" + (recorded[q.id] ? "（已记入复习台账）" : "")}</div>` : ""}
        ${q.correct_answer && marked ? `<div class="q-row"><span class="q-label">参考答案</span><div class="q-correct">${esc(q.correct_answer)}</div></div>` : ""}
        ${(q.steps || []).length && marked ? `<div class="q-row"><span class="q-label">解析</span><div>${q.steps.map((s) => `<p>${esc(s)}</p>`).join("")}</div></div>` : ""}
      </div>
      ${!marked ? `
      <div class="practice-actions">
        <button class="btn good big" id="markRight">做对了 ✓</button>
        <button class="btn danger big" id="markWrong">做错了 ✗</button>
      </div>
      <p class="muted small center">先自己做，做完再判。点「做错了」会自动记入复习台账。</p>` : `
      <div class="practice-actions">
        <button class="btn ghost" id="prevQ" ${idx === 0 ? "disabled" : ""}>← 上一题</button>
        ${idx < total - 1
          ? `<button class="btn primary" id="nextQ">下一题 →</button>`
          : `<a class="btn primary" href="#/review?state=pending_correction">去复习台账 →</a>`}
      </div>
      <div class="center"><button class="btn link small" id="resetMark">重新判定本题</button></div>`}
    </div>`);

    const goIdx = (d) => { idx = Math.min(total - 1, Math.max(0, idx + d)); draw(); };
    const p = $("#prevQ"); if (p) p.onclick = () => goIdx(-1);
    const n = $("#nextQ"); if (n) n.onclick = () => goIdx(1);
    const rm = $("#resetMark");
    if (rm) rm.onclick = () => { delete marks[q.id]; draw(); };

    const mr = $("#markRight");
    if (mr) mr.onclick = () => { marks[q.id] = "right"; draw(); };
    const mw = $("#markWrong");
    if (mw) mw.onclick = async () => {
      mw.disabled = true;
      marks[q.id] = "wrong";
      try {
        const res = await S.api("/ledger/manual", {
          method: "POST",
          body: {
            subject: task.subject || "",
            question_no: q.no || "",
            stem: q.stem || "",
            correct_answer: q.correct_answer || "",
            knowledge_point: q.knowledge_point || "",
            question_uid: q.uid || "",
            source_task_id: id,
            note: "做题页自判做错",
          },
        });
        recorded[q.id] = res.entry_id;
        toast(res.created ? "已记入复习台账" : "台账中已有这道题");
      } catch (e) {
        toast(e.message || "记入台账失败，但判定已保留");
      }
      draw();
    };
  };
  draw();
}

/* ---------------- 复习：跨天未解决台账 ---------------- */
const REVIEW_STATES = [
  ["", "全部"],
  ["pending_correction", "待订正"],
  ["corrected_pending_retest", "待复测"],
  ["retest_failed", "复测未通过"],
  ["retest_passed", "已通过"],
];

async function pageReview(app, r, alive) {
  // 带 param 即为条目详情
  if (r.param) return pageReviewDetail(app, r, alive);
  document.title = "复习台账";
  const state = r.query.state || "";
  const subject = r.query.subject || "";
  app.innerHTML = shell("review", "复习台账", `<div class="page"><div class="loading">加载中…</div></div>`);
  const qs = [];
  if (state) qs.push(`states=${encodeURIComponent(state)}`);
  if (subject) qs.push(`subject=${encodeURIComponent(subject)}`);
  const data = await S.api(`/ledger${qs.length ? "?" + qs.join("&") : ""}`);
  if (!alive()) return;
  const counts = data.counts || {};
  const subjects = data.subjects || [];

  const entryHtml = (e) => `
    <a class="ledger-item" href="#/review/${e.id}">
      <div class="ledger-top">
        <span class="tag">${esc(e.subject || "")}</span>
        <span class="q-status st-${esc(e.status)}">${esc(STATUS_LABEL[e.status] || e.status || "")}</span>
        <span class="ledger-state">${esc(STATE_LABEL[e.remediation_state] || e.remediation_state)}</span>
      </div>
      <div class="ledger-stem">${esc((e.stem || "").slice(0, 80))}</div>
      <div class="ledger-meta muted small">${esc(e.no || "")} · ${fmtD(e.last_event_at)}</div>
    </a>`;

  app.innerHTML = shell("review", "复习台账", `
  <div class="page">
    <div class="card">
      <div class="stat-row">
        <div class="stat-box"><div class="stat-num">${counts.pending_correction || 0}</div><div class="muted small">待订正</div></div>
        <div class="stat-box"><div class="stat-num">${counts.corrected_pending_retest || 0}</div><div class="muted small">待复测</div></div>
        <div class="stat-box"><div class="stat-num">${counts.retest_failed || 0}</div><div class="muted small">复测未通过</div></div>
      </div>
      <div class="muted small">只收录跨天未解决的错题与存疑题，当天刚做的不在这里</div>
    </div>
    <div class="chip-row">${REVIEW_STATES.map(([v, label]) =>
      `<a class="chip${state === v ? " on" : ""}" href="#/review${v || subject ? "?" : ""}${v ? "state=" + v : ""}${v && subject ? "&" : ""}${subject ? "subject=" + encodeURIComponent(subject) : ""}">${label}</a>`).join("")}</div>
    ${subjects.length ? `
    <div class="chip-row">${["", ...subjects].map((s) =>
      `<a class="chip${subject === s ? " on" : ""}" href="#/review${state || s ? "?" : ""}${state ? "state=" + state : ""}${state && s ? "&" : ""}${s ? "subject=" + encodeURIComponent(s) : ""}">${s || "全部学科"}</a>`).join("")}</div>` : ""}
    <div class="ledger-list">
      ${(data.entries || []).length ? data.entries.map(entryHtml).join("") :
        `<div class="card center"><p>🎉 这个筛选下没有待处理的错题</p></div>`}
    </div>
  </div>`);
}

async function pageReviewDetail(app, r, alive) {
  const entryId = r.param;
  document.title = "错题详情";
  app.innerHTML = shell("review", "错题详情", `<div class="page"><div class="loading">加载中…</div></div>`);
  const data = await S.api(`/ledger/${encodeURIComponent(entryId)}`);
  if (!alive()) return;
  const e = data.entry;
  const events = data.events || [];

  const eventLabel = { corrected: "订正", retest_passed: "复测通过", retest_failed: "复测未通过", disputed: "异议" };

  app.innerHTML = shell("review", "错题详情", `
  <div class="page">
    <div class="card">
      <div class="q-head">
        <span class="tag">${esc(e.subject || "")}</span>
        <span class="ledger-state">${esc(STATE_LABEL[e.remediation_state] || e.remediation_state)}</span>
      </div>
      ${e.stem ? `<div class="q-stem">${esc(e.stem)}</div>` : ""}
      ${e.student_answer ? `<div class="q-row"><span class="q-label">我的作答</span><div>${esc(e.student_answer)}</div></div>` : ""}
      ${e.correct_answer ? `<div class="q-row"><span class="q-label">正确答案</span><div class="q-correct">${esc(e.correct_answer)}</div></div>` : ""}
      ${e.error_rule ? `<div class="q-row"><span class="q-label">错因</span><div>${esc(e.error_rule)}</div></div>` : ""}
      ${e.knowledge_point ? `<div class="q-row"><span class="q-label">知识点</span><div>${esc(e.knowledge_point)}</div></div>` : ""}
      ${e.note ? `<div class="q-row"><span class="q-label">备注</span><div>${esc(e.note)}</div></div>` : ""}
    </div>
    <div class="card">
      <div class="card-title">跟进动作</div>
      <div class="action-row">
        <button class="btn primary small" data-event="corrected">记为已订正</button>
        <button class="btn good small" data-event="retest_passed">复测通过</button>
        <button class="btn danger small" data-event="retest_failed">复测没通过</button>
      </div>
      <p class="muted small">点之前请确认是真实作答后的结果，不要凭印象登记。</p>
    </div>
    <div class="card">
      <div class="card-title">历史事件（${events.length}）</div>
      ${events.length ? `<ul class="event-list">${events.map((ev) => `
        <li><span class="event-result">${esc(eventLabel[ev.result] || ev.result)}</span>
        <span class="muted small">${esc(ev.occurred_date || fmtD(ev.created_at))}</span>
        ${ev.note ? `<div class="muted small">${esc(ev.note)}</div>` : ""}</li>`).join("")}</ul>`
        : `<div class="muted">还没有订正/复测记录</div>`}
    </div>
    <a class="btn ghost block" href="#/review">返回台账</a>
  </div>`);

  $$("[data-event]", app).forEach((btn) => {
    btn.onclick = async () => {
      const result = btn.getAttribute("data-event");
      btn.disabled = true;
      try {
        await S.api(`/ledger/${encodeURIComponent(entryId)}/events`, {
          method: "POST", body: { result },
        });
        toast("已登记");
        render();
      } catch (e2) {
        toast(e2.message || "登记失败");
        btn.disabled = false;
      }
    };
  });
}

/* ---------------- 历史：任务历史 ---------------- */
async function pageHistory(app, r, alive) {
  document.title = "任务历史";
  app.innerHTML = shell("history", "任务历史", `<div class="page"><div class="loading">加载中…</div></div>`);
  const tasks = await S.api("/tasks?limit=50");
  if (!alive()) return;
  const statusLabel = { done: "已完成", failed: "失败", waiting_input: "待补充", interrupted: "已中断" };

  app.innerHTML = shell("history", "任务历史", `
  <div class="page">
    ${(tasks || []).length ? `<div class="ledger-list">${tasks.map((t) => {
      const done = t.status === "done";
      const link = done
        ? `#/${t.task_type === "training" ? "practice" : "result"}/${t.id}`
        : `#/task/${t.id}`;
      return `
      <a class="ledger-item" href="${link}">
        <div class="ledger-top">
          <span class="tag">${esc(t.subject || "")}</span>
          <span class="tag">${esc(TASK_TYPE_LABEL[t.task_type] || t.task_type || "")}</span>
          <span class="ledger-state">${esc(statusLabel[t.status] || t.status)}</span>
        </div>
        ${t.summary ? `<div class="ledger-stem">${esc(t.summary.slice(0, 80))}</div>` : ""}
        <div class="ledger-meta muted small">${fmtDT(t.created_at)}${t.missing_info_count ? ` · 缺${t.missing_info_count}项信息` : ""}</div>
      </a>`;
    }).join("")}</div>` : `<div class="card center"><p>还没有任务，去「学习」提交第一份作业吧</p>
      <a class="btn primary" href="#/learn">去提交</a></div>`}
  </div>`);
}

/* ---------------- 我的：账号与设置 ---------------- */
async function pageMine(app, r, alive) {
  document.title = "我的";
  app.innerHTML = shell("mine", "我的", `<div class="page"><div class="loading">加载中…</div></div>`);
  const [settings, ov] = await Promise.all([
    S.api("/settings").catch(() => null),
    S.api("/web/overview").catch(() => null),
  ]);
  if (!alive()) return;
  const user = S.user || {};
  const retention = (ov && ov.retention_days) || 730;

  app.innerHTML = shell("mine", "我的", `
  <div class="page">
    <div class="card">
      <div class="card-title">账号</div>
      <div class="q-row"><span class="q-label">孩子</span><div>${esc(user.display_name || user.username || "")}</div></div>
      <div class="q-row"><span class="q-label">用户名</span><div>${esc(user.username || "")}</div></div>
      <div class="q-row"><span class="q-label">数据保留</span><div>错题台账保留 ${Math.round(retention / 365 * 10) / 10} 年，超期自动清理</div></div>
    </div>
    <div class="card">
      <div class="card-title">学习设置</div>
      <form id="settingsForm">
        <label class="field"><span>学科（用空格或逗号分隔）</span>
          <input name="subjects" value="${esc((settings && settings.subjects || []).join(" "))}" placeholder="数学 语文 英语"></label>
        <label class="field"><span>学期起始日期（可选）</span>
          <input name="term_start_date" type="date" value="${esc((settings && settings.term_start_date) || "")}"></label>
        <button class="btn primary block" type="submit">保存设置</button>
      </form>
    </div>
    <div class="card">
      <div class="card-title">关于</div>
      <p class="muted small">学习助手网页版 v1 · 内测中<br>一个账号对应一个孩子，数据按账号隔离保存。</p>
      <button class="btn danger block" id="logoutBtn">退出登录</button>
    </div>
  </div>`);

  $("#settingsForm").onsubmit = async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const btn = $("button[type=submit]", form);
    btn.disabled = true;
    try {
      const subjects = form.subjects.value.split(/[\s,，、]+/).map((s) => s.trim()).filter(Boolean);
      await S.api("/settings", {
        method: "PUT",
        body: { subjects, term_start_date: form.term_start_date.value },
      });
      toast("已保存");
    } catch (e) {
      toast(e.message || "保存失败");
    }
    btn.disabled = false;
  };

  $("#logoutBtn").onclick = async () => {
    const ok = await confirmDialog("退出登录", "<p>确定要退出当前账号吗？</p>");
    if (!ok) return;
    S.logout();
    go("login");
  };
}

/* ---------------- 启动 ---------------- */
window.addEventListener("hashchange", render);
document.addEventListener("DOMContentLoaded", () => {
  if (!location.hash) location.hash = "#/home";
  render();
});
