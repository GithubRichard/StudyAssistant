const api = require('../../utils/api');

Page({
  data: {
    subjects: ['数学', '物理', '化学', '英语'],
    subjectIndex: 0,
    grades: ['七年级', '八年级', '九年级'],
    gradeIndex: 0,
    imagePath: '',
    uploading: false,
  },

  onSubjectChange(e) {
    this.setData({ subjectIndex: Number(e.detail.value) });
  },

  onGradeChange(e) {
    this.setData({ gradeIndex: Number(e.detail.value) });
  },

  chooseImage() {
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['camera', 'album'],
      success: (res) => {
        this.setData({ imagePath: res.tempFiles[0].tempFilePath });
      },
    });
  },

  async submit() {
    if (!this.data.imagePath) {
      wx.showToast({ title: '请先拍照或选择图片', icon: 'none' });
      return;
    }
    this.setData({ uploading: true });
    try {
      const openid = await api.ensureLogin();
      const { task_id } = await api.uploadTask({
        openid,
        subject: this.data.subjects[this.data.subjectIndex],
        gradeLevel: this.data.grades[this.data.gradeIndex],
        filePath: this.data.imagePath,
      });
      wx.navigateTo({ url: '/pages/result/result?task_id=' + task_id });
    } catch (e) {
      wx.showModal({ title: '提交失败', content: e.message || '未知错误', showCancel: false });
    } finally {
      this.setData({ uploading: false });
    }
  },
});
