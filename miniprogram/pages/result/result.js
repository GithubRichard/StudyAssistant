const api = require('../../utils/api');

Page({
  data: {
    taskId: '',
    status: 'pending', // pending | grading | done | failed
    result: null,
    error: '',
  },

  onLoad(options) {
    this.setData({ taskId: options.task_id || '' });
    this.poll();
  },

  onUnload() {
    this.clearTimers();
  },

  clearTimers() {
    if (this.timer) clearInterval(this.timer);
    if (this.timeout) clearTimeout(this.timeout);
    this.timer = this.timeout = null;
  },

  poll() {
    const tick = async () => {
      try {
        const t = await api.getTask(this.data.taskId);
        this.setData({ status: t.status, result: t.result, error: t.error || '' });
        if (t.status === 'done' || t.status === 'failed') this.clearTimers();
      } catch (e) {
        // 网络抖动就等下一次轮询，不打断用户
        console.warn('轮询失败，稍后重试:', e);
      }
    };
    tick();
    this.timer = setInterval(tick, 2500);
    this.timeout = setTimeout(() => this.clearTimers(), 180000); // 最多 3 分钟
  },

  async addMistake(e) {
    const q = e.currentTarget.dataset.q;
    wx.showLoading({ title: '保存中' });
    try {
      const openid = await api.ensureLogin();
      await api.addMistake({
        openid,
        task_id: this.data.taskId,
        question_no: q.no,
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

  retry() {
    wx.navigateBack();
  },
});
