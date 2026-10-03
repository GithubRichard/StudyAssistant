const api = require('../../utils/api');

const STATUS_TEXT = {
  pending: '排队中',
  grading: '执行中',
  waiting_input: '待补充材料',
  interrupted: '结果未确认',
  done: '已完成',
  failed: '失败',
};
const STATUS_PILL = {
  pending: 'pill-pending',
  grading: 'pill-running',
  waiting_input: 'pill-waiting',
  interrupted: 'pill-interrupted',
  done: 'pill-done',
  failed: 'pill-failed',
};
const Q_LABEL = {
  correct: '答对', wrong: '答错', unanswered: '未作答',
  uncertain: '待核实', unprocessed: '未处理',
};
const REVIEW_LABEL = {
  agreed: '复查未发现异议', disagreed: '复查有异议', unverified: '复查无法核查',
  unprocessed: '未完成核查', not_applicable: '未送复查',
};
// 服务端二次复查（第二模型）整体状态与模型身份核验文案
const REVIEW_SUMMARY_TEXT = {
  completed: '已完成', partial: '部分完成', failed: '未完成',
  not_run: '未执行', not_required: '无需复查',
};
const REVIEW_IDENTITY_TEXT = {
  confirmed: '身份已确认', model_only: '模型已核对（网关未报告 provider）',
  mismatch: '路由不符', unknown: '身份未确认',
};
const DELIVERY_LABEL = {
  not_configured: '未配置', skipped: '已跳过', generated: '已生成',
  sent: '已请求发送', committed: '已提交', failed: '失败',
};
// 订正与复测状态（与结果协议 v3 一致）
const REMEDIATION_LABEL = {
  pending_correction: '待订正', corrected_pending_retest: '已订正待复测',
  retest_passed: '复测通过', retest_failed: '复测未通过', not_applicable: '不适用',
};
const REMEDIATION_PILL = {
  pending_correction: 'pill-waiting', corrected_pending_retest: 'pill-running',
  retest_passed: 'pill-done', retest_failed: 'pill-failed',
};
const RETEST_ACTIONS = [
  { label: '复测通过', result: 'retest_passed' },
  { label: '复测未通过', result: 'retest_failed' },
  { label: '已订正，待复测', result: 'corrected' },
];
// 自动轮询上限（毫秒）：超过后不再自动查询，改为手动「继续查询」，绝不假装仍在进行
const AUTO_POLL_LIMIT = 30 * 60 * 1000;
const TERMINAL = ['done', 'failed', 'waiting_input', 'interrupted'];

Page({
  data: {
    taskId: '',
    status: '',
    statusText: '加载中',
    statusPill: 'pill-pending',
    result: null,
    reviewView: null,
    runs: [],
    artifacts: [],
    error: '',
    missingInfo: [],
    followupText: '',
    followupImages: [],
    submitting: false,
    exhausted: false,
    downloading: false,
    qLabel: Q_LABEL,
    reviewLabel: REVIEW_LABEL,
    deliveryLabel: DELIVERY_LABEL,
    remediationLabel: REMEDIATION_LABEL,
    deliveryRows: [],
    gitConflictRecord: '',
    authError: '',
    orientationPages: [],
    orientationReady: false,
  },

  onLoad(options) {
    this.startedAt = Date.now();
    this.setData({ taskId: options.task_id || '' });
    this.tick();
  },

  onShow() {
    if (this.data.taskId && !TERMINAL.includes(this.data.status)) this.schedule();
  },

  onHide() { this.clearTimer(); },
  onUnload() { this.clearTimer(); },

  clearTimer() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  },

  schedule() {
    this.clearTimer();
    this.delay = Math.min((this.delay || 2000) + 1000, 10000);
    this.timer = setTimeout(() => this.tick(), this.delay);
  },

  async tick() {
    if (!this.data.taskId) return;
    try {
      const t = await api.getTask(this.data.taskId);
      const result = t.result || null;
      const ledgerById = {};
      (t.ledger || []).forEach((row) => {
        if (row.question_uid) ledgerById[row.question_uid] = row;
      });
      if (result && result.questions) {
        result.questions = result.questions.map((q) => {
          const state = (q.remediation && q.remediation.state) || 'not_applicable';
          const ledger = (q.uid && ledgerById[q.uid]) || null;
          return Object.assign({}, q, {
            remediationText: REMEDIATION_LABEL[state] || state,
            remediationPill: REMEDIATION_PILL[state] || 'pill-pending',
            remediationDate: (q.remediation && q.remediation.updated_date) || '',
            ledgerId: ledger ? ledger.id : 0,
          });
        });
      }
      const gitItem = (result && result.delivery && result.delivery.git) || null;
      this.setData({
        status: t.status,
        statusText: STATUS_TEXT[t.status] || t.status,
        statusPill: STATUS_PILL[t.status] || 'pill-pending',
        result,
        reviewView: this.buildReviewView(result),
        runs: t.runs || [],
        artifacts: t.artifacts || [],
        error: t.error || '',
        missingInfo: (result && result.missing_info) || [],
        deliveryRows: this.buildDeliveryRows(result),
        gitConflictRecord: (gitItem && gitItem.conflict_record) || '',
      });
      if (t.orientation) {
        this.clearTimer();
        await this.loadOrientation(t);
        return;
      }
      this.setData({orientationPages: [], orientationReady: false});
      if (TERMINAL.includes(t.status)) {
        this.clearTimer();
        return;
      }
    } catch (e) {
      wx.showToast({ title: '查询失败，稍后重试', icon: 'none' });
    }
    if (Date.now() - this.startedAt > AUTO_POLL_LIMIT) {
      this.setData({ exhausted: true });
      this.clearTimer();
      return;
    }
    this.schedule();
  },

  async loadOrientation(task) {
    const run = task.runs[task.runs.length - 1];
    this.orientationRunId = run.id;
    const pages = task.orientation.pages.filter(p => !p.confirmed).map(p => ({page: p.page, rotation: 0, src: ''}));
    this.setData({orientationPages: pages, orientationReady: false});
    try {
      for (const page of pages) {
        const data = await api.getOrientationPreview(this.data.taskId, run.id, page.page);
        const path = wx.env.USER_DATA_PATH + '/orientation-' + this.data.taskId + '-' + page.page + '.jpg';
        await new Promise((resolve, reject) => wx.getFileSystemManager().writeFile({
          filePath: path, data: data.preview.split(',')[1], encoding: 'base64', success: resolve, fail: reject,
        }));
        page.src = path;
      }
      this.setData({orientationPages: pages, orientationReady: true});
    } catch (e) {
      wx.showToast({title: '预览读取失败，请刷新重试', icon: 'none'});
    }
  },

  rotateOrientation(e) {
    const page = Number(e.currentTarget.dataset.page);
    this.setData({orientationPages: this.data.orientationPages.map(p =>
      p.page === page ? {...p, rotation: (p.rotation + 90) % 360} : p)});
  },

  async confirmOrientation() {
    if (this.data.submitting || !this.data.orientationReady) return;
    this.setData({submitting: true});
    try {
      await api.confirmOrientation(this.data.taskId, {run_id: this.orientationRunId,
        rotations: this.data.orientationPages.map(p => ({page: p.page, rotation: p.rotation}))});
      this.setData({orientationPages: [], orientationReady: false});
      this.refresh();
    } catch (e) {
      wx.showToast({title: e.message || '确认失败', icon: 'none'});
    } finally { this.setData({submitting: false}); }
  },

  refresh() {
    this.setData({ exhausted: false });
    this.startedAt = Date.now();
    this.delay = 0;
    this.tick();
  },

  buildDeliveryRows(result) {
    if (!result || !result.delivery) return [];
    const d = result.delivery;
    const rows = [];
    const push = (key, label) => {
      const item = d[key];
      if (!item) return;
      rows.push({
        key, label,
        status: item.status,
        text: DELIVERY_LABEL[item.status] || item.status,
        note: item.note || '',
      });
    };
    push('archive', '学习记录归档');
    push('pdf', 'PDF 交付');
    push('email', '邮件交付');
    push('git', '记录同步');
    return rows;
  },

  // 二次复查汇总视图：旧结果没有这些字段时返回 null（不显示该卡片）
  buildReviewView(result) {
    const summary = (result && result.review_summary) || null;
    if (!summary || !summary.state || summary.state === 'not_required') return null;
    const counts = [];
    if (summary.target_count) counts.push(`应复查 ${summary.target_count} 题`);
    if (summary.scope) counts.push(`送审 ${summary.scope} 题`);
    if (summary.disagreed) counts.push(`有异议 ${summary.disagreed} 题`);
    if (summary.unverified) counts.push(`无法核查 ${summary.unverified} 题`);
    if (summary.unprocessed) counts.push(`未送审 ${summary.unprocessed} 题`);
    const modelBits = [];
    if (summary.model_requested) modelBits.push(`请求 ${summary.model_requested}`);
    if (summary.model_reported) modelBits.push(`实际 ${summary.model_reported}`);
    if (summary.model_requested && summary.model_identity)
      modelBits.push(REVIEW_IDENTITY_TEXT[summary.model_identity] || summary.model_identity);
    return {
      stateText: REVIEW_SUMMARY_TEXT[summary.state] || summary.state,
      counts: counts.join(' · '),
      modelLine: modelBits.join('，'),
      note: summary.note || '',
    };
  },

  onFollowupInput(e) { this.setData({ followupText: e.detail.value }); },

  chooseFollowupImages() {
    const remain = 9 - this.data.followupImages.length;
    if (remain <= 0) return;
    wx.chooseMedia({
      count: remain,
      mediaType: ['image'],
      sourceType: ['camera', 'album'],
      success: (res) => {
        this.setData({
          followupImages: this.data.followupImages.concat(
            res.tempFiles.map((f) => ({ path: f.tempFilePath, assetId: '' }))),
        });
      },
    });
  },

  removeFollowupImage(e) {
    const index = Number(e.currentTarget.dataset.index);
    const list = this.data.followupImages.slice();
    list.splice(index, 1);
    this.setData({ followupImages: list });
  },

  async submitFollowup() {
    const text = (this.data.followupText || '').trim();
    if (!text && this.data.followupImages.length === 0) {
      wx.showToast({ title: '请填写补充内容或上传图片', icon: 'none' });
      return;
    }
    this.setData({ submitting: true });
    wx.showLoading({ title: '提交中', mask: true });
    try {
      const ids = [];
      for (let i = 0; i < this.data.followupImages.length; i += 1) {
        const item = this.data.followupImages[i];
        if (item.assetId) { ids.push(item.assetId); continue; }
        const res = await api.uploadAsset(item.path);
        ids.push(res.asset_id);
      }
      await api.createFollowup(this.data.taskId, { text, asset_ids: ids });
      this.setData({ followupText: '', followupImages: [], exhausted: false });
      this.startedAt = Date.now();
      this.delay = 0;
      this.tick();
    } catch (e) {
      wx.showModal({ title: '提交失败', content: e.message || '未知错误', showCancel: false });
    } finally {
      wx.hideLoading();
      this.setData({ submitting: false });
    }
  },

  async addMistake(e) {
    const q = e.currentTarget.dataset.q;
    wx.showLoading({ title: '保存中' });
    try {
      await api.addMistake({
        task_id: this.data.taskId,
        question_no: q.no || q.id,
        knowledge_point: q.knowledge_point || '',
        note: '',
      });
      wx.showToast({ title: '已加入错题本' });
    } catch (err) {
      wx.showToast({ title: '保存失败', icon: 'none' });
    } finally {
      wx.hideLoading();
    }
  },

  // 登记一次真实发生的复测结果（只追加事件，不改写历史判定）
  retest(e) {
    const q = e.currentTarget.dataset.q;
    if (!q.ledgerId) {
      wx.showToast({ title: '该题尚未写入台账', icon: 'none' });
      return;
    }
    wx.showActionSheet({
      itemList: RETEST_ACTIONS.map((a) => a.label),
      success: (res) => {
        const action = RETEST_ACTIONS[res.tapIndex];
        if (action) this.submitRetest(q.ledgerId, action.result);
      },
    });
  },

  async submitRetest(entryId, result) {
    wx.showLoading({ title: '登记中', mask: true });
    try {
      const res = await api.addLedgerEvent(entryId, { result });
      wx.hideLoading();
      wx.showToast({
        title: '已登记：' + (REMEDIATION_LABEL[res.remediation_state] || ''),
        icon: 'none',
      });
      this.tick();
    } catch (err) {
      wx.hideLoading();
      wx.showModal({ title: '登记失败', content: err.message || '未知错误', showCancel: false });
    }
  },

  // 成果文件下载：pdf 直接打开；md 读出来展示并支持复制
  async downloadArtifact(e) {
    const artifact = e.currentTarget.dataset.a;
    const url = api.artifactUrl(this.data.taskId, artifact.id);
    this.setData({ downloading: true });
    wx.showLoading({ title: '获取中', mask: true });
    try {
      const res = await new Promise((resolve, reject) => {
        wx.downloadFile({
          url,
          header: { Authorization: 'Bearer ' + api.getToken() },
          success: resolve,
          fail: reject,
        });
      });
      if (res.statusCode >= 400) throw new Error('下载失败 ' + res.statusCode);
      if (artifact.kind === 'pdf') {
        wx.openDocument({ filePath: res.tempFilePath, fileType: 'pdf', showMenu: true });
      } else {
        const content = await new Promise((resolve, reject) => {
          wx.getFileSystemManager().readFile({
            filePath: res.tempFilePath, encoding: 'utf8',
            success: (r) => resolve(r.data), fail: reject,
          });
        });
        wx.setClipboardData({
          data: content,
          success: () => wx.showModal({
            title: '归档内容已复制',
            content: '已复制到剪贴板，可粘贴到笔记或发给家人。',
            showCancel: false,
          }),
        });
      }
    } catch (err) {
      wx.showToast({ title: '获取失败', icon: 'none' });
    } finally {
      wx.hideLoading();
      this.setData({ downloading: false });
    }
  },

  retry() { wx.navigateBack(); },
});
