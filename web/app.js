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
// 与 config.yaml 的 limits.max_assets_per_task 默认值一致；服务端仍会按真实配置拒绝超额
const MAX_PICK_IMAGES = 20;

/**
 * 图片选择：多次拍照 / 多次选择逐张累加，可单张移除。
 *
 * 原生 input.files 只读、且每次选择都会整体替换（手机浏览器点「拍照」一次只产出一张），
 * 只按 input.files 渲染会导致「永远只有一张」。因此以内部数组为准，input 只当触发入口：
 * - 追加去重（同名同大小同修改时间视为同一张），重复选择只提示不重复添加；
 * - 超过上限的多余文件丢弃并提示；
 * - HEIC 无法在浏览器里预览，退回文字占位（真正的解码在服务端完成）；
 * - 每次重渲染前回收上一轮的 objectURL，避免连续拍照累积内存。
 * 返回 { files(), clear(), count() }：files() 是当前全部已选图片，供提交时逐张上传。
 */
function createImagePicker(input, listBox, hintBox, opts = {}) {
  const max = opts.max || MAX_PICK_IMAGES;
  const picked = [];
  let urls = [];
  const keyOf = (f) => `${f.name || ""}|${f.size || 0}|${f.lastModified || 0}`;

  function render() {
    urls.forEach((u) => URL.revokeObjectURL(u));
    urls = [];
    listBox.innerHTML = "";
    picked.forEach((file, index) => {
      const item = document.createElement("div");
      item.className = "img-preview-item";
      if (HEIC_RE.test(file.name || "") || /heic|heif/i.test(file.type || "")) {
        const ph = document.createElement("div");
        ph.className = "img-preview-fallback";
        ph.textContent = "HEIC\n提交后自动转换";
        item.appendChild(ph);
      } else {
        const url = URL.createObjectURL(file);
        urls.push(url);
        const img = document.createElement("img");
        img.src = url;
        img.onerror = () => {
          const ph = document.createElement("div");
          ph.className = "img-preview-fallback";
          ph.textContent = "预览不可用\n提交后自动转换";
          img.replaceWith(ph);
        };
        item.appendChild(img);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "img-preview-remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `移除第 ${index + 1} 张`);
      remove.onclick = () => {
        picked.splice(index, 1);
        render();
        if (typeof opts.onChange === "function") opts.onChange(picked.slice());
      };
      item.appendChild(remove);
      listBox.appendChild(item);
    });
    if (hintBox) {
      hintBox.textContent = picked.length
        ? `已选 ${picked.length} / ${max} 张（可继续拍照添加）` : "";
    }
  }

  if (input) {
    input.onchange = () => {
      const incoming = Array.from(input.files || []);
      input.value = "";   // 不重置的话，再选同一张文件不会再触发 change
      if (!incoming.length) return;
      const seen = new Set(picked.map(keyOf));
      let added = 0;
      let duplicated = 0;
      let overflow = 0;
      for (const file of incoming) {
        const key = keyOf(file);
        if (seen.has(key)) { duplicated += 1; continue; }
        if (picked.length >= max) { overflow += 1; continue; }
        seen.add(key);
        picked.push(file);
        added += 1;
      }
      if (added) render();
      if (overflow) toast(`最多 ${max} 张，已忽略 ${overflow} 张`);
      else if (duplicated) toast("这张已经加过了");
      if (typeof opts.onChange === "function") opts.onChange(picked.slice());
    };
  }

  return {
    files: () => picked.slice(),
    count: () => picked.length,
    clear: () => { picked.length = 0; render(); },
  };
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
  assetVersion: "",   // 页面加载时的前端版本基线，用于探测"服务端已换新前端"

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

/* ---------------- 前端版本探测 ---------------- */
/**
 * 单页应用里点底部 tab 只改 hash，不会重新下载 app.js：标签页只要不整页刷新，
 * 浏览器就一直跑加载时那份脚本 —— 部署了新前端也进不来，用户会觉得"改了没用"。
 * 这里在页面加载时记住服务端当时的前端版本作基线，之后每分钟、以及每次切回前台
 * 时再比一次，发现不一致就在底部提示。
 *
 * 只提示、不自动 location.reload()：用户可能正在填写补充说明或已选好图片，
 * 自动刷新会把输入冲掉（见「waiting_input 轮询重建页面」那次教训）。
 */
let versionProbeBusy = false;

function showUpdateBar() {
  const bar = $("#updateBar");
  if (!bar || !bar.hidden) return;
  bar.hidden = false;
}

async function probeVersion() {
  if (versionProbeBusy || !S.assetVersion) return;
  const bar = $("#updateBar");
  if (bar && !bar.hidden) return;   // 已经提示过了，不必再请求
  versionProbeBusy = true;
  try {
    const meta = await S.api("/web/meta");
    if (meta && meta.asset_version && meta.asset_version !== S.assetVersion) {
      showUpdateBar();
    }
  } catch (_) {
    /* 离线、服务未起、登录态变化都直接忽略：这只是提示，不能影响主流程 */
  } finally {
    versionProbeBusy = false;
  }
}

/* 基线只在第一次拿到时固定；后续探测到的新版本不覆盖，否则永远比不出差异 */
function rememberAssetVersion(meta) {
  if (!S.assetVersion && meta && meta.asset_version) S.assetVersion = meta.asset_version;
}

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
    rememberAssetVersion(S.meta);

    const pages = {
      login: pageLogin, home: pageHome, learn: pageLearn, task: pageTask,
      result: pageResult, practice: pagePractice, review: pageReview,
      history: pageHistory, mine: pageMine, weekly: pageWeekly,
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
  rememberAssetVersion(meta);
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
        <label class="field"><span>图片（可多次拍照逐张添加，最多 20 张；按页码顺序添加，跨页题请拍全两页；支持 iPhone 的 HEIC）</span>
          <input name="images" type="file" accept="image/*,.heic,.heif" multiple></label>
        <div id="imgPreview" class="img-preview"></div>
        <div id="imgHint" class="img-hint"></div>
        <div id="submitMsg" class="muted"></div>
        <button class="btn primary block" type="submit" id="submitBtn">${tab === "training" ? "生成训练题" : "提交"}</button>
      </form>
    </div>
  </div>`);

  // 图片选择：可多次拍照逐张累加（手机端「拍照」一次只产出一张）
  const picker = createImagePicker($("input[name=images]"), $("#imgPreview"), $("#imgHint"));

  $("#taskForm").onsubmit = async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const btn = $("#submitBtn");
    const msg = $("#submitMsg");
    const text = form.text.value.trim();
    const files = picker.files();
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
      // 后端返回的是 task_id，取错字段会让跳转丢掉任务号（页面永远停在「正在处理…」）
      go("task", task.task_id || task.id || "");
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
      // waiting_input 同样是终态：等用户补材料时不要继续轮询——每 2 秒重建一次页面
      // 会把用户正在填写的补充说明与已选图片冲掉。补交后由提交回调重新 poll。
      if (TERMINAL.includes(task.status)) {
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
    if (task.orientation) {
      renderOrientation(app, task, () => poll(), alive);
      return;
    }
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
async function renderOrientation(app, task, onConfirmed, alive) {
  const run = task.runs[task.runs.length - 1];
  const pages = task.orientation.pages.filter(p => !p.confirmed);
  const rotations = new Map(pages.map(p => [p.page, 0]));
  app.innerHTML = shell("home", "确认页面方向", `<div class="page">
    <div class="card"><div class="card-title">确认页面方向</div>
      <p>图片已保留。请旋转到文字朝上，再继续批改。</p></div>
    ${pages.map(p => `<div class="card" data-orientation-page="${p.page}">
      <div class="card-title">第 ${p.page} 页</div>
      <div class="orientation-preview"><img alt="第 ${p.page} 页试卷" hidden></div>
      <p class="muted small" data-preview-status>正在读取预览…</p>
      <button type="button" class="btn ghost" data-rotate-page="${p.page}" disabled>顺时针旋转 90°</button>
    </div>`).join("")}
    <div class="card"><p class="error-text" id="orientationError"></p>
      <button class="btn primary block" id="confirmOrientation" disabled>文字方向正确，继续批改</button>
      <a class="btn ghost block" href="#/history">稍后再确认</a></div></div>`);
  let loaded = 0;
  await Promise.all(pages.map(async p => {
    const card = app.querySelector(`[data-orientation-page="${p.page}"]`);
    try {
      const data = await S.api(`/tasks/${encodeURIComponent(task.id)}/orientation/${p.page}?run_id=${encodeURIComponent(run.id)}`);
      if (!alive()) return;
      const img = card.querySelector("img");
      img.src = data.preview;
      await img.decode();
      if (!alive()) return;
      img.hidden = false;
      card.querySelector("[data-preview-status]").textContent = "请核对标题和题干的阅读方向";
      const button = card.querySelector("button");
      button.disabled = false;
      button.onclick = () => {
        const angle = (rotations.get(p.page) + 90) % 360;
        rotations.set(p.page, angle);
        img.style.transform = `rotate(${angle}deg)`;
      };
      loaded++;
    } catch (e) {
      card.querySelector("[data-preview-status]").textContent = "预览读取失败，请刷新页面重试";
    }
  }));
  if (!alive()) return;
  const submit = app.querySelector("#confirmOrientation");
  submit.disabled = loaded !== pages.length;
  submit.onclick = async () => {
    submit.disabled = true;
    try {
      await S.api(`/tasks/${encodeURIComponent(task.id)}/orientation`, {method: "POST", body: {
        run_id: run.id, rotations: pages.map(p => ({page: p.page, rotation: rotations.get(p.page)})),
      }});
      if (alive()) onConfirmed();
    } catch (e) {
      app.querySelector("#orientationError").textContent = e.message || "确认失败，请刷新后重试";
      submit.disabled = false;
    }
  };
}

const STATUS_LABEL = {
  correct: "答对", wrong: "做错", uncertain: "存疑",
  unanswered: "未作答", unprocessed: "未处理",
};

/* 二次复查（第二模型）文案：状态、逐题结论与模型身份核验结果 */
const REVIEW_SUMMARY_LABEL = {
  completed: "已完成", partial: "部分完成", failed: "未完成",
  not_run: "未执行", not_required: "无需复查",
};
const REVIEW_STATE_LABEL = {
  agreed: "未发现异议", disagreed: "有异议", unverified: "无法核查",
  unprocessed: "未完成核查", not_applicable: "未送复查",
};
const REVIEW_IDENTITY_LABEL = {
  confirmed: "身份已确认", model_only: "模型已核对（网关未报告 provider）",
  mismatch: "路由不符", unknown: "身份未确认",
};

function reviewSummaryHtml(task) {
  const summary = ((task.result || {}).review_summary) || {};
  if (!summary.state || summary.state === "not_required") return "";
  const stateLabel = REVIEW_SUMMARY_LABEL[summary.state] || summary.state;
  const counts = [];
  if (summary.target_count) counts.push(`应复查 ${summary.target_count} 题`);
  if (summary.scope) counts.push(`送审 ${summary.scope} 题`);
  if (summary.disagreed) counts.push(`有异议 ${summary.disagreed} 题`);
  if (summary.unverified) counts.push(`无法核查 ${summary.unverified} 题`);
  if (summary.unprocessed) counts.push(`未送审 ${summary.unprocessed} 题`);
  const modelLine = [];
  if (summary.model_requested) modelLine.push(`请求 ${summary.model_requested}`);
  if (summary.model_reported) modelLine.push(`实际 ${summary.model_reported}`);
  if (summary.model_requested && summary.model_identity)
    modelLine.push(REVIEW_IDENTITY_LABEL[summary.model_identity] || summary.model_identity);
  const tone = summary.state === "completed" ? "good" : (summary.state === "partial" ? "warn" : "bad");
  return `
    <div class="card">
      <div class="card-title">二次复查（第二模型）</div>
      <div class="result-stats"><span class="stat ${tone}">${esc(stateLabel)}</span></div>
      ${counts.length ? `<p>${esc(counts.join(" · "))}</p>` : ""}
      ${modelLine.length ? `<div class="muted small">复查模型：${esc(modelLine.join("，"))}（复查只提异议，不改判）</div>` : ""}
      ${summary.note ? `<div class="muted small">${esc(summary.note)}</div>` : ""}
    </div>`;
}

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

  const reviewRow = (q) => {
    const r = q.review || {};
    if (!r.state || r.state === "not_applicable") return "";
    const label = REVIEW_STATE_LABEL[r.state] || r.state;
    const detail = [r.basis, r.note].filter(Boolean).join("；");
    const tone = r.state === "agreed" ? "" : (r.state === "disagreed" ? "error-text" : "muted");
    // 转写二次确认不符时明确标出（原图重读作答与转写不一致）
    const transcript = r.transcript_ok === false
      ? `；转写二次确认：与转写不符（原图重读作答「${r.reread_answer || "无法辨认"}」）` : "";
    return `<div class="q-row"><span class="q-label">二次复查</span><div class="${tone}">${esc(label)}${detail ? `：${esc(detail)}` : ""}${esc(transcript)}</div></div>`;
  };

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
      ${q.final_decision_basis && (q.review || {}).state === "disagreed" ? `<div class="q-row"><span class="q-label">判定说明</span><div class="muted">${esc(q.final_decision_basis)}</div></div>` : ""}
      ${reviewRow(q)}
      ${q.status === "wrong" || q.status === "uncertain" ? `
      <div class="q-actions">
        ${entry && entry.remediation_state === "withdrawn"
          ? `<span class="muted small">已从台账撤回（你标记了异议）</span>`
          : entry
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
    ${reviewSummaryHtml(task)}
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
        <label class="field"><span>补充图片（可多次拍照逐张添加，最多 20 张；按页码顺序添加，跨页题请拍全两页；支持 iPhone 的 HEIC）</span>
          <input name="images" type="file" accept="image/*,.heic,.heif" multiple></label>
        <div id="followupPreview" class="img-preview"></div>
        <div id="followupHint" class="img-hint"></div>
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
  const picker = createImagePicker(input, preview, $("#followupHint", app));
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = $("button[type=submit]", form);
    const text = form.text.value.trim();
    const files = picker.files();
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
        "<p>这条判分你觉得有问题？点确定后会记一笔「异议」事件，<b>并将该题从复习台账中撤回</b>（不再计入待订正/待复测）。</p>");
      if (!ok) return;
      btn.disabled = true;
      try {
        await S.api(`/ledger/${entryId}/events`, {
          method: "POST",
          body: { result: "disputed", note: "学生认为判分有误" },
        });
        toast("已记录异议，该题已从台账撤回");
        btn.textContent = "已从台账撤回";
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
  if (task.orientation) { go("task", id); return; }
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
  // 已完成但结果还缺信息（如题干缺失/字迹存疑/答案归属存疑）：
  // 在结果下方给出补充材料入口，后端 add_followup 本来就允许 done 任务追加。
  const missing = ((task.result || {}).missing_info || []).filter((m) => (m || "").trim());
  renderResultView(app, task, { title: "批改结果", followup: missing.length > 0 });
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
      ${e.task_id ? `<div class="q-row"><span class="q-label">来源任务</span><div><a href="#/task/${esc(e.task_id)}">查看任务与归档</a></div></div>` : ""}
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
      const deletable = t.status === "failed" || t.status === "interrupted" || t.orientation_pending;
      const stateText = t.orientation_pending ? "待确认方向" : (statusLabel[t.status] || t.status);
      const delHint = t.orientation_pending ? "确定放弃这次方向确认吗？图片会一起删除。" : "确定删除这条失败记录吗？相关图片也会一起删除。";
      return `
      <div class="ledger-item">
        <a href="${link}" style="text-decoration:none;color:inherit;display:block">
          <div class="ledger-top">
            <span class="tag">${esc(t.subject || "")}</span>
            <span class="tag">${esc(TASK_TYPE_LABEL[t.task_type] || t.task_type || "")}</span>
            <span class="ledger-state">${esc(stateText)}</span>
          </div>
          ${t.summary ? `<div class="ledger-stem">${esc(t.summary.slice(0, 80))}</div>` : ""}
          <div class="ledger-meta muted small">${fmtDT(t.created_at)}${t.missing_info_count ? ` · 缺${t.missing_info_count}项信息` : ""}</div>
        </a>
        ${deletable ? `<div style="margin-top:8px;text-align:right"><button class="btn ghost small del-task" data-id="${esc(t.id)}" data-hint="${esc(delHint)}">删除</button></div>` : ""}
      </div>`;
    }).join("")}</div>` : `<div class="card center"><p>还没有任务，去「学习」提交第一份作业吧</p>
      <a class="btn primary" href="#/learn">去提交</a></div>`}
  </div>`);
  app.querySelectorAll(".del-task").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(btn.dataset.hint || "确定删除吗？")) return;
      btn.disabled = true;
      try {
        await S.api(`/tasks/${encodeURIComponent(btn.dataset.id)}`, { method: "DELETE" });
        btn.closest(".ledger-item").remove();
      } catch (e) {
        alert("删除失败：" + (e && e.message || "未知错误"));
        btn.disabled = false;
      }
    });
  });
}

/* ---------------- 我的：账号与设置 ---------------- */
/* ---------------- 周总结 ---------------- */
function _fmtRate(v) {
  return v == null ? "—" : (Math.round(v * 1000) / 10) + "%";
}
function _rateDelta(cur, prev) {
  if (cur == null || prev == null) return `<span class="muted small">暂无上周对比</span>`;
  const d = Math.round((cur - prev) * 1000) / 10;
  if (Math.abs(d) < 0.05) return `<span class="muted small">与上周持平</span>`;
  const up = d > 0;
  return `<span class="small" style="color:${up ? "#1a7f37" : "#cf1322"};font-weight:700">${up ? "↑" : "↓"} ${Math.abs(d)}%</span>`;
}
function _weekLabel(weekStart) {
  // "2026-09-21" -> "9.21–9.27"
  const d = new Date(weekStart + "T00:00:00");
  if (isNaN(d)) return weekStart;
  const e = new Date(d.getTime() + 6 * 86400000);
  return `${d.getMonth() + 1}.${d.getDate()}–${e.getMonth() + 1}.${e.getDate()}`;
}

function _aiAnalysisBlock(s) {
  const ai = s.ai_analysis;
  if (!ai) return "";
  if (ai.error) {
    return `<p class="muted small">🤖 AI 归类分析暂不可用（${esc(ai.error).slice(0, 60)}）</p>`;
  }
  const cats = (ai.categories || []).map((c) => `
    <div style="margin:6px 0;padding:8px 10px;background:#f6f8ff;border-radius:8px">
      <div style="font-weight:700;font-size:14px">${esc(c.name)}
        <span class="muted small">第${(c.question_nos || []).map((n) => esc(n)).join("、")}题</span></div>
      ${c.pattern ? `<div class="small" style="margin-top:2px">共性：${esc(c.pattern)}</div>` : ""}
      ${c.advice ? `<div class="small" style="margin-top:2px;color:#1a7f37">建议：${esc(c.advice)}</div>` : ""}
    </div>`).join("");
  const focus = (ai.focus_next_week || []).map((f) => `<li>${esc(f)}</li>`).join("");
  if (!cats && !ai.knowledge_summary && !focus) return "";
  return `
    <div class="q-label" style="margin:8px 0 4px">🤖 AI 归类分析
      <span class="muted small">${esc(ai.model || "")} · 分析了 ${ai.analyzed_mistakes || 0} 道错题</span></div>
    ${ai.knowledge_summary ? `<p style="font-size:14px;line-height:1.6;margin:4px 0">${esc(ai.knowledge_summary)}</p>` : ""}
    ${cats}
    ${focus ? `<div class="q-label" style="margin:8px 0 4px">下周重点</div><ul class="event-list">${focus}</ul>` : ""}`;
}

async function pageWeekly(app, r, alive) {
  document.title = "周总结";
  app.innerHTML = shell("mine", "周总结", `<div class="page"><div class="loading">加载中…</div></div>`);
  const weeksResp = await S.api("/web/weekly-summaries/weeks").catch(() => null);
  const weeks = (weeksResp && weeksResp.weeks) || [];
  if (!weeks.length) {
    app.innerHTML = shell("mine", "周总结", `<div class="page"><div class="card">
      <div class="card-title">周总结</div>
      <p class="muted">还没有生成的周总结。每周日凌晨会自动生成上一周的学习总结，<br>也可以点下方按钮现在生成一次试试。</p>
      <button class="btn primary block" id="genWeeklyBtn">立即生成上周总结</button>
    </div></div>`);
    $("#genWeeklyBtn").onclick = async () => {
      const btn = $("#genWeeklyBtn");
      btn.disabled = true;
      try {
        await S.api("/web/weekly-summaries/generate", { method: "POST", body: {} });
        toast("已生成");
        render();
      } catch (e) { toast(e.message || "生成失败"); btn.disabled = false; }
    };
    return;
  }
  const sel = (r.query && r.query.week) || weeks[0];
  const data = await S.api("/web/weekly-summaries?week=" + encodeURIComponent(sel)).catch(() => null);
  if (!alive()) return;
  const rows = (data && data.subjects) || [];

  // 全科汇总（页面内聚合）
  let tTasks = 0, tCorrect = 0, tChecked = 0, tMistakes = 0, tCorr = 0, tRetest = 0;
  let accSum = 0, accN = 0, prevSum = 0, prevN = 0;
  rows.forEach(({ summary: s }) => {
    tTasks += s.tasks || 0;
    tCorrect += (s.questions && s.questions.correct) || 0;
    tChecked += (s.questions && (s.questions.correct + s.questions.wrong + s.questions.unanswered)) || 0;
    tMistakes += s.new_mistakes || 0;
    tCorr += s.corrections || 0;
    tRetest += s.retests || 0;
    if (s.accuracy != null) { accSum += s.accuracy; accN++; }
    if (s.prev_accuracy != null) { prevSum += s.prev_accuracy; prevN++; }
  });
  const accAll = accN ? accSum / accN : null;
  const prevAll = prevN ? prevSum / prevN : null;

  const chips = weeks.map((w) =>
    `<a class="chip${w === sel ? " on" : ""}" href="#/weekly?week=${w}">${_weekLabel(w)}</a>`).join("");

  const subjectCards = rows.map(({ subject, summary: s }) => {
    const q = s.questions || {};
    const causes = (s.top_causes || []).map((c) =>
      `<li>${esc(c.cause)} <span class="muted">×${c.count}</span></li>`).join("");
    const points = (s.top_points || []).map((p) =>
      `<li>${esc(p.point)} <span class="muted">×${p.count}</span></li>`).join("");
    return `<div class="card">
      <div class="card-title">${esc(subject)}</div>
      <div class="stat-row">
        <div class="stat-box"><div class="stat-num">${s.tasks || 0}</div><div class="muted small">批改任务</div></div>
        <div class="stat-box"><div class="stat-num">${(q.correct || 0) + (q.wrong || 0) + (q.unanswered || 0)}</div><div class="muted small">批改题目</div></div>
        <div class="stat-box"><div class="stat-num">${_fmtRate(s.accuracy)}</div><div class="muted small">正确率</div></div>
      </div>
      <div class="q-row"><span class="q-label">环比上周</span><div>${_rateDelta(s.accuracy, s.prev_accuracy)}</div></div>
      <div class="q-row"><span class="q-label">新增错题</span><div>${s.new_mistakes || 0} 题</div></div>
      <div class="q-row"><span class="q-label">订正 / 复测</span><div>${s.corrections || 0} / ${s.retests || 0}</div></div>
      <div class="q-row"><span class="q-label">待办</span><div>待订正 ${s.pending_correction || 0} · 待复测 ${s.pending_retest || 0}</div></div>
      ${_aiAnalysisBlock(s)}
      ${causes ? `<div class="q-label" style="margin:8px 0 4px">高频错因</div><ul class="event-list">${causes}</ul>` : ""}
      ${points ? `<div class="q-label" style="margin:8px 0 4px">薄弱知识点</div><ul class="event-list">${points}</ul>` : ""}
      ${(!causes && !points) ? `<p class="muted small">本周没有新增错题，继续保持 👍</p>` : ""}
    </div>`;
  }).join("");

  app.innerHTML = shell("mine", "周总结", `<div class="page">
    <div class="card">
      <div class="card-title">第 ${esc(_weekLabel(sel))} 周 <span class="muted small">${esc(data.week_start || "")} ~ ${esc(data.week_end || "")}</span></div>
      <div class="chip-row" style="margin-bottom:10px">${chips}</div>
      <div class="stat-row">
        <div class="stat-box"><div class="stat-num">${tTasks}</div><div class="muted small">批改任务</div></div>
        <div class="stat-box"><div class="stat-num">${tChecked}</div><div class="muted small">批改题目</div></div>
        <div class="stat-box"><div class="stat-num">${_fmtRate(accAll)}</div><div class="muted small">平均正确率</div></div>
      </div>
      <div class="q-row"><span class="q-label">环比上周</span><div>${_rateDelta(accAll, prevAll)}</div></div>
      <div class="q-row"><span class="q-label">新增错题</span><div>${tMistakes} 题</div></div>
      <div class="q-row"><span class="q-label">订正 / 复测</span><div>${tCorr} / ${tRetest}</div></div>
    </div>
    ${subjectCards || `<div class="card"><p class="muted">这周没有可总结的数据。</p></div>`}
    <div class="card"><button class="btn block" id="regenWeeklyBtn">重新生成本周总结</button>
      <p class="muted small" style="margin-top:8px">每周日凌晨自动生成上一周总结；数据有变化时可手动重新生成。</p></div>
  </div>`);

  $("#regenWeeklyBtn").onclick = async () => {
    const btn = $("#regenWeeklyBtn");
    btn.disabled = true;
    try {
      await S.api("/web/weekly-summaries/generate", { method: "POST", body: { week_start: sel } });
      toast("已重新生成");
      render();
    } catch (e) { toast(e.message || "生成失败"); btn.disabled = false; }
  };
}

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
      <div class="card-title">学习总结</div>
      <a href="#/weekly" class="ledger-item" style="margin:0">
        <div class="ledger-top"><span style="font-size:15px;font-weight:700">📊 周总结</span>
          <span class="ledger-state">查看 &gt;</span></div>
        <div class="ledger-meta muted small">每周日凌晨自动生成，按科目汇总上周的学习情况</div>
      </a>
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
  const reloadBtn = $("#updateReload");
  if (reloadBtn) reloadBtn.onclick = () => location.reload();
  // 常驻标签页：定时 + 切回前台时各查一次，发现服务端换了新前端就提示刷新
  setInterval(probeVersion, 60 * 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) probeVersion();
  });
  if (!location.hash) location.hash = "#/home";
  render();
});
