const api = require('../../utils/api');

// 任务类型与规范一致：批改 / 问答 / 周报 / 训练（专项·月考·期中·期末）/ 复测
const TASK_TYPES = [
  { key: 'grading', label: '作业批改', needsScope: false, needsKind: false, needsExamScope: false,
    hint: '拍作业照，逐题批改并解释错因；默认归入当天该学科的错题解析' },
  { key: 'qa', label: '学习问答', needsScope: false, needsKind: false, needsExamScope: false,
    hint: '问概念或解题方法，可附题目照片；有价值时归入当天记录' },
  { key: 'weekly_report', label: '周报分析', needsScope: true, needsKind: false, needsExamScope: false,
    hint: '汇总一周学习情况；未指定区间时默认本周一至今天' },
  { key: 'training', label: '针对性训练', needsScope: true, needsKind: true, needsExamScope: true,
    hint: '依据历史错题出题；请选择专项 / 月考 / 期中 / 期末' },
  { key: 'retest', label: '复测', needsScope: false, needsKind: false, needsExamScope: false,
    hint: '孩子真实作答后记录复测结果；答对一次不等于稳定掌握' },
];
const TRAINING_KINDS = [
  { key: 'topic', label: '专项' },
  { key: 'monthly', label: '月考' },
  { key: 'midterm', label: '期中' },
  { key: 'final', label: '期末' },
];
const GRADES = ['七年级', '八年级', '九年级', '高一', '高二', '高三'];
const UNSPECIFIED = '未指定';
// 学科留空交给模型按材料判断：试卷本身就能推断学科，不必让用户先选
const AUTO_SUBJECT = '自动识别（按试卷判断）';
const MAX_IMAGES = 9;

const RUNTIME_TEXT = {
  ready: '技能服务已就绪',
  skill_unknown: '无法确认技能是否已安装（技能列表接口异常），任务仍会尝试执行',
  skill_missing: 'Hermes 未加载学习技能，任务可能无法完成',
  unreachable: '暂时连不上 Hermes，请稍后再试',
  not_configured: '服务端尚未配置 Hermes，无法执行学习任务',
  legacy: '当前为旧直连模式：不执行技能流程与二次核查',
  unknown: '运行状态未知',
};

function pad(n) {
  return n < 10 ? '0' + n : '' + n;
}

function isoDate(d) {
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

Page({
  data: {
    taskTypes: TASK_TYPES,
    typeIndex: 0,
    taskHint: TASK_TYPES[0].hint,
    needsScope: false,
    needsKind: false,
    needsExamScope: false,
    trainingKinds: TRAINING_KINDS,
    kindIndex: 0,
    subjects: [AUTO_SUBJECT],
    subjectIndex: 0,
    grades: [UNSPECIFIED].concat(GRADES),
    gradeIndex: 0,
    termStart: '',
    settingsSource: '',
    text: '',
    examScope: '',
    images: [],
    maxImages: MAX_IMAGES,
    scopeStart: '',
    scopeEnd: '',
    scopeHint: '',
    submitting: false,
    runtimeText: '正在检查服务状态…',
    runtimeOk: true,
  },

  onShow() {
    this.checkRuntime();
    this.loadSettings();
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

  async loadSettings() {
    try {
      const st = await api.getSettings();
      const subjectList = (st.subjects && st.subjects.length) ? st.subjects : [];
      const subjects = [AUTO_SUBJECT].concat(subjectList);
      const current = this.data.subjects[this.data.subjectIndex];
      const keep = subjects.indexOf(current);
      this.setData({
        subjects,
        subjectIndex: keep >= 0 ? keep : 0,
        termStart: st.term_start_date || '',
        settingsSource: st.source || '',
      });
      this.refreshScopeHint();
    } catch (e) {
      // 设置读不到不影响提交；如实提示来源
      this.setData({ subjects: [AUTO_SUBJECT, '语文', '数学', '英语'], settingsSource: 'unavailable' });
      this.refreshScopeHint();
    }
  },

  refreshScopeHint() {
    const type = this.data.taskTypes[this.data.typeIndex];
    const kind = TRAINING_KINDS[this.data.kindIndex].key;
    const today = new Date();
    const todayStr = isoDate(today);
    let hint = '本次不限定资料区间：按需读取当天记录与必要历史';

    if (this.data.scopeStart || this.data.scopeEnd) {
      hint = '已按你填写的区间执行（' + (this.data.scopeStart || '不限') + ' ~ '
        + (this.data.scopeEnd || todayStr) + '）';
    } else if (type.key === 'weekly_report') {
      const monday = new Date(today.getTime() - ((today.getDay() + 6) % 7) * 86400000);
      hint = '默认区间：' + isoDate(monday) + ' ~ ' + todayStr + '（本周一至今天）';
    } else if (type.key === 'training' && kind === 'monthly') {
      hint = '默认区间：' + isoDate(new Date(today.getFullYear(), today.getMonth(), 1))
        + ' ~ ' + todayStr + '（当月 1 日至今天）';
    } else if (type.key === 'training' && (kind === 'midterm' || kind === 'final')) {
      hint = this.data.termStart
        ? '默认区间：' + this.data.termStart + ' ~ ' + todayStr + '（本学期开学至今天）'
        : '缺少本学期开学日期：请到「我的 → 学习设置」填写，否则无法按整学期累计记录筛选';
    } else if (type.key === 'training') {
      hint = '专项训练不限定区间：按需读取相关错题与复测记录';
    }
    this.setData({ scopeHint: hint });
  },

  onTypeChange(e) {
    const index = Number(e.currentTarget.dataset.index);
    const type = TASK_TYPES[index];
    this.setData({
      typeIndex: index,
      taskHint: type.hint,
      needsScope: !!type.needsScope,
      needsKind: !!type.needsKind,
      needsExamScope: !!type.needsExamScope,
    }, () => this.refreshScopeHint());
  },

  onKindChange(e) {
    this.setData({ kindIndex: Number(e.currentTarget.dataset.index) },
      () => this.refreshScopeHint());
  },

  onSubjectChange(e) { this.setData({ subjectIndex: Number(e.detail.value) }); },
  onGradeChange(e) { this.setData({ gradeIndex: Number(e.detail.value) }); },
  onTextInput(e) { this.setData({ text: e.detail.value }); },
  onExamScopeInput(e) { this.setData({ examScope: e.detail.value }); },
  onScopeStart(e) { this.setData({ scopeStart: e.detail.value }, () => this.refreshScopeHint()); },
  onScopeEnd(e) { this.setData({ scopeEnd: e.detail.value }, () => this.refreshScopeHint()); },

  goSettings() { wx.navigateTo({ url: '/pages/settings/settings' }); },

  chooseImages() {
    const remain = MAX_IMAGES - this.data.images.length;
    if (remain <= 0) {
      wx.showToast({ title: '最多 ' + MAX_IMAGES + ' 张', icon: 'none' });
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
      const key = 'images[' + i + '].status';
      this.setData({ [key]: 'uploading' });
      try {
        const res = await api.uploadAsset(images[i].path);
        this.setData({ ['images[' + i + '].assetId']: res.asset_id, [key]: 'ready' });
      } catch (err) {
        this.setData({ [key]: 'failed' });
        throw err;
      }
    }
  },

  newIdempotencyKey() {
    return 'mp-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
  },

  async submit() {
    const type = this.data.taskTypes[this.data.typeIndex];
    const text = (this.data.text || '').trim();
    if (this.data.images.length === 0 && !text) {
      wx.showToast({ title: '请上传图片或填写说明', icon: 'none' });
      return;
    }
    if (type.key === 'grading' && this.data.images.length === 0) {
      wx.showToast({ title: '作业批改需要至少一张照片', icon: 'none' });
      return;
    }

    this.setData({ submitting: true });
    wx.showLoading({ title: '提交中', mask: true });
    try {
      await api.ensureLogin();
      await this.uploadPending();
      const assetIds = this.data.images.map((i) => i.assetId).filter(Boolean);
      const subject = this.data.subjects[this.data.subjectIndex];
      const grade = this.data.grades[this.data.gradeIndex];
      // 失败重试时沿用同一幂等键，避免重复创建任务
      if (!this.pendingKey) this.pendingKey = this.newIdempotencyKey();
      const created = await api.createStudyTask({
        task_type: type.key,
        subject: subject === AUTO_SUBJECT ? '' : subject,
        grade_level: grade === UNSPECIFIED ? '' : grade,
        training_kind: type.needsKind ? TRAINING_KINDS[this.data.kindIndex].key : '',
        exam_scope: type.needsExamScope ? (this.data.examScope || '').trim() : '',
        text,
        asset_ids: assetIds,
        scope_start: this.data.scopeStart,
        scope_end: this.data.scopeEnd,
      }, this.pendingKey);
      this.pendingKey = '';
      this.setData({ images: [], text: '', examScope: '' });
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
