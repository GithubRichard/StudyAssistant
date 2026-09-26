const api = require('../../utils/api');

const STATE_LABEL = {
  pending_correction: '待订正',
  corrected_pending_retest: '已订正待复测',
  retest_passed: '复测通过',
  retest_failed: '复测未通过',
  not_applicable: '不适用',
};
const STATE_PILL = {
  pending_correction: 'pill-waiting',
  corrected_pending_retest: 'pill-running',
  retest_passed: 'pill-done',
  retest_failed: 'pill-failed',
};
const FILTERS = [
  { key: '', label: '全部' },
  { key: 'pending_correction', label: '待订正' },
  { key: 'corrected_pending_retest', label: '待复测' },
  { key: 'retest_failed', label: '复测未通过' },
  { key: 'retest_passed', label: '复测通过' },
];
const RESULT_OPTIONS = [
  { key: 'retest_passed', label: '复测通过' },
  { key: 'retest_failed', label: '复测未通过' },
  { key: 'corrected', label: '已订正待复测' },
];

function pad(n) { return n < 10 ? '0' + n : '' + n; }

Page({
  data: {
    subjects: [],
    subjectIndex: 0,
    filters: FILTERS,
    filterIndex: 0,
    entries: [],
    counts: {},
    countsText: '',
    loading: true,
    failed: false,
    sheetVisible: false,
    activeEntry: null,
    resultOptions: RESULT_OPTIONS,
    resultIndex: 0,
    occurredDate: '',
    studentAnswer: '',
    note: '',
    saving: false,
    stateLabel: STATE_LABEL,
  },

  onShow() { this.load(); },

  async load() {
    this.setData({ loading: true });
    try {
      await api.ensureLogin();
      const subject = this.data.subjects[this.data.subjectIndex] || '';
      const filter = FILTERS[this.data.filterIndex].key;
      const data = await api.getLedger({ subject, states: filter, limit: 200 });
      const subjects = data.subjects || [];
      this.setData({
        failed: false,
        subjects,
        entries: (data.entries || []).map((it) => ({
          ...it,
          stateLabel: STATE_LABEL[it.remediation_state] || it.remediation_state,
          pill: STATE_PILL[it.remediation_state] || 'pill-pending',
          located: (it.source || '未记录来源') + (it.page ? ' ' + it.page : '')
            + (it.no ? ' 第' + it.no + '题' : ''),
          when: this.formatTime(it.last_event_at || it.created_at),
        })),
        countsText: this.buildCountsText(data.counts || {}),
      });
      if (this.data.subjectIndex >= subjects.length) this.setData({ subjectIndex: 0 });
    } catch (e) {
      this.setData({ failed: true });
      console.warn('加载台账失败:', e);
    } finally {
      this.setData({ loading: false });
    }
  },

  buildCountsText(counts) {
    return '待订正 ' + (counts.pending_correction || 0)
      + ' · 待复测 ' + (counts.corrected_pending_retest || 0)
      + ' · 复测未通过 ' + (counts.retest_failed || 0)
      + ' · 复测通过 ' + (counts.retest_passed || 0);
  },

  formatTime(seconds) {
    if (!seconds) return '';
    const d = new Date(seconds * 1000);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  },

  switchSubject(e) {
    this.setData({ subjectIndex: Number(e.currentTarget.dataset.index) }, () => this.load());
  },

  switchFilter(e) {
    this.setData({ filterIndex: Number(e.currentTarget.dataset.index) }, () => this.load());
  },

  refresh() { this.load(); },

  goTask(e) {
    const taskId = e.currentTarget.dataset.task;
    if (!taskId) {
      wx.showToast({ title: '该条目未关联任务', icon: 'none' });
      return;
    }
    wx.navigateTo({ url: '/pages/result/result?task_id=' + taskId });
  },

  openSheet(e) {
    const entry = e.currentTarget.dataset.entry;
    const today = new Date();
    this.setData({
      sheetVisible: true,
      activeEntry: entry,
      resultIndex: 0,
      occurredDate: today.getFullYear() + '-' + pad(today.getMonth() + 1) + '-' + pad(today.getDate()),
      studentAnswer: '',
      note: '',
    });
  },

  closeSheet() { this.setData({ sheetVisible: false, activeEntry: null }); },

  pickResult(e) { this.setData({ resultIndex: Number(e.currentTarget.dataset.index) }); },
  onDateChange(e) { this.setData({ occurredDate: e.detail.value }); },
  onAnswerInput(e) { this.setData({ studentAnswer: e.detail.value }); },
  onNoteInput(e) { this.setData({ note: e.detail.value }); },

  async saveEvent() {
    const entry = this.data.activeEntry;
    if (!entry) return;
    this.setData({ saving: true });
    wx.showLoading({ title: '登记中', mask: true });
    try {
      const payload = {
        result: RESULT_OPTIONS[this.data.resultIndex].key,
        occurred_date: this.data.occurredDate || '',
        student_answer: (this.data.studentAnswer || '').trim(),
        note: (this.data.note || '').trim(),
      };
      const res = await api.addLedgerEvent(entry.id, payload);
      wx.hideLoading();
      const archiveNote = res.archive && res.archive.status === 'generated'
        ? '已追加到归档记录' : (res.archive && res.archive.note) || '未写入归档文件';
      wx.showModal({
        title: '已登记',
        content: '状态：' + (STATE_LABEL[res.remediation_state] || res.remediation_state)
          + '\n' + archiveNote,
        showCancel: false,
      });
      this.closeSheet();
      this.load();
    } catch (e) {
      wx.hideLoading();
      wx.showModal({ title: '登记失败', content: e.message || '未知错误', showCancel: false });
    } finally {
      this.setData({ saving: false });
    }
  },
});
