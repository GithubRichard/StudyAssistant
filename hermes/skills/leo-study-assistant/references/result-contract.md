# 结果协议（result contract）

业务后端会解析最终回答中的 ```json 代码块并做严格校验。字段名、取值白名单必须一致；多余字段会被忽略，缺失必填字段会让整次任务判定为失败。

## 顶层结构

```json
{
  "schema_version": 2,
  "task_type": "grading",
  "subject": "数学",
  "grade_level": "七年级",
  "scope": {
    "start_date": "2026-09-01",
    "end_date": "2026-09-26",
    "sources": ["9月3周数学作业 P12-13"]
  },
  "overview": {
    "checked_questions": 0,
    "correct": 0,
    "wrong": 0,
    "unanswered": 0,
    "uncertain": 0,
    "unprocessed": 0,
    "summary": "一句话结论"
  },
  "questions": [],
  "sections": [
    {"title": "做得好的题", "body": "Markdown 正文"}
  ],
  "missing_info": ["需要家长补充：本学期开学日期"],
  "parent_tips": ["每天 10 分钟，重做 P12 第 3 题变式"],
  "review_summary": {
    "state": "not_required",
    "scope": 0,
    "disagreed": 0,
    "unverified": 0,
    "note": "无已判错题，跳过二次核查"
  },
  "archive": {
    "suggested_path": "数学/错题解析/2026-09-26.md",
    "action": "append",
    "content_markdown": "## 来源：9月3周数学作业\n\n..."
  },
  "delivery": {
    "pdf": {"status": "not_configured", "note": "服务端未安装 PDF 生成能力"},
    "email": {"status": "not_configured", "note": "未配置邮件渠道"},
    "git": {"status": "not_configured", "note": "未配置学习记录同步"}
  }
}
```

## 字段约定

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 固定 `2` |
| `task_type` | 是 | `grading` / `qa` / `weekly_report` / `training` / `retest` |
| `subject` | 是 | 学科名，宽松自由文本 |
| `grade_level` | 否 | 未知留空字符串，不编造 |
| `scope.start_date` / `end_date` | 否 | 实际使用的资料区间，`YYYY-MM-DD` |
| `scope.sources` | 否 | 本次使用的来源标签列表 |
| `overview.summary` | 否 | 简短结论 |
| `questions` | 否 | 题目数组，见下节 |
| `sections` | 否 | 报告正文段落，Markdown |
| `missing_info` | 否 | 待补充材料或信息，逐条可执行 |
| `parent_tips` | 否 | 家长可执行的复习建议 |
| `review_summary` | 否 | 二次核查状态，见下节 |
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
  "review": {
    "state": "agreed",
    "note": "核查未发现异议",
    "basis": "由 2x=8 推出 x=4，原判定成立"
  },
  "final_decision": "kept_wrong",
  "final_decision_basis": "复核原始作答后维持原判定"
}
```

| 字段 | 取值 |
| --- | --- |
| `status` | `correct` / `wrong` / `unanswered` / `uncertain` / `unprocessed` |
| `review.state` | `agreed`（未发现异议）/ `disagreed`（有异议）/ `unverified`（无法核查）/ `unprocessed`（未送核查）/ `not_applicable`（非错题或未核查） |
| `final_decision` | `kept_wrong` / `corrected_to_correct` / `kept_correct` / `kept_uncertain` / `reclassified_unanswered` / `pending` |

规则：

- `status=wrong` 的题必须有 `correct_answer` 或 `steps`，并给出具体 `error_rule`，不写「粗心」。
- 未作答与存疑题不得给出 `final_decision: kept_wrong`。
- `review.state=disagreed` 时必须给 `review.basis`，并且 `final_decision_basis` 说明复核依据。
- 题目 `id` 在同一结果内唯一；同日同来源同页码同题号重算时使用同一 `id`，避免重复计入出错事件。
- 五态统计必须与 `questions` 实际内容一致，由后端重算核对，不一致则任务标记为校验失败。

## 核查汇总（`review_summary`）

| 字段 | 取值 |
| --- | --- |
| `state` | `not_required`（无错题）/ `completed` / `partial` / `failed` / `not_run` |
| `scope` | 送核查的错题数 |
| `disagreed` | 有异议题数 |
| `unverified` | 无法核查题数 |
| `note` | 原因说明，必须与真实调用情况一致 |

## 交付状态（`delivery`）

每个子项含 `status` 与可选 `note`；`status` 取值：

| 取值 | 含义 |
| --- | --- |
| `not_configured` | 服务端未配置该能力 |
| `skipped` | 本次按规则跳过（例如无错题不生成 PDF） |
| `generated` / `sent` / `committed` | 真实发生且可验证 |
| `failed` | 尝试后失败，`note` 说明原因 |

不允许多报：模型自述「已生成」不能代替真实产物；后端会核对文件是否存在后才登记下载。
