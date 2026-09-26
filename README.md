# Leo 学习任务服务

微信小程序**或网页版**提交学习任务 → 本服务做鉴权、附件与任务管理 → **Hermes Agent 执行学习技能**（作业批改、错题解析、周报、考前训练、复测）→ 结果、归档与交付状态返回前端。

> 网页版与小程序共用同一套接口、同一份学习记录，区别只在登录方式：网页版用配置密码，小程序用微信 code。

> 完整设计见 [DESIGN.md](./DESIGN.md)。技能规则在 `hermes/skills/leo-study-assistant/`。
> 本服务**不实现 Agent 推理**，也**不会**把模型自述当作已完成归档或已发送邮件。

## 1. 架构

```
小程序 ──HTTPS──► FastAPI（会话鉴权 / 附件 / 任务队列 / 结果校验 / 受控归档）
                        │                        │
                        │ SQLite + 文件            │ 内部 HTTP（仅本机）
                        ▼                        ▼
                 data/ 与 workspace/        Hermes Agent（技能 + 工具 + 底层模型）
```

职责边界：

| 组件 | 负责 | 不负责 |
|---|---|---|
| 小程序 / 网页版 | 提交材料、显示真实状态、下载成果 | 不持有 Hermes 地址/密钥 |
| 本服务 | 身份与归属、配额幂等、队列、结果校验、归档落盘 | 不伪造进度，不代发邮件/代提交 Git |
| Hermes | 加载技能、多轮推理、产出结构化结果 | 不直接写学习记录 |

## 2. 本地跑起来（不联网）

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

## 4. 配置要点（config.yaml）

| 配置 | 说明 |
|---|---|
| `engine.mode` | `hermes`（默认）或 `legacy`（旧的多模型直连，结果会标注未执行技能流程） |
| `hermes.agent_model` | Hermes 的 Agent 别名（默认 `hermes-agent`），**不是**底层模型 ID |
| `hermes.verify_skill` | 是否用 `/v1/skills` 校验技能已安装 |
| `auth.allowed_openids` | 允许使用的微信 openid 白名单；为空=不限制（仅开发） |
| `workspace.dir` | 授权学习工作区（学习记录与归档） |
| `delivery.{pdf,email,git}_enabled` | 外部交付开关，默认全关；未启用时结果中标注未配置 |
| `limits.*` | 图片数量/大小、轮次、单任务时长、执行器开关 |

## 5. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `code` → 会话令牌（已配微信时失败即拒绝，不再降级开发身份） |
| POST | `/api/logout` | 失效当前会话 |
| GET | `/api/web/meta` | 网页版公开信息（标题、开关、是否已配密码），不含任何密钥 |
| POST | `/api/web/login` | 网页版登录：`password`（+可选 `user`）→ 会话令牌 |
| POST | `/api/assets` | 上传单张图片 → `asset_id` |
| POST | `/api/study/tasks` | 创建学习任务，可带 `Idempotency-Key` |
| POST | `/api/tasks/{id}/followups` | 追加补充材料，创建新执行轮次 |
| POST | `/api/tasks` | 旧版单图入口，复用同一鉴权与任务流程 |
| GET | `/api/tasks/{id}` | 任务详情：状态、轮次、五态结果、交付状态、成果列表 |
| GET | `/api/tasks` | 历史（分页） |
| GET | `/api/tasks/{id}/artifacts/{aid}` | 下载通过校验的成果文件 |
| GET | `/api/quota` | 剩余可用次数 |
| GET | `/api/runtime` | 脱敏运行状态（引擎/技能就绪/交付开关/限额） |
| GET | `/api/providers` | 仅反映 legacy 直连配置，**不代表 Hermes 就绪** |
| POST/GET | `/api/mistakes` | 错题本（人工收藏索引） |
| GET | `/healthz` | 存活探针（不代表技能可用） |

除 `/api/login`、`/api/web/meta`、`/api/web/login`、`/healthz` 与网页版静态资源外，全部需要 `Authorization: Bearer <token>`。

任务状态：`pending`（排队）→ `grading`（执行中）→ `done` / `waiting_input`（待补充材料）/ `failed` / `interrupted`（结果未确认）。

## 6. 网页版（IP 直连，免小程序备案）

浏览器直接打开 `http://<服务器IP>:<端口>/` 即可使用，功能与小程序一致：学习（任务类型/图片/文字/范围）、结果（状态轮询、五态、核查、补充材料、成果下载）、历史、错题本、我的。

准备两步：

1. 配置访问密码（**必填**，未配置时网页登录接口一律拒绝）：

```bash
# .env
WEB_PASSWORD=<一段足够长的随机密码>
```

2. 让服务监听外网地址：

```bash
# .env
APP_HOST=0.0.0.0
```

```bash
docker compose up -d --force-recreate   # 改完 .env 必须重建容器
```

然后访问 `http://<服务器IP>:8000/`（会自动跳到 `/web/`）。

| 配置项（config.yaml `web`） | 说明 |
|---|---|
| `enabled` | 总开关，默认 `true`；设 `false` 时网页登录接口直接拒绝 |
| `password` | 访问密码，**必须**从环境变量 `WEB_PASSWORD` 注入，不写入仓库 |
| `user` | 网页账号名，最终身份为 `web:<user>`，与微信 openid 隔离 |
| `title` | 页面标题 |
| `allowed_origins` | 前后端分离部署时的跨域白名单；同源部署留空（默认） |

本地用 Python 直接运行（不经 Docker 时 `.env` **不会**自动加载，需手动导出变量）：

```bash
export WEB_PASSWORD=<密码>
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

页面：学习（任务类型/图片/文字/范围）、结果（状态、五态、核查、补充材料、成果）、历史、错题本、我的（运行状态）。

## 8. 部署（Linux，容器 host 网络）

```bash
git clone <本仓库> /opt/study-assistant && cd /opt/study-assistant
cp config.example.yaml config.yaml
cp .env.example .env        # 填 HERMES_API_KEY；用网页版还需填 WEB_PASSWORD
mkdir -p data workspace
docker compose up -d --build
docker compose logs -f
curl -s localhost:8000/api/runtime   # 需要令牌，也可直接看日志中的就绪提示
```

- 容器用 `network_mode: host` 才能访问宿主机的 Hermes（`127.0.0.1:8642`）；macOS 本地请直接用 Python 运行
- 默认只监听 `127.0.0.1:8000`，公网由 Caddy/Nginx 反向代理 + 自动 HTTPS
- **要用 `http://服务器IP:8000` 直连**：在 `.env` 里设 `APP_HOST=0.0.0.0`，同时
  ① 云防火墙只放行你的出口 IP（不要用 0.0.0.0/0）
  ② 用**网页版**必须配置 `WEB_PASSWORD`（未配置时网页登录一律拒绝）
  ③ 用**小程序**必须配置 `WECHAT_SECRET`——否则任何人伪造 `code` 就能登录并消耗额度
  ④ http 下令牌是明文传输，长期使用请换 https + 域名
- **`.env` 改动后必须 `docker compose up -d --force-recreate`**，仅 `restart` 不会更新环境变量
- 改技能需两步：更新 `hermes/skills/` 里的文件 + 重新安装到 Hermes profile（或挂载同一目录）

## 9. 常见问题

| 现象 | 排查 |
|---|---|
| 登录 401 | 已配微信时 code 无效即拒绝；确认 `WECHAT_SECRET` 正确 |
| 网页版提示「未配置网页访问密码」 | `.env` 里的 `WEB_PASSWORD` 为空；填好后必须 `--force-recreate` |
| 网页版打不开 `/` | 是否设了 `APP_HOST=0.0.0.0`、云防火墙是否放行；容器内是否包含 `web/` 目录 |
| 网页版密码输错多次后无法登录（429） | 防爆破临时锁定，等 5 分钟或重启服务 |
| `/api/runtime` 显示 `skill_missing` | 技能没装到 Hermes profile，或 Hermes 未重启/未开新会话 |
| 任务一直 `pending` | 执行器是否启用（`limits.worker_enabled`）、容器是否在运行 |
| 任务 `interrupted` | 执行超时或服务重启；**不会自动重试**，避免重复归档，可补充材料后重发 |
| 结果 `failed` 且提示协议校验失败 | 模型输出不含合法结果 JSON 或违反五态/错因规则，查看 `error` |
| 归档没写入 | 检查 `archive.suggested_path` 是否在允许目录、是否越界 |
| 改了 `.env` 没生效 | 必须 `--force-recreate` 重建容器 |

## 10. 目录结构

```
StudyAssistant/
├── app/
│   ├── main.py          启动、迁移、工作区初始化、执行器生命周期
│   ├── config.py        配置模型（engine/hermes/auth/web/limits/delivery）
│   ├── schemas.py       结果协议 v2、请求模型、旧结果兼容
│   ├── auth.py          会话令牌、白名单、归属校验
│   ├── hermes.py        Hermes 适配：就绪检查、执行、错误分类
│   ├── tasks.py         幂等创建、数据库认领执行、轮次与视图
│   ├── workspace.py     工作区、附件、受控归档、成果登记
│   ├── migrations.py    版本化增量迁移（旧库先备份）
│   ├── db.py            SQLite 数据层
│   ├── providers.py     legacy：OpenAI 兼容协议封装
│   └── grading.py       legacy：单轮批改与 JSON 校验
├── hermes/skills/leo-study-assistant/   技能副本（SKILL.md + references/）
├── miniprogram/                          微信小程序（5 页）
├── web/                                  网页版（index.html + app.js + styles.css）
├── tests/                                离线测试
├── test_smoke.py                         端到端烟雾测试（模拟 Hermes）
├── config.example.yaml / .env.example
└── Dockerfile / docker-compose.yml
```

## 11. 尚未实现（如需启用请另行授权）

- 与真实 Hermes 的联调（版本、工具权限、模型工具调用能力）
- 二次核查模型映射（技能指定的 IDE 模型名不是 API 型号）
- PDF 生成、云端邮件、学习记录 Git 同步
- 多家庭隔离（当前定位为家庭自用）
