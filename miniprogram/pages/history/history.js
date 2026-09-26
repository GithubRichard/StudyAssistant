const api = require('../../utils/api');

const STATUS_TEXT = { pending: '排队中', grading: '批改中', done: '已完成', failed: '失败' };

Page({
  data: { tasks: [], statusText: STATUS_TEXT },

  onShow() {
    this.load();
  },

  async load() {
    try {
      const openid = await api.ensureLogin();
      const tasks = await api.getTasks(openid);
      this.setData({
        tasks: tasks.map((t) => ({
          ...t,
          time: new Date(t.created_at * 1000).toLocaleString('zh-CN', { hour12: false }),
        })),
      });
    } catch (e) {
      console.warn('加载历史失败:', e);
    }
  },

  goResult(e) {
    wx.navigateTo({ url: '/pages/result/result?task_id=' + e.currentTarget.dataset.id });
  },
});
