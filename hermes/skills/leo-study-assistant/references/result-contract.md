# 结果协议（result contract）· schema_version = 3

业务后端会解析最终回答中的 ```json 代码块并严格校验。字段名与取值白名单必须一致；
多余字段被忽略，缺失或违规会让整次任务判定为失败（不会写入学习记录）。

**文本字段一律用字符串**：没有内容就写空字符串 `""` 或直接省略该键，**不要写 JSON `null`**。
（后端会把 `null` 当作「未提供」处理，但不要依赖这一点——必填字段写 `null` 照样判失败。）

**计数字段只写阿拉伯数字**：`overview.*`、`review_summary.scope / disagreed / unverified`
等栏位不要写说明文字（后端会从文字里抠数字或按 0 处理，但真实含义就丢了）。

## 顶层结构

```json
{
  "schema_version": 3,
  "task_type": "grading",
  "subject": "数学",
  "grade_level": "七年级",
  "exam_scope": "",
  "training_kind": "",
  "scope": {"start_date": "2026-09-01", "end_date": "2026-09-26", "sources": ["9月3周数学作业 P12-13"]},
  "overview": {
    "checked_questions": 0,
    "correct": 0, "wrong": 0, "unanswered": 0, "uncertain": 0, "unprocessed": 0,
    "summary": "一句话结论"
  },
  "questions": [],
  "retests": [],
  "sections": [{"title": "做得好的题", "body": "Markdown 正文"}],
  "missing_info": ["需要家长补充：本学期开学日期"],
  "parent_tips": ["每天 10 分钟，重做 P12 第 3 题变式"],
  "review_summary": {"state": "not_required", "scope": 0, "disagreed": 0, "unverified": 0,
                     "note": "无已判错题，跳过二次核查"},
  "archive": {"suggested_path": "数学/错题解析/2026-09-26.md", "action": "append",
              "content_markdown": "## 来源：9月3周数学作业\n\n..."},
  "delivery": {
    "pdf": {"status": "not_configured", "note": "服务端未安装 PDF 生成能力"},
    "email": {"status": "not_configured", "note": "未配置邮件渠道"},
    "git": {"status": "not_configured", "note": "学习记录同步由业务后端执行"}
  }
}
```

## 字段约定

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 固定 `3` |
| `task_type` | 是 | `grading` / `qa` / `weekly_report` / `training` / `retest` |
| `subject` | 是 | 学科名；未知留空字符串，由材料判断，不擅自假设 |
| `grade_level` | 否 | 未知留空 |
| `exam_scope` | 否 | 仅训练任务：学校考试范围；未提供说明「仅基于已归档错题」 |
| `training_kind` | 否 | 仅 `training`：`topic` / `monthly` / `midterm` / `final`；其他任务必须留空 |
| `scope.start_date` / `end_date` | 否 | **实际使用**的资料区间；后端会按任务类型补默认值 |
| `scope.sources` | 否 | 本次使用的来源标签 |
| `overview.summary` | 否 | 简短结论；五态计数与 `remediation` 由后端按逐题数据重算 |
| `questions` | 否 | 题目数组，见下节 |
| `retests` | 否 | 本次真实发生的复测/订正事件，见下节 |
| `sections` | 否 | 报告正文段落（Markdown） |
| `missing_info` | 否 | 待补充材料或信息，逐条可执行 |
| `parent_tips` | 否 | 家长可执行的复习建议 |
| `review_summary` | 否 | 二次核查状态 |
| `archive.content_markdown` | 否 | 追加到归档文件的 Markdown 正文 |
| `delivery.*.status` | 否 | 见交付状态白名单 |

## 题目字段（`questions[]`）

```json
{
  "id": "math-p12-q3",
  "no": "3",
  "source": "9月3周数学作业",
  "page": "P12",
  "stem": "解方程 2x+1=9",
  "student_answer": "x=5",
  "status": "wrong",
  "correct_answer": "x=4",
  "steps": ["移项得 2x=8", "两边同除 2 得 x=4"],
  "error_rule": "移项时忘记变号",
  "knowledge_point": "一元一次方程移项",
  "evidence": "原图 P12 第 3 题，孩子写上 x=5",
  "review": {"state": "agreed", "note": "核查未发现异议", "basis": "由 2x=8 推出 x=4"},
  "final_decision": "kept_wrong",
  "final_decision_basis": "复核原始作答后维持原判定",
  "remediation": {"state": "pending_correction", "updated_date": "",
                  "linked_training": "", "note": "等待孩子订正"}
}
```

| 字段 | 取值 |
| --- | --- |
| `status` | `correct` / `wrong` / `unanswered` / `uncertain` / `unprocessed` |
| `review.state` | `agreed` / `disagreed` / `unverified` / `unprocessed` / `not_applicable` |
| `final_decision` | `kept_wrong` / `corrected_to_correct` / `kept_correct` / `kept_uncertain` / `reclassified_unanswered` / `pending` |
| `remediation.state` | `pending_correction` / `corrected_pending_retest` / `retest_passed` / `retest_failed` / `not_applicable` |

`remediation` 语义（订正与复测）：

| 状态 | 含义 | 何时使用 |
| --- | --- | --- |
| `pending_correction` | 待订正 | 判错、尚未看到订正证据 |
| `corrected_pending_retest` | 已订正待复测 | 有订正证据但没有新的作答结果 |
| `retest_passed` | 复测通过 | 孩子再次实际作答且正确 |
| `retest_failed` | 复测未通过 | 孩子再次实际作答仍错 |
| `not_applicable` | 不适用 | 非错题（答对、未作答、存疑、未处理） |

- `corrected_pending_retest` / `retest_passed` / `retest_failed` 必须给出 `updated_date`（实际发生日期）。
- **生成练习不等于完成练习，完成订正不等于已经掌握**；没有新结果时保持原状态，不得因为「做过练习」写成通过。
- 非错题的 `remediation.state` 必须是 `not_applicable`，`updated_date` 写空字符串 `""`。

## 复测事件（`retests[]`）

```json
{
  "source": "9月3周数学作业", "page": "P12", "no": "3",
  "occurred_date": "2026-09-26",
  "result": "retest_passed",
  "student_answer": "x=4",
  "note": "重新讲解移项规则后独立完成"
}
```

| 字段 | 取值 |
| --- | --- |
| `occurred_date` | 必填，`YYYY-MM-DD`，真实发生日期 |
| `result` | `retest_passed` / `retest_failed` / `corrected` |

- 只登记真实发生的作答，不重复登记同一事件；`question_uid` 可留空，由服务端按来源+日期+页码+题号回填。
- 再次实际作答才是新的复测事件；重新核查、重读图片不算。

## 核查汇总（`review_summary`）

| 字段 | 取值 |
| --- | --- |
| `state` | `not_required` / `completed` / `partial` / `failed` / `not_run` |
| `scope` / `disagreed` / `unverified` | 整数：送核查 / 有异议 / 无法核查的题数；没有错题时 `scope` 写 `0`，不要写文字说明 |
| `note` | 原因说明，必须与真实调用情况一致 |

## 交付状态（`delivery`）

`status` 取值：`not_configured` / `skipped` / `generated` / `sent` / `committed` / `failed`。

- 归档、提交与推送**由业务后端在结果校验后执行**：你只给出 `archive.content_markdown`，
  并把 `delivery` 如实写为未配置或未执行，不得声称已提交、已推送、已生成 PDF、已发送邮件。
- `delivery.git` 的真实提交/推送结果由后端覆盖，模型自述不作为成功依据。

## 归档路径约束

`suggested_path` 必须是 `学科/子目录/YYYY-MM-DD[-主题].md`，且子目录与任务类型一致：

| 任务类型 | 允许子目录 |
| --- | --- |
| `grading` / `qa` | `错题解析` |
| `weekly_report` | `周报分析` |
| `training` / `retest` | `强化训练` |

路径越界、层级不符或子目录错位会被后端拒绝写入（任务标记失败或归档 skipped）。

## 硬性规则

- `status=wrong` 必须有 `correct_answer` 或 `steps`，且 `error_rule` 必须具体，不得写「粗心」。
- 未作答与存疑题不得标为 `kept_wrong`，也不得记为「已掌握」。
- `review.state=disagreed` 必须给出 `review.basis` 与 `final_decision_basis`。
- 题目 `id` 在同一结果内唯一；同来源同页码同题号重复出现时，服务端按去重键区分并提示核对。
- 错误率只在分母（已检查题数）可确认时计算，由后端重算，不要自己编造百分比。
