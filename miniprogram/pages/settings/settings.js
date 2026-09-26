const api = require('../../utils/api');

const GRADES = ['七年级', '八年级', '九年级', '高一', '高二', '高三'];
const UNSPECIFIED = '未指定';
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

Page({
  data: {
    grades: [UNSPECIFIED].concat(GRADES),
    gradeIndex: 0,
    termStartDate: '',
    subjects: [],
    newSubject: '',
    source: '',
    saving: false,
    runtimeText: '',
    workspaceText: '',
    gitText: '',
    lastSyncNote: '',
    loading: true,
  },

  onShow() { this.load(); },

  async load() {
    this.setData({ loading: true });
    try {
      await api.ensureLogin();
      const [st, rt] = await Promise.all([api.getSettings(), api.getRuntime()]);
      const grades = this.data.grades;
      const idx = grades.indexOf(st.grade_level || UNSPECIFIED);
      const hermes = (rt.hermes && (HERMES_TEXT[rt.hermes.state] || rt.hermes.state)) || '未知';
      const ws = rt.workspace || {};
      const git = rt.git || {};
      const last = git.last_sync || null;
      this.setData({
        gradeIndex: idx >= 0 ? idx : 0,
        termStartDate: st.term_start_date || '',
        subjects: st.subjects || [],
        source: st.source === 'saved' ? '已保存的设置' : '服务端配置文件默认值',
        runtimeText: 'Hermes：' + hermes,
        workspaceText: '工作区：' + (ws.dir || '-')
          + '｜README ' + (ws.readme_exists ? '已就绪' : '缺失')
          + '｜原题目录 ' + (ws.original_dir_exists ? '已建立' : '未建立')
          + '｜.gitignore ' + (ws.gitignore_exists ? '已就绪' : '缺失'),
        gitText: '学习记录同步：' + (git.enabled ? '已启用（远端 ' + (git.remote || 'origin') + '）' : '未启用')
          + (last ? '｜最近一次：' + (GIT_TEXT[last.status] || last.status) : ''),
        lastSyncNote: last ? (last.reason || '') : '',
      });
    } catch (e) {
      wx.showToast({ title: '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  onGradeChange(e) { this.setData({ gradeIndex: Number(e.detail.value) }); },
  onTermChange(e) { this.setData({ termStartDate: e.detail.value }); },
  onNewSubjectInput(e) { this.setData({ newSubject: e.detail.value }); },

  addSubject() {
    const name = (this.data.newSubject || '').trim();
    if (!name) return;
    if (this.data.subjects.indexOf(name) >= 0) {
      wx.showToast({ title: '已有该学科', icon: 'none' });
      return;
    }
    if (this.data.subjects.length >= 8) {
      wx.showToast({ title: '学科数量过多', icon: 'none' });
      return;
    }
    this.setData({ subjects: this.data.subjects.concat([name]), newSubject: '' });
  },

  removeSubject(e) {
    const index = Number(e.currentTarget.dataset.index);
    const subjects = this.data.subjects.slice();
    subjects.splice(index, 1);
    this.setData({ subjects });
  },

  async save() {
    const grade = this.data.grades[this.data.gradeIndex];
    this.setData({ saving: true });
    try {
      const st = await api.updateSettings({
        grade_level: grade === UNSPECIFIED ? '' : grade,
        term_start_date: this.data.termStartDate || '',
        subjects: this.data.subjects,
      });
      wx.showToast({ title: '已保存' });
      this.setData({
        subjects: st.subjects || [],
        termStartDate: st.term_start_date || '',
        source: '已保存的设置',
      });
      this.load();
    } catch (e) {
      wx.showModal({ title: '保存失败', content: e.message || '未知错误', showCancel: false });
    } finally {
      this.setData({ saving: false });
    }
  },
});
