# 作业批改服务 · 快速开始

> 完整技术方案见 [DESIGN.md](./DESIGN.md)

## 1. 本地试跑（5 分钟）

```bash
cd homework-grader-server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 准备配置
cp config.example.yaml config.yaml
cp .env.example .env
# 编辑 .env，填入至少一家的 API Key（通义千问/智谱/DeepSeek/豆包）

# 烟雾测试（用模拟模型，不花钱）
python test_smoke.py

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 http://localhost:8000/docs 看交互式接口文档。

## 2. 配置多个大模型（核心操作）

编辑 `config.yaml` 的 `llm` 部分：

```yaml
llm:
  default_provider: "qwen"                    # 默认用谁，改这里
  fallback_order: ["qwen", "glm", "deepseek"] # 故障自动切换顺序
  providers:
    qwen:
      api_key: "${QWEN_API_KEY}"              # 去 .env 里填真实 key
      model: "qwen-vl-max"                    # 换新模型只改这一行
      enabled: true
    glm:
      api_key: "${GLM_API_KEY}"
      model: "glm-4v"
      enabled: true                            # 改 false 就停用这家
```

密钥统一放 `.env`（已加入 `.gitignore`，不会误提交）：

```
QWEN_API_KEY=sk-xxxx
GLM_API_KEY=xxxx
DEEPSEEK_API_KEY=sk-xxxx
DOUBAO_API_KEY=xxxx
```

改完配置重启服务即可：`docker compose restart`

用 `GET /api/providers` 确认哪些模型在线（不返回密钥）。

## 3. 部署到云服务器

```bash
# 1. 服务器装 Docker
curl -fsSL https://get.docker.com | sh

# 2. 把代码传上去（scp / git / 宝塔都行），然后：
cp config.example.yaml config.yaml   # 按第 2 节配好
cp .env.example .env                 # 填好 API Key
docker compose up -d --build
docker compose logs -f               # 看日志确认启动
```

**HTTPS（小程序强制要求）**，用 Caddy 一行搞定，自动申请续期证书：

```bash
# 安装 caddy 后执行：
caddy reverse-proxy --from https://api.你的域名 --to localhost:8000
```

**域名备案**：小程序线上调用要求域名已 ICP 备案（个人可办非经营性备案，免费约 2-4 周）。
开发调试时可在微信开发者工具勾选"不校验合法域名"先跑通。

## 4. 小程序对接约定

- 所有接口前缀 `/api`，图片用 `multipart/form-data` 上传
- 批改是异步的：`POST /api/tasks` 返回 `task_id` → 前端轮询 `GET /api/tasks/{task_id}`
- 用户标识用 `openid`（`POST /api/login` 用微信 `code` 换）

## 5. 常见问题

| 问题 | 排查 |
|---|---|
| 启动报"没有可用的模型 provider" | `.env` 的 key 没填，或对应 `enabled: false` |
| 任务一直 pending | 看日志，大概率 key 无效/余额不足，已自动走 fallback |
| 费用异常 | 查 `daily_cost` 表；`GET /api/providers` 确认单价配置 |
| 想换更便宜的模型 | `config.yaml` 改 `model` + `price_*`，重启 |

## 6. 小程序前端

`miniprogram/` 目录是配套的微信小程序前端（原生开发，5 个页面：批改/结果/历史/错题本/我的）：

1. 微信开发者工具 → 导入项目 → 选择 `miniprogram` 目录，填入你的 AppID
2. 改 `miniprogram/utils/config.js` 里的 `BASE_URL` 为你的服务器地址
3. 详情 → 本地设置 → 勾选「不校验合法域名」即可联调；上线前完成域名备案并在小程序后台配置服务器域名

详细步骤见 `miniprogram/README.md`。

## 7. 目录结构

```
homework-grader-server/
├── DESIGN.md              技术方案与设计文档
├── README.md              本文件
├── config.example.yaml    配置模板（多模型/配额/预算/prompt）
├── .env.example           密钥模板
├── docker-compose.yml / Dockerfile
├── requirements.txt
├── test_smoke.py          烟雾测试
├── miniprogram/             微信小程序前端（导入开发者工具即用）
│   ├── utils/config.js      改 BASE_URL 为你的服务器地址
│   └── pages/               index批改 / result结果 / history历史 / mistakes错题本 / mine我的
└── app/
    ├── main.py            启动入口
    ├── api.py             REST 接口 + 后台批改任务
    ├── config.py          配置加载与校验
    ├── db.py              SQLite 数据层
    ├── providers.py       多大模型统一封装（核心）
    ├── grading.py         批改 Agent（prompt/校验/fallback）
    ├── wechat.py          微信登录/内容安全/订阅消息
    └── limits.py          （配额与熔断逻辑在 api.py + db.py 内）
```
