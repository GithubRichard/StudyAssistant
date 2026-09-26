// 后端 API 封装：会话令牌鉴权 + 学习任务接口
const { BASE_URL } = require('./config');

const TOKEN_KEY = 'session_token';
const OPENID_KEY = 'openid';

function getToken() {
  return wx.getStorageSync(TOKEN_KEY) || '';
}

function getOpenid() {
  return wx.getStorageSync(OPENID_KEY) || '';
}

function clearSession() {
  wx.removeStorageSync(TOKEN_KEY);
  wx.removeStorageSync(OPENID_KEY);
}

function authHeader(extra) {
  const header = Object.assign({}, extra || {});
  const token = getToken();
  if (token) header.Authorization = 'Bearer ' + token;
  return header;
}

function errorText(res, fallback) {
  const data = res && res.data;
  if (data && typeof data === 'object' && data.detail) {
    return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
  }
  return (res && res.statusCode ? fallback + ' ' + res.statusCode : fallback);
}

// BASE_URL 还是占位符时给出明确提示，避免出现 ERR_NAME_NOT_RESOLVED 这类难懂的报错
const PLACEHOLDER_HINT =
  '请先修改 miniprogram/utils/config.js 里的 BASE_URL 为你的服务器地址（调试可用 http://服务器IP:8000）';

function baseUrlError() {
  if (!BASE_URL || BASE_URL.indexOf('你的域名') >= 0 || BASE_URL.indexOf('example.com') >= 0) {
    return new Error(PLACEHOLDER_HINT);
  }
  return null;
}

// 需要登录的请求：401 时自动重新登录并重试一次
function request(path, options = {}, retried = false) {
  const { method = 'GET', data = {}, header = {} } = options;
  const invalid = baseUrlError();
  if (invalid) return Promise.reject(invalid);
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + path,
      method,
      data,
      header: authHeader(header),
      success: async (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
          return;
        }
        if (res.statusCode === 401 && !retried) {
          clearSession();
          try {
            await login();
            resolve(await request(path, options, true));
          } catch (e) {
            reject(e);
          }
          return;
        }
        reject(new Error(errorText(res, '请求失败')));
      },
      fail: () => reject(new Error('网络错误：请检查服务器地址配置和网络')),
    });
  });
}

function requestJson(path, body, header) {
  return request(path, {
    method: 'POST',
    data: body,
    header: Object.assign({ 'content-type': 'application/json' }, header || {}),
  });
}

function formRequest(path, data, header) {
  return request(path, {
    method: 'POST',
    data,
    header: Object.assign({ 'content-type': 'application/x-www-form-urlencoded' }, header || {}),
  });
}

// 登录：code 换会话令牌。并发调用复用同一个请求，避免多次 wx.login 抢跑
let loginPromise = null;

function login() {
  if (loginPromise) return loginPromise;
  loginPromise = new Promise((resolve, reject) => {
    wx.login({
      success: async (res) => {
        try {
          const data = await formRequest(
            '/api/login', { code: res.code }, { Authorization: '' });
          if (!data.token) {
            reject(new Error('登录失败：服务器未返回会话令牌'));
            return;
          }
          wx.setStorageSync(TOKEN_KEY, data.token);
          wx.setStorageSync(OPENID_KEY, data.openid);
          resolve(data);
        } catch (e) {
          reject(e);
        }
      },
      fail: () => reject(new Error('wx.login 调用失败')),
    });
  });
  const done = () => { loginPromise = null; };
  loginPromise.then(done, done);
  return loginPromise;
}

function ensureLogin() {
  if (getToken()) return Promise.resolve(getOpenid());
  return login().then((data) => data.openid);
}

function logout() {
  return request('/api/logout', { method: 'POST' })
    .catch(() => null)
    .then(() => clearSession());
}

// 上传单张图片，返回 { asset_id }
function uploadAsset(filePath) {
  const invalid = baseUrlError();
  if (invalid) return Promise.reject(invalid);
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: BASE_URL + '/api/assets',
      filePath,
      name: 'file',
      header: authHeader({}),
      success: (res) => {
        let data;
        try {
          data = JSON.parse(res.data);
        } catch (e) {
          reject(new Error('服务器返回异常'));
          return;
        }
        if (res.statusCode === 201 && data.asset_id) resolve(data);
        else reject(new Error(data.detail || ('上传失败 ' + res.statusCode)));
      },
      fail: () => reject(new Error('上传失败，请检查网络')),
    });
  });
}

// 提交学习任务（幂等：同一 idempotencyKey 重复提交返回同一任务）
function createStudyTask(payload, idempotencyKey) {
  return requestJson('/api/study/tasks', payload, { 'Idempotency-Key': idempotencyKey });
}

function createFollowup(taskId, payload) {
  return requestJson('/api/tasks/' + taskId + '/followups', payload);
}

const getTask = (taskId) => request('/api/tasks/' + taskId);
const getTasks = (limit = 20, offset = 0) =>
  request('/api/tasks', { data: { limit, offset } });
const getQuota = () => request('/api/quota');
const getRuntime = () => request('/api/runtime');
const getProviders = () => request('/api/providers');

// 家庭设置（学期起始日期、年级、学科清单）
const getSettings = () => request('/api/settings');
const updateSettings = (payload) =>
  request('/api/settings', {
    method: 'PUT',
    data: payload,
    header: { 'content-type': 'application/json' },
  });

// 复习台账与复测登记
const getLedger = (params = {}) => request('/api/ledger', { data: params });
const getLedgerEntry = (entryId) => request('/api/ledger/' + entryId);
const addLedgerEvent = (entryId, payload) =>
  requestJson('/api/ledger/' + entryId + '/events', payload);

function addMistake({ task_id, question_no, knowledge_point, note }) {
  return formRequest('/api/mistakes', { task_id, question_no, knowledge_point, note });
}
const getMistakes = (limit = 100, offset = 0) =>
  request('/api/mistakes', { data: { limit, offset } });

function artifactUrl(taskId, artifactId) {
  return BASE_URL + '/api/tasks/' + taskId + '/artifacts/' + artifactId;
}

module.exports = {
  ensureLogin, login, logout, getOpenid, getToken,
  uploadAsset, createStudyTask, createFollowup,
  getTask, getTasks, getQuota, getRuntime, getProviders,
  getSettings, updateSettings,
  getLedger, getLedgerEntry, addLedgerEvent,
  addMistake, getMistakes, artifactUrl,
};
