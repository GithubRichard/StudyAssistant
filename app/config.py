"""配置加载：config.yaml + 环境变量替换，Pydantic 强校验。

两种执行引擎：
- `engine.mode: hermes`（默认）：由 Hermes Agent 执行学习技能，旧的多模型直连配置仅作兼容保留。
- `engine.mode: legacy`：旧单轮批改路径，需要 llm 与 system_prompt_file 齐备。

Hermes 模式下缺少 Hermes 地址或密钥不会导致启动失败，但运行时会如实报告「未就绪」，
不会静默退回普通模型。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

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
    price_input_per_1m: float = 0.0
    price_output_per_1m: float = 0.0
    enabled: bool = True


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
    """Hermes Agent API 接入配置。密钥只从环境变量注入，不写入仓库。"""

    base_url: str = ""                       # 例如 http://127.0.0.1:8642（默认仅本机可达）
    api_key: str = ""
    agent_model: str = "hermes-agent"        # Hermes 的 Agent 别名，不是底层模型 ID
    skill_name: str = "leo-study-assistant"  # 期望已安装的技能名，用于就绪检查
    timeout_seconds: float = 300.0           # 单轮执行上限
    max_response_bytes: int = 2_000_000      # 响应体上限，防止超大输出拖垮进程
    session_ttl_seconds: int = 3 * 24 * 3600  # 任务级会话标识有效期
    verify_skill: bool = True                # 是否通过 /v1/skills 校验技能已安装
    readiness_ttl_seconds: int = 30          # 就绪状态缓存时长

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


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
