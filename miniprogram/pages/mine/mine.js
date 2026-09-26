const api = require('../../utils/api');
const { BASE_URL } = require('../../utils/config');

const ENGINE_TEXT = {
  hermes: 'Hermes 技能引擎',
  legacy: '旧直连模式（不执行技能流程与二次核查）',
};
const HERMES_TEXT = {
  ready: '就绪（技能已安装）',
  skill_unknown: '无法确认（技能列表接口异常）',
  skill_missing: '技能未安装',
  unreachable: '连不上 Hermes',
  not_configured: '未配置 Hermes',
};
const GIT_TEXT = {
  committed: '已提交并推送',
  failed: '同步失败',
  skipped: '已跳过（无变化）',
  not_configured: '未启用同步',
};
const DELIVERY_LABEL = { pdf: 'PDF 交付', email: '邮件交付', git: '学习记录同步' };

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
    familyText: '-',
    gitText: '-',
    gitOk: false,
    lastSyncNote: '',
    error: '',
  },

  onShow() { this.load(); },

  async load() {
    try {
      await api.ensureLogin();
      this.setData({ openid: api.getOpenid() });
      const [quota, runtime] = await Promise.all([api.getQuota(), api.getRuntime()]);
      const hermes = runtime.hermes || {};
      const git = runtime.git || {};
      const last = git.last_sync || null;
      const family = runtime.family || {};
      const ws = runtime.workspace || {};
      const delivery = Object.keys(runtime.delivery || {}).map((k) => ({
        key: k,
        label: DELIVERY_LABEL[k] || k,
        enabled: runtime.delivery[k],
      }));
      this.setData({
        remaining: quota.remaining,
        engineText: ENGINE_TEXT[runtime.engine.mode] || runtime.engine.mode,
        hermesText: HERMES_TEXT[hermes.state] || (runtime.engine.mode === 'legacy' ? '不适用' : '未知'),
        hermesOk: hermes.state === 'ready',
        delivery,
        limits: runtime.limits,
        workspace: ws,
        familyText: '学期开学 ' + (family.term_start_date || '未填写')
          + ' · 年级 ' + (family.default_grade_level || '未指定')
          + ' · 学科 ' + ((family.subjects || []).join('、') || '未配置'),
        gitText: git.enabled
          ? ('已启用（远端 ' + (git.remote || 'origin') + '）'
            + (last ? '｜最近一次：' + (GIT_TEXT[last.status] || last.status) : '｜尚未同步过'))
          : '未启用（记录只保存在工作区，不上传远端）',
        gitOk: !!(last && last.status === 'committed'),
        lastSyncNote: last ? (last.reason || '') : '',
        error: hermes.detail || '',
      });
    } catch (e) {
      this.setData({ error: e.message || '加载失败' });
      console.warn('加载我的页面失败:', e);
    }
  },

  goSettings() { wx.navigateTo({ url: '/pages/settings/settings' }); },

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
