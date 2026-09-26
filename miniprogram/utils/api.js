// 后端 API 封装：所有网络请求都走这里
const { BASE_URL } = require('./config');

function request(path, { method = 'GET', data = {} } = {}) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + path,
      method,
      data,
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
        } else {
          const msg = (res.data && (res.data.detail || res.data.msg)) || ('请求失败 ' + res.statusCode);
          reject(new Error(msg));
        }
      },
      fail: () => reject(new Error('网络错误：请检查服务器地址配置和网络')),
    });
  });
}

function formRequest(path, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + path,
      method: 'POST',
      header: { 'content-type': 'application/x-www-form-urlencoded' },
      data,
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
        else reject(new Error((res.data && res.data.detail) || '请求失败'));
      },
      fail: () => reject(new Error('网络错误')),
    });
  });
}

function getOpenid() {
  return wx.getStorageSync('openid') || '';
}

// 确保已登录：有 openid 直接用，没有就 wx.login 换一个
function ensureLogin() {
  const cached = getOpenid();
  if (cached) return Promise.resolve(cached);
  return new Promise((resolve, reject) => {
    wx.login({
      success: async (res) => {
        try {
          const data = await formRequest('/api/login', { code: res.code });
          wx.setStorageSync('openid', data.openid);
          resolve(data.openid);
        } catch (e) {
          reject(e);
        }
      },
      fail: reject,
    });
  });
}

// 上传作业图，返回 { task_id, status }
function uploadTask({ openid, subject, gradeLevel, filePath }) {
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: BASE_URL + '/api/tasks',
      filePath,
      name: 'file',
      formData: { openid, subject, grade_level: gradeLevel },
      success: (res) => {
        let data;
        try {
          data = JSON.parse(res.data);
        } catch (e) {
          return reject(new Error('服务器返回异常'));
        }
        if (res.statusCode === 201) resolve(data);
        else reject(new Error(data.detail || '上传失败'));
      },
      fail: () => reject(new Error('上传失败，请检查网络')),
    });
  });
}

const getTask = (taskId) => request('/api/tasks/' + taskId);
const getTasks = (openid, limit = 20) =>
  request('/api/tasks', { data: { openid, limit } });
const getQuota = (openid) => request('/api/quota', { data: { openid } });
const getProviders = () => request('/api/providers');

function addMistake({ openid, task_id, question_no, knowledge_point, note }) {
  return formRequest('/api/mistakes', { openid, task_id, question_no, knowledge_point, note });
}
const getMistakes = (openid, limit = 100) =>
  request('/api/mistakes', { data: { openid, limit } });

function logout() {
  wx.removeStorageSync('openid');
}

module.exports = {
  ensureLogin, getOpenid, logout,
  uploadTask, getTask, getTasks, getQuota, getProviders,
  addMistake, getMistakes,
};
