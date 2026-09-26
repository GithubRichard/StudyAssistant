const api = require('../../utils/api');

Page({
  data: { mistakes: [] },

  onShow() {
    this.load();
  },

  async load() {
    try {
      await api.ensureLogin();
      const mistakes = await api.getMistakes(100, 0);
      this.setData({
        mistakes: mistakes.map((m) => ({
          ...m,
          time: new Date(m.created_at * 1000).toLocaleDateString('zh-CN'),
        })),
      });
    } catch (e) {
      console.warn('加载错题本失败:', e);
    }
  },

  // 复习台账是主入口：按知识点与订正状态聚合，并支持登记复测
  goReview() {
    wx.switchTab({ url: '/pages/review/review' });
  },

  goTask(e) {
    wx.navigateTo({ url: '/pages/result/result?task_id=' + e.currentTarget.dataset.task });
  },
});
