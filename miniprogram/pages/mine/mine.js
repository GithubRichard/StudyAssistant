const api = require('../../utils/api');
const { BASE_URL } = require('../../utils/config');

Page({
  data: {
    openid: '',
    remaining: '-',
    providers: [],
    baseUrl: BASE_URL,
  },

  onShow() {
    this.load();
  },

  async load() {
    try {
      const openid = await api.ensureLogin();
      this.setData({ openid });
      const [{ remaining }, { providers, chain }] = await Promise.all([
        api.getQuota(openid),
        api.getProviders(),
      ]);
      this.setData({
        remaining,
        providers: providers.map((p) => ({ ...p, active: chain.includes(p.name) })),
      });
    } catch (e) {
      console.warn('加载我的页面失败:', e);
    }
  },

  copyBaseUrl() {
    wx.setClipboardData({ data: this.data.baseUrl });
  },

  relogin() {
    wx.showModal({
      title: '切换账号',
      content: '将清除本地登录信息并重新登录',
      success: (res) => {
        if (res.confirm) {
          api.logout();
          this.load();
        }
      },
    });
  },
});
