# Leo 学习任务服务

微信小程序**或网页版**提交学习任务 → 本服务做鉴权、附件与任务管理 → **Hermes Agent 执行学习技能**（作业批改、错题解析、周报、考前训练、复测）→ 结果校验、归档、受控 Git 同步与台账更新，状态如实返回前端。

> 网页版与小程序共用同一套接口、同一份学习记录，区别只在登录方式：网页版用配置密码，小程序用微信 code。

> 完整设计见 [DESIGN.md](./DESIGN.md)。技能规则在 `hermes/skills/leo-study-assistant/`。
> 本服务**不实现 Agent 推理**，也**不会**把模型自述当作已完成归档或已发送邮件。

## 1. 架构

```
小程序 ──HTTPS──► FastAPI（会话鉴权 / 附件 / 任务队列 / 结果校验 / 受控归档 / 台账 / 受控 Git）
                        │                        │
                        │ SQLite + 文件            │ 内部 HTTP（仅本机）
                        ▼                        ▼
                 data/ 与 workspace/        Hermes Agent（技能 + 工具 + 底层模型）
```

职责边界：

| 组件 | 负责 | 不负责 |
|---|---|---|
| 小程序 / 网页版 | 提交材料、显示真实状态、复习台账与复测登记、下载成果 | 不持有 Hermes 地址/密钥 |
| 本服务 | 身份与归属、配额幂等、队列、结果校验、归档落盘、台账去重、受控提交推送 | 不伪造进度，不代发邮件，不把模型自述当作已提交 |
| Hermes | 加载技能、多轮推理、产出结构化结果 | 不直接写学习记录、不执行 git |

## 2. 本地跑起来（不联网）

图片批改会用 Tesseract OSD 自动识别并校正试卷文字的 0°/90°/180°/270°方向。
本地运行前需安装 Tesseract，并包含英文、简体中文和 OSD 语言数据；Docker 镜像会自动安装。
Debian/Ubuntu 可执行 `sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-osd`。
方向识别置信度不足时，服务会用视觉模型补充判断一次；仍不能确认则进入“确认页面方向”，
在网页或小程序里旋转预览、确认后继续同一轮批改。图片保留，不重复上传或扣任务次数。
每页最多一次补充判向调用，不做模型切换或自动重试；确认角度统一为顺时针。
`staged_grading.orientation_provider` 可指定直接支持图片输入的模型，留空则选模型链中首个
`supports_vision: true` 的 provider；不支持图片的 provider 应配置 `supports_vision: false`。
`orientation_visual_fallback: false` 可关闭视觉补判，直接等待人工确认。
复查要求直接读取已确认方向的图片；只能获取图片文字摘要时必须标“未能核验”。
真实网关是否保留图像输入仍需在部署环境核实，配置声明和模型自述不能独立证明它看到了图片。
开启 `SA_DEBUG_THINKING=1` 后，判向和各阶段产出也写入思考日志，带统一的任务 ID、轮次、
`session=study-<task_id>-<run_no>`；切分脚本会把同一轮的判向、思考与阶段产出合并为一个文件。

```bash
cd StudyAssistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
cp .env.example .env          # 本地离线验证可以不填任何密钥

# 离线验证：单元测试 + 端到端烟雾测试（全部使用模拟 Hermes）
python -m unittest discover -s tests -t .
python test_smoke.py
```

真的想连本地 Hermes 时：

```bash
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

> 使用 `--factory`：导入模块时不加载配置，缺 `config.yaml` 会在启动阶段给出清晰报错。

## 3. 接入 Hermes（关键前置）

本服务调用的是 **Hermes Agent API**，不是大模型 API。默认 `http://127.0.0.1:8642`，且只应服务端内部访问。

1. 开启 Hermes 的 API Server（默认关闭）并设置密钥：

```yaml
# ~/.hermes/config.yaml
gateway:
  api_server:
    enabled: true
    key: "<API_SERVER_KEY>"
```

```bash
# .env（本服务）
HERMES_BASE_URL=http://127.0.0.1:8642
HERMES_API_KEY=<API_SERVER_KEY>
```

2. 安装学习技能到 Hermes 的 profile 技能目录（**放进应用镜像不等于 Hermes 已加载**）：

```bash
mkdir -p ~/.hermes/skills/leo-study-assistant
cp -r hermes/skills/leo-study-assistant/* ~/.hermes/skills/leo-study-assistant/
# 已安装技能在新会话生效
```

3. 确认技能真的被识别：

```bash
curl -s http://127.0.0.1:8642/v1/skills -H "Authorization: Bearer $HERMES_API_KEY"
# 然后在小程序「我的」页或 GET /api/runtime 看 hermes.state 是否为 ready
```

| `hermes.state` | 含义 | 处理 |
|---|---|---|
| `not_configured` | 未配地址/密钥 | 填 `.env` |
| `unreachable` | 连不上 | 检查 Hermes 是否在跑、地址是否正确 |
| `skill_missing` | 连上但技能未安装 | 按上面第 2 步安装技能 |
| `auth_failed` | 网关可达，但密钥不一致 | 核对 `HERMES_API_KEY` 与 Hermes 的 `gateway.api_server.key` |
| `skill_unknown` | 网关可用，但 `/v1/skills` 接口异常，无法确认技能状态（任务仍会尝试执行） | 查 Hermes 日志，必要时 `hermes doctor` / `hermes update` |
| `ready` | 可用 | — |

> 就绪判定分三步：先用 `/health`（依次回退 `/v1/health`、`/v1/capabilities`）判断网关是否**有响应**（非 2xx 也算活着），再识别密钥是否有效，最后才枚举技能。**探针返回 5xx 或技能列表接口坏了，都不会被误报成「连不上」，也不会阻止任务执行。**

### 3.1 服务端二次复查（第二模型，可选）

首轮批改通过协议校验后，服务端会把**判错题与存疑题**的「提取转写（题干/学生作答）+ 首轮批改结论」
交给另一个模型做纯文字复查（不再读原图，只核查转写与结论是否自洽）；
复查只提异议、不改判，结论写进结果与归档（详见 `hermes/skills/leo-study-assistant/references/review-rules.md`）。

配置（`config.yaml` 的 `hermes` 段，全部留空 = 不复查，结果如实标 `not_run`）：

```yaml
hermes:
  # 方式一：网关 model_routes 别名（推荐；网关侧配 glm: {provider: zai, model: glm-5.3}）
  review_model: "glm"
  review_model_options: {"reasoning": {"effort": "high"}}
  review_expected_model: "glm-5.3"     # 期望别名解析到的底层模型（用于身份核验）
  review_expected_provider: "zai"
  # 方式二：底层模型 ID + provider（需网关开启 direct_model_requests）
  # review_model: "glm-5.3"
  # review_provider: "zai"
  review_timeout_seconds: 300          # 实际取 min(此值, 任务剩余预算)
  review_max_questions: 30             # 超出部分如实标未送审，判错题优先
```

启用前必须在真实环境确认（离线测试通过 ≠ 可用）：

1. **路由生效**：只给 `model` 不给 `provider` 会被网关静默忽略（回落全局默认模型）；
   `model_routes` 别名只需 `model`，改完要 `hermes gateway restart`。
2. **effort 兼容**：GLM-5.3 / 5.3-flash 只接受 `low / high / max`，`medium` 会 400（code 1210）；
   用 `zai`，不要用自定义端点的 `zhipu`（会绕过 GLM 专属适配）。
3. **身份可核验**：结果里的「实际模型」来自网关报告；报告缺失或与首轮相同都会按
   「身份未确认 / 路由不符」如实标注为复查失败，不用请求值冒充。
   网关**只回 `model` 不回 `provider`** 时不算失败：模型名与 `review_expected_model`
   一致且不同于首轮模型即按 `model_only` 采纳，结果里标注「模型已核对（网关未报告 provider）」。只在网关确实回了 provider 且与 `review_expected_provider` 不符时才判「路由不符」。
4. **转写二次确认**：复查分两步（一次调用内完成）——先对照作业原图逐题重读学生作答，
   核对提取转写是否识别错误（如卷面是 A、转写成 B），再核查首轮结论；
   复查模型需要图片链路。拿不到原图时退化为纯文字核查（`coverage=transcript_only`）。
5. **工具隔离**：只读提示词不是权限控制，启用前核实复查会话的实际工具权限。

复查失败不影响首轮成果：任务照常归档入台账，`review_summary.state` 如实标 `failed`，
小程序与网页的结果页都会展示复查状态、异议依据与模型身份。

## 4. 配置要点（config.yaml）

| 配置 | 说明 |
|---|---|
| `engine.mode` | `hermes`（默认）或 `legacy`（旧的多模型直连，结果会标注未执行技能流程） |
| `hermes.agent_model` | Hermes 的 Agent 别名（默认 `hermes-agent`），**不是**底层模型 ID |
| `hermes.verify_skill` | 是否用 `/v1/skills` 校验技能已安装 |
| `hermes.review_model` 等 | 服务端二次复查（第二模型），**默认留空不启用**；配置方式见下节 |
| `auth.allowed_openids` | 允许使用的微信 openid 白名单；为空=不限制（仅开发） |
| `workspace.dir` | 授权学习工作区；归档按账号隔离（`<dir>/<账号>/<学科>/…`，见下节；原题目录仅本地） |
| `family.{default_grade_level,subjects,term_start_date}` | 家庭学习配置默认值；小程序「学习设置」保存后覆盖（学期起始日期用于期中/期末默认区间） |
| `git.{enabled,remote,timeout_seconds,author_*}` | 受控学习记录同步；`enabled` 未配置时回落到 `delivery.git_enabled`，默认关闭 |
| `delivery.{pdf,email}_enabled` | 外部交付开关，本轮默认关闭；未启用时结果中标注未配置 |
| `limits.*` | 图片数量/大小、轮次、单任务时长、执行器开关 |

> 配置里的环境变量只支持 `${VAR}` / `${VAR:-默认值}` 花括号写法。**不支持裸 `$VAR`**——它会把
> `password_hash` 里的 `$<salt_hex>` 当作变量名替换掉（盐以 a-f 开头时命中），导致网页版密码永远提示不正确。

### 工作区与账号隔离

- **归档按账号分目录**：`<workspace.dir>/<账号>/<学科>/{错题解析,周报分析,强化训练}/YYYY-MM-DD*.md`。
  账号目录名由身份派生：网页账号 `web:leo` → `leo`，微信身份 → `wx-<openid>`，无法派生时回落 `family`。
- 技能产出的 `archive.suggested_path` 仍只写 **3 段**（`学科/子目录/文件名.md`），账号层由服务端按任务身份前置拼接；
  路径越界（`..`、绝对路径、写进别人账号目录）一律拒绝并如实报错。
- 所有账号**共用同一个 Git 仓库**；`README.md`、`.gitignore`、`冲突记录-*.md` 仍在工作区根，属全局文件。
- 骨架（`<账号>/<学科>/…` 与原题目录）在该账号首次登录时创建，服务启动时也会为所有已知账号补齐。
- 旧版（根级学科目录）迁移 —— 脚本依赖 pydantic/Pillow，**在容器里跑**（镜像已含 `scripts/`）：

```bash
cd /opt/study-assistant && git pull
docker compose up -d --build && docker compose stop grader
# 先预演，只打印计划
docker compose run --rm --no-deps --entrypoint python3 grader \
    scripts/migrate_workspace_accounts.py --account leo
# 确认后实际执行：搬目录 + 升级 .gitignore + 改写库内路径
docker compose run --rm --no-deps --entrypoint python3 grader \
    scripts/migrate_workspace_accounts.py --account leo --apply
docker compose up -d --build --force-recreate grader
```

脚本幂等、改库前自动备份（`app.db.bak-<时间戳>`）、**不自动 git 提交**，结束后会打印建议命令。

### 学习记录同步（受控 Git）

开启 `git.enabled` 后，任务归档成功即执行提交推送：**只 `git add -- <本次归档文件>`**，
禁止全量暂存与 `git add -f`；提交前校验分支、上游远端与暂存区（存在他人改动则停止）；
推送不带 force，不做 merge/rebase，不改 Git 配置。
遇内容冲突或分支分歧（非快进）时在工作区根生成 `冲突记录-YYYY-MM-DD-HHmmss.md` 并停止上传，
`delivery.git` 如实返回 `committed / failed / skipped / not_configured`（含 `pushed`、`commit`、`conflict_record`）。

## 5. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `code` → 会话令牌（已配微信时失败即拒绝，不再降级开发身份） |
| POST | `/api/logout` | 失效当前会话 |
| GET | `/api/web/meta` | 网页版公开信息（标题、开关、是否已配账号），不含任何密钥与账号名单 |
| POST | `/api/web/login` | 网页版登录：`username` + `password`（须在 `web.users` 名单内）→ 会话令牌 |
| POST | `/api/assets` | 上传单张图片 → `asset_id` |
| POST | `/api/study/tasks` | 创建学习任务，可带 `Idempotency-Key` |
| POST | `/api/tasks/{id}/followups` | 追加补充材料，创建新执行轮次 |
| POST | `/api/tasks` | 旧版单图入口，复用同一鉴权与任务流程 |
| GET | `/api/tasks/{id}` | 任务详情：状态、轮次、五态结果、交付状态、成果列表 |
| GET | `/api/tasks` | 历史（分页） |
| GET | `/api/tasks/{id}/artifacts/{aid}` | 下载通过校验的成果文件 |
| GET | `/api/quota` | 剩余可用次数 |
| GET | `/api/runtime` | 脱敏运行状态（引擎/技能就绪/交付开关/Git/工作区骨架/家庭默认值/限额） |
| GET/PUT | `/api/settings` | 家庭设置：学期起始日期、默认年级、学科清单（未保存时回落配置默认值） |
| GET | `/api/ledger` | 复习台账：按学科与订正状态筛选，返回条目与状态计数 |
| GET | `/api/ledger/{id}` | 台账条目详情与复测事件历史 |
| POST | `/api/ledger/{id}/events` | 登记一次真实复测/订正：追加事件、更新状态并写入关联归档文件 |
| GET | `/api/providers` | 仅反映 legacy 直连配置，**不代表 Hermes 就绪** |
| POST/GET | `/api/mistakes` | 错题本（人工收藏索引，与自动台账分开） |
| GET | `/healthz` | 存活探针（不代表技能可用） |

除 `/api/login`、`/api/web/meta`、`/api/web/login`、`/healthz` 与网页版静态资源外，全部需要 `Authorization: Bearer <token>`。

任务状态：`pending`（排队）→ `grading`（执行中）→ `done` / `waiting_input`（待补充材料）/ `failed` / `interrupted`（结果未确认）。

## 6. 网页版（IP 直连，免小程序备案）

浏览器直接打开 `http://<服务器IP>:<端口>/` 即可使用，功能与小程序一致：学习（任务类型/图片/文字/范围）、结果（状态轮询、五态、核查、补充材料、成果下载）、历史、错题本、我的。

登录方式：**预设账号白名单**（用户名 + 密码）。一个账号 = 一个孩子，数据身份为 `web:<username>`，与微信 openid 隔离；密码只存 `pbkdf2_sha256` 哈希，不写明文。`web.users` 为空时登录页只显示提示、登不进去。

准备三步：

1. 生成账号片段（每个孩子一个账号，可重复执行添加多个）：

```bash
python3 scripts/make_web_user.py --username leo --display-name Leo
# 按提示输入两次密码（不回显），把输出的片段粘到 config.yaml 的 web.users: 下面
```

2. 让服务监听外网地址：

```bash
# .env
APP_HOST=0.0.0.0
docker compose up -d --force-recreate   # 改完 .env 必须重建容器
```

3. 改完 `config.yaml` 后**重建容器**（不要只 `restart`）：

```bash
docker compose up -d --force-recreate grader
```

> `config.yaml` 是以**单文件**方式挂载进容器的；主机上用 vim / `sed -i` 保存会替换文件（inode 变了），容器里仍指向旧文件，
> `docker compose restart` 不会重新挂载，改动看起来「没生效」。重建容器才会重新绑定。改 `.env` 同样必须重建。

然后访问 `http://<服务器IP>:8000/`（会自动跳到 `/web/`）。

| 配置项（config.yaml `web`） | 说明 |
|---|---|
| `enabled` | 总开关，默认 `true`；设 `false` 时网页登录接口直接拒绝 |
| `title` | 页面标题 |
| `users` | 账号名单，每项含 `username` / `display_name` / `password_hash`（用 `scripts/make_web_user.py` 生成，只存哈希） |
| `allowed_origins` | 前后端分离部署时的跨域白名单；同源部署留空（默认） |

> `config.yaml` **不进版本库**，`git pull` **不会**更新服务器上那一份。所以新增/修改账号都要手工编辑服务器上的 `config.yaml`，改完重启服务生效。
> `username` 只允许 1~32 位字母数字与 `-_`，最终身份为 `web:<username>`；密码不写明文，只存哈希。

本地用 Python 直接运行：

```bash
python3 scripts/make_web_user.py --username leo --display-name Leo   # 生成片段贴进 config.yaml
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
# 浏览器访问 http://127.0.0.1:8000/
```

安全须知：

- 网页登录接口有简单的防爆破：同一来源连续输错 10 次锁定 5 分钟。
- **http 直连时令牌是明文传输**，务必同时用云防火墙只放行你自己的出口 IP（不要 `0.0.0.0/0`）。
- 正式长期使用建议仍换成 https + 已备案域名；备案是**小程序**上线的要求，网页版用 IP 只是权宜之计。

## 7. 小程序

1. 微信开发者工具导入 `miniprogram/`，填入 AppID
2. 改 `miniprogram/utils/config.js` 的 `BASE_URL`（**只填本服务地址，不要填 Hermes**）
3. 调试时勾选「不校验合法域名」；上线前完成备案与域名配置（`request` + `uploadFile`）

页面：学习（任务类型、训练类型、资料区间、考试范围、图片与文字）、复习（错题台账、按学科与订正状态筛选、登记复测结果）、结果（状态、五态、订正与复测、二次复查状态与异议依据、补充材料、成果、Git 同步状态）、历史、我的（学习设置入口、运行与同步状态）、学习设置（学期起始日期/年级/学科清单）；错题本页保留人工收藏的阅读入口。

> 网页版（`web/`）已具备任务结果（含二次复查展示）、复习台账与历史页面；「学习设置」暂仅小程序提供。接口保持向后兼容（新增字段只会被忽略）。

## 8. 部署（Linux，容器 host 网络）

```bash
git clone <本仓库> /opt/study-assistant && cd /opt/study-assistant
cp config.example.yaml config.yaml
cp .env.example .env        # 填 HERMES_API_KEY；网页账号在 config.yaml 的 web.users 里配（见第 6 节）
mkdir -p data workspace
docker compose up -d --build
docker compose logs -f
curl -s localhost:8000/api/runtime   # 需要令牌，也可直接看日志中的就绪提示
```

- 容器用 `network_mode: host` 才能访问宿主机的 Hermes（`127.0.0.1:8642`）；macOS 本地请直接用 Python 运行
- 默认只监听 `127.0.0.1:8000`，公网由 Caddy/Nginx 反向代理 + 自动 HTTPS
- **要用 `http://服务器IP:8000` 直连**：在 `.env` 里设 `APP_HOST=0.0.0.0`，同时
  ① 云防火墙只放行你的出口 IP（不要用 0.0.0.0/0）
  ② 用**网页版**必须在 config.yaml 的 `web.users` 里配好账号（未配置时网页登录一律拒绝）
  ③ 用**小程序**必须配置 `WECHAT_SECRET`——否则任何人伪造 `code` 就能登录并消耗额度
  ④ http 下令牌是明文传输，长期使用请换 https + 域名
- **`.env` 改动后必须 `docker compose up -d --force-recreate`**，仅 `restart` 不会更新环境变量
- 改技能需两步：更新 `hermes/skills/` 里的文件 + 重新安装到 Hermes profile（或挂载同一目录）；`scripts/update-and-logs.sh` 会把这一步一起做掉

### 日常更新：一条命令

```bash
cd /opt/study-assistant && scripts/update-and-logs.sh
```

它按顺序做四件事，并把每一步的真实结果打印出来：

1. `git pull --ff-only` 拉取最新代码（无新提交时跳过重建，直接进日志）；
2. 把 `hermes/skills/leo-study-assistant/` 同步到 `~/.hermes/skills/leo-study-assistant/`（找不到 Hermes profile 技能目录时如实跳过并给出命令，不假装已同步）；
3. `docker compose up -d --build --force-recreate grader` 重建容器（重建才会重新挂载 `config.yaml`、重新注入 `.env`）；
4. 轮询 `http://127.0.0.1:$APP_PORT/healthz` 确认起来后，直接 `docker compose logs -f --tail=100 grader`（`Ctrl+C` 只退出看日志，容器继续运行）。

开关：`--dry-run`（只打印将执行的命令，零副作用）、`--no-skill`、`--no-follow`、`--force-rebuild`（没有新提交也重建）、`--allow-dirty`（工作区有未提交改动时放行）、`--tail N`、`--skill-dir DIR`、`--help`。

它**不**做这些事：不改 `config.yaml` 与 `.env`（二者不进版本库，`git pull` 不会更新服务器上那两份，账号配置要手工改）、不做数据库迁移（见上文 `scripts/migrate_workspace_accounts.py`）、不碰 git 历史（没有 `reset`/`checkout`）；工作区有已跟踪文件的未提交改动时默认中止，避免 pull 冲突或覆盖。

**推送思考日志**：`scripts/push-thinking-log.sh` 把服务器 `data/logs/thinking.log` 与 grader 容器日志（`docker compose logs`，默认最近 5000 行）推送到远端 `server-logs` 分支（`--all` 一并推轮转历史；`--branch` 改分支；`--no-grader` 只推 thinking.log；`--grader-tail N` 改行数；`--dry-run` 预演）。用 git worktree 做临时工作区，主工作树不动；日志无变化时跳过。注意推送需要该仓库的写权限。

**核对线上版本**：网页顶栏标题旁与"我的 → 关于"里显示服务端代码的 git 短哈希（如 `9689b9b`），构建时由 `scripts/update-and-logs.sh` 以 `--build-arg GIT_VERSION=` 烘入镜像（Dockerfile 的 `ARG GIT_VERSION`）；本地直接跑时回落到 `git rev-parse`。显示 `unknown` 说明构建时没传参，用脚本更新即恢复。

## 9. 常见问题

| 现象 | 排查 |
|---|---|
| 登录 401 | 已配微信时 code 无效即拒绝；确认 `WECHAT_SECRET` 正确 |
| 网页版提示「服务端尚未配置网页账号（web.users）」 | config.yaml 的 `web.users` 为空；用 `scripts/make_web_user.py` 生成账号片段粘进去，再重启服务 |
| 网页版登录提示「用户名或密码不正确」 | ① 用户名须与 `web.users` 里的 `username` 完全一致（**不是**中文 `display_name`，大小写敏感、别带空格）；② 密码首尾空格会被忽略，中英文输入法/全角字符会导致不一致；③ 浏览器可能自动填充了旧密码，清空后重输。提示不区分"用户名不存在"与"密码错误"是故意的（防账号枚举） |
| 改了 `web.users` 但登录状态没变化 | `config.yaml` 是**单文件挂载**，`docker compose restart` 不会重新挂载 → 用 `docker compose up -d --force-recreate grader` 重建容器 |
| 网页版密码核对无误，服务端仍返回 401 | 旧版配置解析会把哈希里的 `$<salt_hex>` 当环境变量吃掉（盐以 a-f 开头时命中）→ 更新代码后 `docker compose up -d --build --force-recreate grader`，`config.yaml` 无需改动 |
| 网页版打不开 `/` | 是否设了 `APP_HOST=0.0.0.0`、云防火墙是否放行；容器内是否包含 `web/` 目录 |
| 网页版密码输错多次后无法登录（429） | 防爆破临时锁定，等 5 分钟或重启服务 |
| `/api/runtime` 显示 `skill_missing` | 技能没装到 Hermes profile，或 Hermes 未重启/未开新会话 |
| 任务一直 `pending` | 执行器是否启用（`limits.worker_enabled`）、容器是否在运行 |
| 任务 `interrupted` | 执行超时或服务重启；**不会自动重试**，避免重复归档，可补充材料后重发 |
| 结果 `failed` 且提示协议校验失败 | 模型输出不含合法结果 JSON 或违反五态/错因规则，查看 `error` |
| 归档没写入 | 检查 `archive.suggested_path` 是否为 3 段合法路径（`学科/子目录/文件名.md`）、是否越界；账号层由服务端拼接，不要自己加 |
| 加了第二个账号，两个孩子的记录混在一起 | 归档已按账号分目录；若仍是旧布局，先 `git pull` + `docker compose up -d --build --force-recreate grader`，再用 `scripts/migrate_workspace_accounts.py` 迁移历史文件 |
| 改了 `.env` 没生效 | 必须 `--force-recreate` 重建容器 |
| 结果里复查状态是 `failed` / `not_run` | `not_run` = 未配置 `hermes.review_model`；`failed` = 调用失败、身份未确认/路由不符或复查输出未通过对账，看 `review_summary.note` 与服务端日志；首轮批改与归档不受影响 |
| 想看 Hermes 调用日志 | `docker logs <容器名>` 里找 `app.hermes:` 开头的行（含调用/完成/tokens/耗时）；日志级别由 `.env` 的 `LOG_LEVEL` 控制（默认 INFO），改完重建容器 |
| 想看模型的思考过程（分析判题问题） | `.env` 里加 `SA_DEBUG_THINKING=1` 后重建容器；思考内容写入 `data/logs/thinking.log`（按天轮转，最多保留 5 天），实时查看跑 `scripts/watch-thinking.sh`（脚本会先检查开关）。首尾有 `【模型思考过程】` / `【思考过程结束】` 标记，不再进 `docker logs`。分析完设回 `0` 并重建。不进数据库、不进批改结果 |
| 配了复查模型但显示「身份未确认」 | 网关响应没报告实际模型：配置 `review_expected_model` 并确认网关版本会返回模型身份；报告与首轮相同模型则是路由未生效（检查 `model_routes` 与 `direct_model_requests`） |
| 复查显示「模型已核对（网关未报告 provider）」 | 网关只回 `model` 不回 `provider`（常见于腾讯 tokenhub 等部署），复查照常采纳，只是 provider 这一层没核对；如需完全核验，让网关在响应里返回 `provider`，或忽略该提示 |
| 分阶段批改报 `max_tokens参数非法：限制数值范围[1,1024]` | 该 provider 的输出上限比阶段上限小（如 `glm-4v-flash` 只有 1024）：在 `llm.providers.<名>` 下配 `max_output_tokens`，或换用支持更大输出的模型。不配的话备胎一调用就被 400 拒绝，实际等于没有备胎 |
| 分阶段批改报「复核返回了未要求的题号」 | 已修：放大复核回传的题号（如「题1」）现在做有限映射；仍不匹配时只忽略该条并在提取阶段记录 `zoom_note`，不再让整单失败 |
| 分阶段批改报 `questions.N.page Input should be a valid string` | 已修：题号/页码等文本字段现在容错模型写成数字（`"page": 1`）；`steps`/`explanation` 写成单字符串也会包装成数组。旧版本这会让整阶段失败并切备胎 |
| 分阶段批改报 `Extra data: line 8 column 6` | 已修：模型在结果 JSON 后面又输出了一段 JSON/说明，现在按 `raw_decode` 取第一个完整对象，不再整体解析失败 |

## 10. 目录结构

```
StudyAssistant/
├── app/
│   ├── main.py          启动、迁移、工作区初始化、执行器生命周期
│   ├── config.py        配置模型（engine/hermes/auth/web/limits/delivery）
│   ├── schemas.py       结果协议 v2、请求模型、旧结果兼容
│   ├── auth.py          会话令牌、白名单、归属校验
│   ├── hermes.py        Hermes 适配：就绪检查、执行、错误分类
│   ├── tasks.py         幂等创建、数据库认领执行、轮次、视图与台账写入
│   ├── scope.py         资料区间计算（月考当月、期中期末学期、周报本周）
│   ├── git_sync.py      受控 Git 提交推送与冲突记录（只提交授权文件）
│   ├── workspace.py     工作区骨架（按账号）、附件、受控归档、复测追加、成果登记
│   ├── migrations.py    版本化增量迁移（旧库先备份；V3：家庭设置/台账/事件/Git 日志）
│   ├── db.py            SQLite 数据层（含台账去重与事件）
│   ├── providers.py     legacy：OpenAI 兼容协议封装
│   └── grading.py       legacy：单轮批改与 JSON 校验
├── hermes/skills/leo-study-assistant/   技能副本（SKILL.md + references/）
├── miniprogram/                          微信小程序（7 页）
├── web/                                  网页版（index.html + app.js + styles.css）
├── scripts/                              运维脚本（生成网页账号、迁移工作区到账号目录）
├── tests/                                离线测试
├── test_smoke.py                         端到端烟雾测试（模拟 Hermes）
├── config.example.yaml / .env.example
└── Dockerfile / docker-compose.yml
```

服务器上的学习记录（`docker-compose.yml` 里的 `./workspace`）结构：

```
workspace/
├── README.md                          # 学习规范（全局）
├── .gitignore                         # 原题目录忽略规则（含账号层，全局）
├── 冲突记录-*.md                       # 仅 Git 冲突时生成（全局）
└── <账号>/                             # leo / kid2 / wx-<openid>；一个账号一棵树
    └── <学科>/
        ├── 错题解析/2026-09-27.md
        ├── 周报分析/2026-09-21.md
        ├── 强化训练/2026-09-27-一元一次方程.md
        └── 原题/<年份>/                 # 仅本地，不进 Git（.gitkeep 占位）
```

私有数据仍在 `data/`：`app.db`、`uploads/<随机id>.jpg`、`runs/<task_id>/run<N>/`，**不进工作区、不进 Git**。

### 拆分思考日志

```bash
python3 scripts/split_thinking.py data/logs/thinking.log -o data/logs/thinking-sessions
# 也可以拆分导出的日志
python3 scripts/split_thinking.py thinking.txt -o data/logs/thinking-export-sessions
```

同一 `session=` 的模型调用合并为一个文件；没有会话 ID 的调用每段单独保存
（目前分阶段批改日志没有 ID，不能可靠还原完整批改会话）。原始日志不修改，
输出目录必须不存在，避免覆盖；残缺段仍保存并提示，段外内容保存为 `unassigned`。
不指定 `-o` 时，自动在输入文件旁新建带时间戳的目录。

### 数学示意图与失败排查

绘图继续优先使用 `llm.default_provider`（可保持现有 DS），再按 `fallback_order`
尝试备选模型。单个模型先用 4000 tokens；输出被截断或只有思考、没有正式输出时，
最多放大到 8000 tokens 重试一次。实际额度不超过该模型的 `max_output_tokens`；
达到配置上限时不重复同参数重试。无需新增配置，也不向网关发送未经确认的禁用思考参数。

模型只画题干明确描述的结构，不求解、不猜点位或阴影。不从思考内容中提取图形。
题干几何信息不足时明确提示补充原图或点位关系，不把猜测当成可靠的原图重绘。
当前绘图调用仍为纯文本；补充的图片需先由转写流程提供足够的几何关系。

`questions[].diagram_svg` 保存清洗后的静态 SVG，结果校验/保存/读取均保留该字段。
`questions[].diagram` 记录 `status`、`reason`、`message`、`provider` 和 `attempts`。
失败不会中断批改，但会在结果页显示原因；老任务可点击「生成示意图」补生成。
手动接口同时返回 `generated`、`failures`、`skipped` 和逐题 `results`，
只有确实无几何图形时才显示「无需绘图」。

离线回归：`python -m unittest tests.test_diagram tests.test_diagram_flow`；
页面函数回归：`node tests/test_diagram_web.js`。

## 11. 尚未实现（如需启用请另行授权）

- 与真实 Hermes 的联调（版本、工具权限、模型工具调用能力）
- 二次复查的**真实环境联调**（服务端编排已实现并通过离线测试）：`model_routes` 别名与 `direct_model_requests` 实际行为、响应身份字段语义、复查会话工具隔离；未确认前 `hermes.review_model` 保持留空
- PDF 生成与云端邮件（本轮不做，`delivery.pdf/email` 如实标注未配置）
- 网页版前端的学习设置界面（复习台账与任务结果已就绪，含二次复查展示）
- 家长聚合账号（一个家长看多个孩子）：归档已按账号隔离，但仍是一个账号 = 一个孩子，没有「家长视角」

> 学习记录 Git 同步已在服务端实现（受控提交推送 + 冲突记录），默认关闭，需在配置中显式开启。
