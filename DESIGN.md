# Leo 学习任务服务 · 技术方案与设计文档

> 版本：v0.3.0（对齐学习规范：订正复测、台账、受控 Git 同步）| 日期：2026-09-26
> 目标读者：个人开发者（家庭自用，业余时间维护）
> v0.1 的「多模型直连批改」保留为 legacy 引擎，见 §7

---

## 1. 项目目标与范围

微信小程序提交学习任务 → 云服务器上的业务后端负责身份、附件与任务 → 由 **Hermes Agent 执行学习技能**（批改、错题解析、周报、考前训练、复测）→ 结果校验、受控归档、错题台账与受控 Git 同步，状态如实返回前端。

学习闭环：**每日错题解析 → 周报归纳 → 专项或考前训练 → 实际复测 → 更新记录**。

- 技能规则来自 `hermes/skills/leo-study-assistant/`（本仓库副本，已解除与本机 IDE 的耦合）
- 业务后端不实现 Agent 推理，只负责：鉴权、附件、任务队列、结果校验、受控归档、台账去重、
  受控提交推送、状态如实呈现
- 本轮不做：服务器部署、真实 Hermes 联调、PDF 生成、云端邮件（`delivery.pdf/email` 如实标注未配置）

---

## 2. 总体架构

```
┌──────────────┐   HTTPS（域名需备案）    ┌────────────────────────────────────────┐
│  微信小程序   │ ──────────────────────► │  云服务器                               │
│  仅持有会话令牌│ ◄────────────────────── │  ┌──────────────────────────────────┐  │
└──────────────┘    JSON / multipart      │  │ 反向代理 (Caddy，仅暴露 443)      │  │
                                          │  └──────────────┬───────────────────┘  │
                                          │                 ▼ 127.0.0.1:8000       │
                                          │  ┌──────────────────────────────────┐  │
                                          │  │ FastAPI 业务后端                  │  │
                                          │  │ 鉴权 / 附件 / 任务队列 / 结果校验  │  │
                                          │  │ 受控归档 / 状态呈现               │  │
                                          │  └───────┬───────────────┬──────────┘  │
                                          │          │ SQLite        │ 内部 HTTP    │
                                          │          ▼               ▼              │
                                          │  data/（库+图片+输出）  Hermes Agent     │
                                          │  workspace/（学习记录）  127.0.0.1:8642  │
                                          └────────────────────────────┬───────────┘
                                                                       ▼
                                                          底层模型（由 Hermes 配置）
```

**关键分层**

| 层 | 职责 | 不做什么 |
|---|---|---|
| 小程序 | 提交材料、展示状态与结果、复习台账与复测登记、下载成果 | 不持有 Hermes 地址/密钥，不直接调用 Agent |
| 业务后端 | 身份与归属、配额、幂等、队列、结果校验、归档落盘、台账去重、受控提交推送 | 不实现 Agent 工具循环，不伪造进度，不代发邮件 |
| Hermes Agent | 加载技能与工具、多轮推理、产出结构化结果 | 不直接写学习记录、不执行 git（写入与推送由后端校验后执行） |

原题资料边界：上传的作业照片只落在 `data/`（服务端私有目录），**不写入工作区、不进入归档 Markdown、不进入 Git**；
工作区保留 `<账号>/<学科>/原题/年份/周次/` 目录骨架与 `.gitignore` 忽略规则，仅 `.gitkeep` 可被跟踪。

账号隔离：工作区按账号分目录（`<工作区根>/<账号>/<学科>/...`，一个账号 = 一个孩子）。技能只产出 3 段
`archive.suggested_path`，账号层由服务端按任务身份（`task.openid`）拼接；多个账号共用同一个 Git 仓库，
`README.md`、`.gitignore`、冲突记录仍在工作区根，属全局文件。

---

## 3. Hermes 接入设计

### 3.1 只用已核实的接口

- `POST /v1/chat/completions`：服务端执行完整工具循环；支持 `image_url` 内联图片
- `GET /v1/skills`：技能发现，用于判断「技能是否真的装好了」
- `GET /v1/capabilities`：当前版本能力（备用）

不使用尚未确认图片输入契约的 Runs 接口，不编造镜像特有参数。

### 3.2 就绪判定

`/api/runtime` 区分三种状态，避免「配了就等于能用」的错觉：

| 状态 | 含义 |
|---|---|
| `not_configured` | 未配置地址或密钥 |
| `unreachable` | 配置了但连不上 |
| `skill_missing` | 连上了但技能未出现在 `/v1/skills` |
| `skill_unknown` | 网关可用但技能接口异常，无法确认；任务仍会尝试执行 |
| `ready` | 地址、密钥、技能三者齐备 |

### 3.3 错误分类与重试边界

| 异常 | 可确认「未执行」 | 处理 |
|---|---|---|
| `HermesNotConfigured` / `HermesAuthError` | 是 | 任务 `failed`，退还预留次数 |
| `HermesRejected`（4xx） | 是 | 任务 `failed`，退还预留次数 |
| `HermesUnavailable`（5xx）/ 超时 / 连接中断 | 否 | 任务 `interrupted`，**不自动重发**，不退还次数 |
| `HermesResultInvalid` | 否 | 任务 `failed`，记录校验原因 |

关闭 HTTP 连接不代表远端取消执行，因此「结果未确认」一律按未确认上报。

### 3.4 服务端二次复查（第二模型）

首轮批改通过协议校验后，由 `TaskRunner` 串入一次**独立会话**的复查调用（`review-{task_id}-{run_no}`，
不复用首轮会话的模型锁）：

```
首轮 run_task（agent_model）
  → 预处理前置：学科回填 / uid / 补充轮次覆盖校验 / 区间合并
  → 复查编排（app/review.py 纯逻辑 + HermesClient.review_questions）
  → 防御式二次校验（失败回退规范化基线，不写非法结果）
  → _finish：归档（含服务端复查附记）→ Git → 台账 → 落库
```

关键边界：

| 维度 | 约定 |
|---|---|
| 候选范围 | `wrong` + `uncertain`；`unanswered` 不送；超限时判错题优先，未送审标 `unprocessed` |
| 复查步骤 | 一次调用内分两步：**先转写二次确认**（复查模型对照作业原图逐题重读学生作答，核对提取转写；卷面是 A、转写成 B 即为对首轮结论的实质异议，该题标 `disagreed` 并在 `basis` 写清转写差异），**再逻辑核查**（核查首轮求解、比对与诊断是否自洽）。拿不到原图时退化为纯文字核查（`coverage=transcript_only`）；复查模型需要图片链路 |
| 模型切换 | 请求体 `model`（`model_routes` 别名或底层 ID）+ `provider` + `model_options`；底层 ID 不带 `provider` 会被网关静默忽略 |
| 身份核验 | 以网关**报告**的模型为准；报告缺失=身份未确认、与首轮同模型=路由不符、模型或 provider 与 `review_expected_*` 不符=不符；三者都不采纳为可信结论，请求值不得冒充实际值。**网关不回 `provider` 时按 `model_only` 采纳**（模型名已核对且不同于首轮模型，provider 只是次级旁证），并在 `review_summary.note` 如实写明核验范围 |
| 覆盖对账 | 按送审 id 集合核对复查输出；空列表/漏题/未知 id/重复 id 整体拒绝，送审题标 `unverified` |
| 改判边界 | 复查只写 `review` / `review_summary`；`status`、作答、答案、步骤、`remediation`、`retests` 原样保留；异议在 `final_decision_basis` 追加「尚未重新裁决」记录 |
| 失败回退 | 首轮模型自述的复查字段一律被规范化覆盖；复查失败不吞首轮成果，归档与台账照常，复查字段如实标注 |
| 归档一致 | 服务端「二次复查记录」附记与结果 JSON 同源，随本轮归档一次写入（轮次幂等） |
| 补充轮次 | 每轮重新复查当前全部候选题（覆盖补图与上轮失败），每轮最多一次调用 |
| 资源 | 共享任务预算（单调时钟）；无重试、无递归委派；两轮 usage 如实累计，不编造 |

---

## 4. 结果协议（schema_version = 3）

```json
{
  "schema_version": 3,
  "task_type": "grading",
  "exam_scope": "",
  "training_kind": "",
  "scope": {"start_date": "2026-09-01", "end_date": "2026-09-26", "sources": ["9月3周作业"]},
  "questions": [{
    "id": "math-p12-q1", "no": "1", "source": "9月3周作业", "page": "P12",
    "student_answer": "x=5", "status": "wrong",
    "correct_answer": "x=4", "steps": ["2x=8", "x=4"],
    "error_rule": "移项时忘记变号", "knowledge_point": "一元一次方程",
    "review": {"state": "agreed", "note": "核查未发现异议", "basis": "由 2x=8 得 x=4"},
    "final_decision": "kept_wrong", "final_decision_basis": "复核后维持原判定",
    "remediation": {"state": "pending_correction", "updated_date": "", "linked_training": "", "note": ""}
  }],
  "retests": [{"source": "9月3周作业", "page": "P12", "no": "1",
              "occurred_date": "2026-09-28", "result": "retest_passed", "student_answer": "x=4"}],
  "review_summary": {"state": "completed", "scope": 1, "disagreed": 0, "unverified": 0},
  "archive": {"suggested_path": "数学/错题解析/2026-09-26.md", "action": "append",
              "content_markdown": "..."},
  "delivery": {"pdf": {"status": "not_configured"}, "email": {"status": "not_configured"},
               "git": {"status": "not_configured", "committed": false, "pushed": false,
                       "commit": "", "conflict_record": ""}}
}
```

强制约束（校验不通过即任务失败，不入库）：

- 题目五态：`correct` / `wrong` / `unanswered` / `uncertain` / `unprocessed`
- 判错题必须给出 `correct_answer` 或 `steps`，且 `error_rule` 不能是「粗心」这类笼统表述
- 未作答与存疑题不得标为 `kept_wrong`
- 核查有异议（`review.state=disagreed`）必须给出可核验依据
  （`review` / `review_summary` 由服务端按二次复查的真实执行情况填写，首轮只写默认值）
- 题目 `id` 唯一；`overview` 若填写则必须与逐题统计一致（后端会重算五态、订正状态计数与错误率口径）
- 归档子目录必须与任务类型匹配（`错题解析` / `周报分析` / `强化训练`）
- 订正与复测：判错题必须给出 `remediation.state`；`corrected_pending_retest` / `retest_passed` /
  `retest_failed` 必须给出实际发生日期；非错题必须为 `not_applicable`
- `retests[]` 每项必须有真实发生日期（`occurred_date`），只追加、不改写历史判定
- 题目稳定去重键 `uid`（学科+来源+日期+页码+题号）由服务端回填，用于台账去重与复测关联
- 旧结果（v2）只读转换展示并标注「未记录订正与复测状态」；v1 旧批改结果同样只读转换

---

## 5. 数据模型（SQLite，增量迁移）

v1 基础表保留：`users` / `quota_usage` / `tasks` / `mistakes` / `daily_cost`。

v2 新增：

| 表 | 用途 |
|---|---|
| `sessions` | 会话令牌摘要、有效期（明文不落库） |
| `assets` | 上传图片（sha256、尺寸、路径） |
| `task_assets` | 任务/轮次与附件的顺序关联 |
| `task_runs` | 执行轮次（首轮/补充材料）、Hermes 会话标识、结果与错误 |
| `idempotency` | 幂等键 → 任务，防重复创建与重复扣次 |
| `quota_reservations` | 配额预留/结算/退还流水 |
| `artifacts` | 通过校验的成果文件（归档、PDF） |

`tasks` 新增 `task_type`、`input_text`、`run_count`、`claim_owner`、`claim_expires_at`、`idempotency_key`、`archive_path`。

v3 新增：

| 表 / 列 | 用途 |
|---|---|
| `family_settings` | 家庭设置（学期起始日期、默认年级、学科清单 JSON） |
| `question_events` | 订正与复测事件（只追加，历史判定不被改写） |
| `git_sync_log` | 每次受控 Git 同步的真实结果（状态、提交号、冲突记录路径、原因） |
| `mistakes` 扩展列 | `subject`/`source`/`page`/`question_uid`/`stem`/`student_answer`/`correct_answer`/`error_rule`/`status`/`remediation_state`/`last_event_at`/`archive_path`，并加 `(openid, subject, remediation_state)` 索引与 `question_uid` 条件唯一索引 |
| `tasks` 扩展列 | `exam_scope`、`training_kind`、`scope_start`、`scope_end`、`git_status` |

台账与人工收藏共用 `mistakes` 表：**台账条目 `question_uid` 非空**（自动去重入账），
人工收藏条目 `question_uid` 为空，两者在接口层分开返回。

迁移规则：有数据的旧库先备份为 `app.db.bak-<时间戳>`，失败即中止；只加表加列，不删不改历史数据。

---

## 6. 可靠性与安全设计

| 主题 | 做法 |
|---|---|
| 鉴权 | 登录签发随机令牌，库中只存 SHA-256；接口一律用会话身份，忽略客户端传入的 openid |
| 授权 | `auth.allowed_openids` 白名单（为空=不限制，仅开发）；非名单账号登录直接 403 |
| 归属 | 任务、附件、成果下载逐项校验 openid，不一致按 404 处理，避免泄露资源是否存在 |
| 微信登录 | 已配置 appid/secret 时登录失败必须拒绝，不再降级为开发身份 |
| 幂等 | `Idempotency-Key` + 请求指纹；同键同内容返回原任务，同键不同内容返回 409 |
| 配额 | 创建任务时同一事务内预留；重复提交不重复扣次；确认未执行才退还 |
| 队列 | 数据库认领（claim + 租约）；重启时把已派发未结束的任务标记 `interrupted`，不自动重放 |
| 归档 | 只允许 `<账号>/学科/{错题解析,周报分析,强化训练}/YYYY-MM-DD*.md`（账号层由服务端按身份拼接，技能只给 3 段），且子目录必须与任务类型匹配；读—合并—原子替换；同一轮次重复写会被标记跳过；复测结果追加到既有文件，不覆盖历史 |
| 账号隔离 | 身份 → 目录名唯一映射：`web:<user>` → `<user>`、微信身份 → `wx-<openid>`、无法派生时回落 `family`；路径含 `..`、绝对路径或指向他人账号目录一律拒绝 |
| 原题边界 | 上传图片只存 `data/`；工作区只创建 `<账号>/<学科>/原题/<年份>/` 骨架与 `.gitignore`；归档 Markdown 不含原图 |
| 台账 | 错题与存疑题按 `uid`（学科+来源+日期+页码+题号）去重入账；复测事件只追加；状态由结果与事件驱动 |
| 成果 | 只有授权目录内真实存在、类型与大小合规的文件才登记下载 |
| 交付状态 | 服务端未启用的 PDF/邮件一律 `not_configured`；`delivery.git` 以受控同步的真实结果为准，模型自述不作为成功依据 |
| 学习记录同步 | 只 `git add -- <本次归档文件>`，禁止全量暂存与强制添加；推送前校验分支、上游与暂存区；不带 force、不 merge/rebase、不改 Git 配置；冲突或非快进时生成 `冲突记录-YYYY-MM-DD-HHmmss.md` 并停止上传；无变化不建空提交 |
| 密钥 | Hermes 密钥只在服务端 `.env`；小程序只拿业务会话令牌；日志不打印令牌与原图 |

任务状态机：`pending → grading → (done | waiting_input | failed | interrupted)`；`waiting_input` 表示结果已产出但需要补充材料，可追加新轮次。

---

## 7. legacy 引擎（engine.mode = legacy）

v0.1 的「单图 + 多模型直连」路径保留，用于显式回退：

- `app/providers.py` 仍按 OpenAI 兼容协议封装多家厂商，`provider_chain()` 决定顺序与备胎
- 结果会通过 `grading_result_to_v3()` 转成当前协议结构，并标注「旧模式不执行技能流程与二次核查」
- 默认 `engine.mode: hermes`；Hermes 不可用时**不会静默退回** legacy，必须由配置显式切换

---

## 7.5 分阶段批改（staged grading，默认开启）

动机：单次大调用里 OCR/手写转写、求解、判定混在同一个注意力窗口，
转写错误会污染求解，学生答案会锚定模型。拆成四阶段后每阶段职责单一、输入受控：

1. **提取**（多模态，一次看全所有图片）：只转写题干与手写答案，不做对错判断；
   字迹无法辨认标 `uncertain`，绝不猜。
2. **独立求解**（纯文本）：只给题干，**不给学生答案**，物理隔离锚定效应。
3. **比对判定**：服务端先做确定性归一化比对（全角/空白/负号统一），
   模型只裁决仍不等价的项（纯文本）。
4. **错因诊断**（纯文本）：只针对错题；`error_rule` 禁止"粗心"类空话（服务端校验）。

实现要点（`app/staged.py`）：

- 每阶段独立走 provider 链 + JSON 强校验 + 语义检查，失败换备胎；全部失败抛 `StageError`
- 每阶段产出经 `on_stage` 回调写入 `task_runs.stage / stages_json`（migration v4），
  支持断点观察与按阶段重试；阶段失败退款（`certain_not_executed=True`）
- 组装阶段把产出合并为 v3 结果并过 `validate_result` 严格校验，再进原有 `_finish`
 （uid 回填、台账、归档、Git、订正事件逻辑全部复用）
- 补充轮次：未受影响题目由服务端直接透传、不经过模型；只重算受影响题；
  `revision_coverage_gaps` 的 uid 覆盖校验仍然是最后防线
- 路由（`tasks.execute`）：grading 任务 + `staged_grading.enabled` + provider 链非空 →
  `_run_staged`；否则走 Hermes 单次 / legacy 路径。`_finish` 的 provider 字段如实标记 `staged`

---

## 8. 部署方案

```
本地 --git push--> 服务器 --git pull--> docker compose up -d --build
                                              └─ 应用容器 host 网络，监听 127.0.0.1:8000
                                                 Caddy/Nginx 反向代理 + 自动 HTTPS
```

1. 2 核 2G 轻量服务器足够（应用本身很轻，Agent 由 Hermes 承担）
2. 域名 + **ICP 备案**（小程序 `request`/`uploadFile` 强制 HTTPS + 备案域名）
3. 应用容器使用 `network_mode: host` 才能访问宿主机的 Hermes（`127.0.0.1:8642`）；macOS 本地验证直接跑 Python
4. 技能更新：改 `hermes/skills/` 后除重建镜像，还要**同步安装到 Hermes 的 profile 技能目录**
5. 环境变量更新：`.env` 在创建容器时注入，必须 `docker compose up -d --force-recreate`，仅 `restart` 不生效
6. 日常更新用 `scripts/update-and-logs.sh`：`git pull --ff-only` → 技能副本同步到 Hermes profile → `docker compose up -d --build --force-recreate grader` → 轮询 `/healthz` 就绪后跟随日志；`--dry-run` 可零副作用预览。该脚本不改 `config.yaml`/`.env`、不做数据库迁移、不碰 git 历史

---

## 9. 测试与验证

| 层次 | 文件 | 说明 |
|---|---|---|
| 协议与错误分类 | `tests/test_hermes.py` | 用 `httpx.MockTransport` 模拟 Hermes，验证就绪判定、不重试、结果校验与 v3 约束 |
| 任务链路 | `tests/test_tasks.py` | 鉴权归属、幂等、配额结算/退还、执行器结果、中断恢复、补充材料、归档与 Git 交付串联 |
| 资料区间 | `tests/test_scope.py` | 月考/期中/期末/周报默认区间、用户指定优先与缺口标注 |
| 台账 | `tests/test_ledger.py` | 去重入账、状态流转、事件追加不改写历史、复测追加与路径边界 |
| 受控 Git | `tests/test_git_sync.py` | 临时仓库验证只提交授权文件、原题不入库、无变化不建空提交、非快进停止并生成冲突记录 |
| 工作区 | `tests/test_workspace.py` | 路径越界、归档追加幂等、原题骨架与 .gitignore、冲突记录命名、成果真实性 |
| 账号隔离 | `tests/test_workspace_accounts.py` | 身份→目录名映射、净化与兜底、账号骨架合并、账号层路径拒绝越界 |
| 迁移 | `tests/test_migrations.py` | 旧库备份升级（V1→V3）、旧任务保留 |
| 端到端 | `test_smoke.py` | TestClient + 模拟 Hermes 跑通完整闭环（不联网），含台账、复测登记、设置与区间 |

**验证边界**：以上全部为离线模拟验证，不代表真实 Hermes 版本、工具权限、模型工具调用能力已验证。

---

## 10. 已知限制 / 待办

- [ ] 未与真实 Hermes 联调：实例版本、运行用户、技能安装路径、沙箱能力需部署前核实
- [x] 二次核查（服务端第二模型复查）已实现编排：独立会话、身份核验、覆盖对账、失败回退、归档附记与两端展示（离线测试覆盖）
- [ ] 复查真实环境联调待确认：`model_routes` 别名与 `direct_model_requests` 的实际行为、响应身份字段语义、复查会话工具隔离、复查模型图片链路——未确认前保持 `hermes.review_model` 留空
- [ ] PDF 生成与云端邮件未实现（开关默认关闭，结果中如实标注）
- [ ] 网页版前端未提供学习设置界面（复习台账与任务结果已就绪，含二次复查展示）
- [ ] 学习记录同步仅在单进程执行器内串行执行；多实例部署前需要外部锁
- [ ] 错题台账与归档 Markdown 通过 `uid` 注释关联，尚无回溯重建索引的工具
- [ ] 单进程单并发执行器：横向扩容前需要把认领机制换成外部队列
- [ ] SQLite 在高并发写下仍可能锁库；家庭自用场景足够
- [ ] 微信订阅消息：`wechat.py` 保留函数，前端授权流程未接入
