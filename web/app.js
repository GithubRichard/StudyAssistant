'use strict';

/* ============================================================
 * Leo 学习助手 · 网页版
 * 与小程序共用同一套后端接口，仅把「微信登录」换成「密码登录」。
 * 需要通过 IP 直连访问时，浏览器打开 http://<服务器IP>:<端口>/ 即可。
 * ============================================================ */

const TOKEN_KEY = 'sa_web_token';
const OPENID_KEY = 'sa_web_openid';

const TASK_TYPES = [
  { key: 'grading', label: '作业批改', hint: '拍作业照，逐题批改并给出错因' },
  { key: 'qa', label: '学习问答', hint: '问概念或解题方法，可附题目照片' },
  { key: 'weekly_report', label: '周报分析', hint: '按日期区间汇总学习情况' },
  { key: 'training', label: '针对性训练', hint: '按历史错题出题（月考/期中/期末）' },
  { key: 'retest', label: '复测', hint: '记录实际作答结果' },
];
const NEEDS_SCOPE = { weekly_report: 1, training: 1, retest: 1 };
const SUBJECTS = ['数学', '语文', '英语', '物理', '化学'];
const GRADES = ['七年级', '八年级', '九年级', '高一', '高二', '高三'];
const MAX_IMAGES = 9;

const RUNTIME_TEXT = {
  ready: '技能服务已就绪',
  auth_failed: '服务端与 Hermes 的密钥不一致，请检查配置',
  skill_unknown: '无法确认技能是否已安装（技能列表接口异常），任务仍会尝试执行',
  skill_missing: 'Hermes 未加载学习技能，任务可能无法完成',
  unreachable: '暂时连不上 Hermes，请稍后再试',
  not_configured: '服务端尚未配置 Hermes，无法执行学习任务',
  legacy: '当前为旧直连模式：不执行技能流程与二次核查',
  unknown: '运行状态未知',
};
const STATUS_TEXT = {
  pending: '排队中', grading: '执行中', waiting_input: '待补充材料',
  interrupted: '结果未确认', done: '已完成', failed: '失败',
};
const STATUS_CLASS = {
  pending: 'pending', grading: 'grading', waiting_input: 'waiting_input',
  interrupted: 'interrupted', done: 'done', failed: 'failed',
};
const Q_LABEL = {
  correct: '答对', wrong: '答错', unanswered: '未作答',
  uncertain: '待核实', unprocessed: '未处理',
};
const REVIEW_LABEL = {
  agreed: '核查未发现异议', disagreed: '核查有异议', unverified: '无法核查',
  unprocessed: '未送核查', not_applicable: '未核查',
};
const DELIVERY_LABEL = {
  not_configured: '未配置', skipped: '已跳过', generated: '已生成',
  sent: '已请求发送', committed: '已提交', failed: '失败',
};
const AUTO_POLL_LIMIT = 30 * 60 * 1000;
const TERMINAL = ['done', 'failed', 'waiting_input', 'interrupted'];

const app = document.getElementById('app');

/* ---------- 工具 ---------- */

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function el(id) { return document.getElementById(id); }

function fmtTime(seconds) {
  if (!seconds) return '';
  return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false });
}

function fmtDate(seconds) {
  if (!seconds) return '';
  return new Date(seconds * 1000).toLocaleDateString('zh-CN');
}

let toastTimer = null;
function toast(message) {
  const node = el('toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 2200);
}

function closeModal() { el('modal').hidden = true; }

function openModal(title, bodyHtml, actions) {
  el('modalTitle').textContent = title;
  el('modalBody').innerHTML = bodyHtml;
  const box = el('modalActions');
  box.innerHTML = '';
  (actions || [{ label: '知道了', primary: true, onClick: closeModal }]).forEach((action) => {
    const btn = document.createElement('button');
    btn.textContent = action.label;
    if (action.primary) btn.className = 'primary';
    else btn.className = 'ghost';
    btn.onclick = () => { if (action.onClick) action.onClick(); else closeModal(); };
    box.appendChild(btn);
  });
  el('modal').hidden = false;
}

function isActive(name) { return parseHash().name === name; }

/* ---------- 会话与接口 ---------- */

const session = {
  token: () => localStorage.getItem(TOKEN_KEY) || '',
  openid: () => localStorage.getItem(OPENID_KEY) || '',
  save(t, o) { localStorage.setItem(TOKEN_KEY, t); localStorage.setItem(OPENID_KEY, o || ''); },
  clear() { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(OPENID_KEY); },
};

async function api(path, opts = {}) {
  const { method = 'GET', json, form, headers = {} } = opts;
  const head = Object.assign({}, headers);
  if (session.token()) head.Authorization = 'Bearer ' + session.token();

  let body;
  if (json !== undefined) {
    head['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  } else if (form) {
    body = new FormData();
    Object.keys(form).forEach((k) => {
      const v = form[k];
      if (v !== undefined && v !== null && v !== '') body.append(k, v);
    });
  }

  const res = await fetch(path, { method, headers: head, body });
  if (res.status === 401) {
    session.clear();
    if (!/login/.test(location.hash)) location.hash = '#/login';
    throw new Error('会话已失效，请重新登录');
  }
  if (!res.ok) {
    let detail = '请求失败（' + res.status + '）';
    try {
      const data = await res.json();
      if (data && data.detail) {
        detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
    } catch (e) { /* 保留默认文案 */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

async function downloadArtifact(taskId, artifact, onDone) {
  try {
    const res = await fetch('/api/tasks/' + taskId + '/artifacts/' + artifact.id, {
      headers: { Authorization: 'Bearer ' + session.token() },
    });
    if (!res.ok) throw new Error('下载失败 ' + res.status);
    const blob = await res.blob();
    if (artifact.kind === 'pdf') {
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } else {
      const text = await blob.text();
      openModal('归档内容', '<pre class="body">' + esc(text) + '</pre>', [
        {
          label: '复制到剪贴板',
          primary: true,
          onClick: () => {
            navigator.clipboard.writeText(text)
              .then(() => { closeModal(); toast('已复制'); })
              .catch(() => toast('复制失败，请手动选择文本'));
          },
        },
        { label: '关闭', onClick: closeModal },
      ]);
    }
  } catch (e) {
    toast(e.message || '获取失败');
  } finally {
    if (onDone) onDone();
  }
}

/* ---------- 路由 ---------- */

const state = {
  meta: null,
  form: {
    typeIndex: 0, subjectIndex: 0, gradeIndex: 0,
    text: '', scopeStart: '', scopeEnd: '',
    images: [], busy: false,
  },
  task: null,
  taskId: '',
  timer: null,
  delay: 2000,
  startedAt: 0,
  exhausted: false,
  followup: { text: '', images: [], busy: false },
};

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const parts = raw.split('/');
  return { name: parts[0] || 'submit', arg: parts[1] || '' };
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function stopPolling() {
  if (state.timer) clearTimeout(state.timer);
  state.timer = null;
}

const NAV = [
  { hash: '#/submit', name: 'submit', label: '学习' },
  { hash: '#/history', name: 'history', label: '历史' },
  { hash: '#/mistakes', name: 'mistakes', label: '错题本' },
  { hash: '#/mine', name: 'mine', label: '我的' },
];

function shell(active, content) {
  const title = (state.meta && state.meta.title) || 'Leo 学习助手';
  app.innerHTML =
    '<div class="topbar"><div><h1>' + esc(title) + '</h1>' +
    '<div class="sub">批改 · 错题解析 · 周报 · 训练 · 复测</div></div></div>' +
    content +
    '<nav class="nav">' +
    NAV.map((n) => '<a href="' + n.hash + '"' + (n.name === active ? ' class="on"' : '') + '>' + n.label + '</a>').join('') +
    '</nav>';
}

function render() {
  const { name, arg } = parseHash();
  if (name !== 'login' && !session.token()) { location.hash = '#/login'; return; }
  if (name === 'login' && session.token()) { location.hash = '#/submit'; return; }
  stopPolling();
  if (name === 'task') renderTask(arg);
  else if (name === 'history') renderHistory();
  else if (name === 'mistakes') renderMistakes();
  else if (name === 'mine') renderMine();
  else if (name === 'login') renderLogin();
  else renderSubmit();
}

/* ---------- 登录 ---------- */

async function renderLogin() {
  if (!state.meta) {
    try { state.meta = await api('/api/web/meta'); } catch (e) { /* 保持默认 */ }
    if (!isActive('login')) return;
    if (state.meta && state.meta.title) document.title = state.meta.title;
  }
  const title = (state.meta && state.meta.title) || 'Leo 学习助手';
  const configured = !state.meta || state.meta.configured !== false;
  const enabled = !state.meta || state.meta.enabled !== false;

  app.innerHTML =
    '<div class="topbar"><div><h1>' + esc(title) + '</h1>' +
    '<div class="sub">网页版 · 无需小程序备案，IP 直连可用</div></div></div>' +
    '<div class="card">' +
    '<span class="sec-title">访问登录</span>' +
    (enabled && configured ? '' :
      '<div class="disclaimer">' +
      (enabled ? '服务端尚未配置网页访问密码（WEB_PASSWORD），请先在 .env 中设置并重建容器。'
               : '网页版当前已被管理员关闭（web.enabled = false）。') +
      '</div>') +
    '<label class="field"><span>访问密码</span>' +
    '<input id="pw" type="password" autocomplete="current-password" placeholder="请输入管理员配置的访问密码"></label>' +
    '<label class="field"><span>账号名（可选，用于区分不同使用者）</span>' +
    '<input id="uname" type="text" autocomplete="username" placeholder="family"></label>' +
    '<button class="primary" id="doLogin"' + (enabled && configured ? '' : ' disabled') + '>登录</button>' +
    '<span class="muted">密码只用于换取会话令牌；服务端不会保存明文密码，也不会把 Hermes 密钥下发到浏览器。</span>' +
    '</div>' +
    '<div class="card tight"><span class="muted">提示：网页版通过 http 直连时令牌是明文传输，建议仅在可信网络中使用，或后续换成 https 域名。</span></div>';

  const submit = async () => {
    const password = el('pw').value;
    const user = el('uname').value.trim();
    if (!password) { toast('请输入访问密码'); return; }
    el('doLogin').disabled = true;
    try {
      const data = await api('/api/web/login', { method: 'POST', form: { password, user } });
      session.save(data.token, data.openid);
      state.meta = null;
      toast('登录成功');
      location.hash = '#/submit';
    } catch (e) {
      toast(e.message || '登录失败');
      el('doLogin').disabled = false;
    }
  };
  el('doLogin').onclick = submit;
  el('pw').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  el('uname').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  const pw = el('pw');
  if (!pw.disabled) pw.focus();
}

/* ---------- 学习（提交任务）---------- */

function renderSubmit() {
  const form = state.form;
  const type = TASK_TYPES[form.typeIndex];
  const needsScope = !!NEEDS_SCOPE[type.key];

  shell('submit',
    '<div class="card tight" id="runtimeCard"><span class="muted">正在检查服务状态…</span></div>' +
    '<div class="card">' +
    '<span class="sec-title">任务类型</span>' +
    '<div class="chips">' + TASK_TYPES.map((t, i) =>
      '<div class="chip' + (i === form.typeIndex ? ' on' : '') + '" data-type="' + i + '">' + t.label + '</div>').join('') +
    '</div><span class="muted">' + esc(type.hint) + '</span></div>' +

    (needsScope ?
      '<div class="card"><span class="sec-title">资料区间</span>' +
      '<label class="field"><span>开始日期</span><input type="date" id="scopeStart" value="' + esc(form.scopeStart) + '"></label>' +
      '<label class="field"><span>结束日期</span><input type="date" id="scopeEnd" value="' + esc(form.scopeEnd) + '"></label>' +
      '<span class="muted">未指定时按技能默认规则：月考取当月、期中期末取本学期</span></div>' : '') +

    '<div class="card">' +
    '<label class="field"><span>学科</span><select id="subject">' +
    SUBJECTS.map((s, i) => '<option' + (i === form.subjectIndex ? ' selected' : '') + '>' + s + '</option>').join('') +
    '</select></label>' +
    '<label class="field"><span>年级</span><select id="grade">' +
    GRADES.map((g, i) => '<option' + (i === form.gradeIndex ? ' selected' : '') + '>' + g + '</option>').join('') +
    '</select></label></div>' +

    '<div class="card">' +
    '<span class="sec-title">图片（<span id="imgCount">' + form.images.length + '</span>/' + MAX_IMAGES + '）</span>' +
    '<div class="grid">' + form.images.map((item, i) =>
      '<div class="thumb-wrap"><img class="thumb" src="' + item.url + '" alt="">' +
      '<span class="thumb-state ' + item.status + '">' +
      (item.status === 'ready' ? '已上传' : item.status === 'uploading' ? '上传中' : item.status === 'failed' ? '失败' : '待上传') +
      '</span><button class="thumb-del" data-del="' + i + '">×</button></div>').join('') +
    (form.images.length < MAX_IMAGES ?
      '<div class="thumb-add" id="pickImages"><b>+</b><span>拍照/相册</span></div>' : '') +
    '</div>' +
    '<input type="file" id="imgInput" accept="image/*" multiple hidden>' +
    '<span class="muted">按作业页码顺序添加，拍得越正越清晰，结果越准</span></div>' +

    '<div class="card"><span class="sec-title">补充说明（可选）</span>' +
    '<textarea id="taskText" maxlength="2000" placeholder="例如：这是今天的数学作业，第 12 页；只看第 3、4 题">' + esc(form.text) + '</textarea></div>' +

    '<button class="primary" id="submitTask"' + (form.busy ? ' disabled' : '') + '>' +
    (form.busy ? '提交中…' : '提交学习任务') + '</button>' +
    '<div class="card tight" style="margin-top:14px"><span class="muted">AI 批改仅供参考，请以老师讲解为准；缺少材料时会在结果中列出待补充项</span></div>'
  );

  checkRuntime();

  app.querySelectorAll('[data-type]').forEach((node) => {
    node.onclick = () => {
      const i = Number(node.dataset.type);
      if (i === state.form.typeIndex) return;
      state.form.typeIndex = i;
      renderSubmit();
    };
  });

  el('subject').onchange = (e) => { state.form.subjectIndex = e.target.selectedIndex; };
  el('grade').onchange = (e) => { state.form.gradeIndex = e.target.selectedIndex; };
  el('taskText').oninput = (e) => { state.form.text = e.target.value; };
  if (el('scopeStart')) el('scopeStart').onchange = (e) => { state.form.scopeStart = e.target.value; };
  if (el('scopeEnd')) el('scopeEnd').onchange = (e) => { state.form.scopeEnd = e.target.value; };

  el('pickImages').onclick = () => el('imgInput').click();
  el('imgInput').onchange = (e) => {
    const remain = MAX_IMAGES - state.form.images.length;
    Array.from(e.target.files || []).slice(0, remain).forEach((file) => {
      state.form.images.push({ file, url: URL.createObjectURL(file), assetId: '', status: 'pending' });
    });
    renderSubmit();
  };
  app.querySelectorAll('[data-del]').forEach((node) => {
    node.onclick = () => {
      const i = Number(node.dataset.del);
      const [removed] = state.form.images.splice(i, 1);
      if (removed) URL.revokeObjectURL(removed.url);
      renderSubmit();
    };
  });

  el('submitTask').onclick = submitTask;
}

async function checkRuntime() {
  try {
    const rt = await api('/api/runtime');
    if (!isActive('submit')) return;
    let st = 'unknown';
    if (rt.engine && rt.engine.mode === 'legacy') st = 'legacy';
    else if (rt.hermes) st = rt.hermes.state;
    const card = el('runtimeCard');
    if (!card) return;
    const ok = st === 'ready';
    card.innerHTML = '<div class="row"><span>运行状态</span>' +
      '<span class="pill ' + (ok ? 'ready' : 'warn') + '">' + esc(RUNTIME_TEXT[st] || RUNTIME_TEXT.unknown) + '</span></div>' +
      '<span class="muted">「已配置」不等于「已就绪」：只有技能确实加载完成才会显示就绪。</span>';
  } catch (e) {
    const card = el('runtimeCard');
    if (card) card.innerHTML = '<span class="muted">无法获取服务状态：' + esc(e.message) + '</span>';
  }
}

async function submitTask() {
  const form = state.form;
  const type = TASK_TYPES[form.typeIndex].key;
  const text = (form.text || '').trim();
  if (form.images.length === 0 && !text) { toast('请上传图片或填写说明'); return; }
  if (type === 'grading' && form.images.length === 0) { toast('作业批改需要至少一张照片'); return; }

  form.busy = true;
  el('submitTask').disabled = true;
  el('submitTask').textContent = '提交中…';
  try {
    for (const item of form.images) {
      if (item.assetId) continue;
      item.status = 'uploading';
      renderSubmit();
      const res = await api('/api/assets', { method: 'POST', form: { file: item.file } });
      item.assetId = res.asset_id;
      item.status = 'ready';
    }
    const assetIds = form.images.map((i) => i.assetId).filter(Boolean);
    if (!form.pendingKey) {
      form.pendingKey = 'web-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    }
    const created = await api('/api/study/tasks', {
      method: 'POST',
      headers: { 'Idempotency-Key': form.pendingKey },
      json: {
        task_type: type,
        subject: SUBJECTS[form.subjectIndex],
        grade_level: GRADES[form.gradeIndex],
        text,
        asset_ids: assetIds,
        scope_start: form.scopeStart,
        scope_end: form.scopeEnd,
      },
    });
    form.pendingKey = '';
    form.images.forEach((i) => URL.revokeObjectURL(i.url));
    form.images = [];
    form.text = '';
    toast('已提交，正在执行');
    go('#/task/' + created.task_id);
  } catch (e) {
    form.busy = false;
    openModal('提交失败', esc(e.message || '未知错误') + '\n\n已上传的图片不会重复上传，可直接重试。');
    if (isActive('submit')) renderSubmit();
  }
}

/* ---------- 任务结果 ---------- */

function renderTask(taskId) {
  if (taskId) {
    state.taskId = taskId;
    state.task = null;
    state.exhausted = false;
    state.delay = 2000;
    state.startedAt = Date.now();
    state.followup = { text: '', images: [], busy: false };
  }
  drawTask('正在加载…');
  tick();
}

function drawTask(placeholder) {
  shell('history', '<div id="taskBody">' + placeholder + '</div>');
  paintTask();
}

function paintTask() {
  const body = el('taskBody');
  if (!body) return;
  const task = state.task;
  if (!task) { body.innerHTML = '<div class="card loading"><span class="state-line">正在执行学习任务…</span></div>'; return; }

  const status = task.status;
  const result = task.result || null;
  const artifacts = task.artifacts || [];
  const runs = task.runs || [];
  const deliveryRows = buildDeliveryRows(result);
  let html = '';

  html += '<div class="disclaimer">AI 批改仅供参考，请以老师讲解为准</div>';

  html += '<div class="card"><div class="q-head"><span class="q-no">任务 ' + esc(task.id) + '</span>' +
    '<span class="pill ' + (STATUS_CLASS[status] || 'pending') + '">' + esc(STATUS_TEXT[status] || status) + '</span></div>' +
    (result ? '<span class="muted">' + esc(result.task_type_label || '') + ' · ' + esc(result.subject || '') + ' ' + esc(result.grade_level || '') + '</span>' : '') +
    '</div>';

  if (status === 'pending' || status === 'grading') {
    html += '<div class="card loading"><span class="state-line">正在执行学习任务…</span>' +
      '<span class="muted">技能会先读取工作区规范与历史记录，再逐题分析并做错题核查，耗时较长属正常。</span>' +
      '<span class="muted">可以离开本页，回来后会自动继续查询。</span></div>';
  }
  if (state.exhausted) {
    html += '<div class="card"><span class="state-line">自动查询已暂停</span>' +
      '<span class="muted">任务可能仍在远端执行。点击下方按钮继续查询，不会重复提交任务。</span>' +
      '<button class="mini" id="resumePoll" style="margin-top:10px">继续查询</button></div>';
  }
  if (status === 'interrupted') {
    html += '<div class="card"><span class="state-line">执行结果未确认</span>' +
      '<span class="muted">' + esc(task.error || '') + '</span>' +
      '<span class="muted">服务不会自动重试，以免重复归档。可补充材料后再次发起。</span></div>';
  }
  if (status === 'failed') {
    html += '<div class="card"><span class="state-line err">任务失败</span>' +
      '<span class="muted">' + esc(task.error || '') + '</span>' +
      '<button class="mini" id="backSubmit" style="margin-top:10px">返回重新发起</button></div>';
  }

  if (result) {
    if (result.legacy) {
      html += '<div class="card warn-box"><span class="muted">旧版本结果：未记录二次核查，也不含五态判定与交付状态。</span></div>';
    }

    html += '<div class="card"><span class="sec-title">概览</span><div class="stat-row">' +
      stat(result.overview.checked_questions, '已检查', '') +
      stat(result.overview.correct, '答对', 'ok') +
      stat(result.overview.wrong, '答错', 'bad') +
      stat(result.overview.unanswered, '未作答', 'wait') +
      stat(result.overview.uncertain, '待核实', 'info') +
      '</div>' +
      (result.overview.summary ? '<span class="muted">' + esc(result.overview.summary) + '</span>' : '') +
      '</div>';

    const scope = result.scope || {};
    if (scope.start_date || scope.end_date || (scope.sources || []).length) {
      html += '<div class="card"><span class="sec-title">资料范围</span>' +
        (scope.start_date || scope.end_date ?
          '<span class="muted">区间：' + esc(scope.start_date || '不限') + ' ~ ' + esc(scope.end_date || '不限') + '</span>' : '') +
        (scope.sources || []).map((s) => '<span class="muted">来源：' + esc(s) + '</span>').join('') +
        '</div>';
    }

    if ((result.missing_info || []).length) {
      html += '<div class="card"><span class="sec-title">需要补充</span>' +
        result.missing_info.map((m) => '<span class="miss">· ' + esc(m) + '</span>').join('') + '</div>';
    }

    const review = result.review_summary || {};
    if (review.state && review.state !== 'not_required') {
      html += '<div class="card"><span class="sec-title">错题二次核查</span><span class="muted">' +
        esc('状态：' + review.state + ' · 送核查 ' + review.scope + ' 题 · 有异议 ' + review.disagreed +
          ' 题 · 无法核查 ' + review.unverified + ' 题') + '</span>' +
        (review.note ? '<span class="muted">' + esc(review.note) + '</span>' : '') + '</div>';
    }

    (result.questions || []).forEach((q, index) => {
      html += '<div class="card q"><div class="q-head"><span class="q-no">第 ' + esc(q.no || q.id) + ' 题' +
        (q.page ? '<span class="q-src"> · ' + esc(q.page) + '</span>' : '') + '</span>' +
        '<span class="pill">' + esc(Q_LABEL[q.status] || q.status) + '</span></div>' +
        (q.source ? '<span class="q-src">来源：' + esc(q.source) + '</span>' : '') +
        (q.stem ? '<div class="q-row"><span class="k">题目：</span>' + esc(q.stem) + '</div>' : '') +
        '<div class="q-row"><span class="k">孩子原答案：</span>' + esc(q.student_answer || '—') + '</div>' +
        (q.correct_answer ? '<div class="q-row"><span class="k">正确答案：</span><span class="ans">' + esc(q.correct_answer) + '</span></div>' : '') +
        (q.knowledge_point ? '<div class="q-row"><span class="k">知识点：</span>' + esc(q.knowledge_point) + '</div>' : '') +
        (q.error_rule ? '<div class="q-row"><span class="k">错因规则：</span>' + esc(q.error_rule) + '</div>' : '') +
        (q.review && q.review.state && q.review.state !== 'not_applicable' ?
          '<div class="q-row"><span class="k">核查：</span>' + esc(REVIEW_LABEL[q.review.state] || q.review.state) + '</div>' : '') +
        (q.review && q.review.note ? '<div class="q-row"><span class="k">核查说明：</span>' + esc(q.review.note) + '</div>' : '') +
        (q.final_decision_basis ? '<div class="q-row"><span class="k">复核依据：</span>' + esc(q.final_decision_basis) + '</div>' : '') +
        ((q.steps || []).length ?
          '<div class="explain"><span class="k">解题步骤：</span>' +
          q.steps.map((s, i) => '<span class="step">' + (i + 1) + '. ' + esc(s) + '</span>').join('') + '</div>' : '') +
        (q.status === 'wrong' ? '<button class="mini" data-mistake="' + index + '">加入错题本</button>' : '') +
        '</div>';
    });

    (result.sections || []).forEach((s) => {
      html += '<div class="card"><span class="sec-title">' + esc(s.title) + '</span>' +
        '<span class="body">' + esc(s.body) + '</span></div>';
    });

    if ((result.parent_tips || []).length) {
      html += '<div class="card"><span class="sec-title">给家长的建议</span>' +
        result.parent_tips.map((t) => '<span class="body">· ' + esc(t) + '</span>').join('') + '</div>';
    }

    if (deliveryRows.length) {
      html += '<div class="card"><span class="sec-title">交付状态</span>' +
        deliveryRows.map((r) => '<div class="delivery-row"><span class="d-label">' + esc(r.label) + '</span>' +
          '<span class="pill">' + esc(r.text) + '</span></div>').join('') +
        deliveryRows.filter((r) => r.note).map((r) => '<span class="muted">' + esc(r.label + '：' + r.note) + '</span>').join('') +
        '</div>';
    }
  }

  if (artifacts.length) {
    html += '<div class="card"><span class="sec-title">可下载成果</span>' +
      artifacts.map((a) =>
        '<div class="artifact-row" data-artifact="' + a.id + '">' +
        '<span>' + (a.kind === 'pdf' ? 'PDF 文件' : '归档文本') + '（' + a.bytes + ' 字节）</span>' +
        '<span class="link">下载 ›</span></div>').join('') +
      '<span class="muted">只有真实存在并通过校验的文件才会出现在这里。</span></div>';
  }

  if (result && ['waiting_input', 'interrupted', 'done'].indexOf(status) >= 0) {
    html += '<div class="card"><span class="sec-title">补充材料</span>' +
      '<textarea id="followText" placeholder="补充说明，例如：本学期开学日期是 9 月 1 日；这是第 2 页的作答">' + esc(state.followup.text) + '</textarea>' +
      '<div class="grid">' + state.followup.images.map((item, i) =>
        '<div class="thumb-wrap"><img class="thumb" src="' + item.url + '" alt="">' +
        '<button class="thumb-del" data-fdel="' + i + '">×</button></div>').join('') +
      (state.followup.images.length < MAX_IMAGES ?
        '<div class="thumb-add" id="pickFollow"><b>+</b><span>添加图片</span></div>' : '') +
      '</div><input type="file" id="followInput" accept="image/*" multiple hidden>' +
      '<button class="primary" id="submitFollow"' + (state.followup.busy ? ' disabled' : '') + ' style="margin-top:10px">' +
      (state.followup.busy ? '提交中…' : '提交补充材料') + '</button></div>';
  }

  if (runs.length) {
    html += '<div class="card"><span class="sec-title">执行轮次</span>' +
      runs.map((r) => '<div class="run-row"><span>第 ' + r.run_no + ' 轮（' +
        (r.kind === 'followup' ? '补充材料' : '首次提交') + '）</span>' +
        '<span class="pill ' + (STATUS_CLASS[r.status] || 'pending') + '">' + esc(r.status) + '</span></div>').join('') +
      '</div>';
  }

  body.innerHTML = html;

  if (el('resumePoll')) el('resumePoll').onclick = () => {
    state.exhausted = false; state.startedAt = Date.now(); state.delay = 0; tick();
  };
  if (el('backSubmit')) el('backSubmit').onclick = () => go('#/submit');

  app.querySelectorAll('[data-artifact]').forEach((node) => {
    node.onclick = () => {
      const artifact = (task.artifacts || []).find((a) => a.id === node.dataset.artifact);
      if (artifact) downloadArtifact(task.id, artifact, () => toast('已获取'));
    };
  });

  app.querySelectorAll('[data-mistake]').forEach((node) => {
    node.onclick = () => {
      const q = (result.questions || [])[Number(node.dataset.mistake)];
      if (!q) return;
      api('/api/mistakes', {
        method: 'POST',
        form: {
          task_id: task.id, question_no: q.no || q.id,
          knowledge_point: q.knowledge_point || '', note: '',
        },
      }).then(() => toast('已加入错题本')).catch((e) => toast(e.message || '保存失败'));
    };
  });

  if (el('followText')) el('followText').oninput = (e) => { state.followup.text = e.target.value; };
  if (el('pickFollow')) el('pickFollow').onclick = () => el('followInput').click();
  if (el('followInput')) {
    el('followInput').onchange = (e) => {
      const remain = MAX_IMAGES - state.followup.images.length;
      Array.from(e.target.files || []).slice(0, remain).forEach((file) => {
        state.followup.images.push({ file, url: URL.createObjectURL(file) });
      });
      paintTask();
    };
  }
  app.querySelectorAll('[data-fdel]').forEach((node) => {
    node.onclick = () => {
      const [removed] = state.followup.images.splice(Number(node.dataset.fdel), 1);
      if (removed) URL.revokeObjectURL(removed.url);
      paintTask();
    };
  });
  if (el('submitFollow')) el('submitFollow').onclick = submitFollowup;
}

function stat(value, label, cls) {
  return '<div class="stat"><span class="stat-n ' + cls + '">' + (value || 0) + '</span>' +
    '<span class="stat-l">' + label + '</span></div>';
}

function buildDeliveryRows(result) {
  if (!result || !result.delivery) return [];
  const map = { archive: '学习记录归档', pdf: 'PDF 交付', email: '邮件交付', git: '记录同步' };
  return Object.keys(map).map((key) => {
    const item = result.delivery[key];
    if (!item) return null;
    return { key, label: map[key], text: DELIVERY_LABEL[item.status] || item.status, note: item.note || '' };
  }).filter(Boolean);
}

function schedule() {
  stopPolling();
  state.delay = Math.min((state.delay || 2000) + 1000, 10000);
  state.timer = setTimeout(tick, state.delay);
}

async function tick() {
  const taskId = state.taskId;
  if (!taskId) return;
  try {
    const task = await api('/api/tasks/' + taskId);
    if (state.taskId !== taskId) return;
    state.task = task;
    paintTask();
    if (TERMINAL.indexOf(task.status) >= 0) { stopPolling(); return; }
  } catch (e) {
    toast('查询失败，稍后重试');
  }
  if (Date.now() - state.startedAt > AUTO_POLL_LIMIT) {
    state.exhausted = true;
    stopPolling();
    paintTask();
    return;
  }
  schedule();
}

async function submitFollowup() {
  const follow = state.followup;
  const text = (follow.text || '').trim();
  if (!text && follow.images.length === 0) { toast('请填写补充内容或上传图片'); return; }
  follow.busy = true;
  paintTask();
  try {
    const ids = [];
    for (const item of follow.images) {
      const res = await api('/api/assets', { method: 'POST', form: { file: item.file } });
      ids.push(res.asset_id);
    }
    await api('/api/tasks/' + state.taskId + '/followups', {
      method: 'POST', json: { text, asset_ids: ids },
    });
    follow.images.forEach((i) => URL.revokeObjectURL(i.url));
    state.followup = { text: '', images: [], busy: false };
    state.exhausted = false;
    state.startedAt = Date.now();
    state.delay = 0;
    toast('补充材料已提交');
    tick();
  } catch (e) {
    state.followup.busy = false;
    toast(e.message || '提交失败');
    paintTask();
  }
}

/* ---------- 历史 ---------- */

async function renderHistory() {
  shell('history', '<div class="card loading"><span class="muted">加载中…</span></div>');
  let tasks;
  try {
    tasks = await api('/api/tasks?limit=50&offset=0');
  } catch (e) {
    if (isActive('history')) shell('history', '<div class="empty">加载失败：' + esc(e.message) + '</div>');
    return;
  }
  if (!isActive('history')) return;
  if (!tasks.length) {
    shell('history', '<div class="empty">还没有学习记录<span class="muted">去「学习」页提交第一份作业吧</span></div>');
    return;
  }
  const typeLabel = { grading: '作业批改', qa: '学习问答', weekly_report: '周报分析', training: '针对性训练', retest: '复测' };
  const html = tasks.map((t) =>
    '<div class="card row-card" data-task="' + t.id + '"><div>' +
    '<span class="t">' + esc(typeLabel[t.task_type] || '学习任务') + ' · ' + esc(t.subject || '未指定学科') + '</span>' +
    '<span class="time">' + esc(fmtTime(t.created_at)) + (t.run_count > 1 ? ' · 第 ' + t.run_count + ' 轮' : '') + '</span>' +
    (t.summary ? '<span class="summary">' + esc(t.summary) + '</span>' : '') +
    (t.missing_info_count ? '<span class="miss-note">有 ' + t.missing_info_count + ' 项待补充</span>' : '') +
    '</div><span class="pill ' + (STATUS_CLASS[t.status] || 'pending') + '">' +
    esc(STATUS_TEXT[t.status] || t.status) + '</span></div>').join('');
  shell('history', html);
  app.querySelectorAll('[data-task]').forEach((node) => {
    node.onclick = () => go('#/task/' + node.dataset.task);
  });
}

/* ---------- 错题本 ---------- */

async function renderMistakes() {
  shell('mistakes', '<div class="card loading"><span class="muted">加载中…</span></div>');
  let list;
  try {
    list = await api('/api/mistakes?limit=100&offset=0');
  } catch (e) {
    if (isActive('mistakes')) shell('mistakes', '<div class="empty">加载失败：' + esc(e.message) + '</div>');
    return;
  }
  if (!isActive('mistakes')) return;
  if (!list.length) {
    shell('mistakes', '<div class="empty">错题本里还没有内容' +
      '<span class="muted">这里只保存你手动加入的题目；空不代表全部答对</span></div>');
    return;
  }
  const html = list.map((m) =>
    '<div class="card row-card" data-task="' + esc(m.task_id) + '"><div>' +
    '<span class="t">第 ' + esc(m.question_no) + ' 题</span>' +
    '<span class="time">' + esc(fmtDate(m.created_at)) + '</span>' +
    (m.knowledge_point ? '<span class="q-src">知识点：' + esc(m.knowledge_point) + '</span>' : '') +
    (m.note ? '<span class="q-src">' + esc(m.note) + '</span>' : '') +
    '</div><span class="link">查看 ›</span></div>').join('');
  shell('mistakes', html);
  app.querySelectorAll('[data-task]').forEach((node) => {
    node.onclick = () => go('#/task/' + node.dataset.task);
  });
}

/* ---------- 我的 ---------- */

const ENGINE_TEXT = { hermes: 'Hermes 技能引擎', legacy: '旧直连模式（不执行技能流程与二次核查）' };
const HERMES_TEXT = {
  ready: '就绪（技能已安装）', auth_failed: '密钥不一致',
  skill_unknown: '无法确认（技能列表接口异常）', skill_missing: '技能未安装',
  unreachable: '连不上 Hermes', not_configured: '未配置 Hermes',
};

async function renderMine() {
  shell('mine', '<div class="card loading"><span class="muted">加载中…</span></div>');
  let quota; let runtime;
  try {
    const both = await Promise.all([api('/api/quota'), api('/api/runtime')]);
    quota = both[0]; runtime = both[1];
  } catch (e) {
    if (isActive('mine')) shell('mine', '<div class="empty">加载失败：' + esc(e.message) + '</div>');
    return;
  }
  if (!isActive('mine')) return;

  const hermes = runtime.hermes || {};
  const deliveryMap = { pdf: 'PDF 交付', email: '邮件交付', git: '记录同步' };
  const delivery = Object.keys(runtime.delivery || {}).map((k) => ({
    key: k, label: deliveryMap[k] || k, enabled: runtime.delivery[k],
  }));
  const limits = runtime.limits || {};
  const workspace = runtime.workspace || {};

  shell('mine',
    '<div class="card"><span class="sec-title">当前账号</span>' +
    '<div class="row"><span class="url">' + esc(session.openid() || '未登录') + '</span>' +
    '<button class="mini ghost" id="copyOpenid">复制</button></div>' +
    '<span class="muted">网页账号与微信 openid 相互独立，学习记录按账号隔离。</span></div>' +

    '<div class="card"><div class="row"><span>剩余可用次数</span>' +
    '<span class="stat-n">' + esc(String(quota.remaining)) + '</span></div></div>' +

    '<div class="card"><span class="sec-title">执行引擎</span>' +
    '<div class="row"><span>模式</span><span class="muted">' + esc(ENGINE_TEXT[runtime.engine.mode] || runtime.engine.mode) + '</span></div>' +
    '<div class="row"><span>Hermes</span><span class="pill ' + (hermes.state === 'ready' ? 'done' : 'waiting_input') + '">' +
    esc(HERMES_TEXT[hermes.state] || (runtime.engine.mode === 'legacy' ? '不适用' : '未知')) + '</span></div>' +
    (hermes.detail ? '<span class="muted">' + esc(hermes.detail) + '</span>' : '') +
    '<span class="muted">「已配置」不等于「已就绪」：只有技能确实加载完成才会显示就绪。</span></div>' +

    '<div class="card"><span class="sec-title">交付能力</span>' +
    delivery.map((d) => '<div class="row"><span>' + esc(d.label) + '</span>' +
      '<span class="pill ' + (d.enabled ? 'done' : 'interrupted') + '">' + (d.enabled ? '已启用' : '未配置') + '</span></div>').join('') +
    '<span class="muted">未启用的能力会在结果中标注为未配置，不会被模型自述替代。</span></div>' +

    '<div class="card"><span class="sec-title">限额</span>' +
    '<div class="row"><span>单次图片上限</span><span class="muted">' + esc(String(limits.max_assets_per_task)) + ' 张</span></div>' +
    '<div class="row"><span>单任务最多轮次</span><span class="muted">' + esc(String(limits.max_runs_per_task)) + ' 轮</span></div>' +
    '<div class="row"><span>单轮执行上限</span><span class="muted">' + esc(String(limits.max_task_minutes)) + ' 分钟</span></div>' +
    (workspace.dir ? '<div class="row"><span>学习工作区</span><span class="muted">' + esc(workspace.dir) + '</span></div>' : '') +
    '</div>' +

    '<div class="card"><span class="sec-title">服务器地址</span>' +
    '<span class="url">' + esc(location.origin) + '</span>' +
    '<span class="muted">浏览器只连接本服务；Hermes 地址与密钥不会出现在前端。</span></div>' +

    '<button class="primary" id="logout">退出登录</button>'
  );

  el('copyOpenid').onclick = () => {
    navigator.clipboard.writeText(session.openid())
      .then(() => toast('已复制')).catch(() => toast('复制失败'));
  };
  el('logout').onclick = () => {
    openModal('退出登录', '将清除本机会话（不会删除学习记录）。', [
      { label: '退出', primary: true, onClick: async () => { closeModal(); await doLogout(); } },
      { label: '取消', onClick: closeModal },
    ]);
  };
}

async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ }
  session.clear();
  state.meta = null;
  location.hash = '#/login';
}

/* ---------- 启动 ---------- */

(async function boot() {
  try {
    state.meta = await api('/api/web/meta');
    if (state.meta && state.meta.title) document.title = state.meta.title;
  } catch (e) { /* 登录页会再次尝试 */ }
  window.addEventListener('hashchange', render);
  el('modal').onclick = (e) => { if (e.target === el('modal')) closeModal(); };
  render();
})();
