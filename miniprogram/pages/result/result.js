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
  agreed: '核查未发现异议', disagreed: '核查有异议', unverified: '无法核查',
  unprocessed: '未送核查', not_applicable: '未核查',
};
const DELIVERY_LABEL = {
  not_configured: '未配置', skipped: '已跳过', generated: '已生成',
  sent: '已请求发送', committed: '已提交', failed: '失败',
};
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
    deliveryRows: [],
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
      this.setData({
        status: t.status,
        statusText: STATUS_TEXT[t.status] || t.status,
        statusPill: STATUS_PILL[t.status] || 'pill-pending',
        result,
        runs: t.runs || [],
        artifacts: t.artifacts || [],
        error: t.error || '',
        missingInfo: (result && result.missing_info) || [],
        deliveryRows: this.buildDeliveryRows(result),
      });
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
