# Leo 学习任务服务 · 技术方案与设计文档

> 版本：v0.2.0（接入 Hermes 技能执行）| 日期：2026-09-26
> 目标读者：个人开发者（家庭自用，业余时间维护）
> v0.1 的「多模型直连批改」保留为 legacy 引擎，见 §7

---

## 1. 项目目标与范围

微信小程序提交学习任务 → 云服务器上的业务后端负责身份、附件与任务 → 由 **Hermes Agent 执行学习技能**（批改、错题解析、周报、考前训练、复测）→ 结果与归档状态返回小程序。

- 技能规则来自 `hermes/skills/leo-study-assistant/`（本仓库副本，已解除与本机 IDE 的耦合）
- 业务后端不实现 Agent 推理，只负责：鉴权、附件、任务队列、结果校验、受控归档、状态如实呈现
- 本轮不做：服务器部署、真实 Hermes 联调、PDF 生成、云端邮件、学习记录 Git 同步（能力开关默认关闭）

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
| 小程序 | 提交材料、展示状态与结果、下载成果 | 不持有 Hermes 地址/密钥，不直接调用 Agent |
| 业务后端 | 身份与归属、配额、幂等、队列、结果校验、归档落盘 | 不实现 Agent 工具循环，不伪造进度 |
| Hermes Agent | 加载技能与工具、多轮推理、产出结构化结果 | 不直接写学习记录（写入由后端校验后执行） |

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
| `ready` | 地址、密钥、技能三者齐备 |

### 3.3 错误分类与重试边界

| 异常 | 可确认「未执行」 | 处理 |
|---|---|---|
| `HermesNotConfigured` / `HermesAuthError` | 是 | 任务 `failed`，退还预留次数 |
| `HermesRejected`（4xx） | 是 | 任务 `failed`，退还预留次数 |
| `HermesUnavailable`（5xx）/ 超时 / 连接中断 | 否 | 任务 `interrupted`，**不自动重发**，不退还次数 |
| `HermesResultInvalid` | 否 | 任务 `failed`，记录校验原因 |

关闭 HTTP 连接不代表远端取消执行，因此「结果未确认」一律按未确认上报。

---

## 4. 结果协议（schema_version = 2）

```json
{
  "schema_version": 2,
  "task_type": "grading",
  "questions": [{
    "id": "math-p12-q1", "no": "1", "source": "9月3周作业", "page": "P12",
    "student_answer": "x=5", "status": "wrong",
    "correct_answer": "x=4", "steps": ["2x=8", "x=4"],
    "error_rule": "移项时忘记变号", "knowledge_point": "一元一次方程",
    "review": {"state": "agreed", "note": "核查未发现异议", "basis": "由 2x=8 得 x=4"},
    "final_decision": "kept_wrong", "final_decision_basis": "复核后维持原判定"
  }],
  "review_summary": {"state": "completed", "scope": 1, "disagreed": 0, "unverified": 0},
  "archive": {"suggested_path": "数学/错题解析/2026-09-26.md", "action": "append",
              "content_markdown": "..."},
  "delivery": {"pdf": {"status": "not_configured"}, "email": {"status": "not_configured"},
               "git": {"status": "not_configured"}}
}
```

强制约束（校验不通过即任务失败，不入库）：

- 题目五态：`correct` / `wrong` / `unanswered` / `uncertain` / `unprocessed`
- 判错题必须给出 `correct_answer` 或 `steps`，且 `error_rule` 不能是「粗心」这类笼统表述
- 未作答与存疑题不得标为 `kept_wrong`
- 核查有异议（`review.state=disagreed`）必须给出可核验依据
- 题目 `id` 唯一；`overview` 若填写则必须与逐题统计一致（后端会重算）
- 旧结果（v1）只读转换展示，并明确标注「未记录二次核查」

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
| 归档 | 只允许 `学科/{错题解析,周报分析,强化训练}/YYYY-MM-DD*.md`；读—合并—原子替换；同一轮次重复写会被标记跳过 |
| 成果 | 只有授权目录内真实存在、类型与大小合规的文件才登记下载 |
| 交付状态 | 服务端未启用的 PDF/邮件/同步一律 `not_configured`，模型自述不作为成功依据 |
| 密钥 | Hermes 密钥只在服务端 `.env`；小程序只拿业务会话令牌；日志不打印令牌与原图 |

任务状态机：`pending → grading → (done | waiting_input | failed | interrupted)`；`waiting_input` 表示结果已产出但需要补充材料，可追加新轮次。

---

## 7. legacy 引擎（engine.mode = legacy）

v0.1 的「单图 + 多模型直连」路径保留，用于显式回退：

- `app/providers.py` 仍按 OpenAI 兼容协议封装多家厂商，`provider_chain()` 决定顺序与备胎
- 结果会通过 `grading_result_to_v2()` 转成 v2 结构，并标注「旧模式不执行技能流程与二次核查」
- 默认 `engine.mode: hermes`；Hermes 不可用时**不会静默退回** legacy，必须由配置显式切换

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

---

## 9. 测试与验证

| 层次 | 文件 | 说明 |
|---|---|---|
| 协议与错误分类 | `tests/test_hermes.py` | 用 `httpx.MockTransport` 模拟 Hermes，验证就绪判定、不重试、结果校验 |
| 任务链路 | `tests/test_tasks.py` | 鉴权归属、幂等、配额结算/退还、执行器结果、中断恢复、补充材料 |
| 工作区 | `tests/test_workspace.py` | 路径越界、归档追加幂等、成果真实性 |
| 迁移 | `tests/test_migrations.py` | 旧库备份升级、旧任务保留 |
| 端到端 | `test_smoke.py` | TestClient + 模拟 Hermes 跑通完整闭环（不联网） |

**验证边界**：以上全部为离线模拟验证，不代表真实 Hermes 版本、工具权限、模型工具调用能力已验证。

---

## 10. 已知限制 / 待办

- [ ] 未与真实 Hermes 联调：实例版本、运行用户、技能安装路径、沙箱能力需部署前核实
- [ ] 二次核查模型映射未确认：技能指定的 IDE 模型名不是 API 型号，未配置时如实报告「核查未完成」
- [ ] PDF 生成、云端邮件、学习记录 Git 同步未实现（开关默认关闭）
- [ ] 错题本仍是人工收藏索引，尚未与归档记录双向关联
- [ ] 单进程单并发执行器：横向扩容前需要把认领机制换成外部队列
- [ ] SQLite 在高并发写下仍可能锁库；家庭自用场景足够
- [ ] 微信订阅消息：`wechat.py` 保留函数，前端授权流程未接入
