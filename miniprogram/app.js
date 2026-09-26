const api = require('./utils/api');

App({
  async onLaunch() {
    // 启动即登录，拿到 openid 存本地，后续接口都用它
    try {
      await api.ensureLogin();
    } catch (e) {
      console.warn('自动登录失败，进入页面时会重试:', e);
    }
  },
  globalData: {}
});
