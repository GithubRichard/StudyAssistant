const api = require('../../utils/api');

const TASK_TYPES = [
  { key: 'grading', label: '作业批改', hint: '拍作业照，逐题批改并给出错因' },
  { key: 'qa', label: '学习问答', hint: '问概念或解题方法，可附题目照片' },
  { key: 'weekly_report', label: '周报分析', hint: '按日期区间汇总学习情况' },
  { key: 'training', label: '针对性训练', hint: '按历史错题出题（月考/期中/期末）' },
  { key: 'retest', label: '复测', hint: '记录实际作答结果' },
];
const NEEDS_SCOPE = { weekly_report: true, training: true, retest: true };
const MAX_IMAGES = 9;

const RUNTIME_TEXT = {
  ready: '技能服务已就绪',
  auth_failed: '服务端与 Hermes 的密钥不一致，请检查配置',
  skill_unknown: '无法确认技能是否已安装（技能列表接口异常），任务仍会尝试执行',
  skill_missing: 'Hermes 未加载学习技能，任务可能无法完成',
  unreachable: '暂时连不上 Hermes，请稍后再试',
  not_configured: '服务端尚未配置 Hermes，无法执行学习任务',
  legacy: '当前为旧直连模式：不执行技能流程与二次核查',
  unknown: '运行状态未知',
};

Page({
  data: {
    taskTypes: TASK_TYPES,
    typeIndex: 0,
    taskHint: TASK_TYPES[0].hint,
    needsScope: false,
    subjects: ['数学', '语文', '英语', '物理', '化学'],
    subjectIndex: 0,
    grades: ['七年级', '八年级', '九年级', '高一', '高二', '高三'],
    gradeIndex: 0,
    text: '',
    images: [],
    maxImages: MAX_IMAGES,
    scopeStart: '',
    scopeEnd: '',
    submitting: false,
    runtimeText: '正在检查服务状态…',
    runtimeOk: true,
  },

  onShow() {
    this.checkRuntime();
  },

  async checkRuntime() {
    try {
      const rt = await api.getRuntime();
      let state = 'unknown';
      if (rt.engine && rt.engine.mode === 'legacy') state = 'legacy';
      else if (rt.hermes) state = rt.hermes.state;
      this.setData({
        runtimeText: RUNTIME_TEXT[state] || RUNTIME_TEXT.unknown,
        runtimeOk: state === 'ready',
      });
    } catch (e) {
      this.setData({ runtimeText: '无法获取服务状态，请检查服务器地址', runtimeOk: false });
    }
  },

  onTypeChange(e) {
    const index = Number(e.currentTarget.dataset.index);
    this.setData({
      typeIndex: index,
      taskHint: TASK_TYPES[index].hint,
      needsScope: !!NEEDS_SCOPE[TASK_TYPES[index].key],
    });
  },
  onSubjectChange(e) { this.setData({ subjectIndex: Number(e.detail.value) }); },
  onGradeChange(e) { this.setData({ gradeIndex: Number(e.detail.value) }); },
  onTextInput(e) { this.setData({ text: e.detail.value }); },
  onScopeStart(e) { this.setData({ scopeStart: e.detail.value }); },
  onScopeEnd(e) { this.setData({ scopeEnd: e.detail.value }); },

  chooseImages() {
    const remain = MAX_IMAGES - this.data.images.length;
    if (remain <= 0) {
      wx.showToast({ title: `最多 ${MAX_IMAGES} 张`, icon: 'none' });
      return;
    }
    wx.chooseMedia({
      count: remain,
      mediaType: ['image'],
      sourceType: ['camera', 'album'],
      success: (res) => {
        const added = res.tempFiles.map((f) => ({
          path: f.tempFilePath, assetId: '', status: 'pending',
        }));
        this.setData({ images: this.data.images.concat(added) });
      },
    });
  },

  removeImage(e) {
    const index = Number(e.currentTarget.dataset.index);
    const images = this.data.images.slice();
    images.splice(index, 1);
    this.setData({ images });
  },

  previewImage(e) {
    const index = Number(e.currentTarget.dataset.index);
    wx.previewImage({
      current: this.data.images[index].path,
      urls: this.data.images.map((i) => i.path),
    });
  },

  // 逐张上传，状态逐张反馈；已上传成功的不会重复上传
  async uploadPending() {
    const images = this.data.images;
    for (let i = 0; i < images.length; i += 1) {
      if (images[i].assetId) continue;
      const key = `images[${i}].status`;
      this.setData({ [key]: 'uploading' });
      try {
        const res = await api.uploadAsset(images[i].path);
        this.setData({ [`images[${i}].assetId`]: res.asset_id, [`images[${i}].status`]: 'ready' });
      } catch (err) {
        this.setData({ [`images[${i}].status`]: 'failed' });
        throw err;
      }
    }
  },

  newIdempotencyKey() {
    return 'mp-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
  },

  async submit() {
    const type = this.data.taskTypes[this.data.typeIndex].key;
    const text = (this.data.text || '').trim();
    if (this.data.images.length === 0 && !text) {
      wx.showToast({ title: '请上传图片或填写说明', icon: 'none' });
      return;
    }
    if (type === 'grading' && this.data.images.length === 0) {
      wx.showToast({ title: '作业批改需要至少一张照片', icon: 'none' });
      return;
    }

    this.setData({ submitting: true });
    wx.showLoading({ title: '提交中', mask: true });
    try {
      await api.ensureLogin();
      await this.uploadPending();
      const assetIds = this.data.images.map((i) => i.assetId).filter(Boolean);
      // 失败重试时沿用同一幂等键，避免重复创建任务
      if (!this.pendingKey) this.pendingKey = this.newIdempotencyKey();
      const created = await api.createStudyTask({
        task_type: type,
        subject: this.data.subjects[this.data.subjectIndex],
        grade_level: this.data.grades[this.data.gradeIndex],
        text,
        asset_ids: assetIds,
        scope_start: this.data.scopeStart,
        scope_end: this.data.scopeEnd,
      }, this.pendingKey);
      this.pendingKey = '';
      this.setData({ images: [], text: '' });
      wx.navigateTo({ url: '/pages/result/result?task_id=' + created.task_id });
    } catch (e) {
      wx.showModal({
        title: '提交失败',
        content: (e.message || '未知错误') + '\n\n已上传的图片不会重复上传，可直接重试。',
        showCancel: false,
      });
    } finally {
      wx.hideLoading();
      this.setData({ submitting: false });
    }
  },
});
