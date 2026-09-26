const api = require('../../utils/api');

const STATUS_TEXT = {
  pending: '排队中',
  grading: '执行中',
  waiting_input: '待补充材料',
  interrupted: '结果未确认',
  done: '已完成',
  failed: '失败',
};
const STATUS_PILL = {
  pending: 'pill-pending',
  grading: 'pill-running',
  waiting_input: 'pill-waiting',
  interrupted: 'pill-interrupted',
  done: 'pill-done',
  failed: 'pill-failed',
};
const TYPE_LABEL = {
  grading: '作业批改', qa: '学习问答', weekly_report: '周报分析',
  training: '针对性训练', retest: '复测',
};
const KIND_LABEL = { topic: '专项', monthly: '月考', midterm: '期中', final: '期末' };

Page({
  data: { tasks: [], loading: true, failed: false },

  onShow() { this.load(); },

  async load() {
    try {
      await api.ensureLogin();
      const tasks = await api.getTasks(20, 0);
      this.setData({
        failed: false,
        tasks: tasks.map((t) => {
          const kind = KIND_LABEL[t.training_kind] || '';
          let scopeText = '';
          if (t.scope_start || t.scope_end) {
            scopeText = (t.scope_start || '不限') + ' ~ ' + (t.scope_end || '不限');
          }
          return {
            ...t,
            time: new Date(t.created_at * 1000).toLocaleString('zh-CN', { hour12: false }),
            statusText: STATUS_TEXT[t.status] || t.status,
            pill: STATUS_PILL[t.status] || 'pill-pending',
            typeLabel: (TYPE_LABEL[t.task_type] || '学习任务') + (kind ? '·' + kind : ''),
            scopeText,
            gitText: t.git_status === 'committed' ? '记录已同步'
              : (t.git_status === 'failed' ? '记录未同步' : ''),
          };
        }),
      });
    } catch (e) {
      this.setData({ failed: true });
      console.warn('加载历史失败:', e);
    } finally {
      this.setData({ loading: false });
    }
  },

  goResult(e) {
    wx.navigateTo({ url: '/pages/result/result?task_id=' + e.currentTarget.dataset.id });
  },
});
