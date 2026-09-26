const api = require('./utils/api');

App({
  async onLaunch() {
    // 启动即登录拿会话令牌；失败不阻塞，进入页面时重新尝试
    try {
      await api.ensureLogin();
    } catch (e) {
      console.warn('自动登录失败，进入页面时会重试:', e);
    }
  },
  globalData: {
    runtime: null,
  },
});
