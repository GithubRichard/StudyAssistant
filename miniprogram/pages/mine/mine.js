const api = require('../../utils/api');
const { BASE_URL } = require('../../utils/config');

const ENGINE_TEXT = {
  hermes: 'Hermes 技能引擎',
  legacy: '旧直连模式（不执行技能流程与二次核查）',
};
const HERMES_TEXT = {
  ready: '就绪（技能已安装）',
  skill_missing: '技能未安装',
  unreachable: '连不上 Hermes',
  not_configured: '未配置 Hermes',
};

Page({
  data: {
    openid: '',
    remaining: '-',
    engineText: '-',
    hermesText: '-',
    hermesOk: false,
    baseUrl: BASE_URL,
    delivery: [],
    limits: null,
    workspace: null,
    error: '',
  },

  onShow() { this.load(); },

  async load() {
    try {
      await api.ensureLogin();
      this.setData({ openid: api.getOpenid() });
      const [quota, runtime] = await Promise.all([api.getQuota(), api.getRuntime()]);
      const hermes = runtime.hermes || {};
      const delivery = Object.keys(runtime.delivery || {}).map((k) => ({
        key: k,
        label: { pdf: 'PDF 交付', email: '邮件交付', git: '记录同步' }[k] || k,
        enabled: runtime.delivery[k],
      }));
      this.setData({
        remaining: quota.remaining,
        engineText: ENGINE_TEXT[runtime.engine.mode] || runtime.engine.mode,
        hermesText: HERMES_TEXT[hermes.state] || (runtime.engine.mode === 'legacy' ? '不适用' : '未知'),
        hermesOk: hermes.state === 'ready',
        delivery,
        limits: runtime.limits,
        workspace: runtime.workspace,
        error: hermes.detail || '',
      });
    } catch (e) {
      this.setData({ error: e.message || '加载失败' });
      console.warn('加载我的页面失败:', e);
    }
  },

  copyBaseUrl() {
    wx.setClipboardData({ data: this.data.baseUrl });
  },

  copyOpenid() {
    if (this.data.openid) wx.setClipboardData({ data: this.data.openid });
  },

  relogin() {
    wx.showModal({
      title: '重新登录',
      content: '将清除本地会话并重新登录（不会删除学习记录）',
      success: async (res) => {
        if (res.confirm) {
          await api.logout();
          this.load();
        }
      },
    });
  },
});
