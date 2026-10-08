"""配置加载：config.yaml + 环境变量替换，Pydantic 强校验。

两种执行引擎：
- `engine.mode: hermes`（默认）：由 Hermes Agent 执行学习技能，旧的多模型直连配置仅作兼容保留。
- `engine.mode: legacy`：旧单轮批改路径，需要 llm 与 system_prompt_file 齐备。

Hermes 模式下缺少 Hermes 地址或密钥不会导致启动失败，但运行时会如实报告「未就绪」，
不会静默退回普通模型。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# 只支持 ${VAR} 和 ${VAR:-默认值} 两种写法。
# 不要恢复"裸 $VAR"写法：`$([A-Za-z_][A-Za-z0-9_]*)` 会把 pbkdf2 哈希里的
# `$<salt_hex>` 当成环境变量名（盐以 a-f 开头时）替换成空串，导致密码永远校验失败。
_ENV_RE = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

ENGINE_MODES = ("hermes", "legacy")


def _sub_env(text: str) -> str:
    def _rep(m: re.Match) -> str:
        name = m.group(1)
        default = m.group(2) or ""
        return os.environ.get(name, default)

    return _ENV_RE.sub(_rep, text)


class ProviderConfig(BaseModel):
    """单个大模型厂商的配置（仅 legacy 模式使用）。"""

    type: str = "openai_compatible"
    base_url: str
    api_key: str = ""
    model: str
    timeout: int = 90
    max_retries: int = 2
    # 厂商输出上限（如 glm-4v-flash 只接受 max_tokens ≤ 1024）；0 = 不限制，
    # 分阶段批改实际取「阶段上限」与该值的较小者，否则备胎一调用就 400
    max_output_tokens: int = 0
    price_input_per_1m: float = 0.0
    price_output_per_1m: float = 0.0
    enabled: bool = True

    # 明确不支持直接图像输入的 provider 不用于判向或转写。
    supports_vision: bool = True


class StagedGradingConfig(BaseModel):
    """分阶段批改配置：提取→独立求解→比对→诊断。

    enabled=True 时，grading 任务优先走分阶段流水线（需 llm.providers 可用），
    否则回落到 Hermes 单次 / legacy 路径。
    各 *_prompt 为空时用 app/staged.py 内置默认值；填了则整体替换该阶段 system prompt。
    """
    enabled: bool = True
    extract_max_tokens: int = 16000
    solve_max_tokens: int = 6000
    compare_max_tokens: int = 4000
    diagnose_max_tokens: int = 6000
    # 输出被 max_tokens 截断（finish_reason=length）时，同一模型放大该倍数
    # 重试一次再切备胎；<=1 时关闭，直接切备胎。注意部分厂商把思考过程
    # 也计入输出额度，难读图片（旋转/潦草手写）容易烧光额度后 JSON 还没出来。
    truncation_retry_multiplier: float = 2.0
    extract_prompt: str = ""
    solve_prompt: str = ""
    diagnose_prompt: str = ""
    # ---- 提取阶段"眼睛"：图片预处理 + 局部放大复核 ----
    # 转写前把图片长边放大到该值（小图/缩略图场景）；0=关闭放大
    extract_image_min_long_side: int = 2048
    # 长边超过该值则缩小（防超大图 token 爆炸）；0=不限制
    extract_image_max_long_side: int = 4096
    # OSD 不确定时只做一次视觉判向；为空时选模型链中首个支持图像的模型。
    # 强烈建议配一个与批改链不同的模型：方向判错会导致整页静默错改，
    # 同一模型既判向又转写时，"判向正确"可能是同一批幻觉的自我确认。
    orientation_provider: str = ""
    orientation_visual_fallback: bool = True
    # 方向待确认超过该天数未处理，自动转 interrupted（可删除，避免永久残留）；0=关闭
    orientation_wait_days: int = 7
    # 首轮转写后，对字迹存疑/空白的题自动做一遍局部放大复核
    extract_zoom_reread: bool = True
    # 局部图网格：2 = 每页切 2x2=4 张重叠局部图
    extract_zoom_grid: int = 2
    # 图片张数超过该值时跳过复核（防多页 token 爆炸）
    extract_zoom_max_images: int = 2
    # 存疑题数超过该值时跳过复核（整页都看不清时复核意义不大）
    extract_zoom_max_items: int = 15
    # ---- 提取阶段"第二双眼睛"：题号/转写复核 ----
    # 题号序列复核：extract 后用一次聚焦调用重读题号序列，diff 不一致的题标存疑。
    # 防题号错位/跳号/漏题（如把 18 读成 19 导致整体顺延）；失败自动降级不阻断主流程。
    number_verify: bool = True
    # 题号复核用的模型：留空则用批改链。强烈建议配一个与批改链不同的模型——
    # 同一模型复核自己时，错误高度相关，"复核一致"可能是把同一批错题又数了一遍。
    number_verify_provider: str = ""
    # 逐题转写复核：每题一次聚焦调用，只核对题号 + 括号原词（不碰学生答案）。
    # 默认关闭：成本为每题一次调用；题数超限时整批跳过。
    per_question_verify: bool = False
    per_question_verify_max_items: int = 10


class WeeklySummaryConfig(BaseModel):
    """周总结配置：每周日凌晨按账号×科目生成上一周学习总结。"""
    # 是否用大模型做错题归类分析与知识点总结；关闭后周总结只有统计部分
    ai_analysis: bool = True
    # 送分析的最大错题数（超出按创建时间取最早的；题干过长会被截断）
    analysis_max_mistakes: int = 30
    # 分析输出上限（tokens）
    analysis_max_tokens: int = 4000


class LlmConfig(BaseModel):
    default_provider: str = "qwen"
    fallback_order: List[str] = Field(default_factory=list)
    providers: Dict[str, ProviderConfig] = Field(default_factory=dict)

    @field_validator("default_provider")
    @classmethod
    def _default_exists(cls, v: str, info) -> str:
        providers = info.data.get("providers", {})
        if providers and v not in providers:
            raise ValueError(f"default_provider '{v}' 不在 providers 中")
        return v


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class EngineConfig(BaseModel):
    mode: str = "hermes"

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        if v not in ENGINE_MODES:
            raise ValueError(f"engine.mode 只能是 {ENGINE_MODES}，当前: {v}")
        return v


class HermesConfig(BaseModel):
    """Hermes Agent API 接入配置。密钥只从环境变量注入，不写入仓库。

    复查模型配置（review_*）默认全部留空：留空即不启用服务端二次复查，
    老配置不改也能启动。启用前提见 README「二次复查」一节：
    需先在真实环境确认路由别名、响应身份字段与图片链路，再行开启。
    """

    base_url: str = ""                       # 例如 http://127.0.0.1:8642（默认仅本机可达）
    api_key: str = ""
    agent_model: str = "hermes-agent"        # Hermes 的 Agent 别名，不是底层模型 ID
    skill_name: str = "leo-study-assistant"  # 期望已安装的技能名，用于就绪检查
    timeout_seconds: float = 300.0           # 单轮执行上限
    max_response_bytes: int = 2_000_000      # 响应体上限，防止超大输出拖垮进程
    session_ttl_seconds: int = 3 * 24 * 3600  # 任务级会话标识有效期
    verify_skill: bool = True                # 是否通过 /v1/skills 校验技能已安装
    readiness_ttl_seconds: int = 30          # 就绪状态缓存时长

    # ---------- 服务端二次复查（第二模型）配置 ----------
    review_model: str = ""                   # model_routes 别名（如 "glm"）或底层模型 ID（如 "glm-5.3"）
    review_provider: str = ""                # 用底层模型 ID 时必填（如 "zai"）；用别名时留空
    review_model_options: Dict[str, Any] = Field(
        default_factory=dict)                # 例如 {"reasoning": {"effort": "high"}}；GLM-5.3 不接受 medium
    review_timeout_seconds: float = 300.0    # 复查单次上限；实际取 min(此值, 任务剩余预算)
    review_max_questions: int = 30           # 单次送复查的题数上限，超出标 unprocessed
    review_expected_model: str = ""          # 期望别名解析到的底层模型（声明性配置，不是调用成功的证据）
    review_expected_provider: str = ""       # 期望的底层 provider（同上，仅用于身份核对）

    @field_validator("review_timeout_seconds", "review_max_questions")
    @classmethod
    def _positive(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"hermes.{info.field_name} 必须为正数")
        return v

    @field_validator("review_model_options")
    @classmethod
    def _check_review_options(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """复查模型选项只允许 JSON 可序列化的模型参数。

        禁止混入 messages / base_url / headers / authorization 等请求级字段：
        模型选项不应成为覆盖请求本体或会话头的后门。
        """
        if not isinstance(v, dict):
            raise ValueError("hermes.review_model_options 必须是字典")
        banned = {"model", "provider", "messages", "base_url", "headers",
                  "authorization", "session", "stream"}
        for key in v:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("hermes.review_model_options 的键必须是非空字符串")
            if key.strip().lower() in banned:
                raise ValueError(f"hermes.review_model_options 不允许包含请求级字段: {key}")
        try:
            json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise ValueError(f"hermes.review_model_options 必须可 JSON 序列化: {e}") from e
        return v

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def review_configured(self) -> bool:
        """复查模型留空 = 不做服务端二次复查（结果如实标 not_run）。"""
        return bool(self.review_model.strip())


class AuthConfig(BaseModel):
    """家庭私用授权：白名单为空表示不限制账号（仅适合本地开发）。"""

    allowed_openids: List[str] = Field(default_factory=list)
    session_ttl_days: int = 30


class WebUserConfig(BaseModel):
    """网页版预设账号：一个账号 = 一个孩子，学习记录按账号隔离。

    username 只允许字母数字与 -_（最终身份为 web:<username>）；
    password_hash 用 scripts/make_web_user.py 生成（pbkdf2_sha256）。
    """

    username: str
    display_name: str = ""
    password_hash: str = ""

    @model_validator(mode="after")
    def _check(self) -> "WebUserConfig":
        name = (self.username or "").strip()
        if not name or len(name) > 32 or any(
            not (ch.isalnum() or ch in "-_") for ch in name
        ):
            raise ValueError("username 只允许 1~32 位字母数字与 -_")
        self.username = name
        if not self.display_name:
            self.display_name = name
        if not self.password_hash:
            raise ValueError(f"账号 {name} 缺少 password_hash")
        return self


class WebConfig(BaseModel):
    """网页版入口：不依赖微信，用预设账号登录，适合「只能用 IP 直连、小程序无法备案」的场景。

    安全约定：
    - `users` 为空时网页登录一律拒绝；用户名必须在预设名单中。
    - 网页账号使用 `web:<username>` 形式的独立身份，与微信 openid 互不影响。
    - config.yaml 不进版本库；密码只存哈希，不存明文。

    注意：`config.yaml` 不进版本库，服务器上那份不会随 `git pull` 更新，
    改完配置后需重启服务生效。
    """

    enabled: bool = True
    title: str = "学习助手"
    users: List[WebUserConfig] = Field(default_factory=list)
    allowed_origins: List[str] = Field(default_factory=list)  # 跨域部署时填写；同源部署留空

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.users)

    def find_user(self, username: str) -> Optional["WebUserConfig"]:
        name = (username or "").strip()
        for u in self.users:
            if u.username == name:
                return u
        return None


class LimitsConfig(BaseModel):
    max_assets_per_task: int = 20
    max_total_upload_mb: int = 40
    max_runs_per_task: int = 6
    max_task_minutes: float = 30.0
    claim_lease_seconds: int = 600
    worker_poll_seconds: float = 2.0
    worker_enabled: bool = True


class DeliveryConfig(BaseModel):
    """外部副作用的开关；默认全关，未配置时结果中如实标注 not_configured。"""

    pdf_enabled: bool = False
    email_enabled: bool = False
    git_enabled: bool = False


class GitSyncConfig(BaseModel):
    """受控 Git 提交推送（学习记录同步）。

    约定：
    - 只提交本次授权的工作区学习记录，禁止全量暂存、禁止 `git add -f` 绕过忽略规则。
    - 不强制推送、不硬重置、不改 Git 配置、不跳过钩子；冲突或分支分歧时停止上传。
    - `enabled` 未显式配置时回落到 `delivery.git_enabled`，保持旧配置可用。
    """

    enabled: Optional[bool] = None
    remote: str = "origin"
    timeout_seconds: float = 60.0
    author_name: str = ""        # 留空则沿用仓库已有的 git user.name / user.email
    author_email: str = ""


class FamilyConfig(BaseModel):
    """家庭学习配置默认值，可由小程序「设置」页覆盖并持久化到数据库。"""

    default_grade_level: str = ""
    subjects: List[str] = Field(default_factory=lambda: ["语文", "数学", "英语"])
    term_start_date: str = ""              # 本学期开学日期 YYYY-MM-DD，用于期中/期末默认区间


class QuotaConfig(BaseModel):
    new_user_bonus: int = 20
    daily_free: int = 3
    max_per_day: int = 50


class BudgetConfig(BaseModel):
    daily_max_cny: float = 50.0


class WechatConfig(BaseModel):
    appid: str = ""
    secret: str = ""
    seccheck_enabled: bool = False
    subscribe_template_id: str = ""


class WorkspaceConfig(BaseModel):
    """授权学习工作区（Hermes 可读；默认只读挂载，写入由服务端校验后执行）。"""

    dir: str = "workspace"
    readonly: bool = True
    init_readme: bool = True


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    hermes: HermesConfig = Field(default_factory=HermesConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    git: GitSyncConfig = Field(default_factory=GitSyncConfig)
    family: FamilyConfig = Field(default_factory=FamilyConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    wechat: WechatConfig = Field(default_factory=WechatConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    data_dir: str = "data"
    retention_days: int = 730  # 错题/任务按账号保留天数，超期由每日定时任务清理
    max_image_mb: int = 5
    max_image_px: int = 1600
    grade_concurrency: int = 4
    system_prompt_file: str = ""
    system_prompt: str = ""
    user_prompt_template: str = "请批改这张{subject}作业照片（{grade_level}）。严格按系统指令要求的 JSON 格式输出。"
    staged_grading: StagedGradingConfig = Field(default_factory=StagedGradingConfig)
    weekly_summary: WeeklySummaryConfig = Field(default_factory=WeeklySummaryConfig)

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "app.db")

    @property
    def upload_dir(self) -> str:
        return str(Path(self.data_dir) / "uploads")

    @property
    def artifact_dir(self) -> str:
        return str(Path(self.data_dir) / "artifacts")

    @property
    def workspace_dir(self) -> str:
        return str(Path(self.workspace.dir))

    @property
    def web_dir(self) -> str:
        """网页版静态资源目录（默认仓库根目录下的 web/，可用 WEB_DIR 覆盖）。"""
        return os.environ.get("WEB_DIR", "web")

    @property
    def is_hermes(self) -> bool:
        return self.engine.mode == "hermes"

    @property
    def git_sync_enabled(self) -> bool:
        """Git 同步总开关：`git.enabled` 未显式配置时回落到 `delivery.git_enabled`。"""
        if self.git.enabled is not None:
            return bool(self.git.enabled)
        return bool(self.delivery.git_enabled)


def load_settings(path: str | None = None) -> Settings:
    path = path or os.environ.get("CONFIG_PATH", "config.yaml")
    cfg_path = Path(path)
    text = _sub_env(cfg_path.read_text(encoding="utf-8"))
    data = yaml.safe_load(text) or {}
    settings = Settings.model_validate(data)

    if settings.system_prompt_file:
        prompt_path = cfg_path.parent / settings.system_prompt_file
        if prompt_path.exists():
            settings.system_prompt = prompt_path.read_text(encoding="utf-8").strip()

    if not settings.is_hermes and not settings.system_prompt:
        raise ValueError("legacy 模式下 system_prompt 为空：请检查 system_prompt_file 指向的文件")
    return settings


def resolve_web_dir(settings: Settings) -> Path:
    """网页版静态资源目录：相对路径按仓库根目录（app/ 的上一级）解析。"""
    web_dir = Path(settings.web_dir)
    if not web_dir.is_absolute():
        web_dir = Path(__file__).resolve().parent.parent / web_dir
    return web_dir


# 文件摘要进程内缓存：路径 -> (mtime_ns, size, 摘要)，避免每次版本探测都读整份文件
_WEB_ASSET_DIGESTS: Dict[str, tuple] = {}


def _file_digest(path: Path) -> str:
    """文件内容短摘要；文件不存在或读取失败时返回空串（不抛异常）。"""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = str(path)
    cached = _WEB_ASSET_DIGESTS.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
    except OSError:
        return ""
    _WEB_ASSET_DIGESTS[key] = (stat.st_mtime_ns, stat.st_size, digest)
    return digest


def web_asset_version(settings: Settings) -> str:
    """网页前端资源版本：对 web/app.js 与 web/styles.css 的内容算短哈希。

    前端用它探测"服务端已经换了新前端、而我这个标签页还在跑旧脚本"。
    任一文件缺失时返回空串，前端据此自动禁用探测（不误报）。
    """
    web_dir = resolve_web_dir(settings)
    parts = [_file_digest(web_dir / name) for name in ("app.js", "styles.css")]
    if not all(parts):
        return ""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def provider_chain(settings: Settings) -> list:
    """legacy 模式的调用顺序：默认 provider 在前，fallback 去重补齐。"""
    order = [settings.llm.default_provider] + list(settings.llm.fallback_order)
    seen: set = set()
    out: list = []
    for name in order:
        cfg = settings.llm.providers.get(name)
        if name not in seen and cfg is not None and cfg.enabled and cfg.api_key:
            seen.add(name)
            out.append(name)
    return out
