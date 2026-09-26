"""配置加载：config.yaml + 环境变量替换，Pydantic 强校验。"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# 支持 ${VAR} 和 ${VAR:-默认值} 两种写法
_ENV_RE = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _sub_env(text: str) -> str:
    def _rep(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        default = m.group(2) if m.group(1) is not None else ""
        return os.environ.get(name, default or "")

    return _ENV_RE.sub(_rep, text)


class ProviderConfig(BaseModel):
    """单个大模型厂商的配置。"""

    type: str = "openai_compatible"  # 目前只有这一种实现，覆盖所有国产厂商
    base_url: str
    api_key: str = ""
    model: str
    timeout: int = 90
    max_retries: int = 2
    price_input_per_1m: float = 0.0   # 元/百万 tokens，用于成本核算
    price_output_per_1m: float = 0.0
    enabled: bool = True


class LlmConfig(BaseModel):
    default_provider: str = "qwen"
    fallback_order: list[str] = Field(default_factory=list)
    providers: dict[str, ProviderConfig]

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


class QuotaConfig(BaseModel):
    new_user_bonus: int = 20  # 新用户送
    daily_free: int = 3       # 每日免费
    max_per_day: int = 50     # 单用户每日上限（防刷）


class BudgetConfig(BaseModel):
    daily_max_cny: float = 50.0  # 全局每日费用上限，超了拒绝新任务


class WechatConfig(BaseModel):
    appid: str = ""
    secret: str = ""
    seccheck_enabled: bool = False
    subscribe_template_id: str = ""


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LlmConfig
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    wechat: WechatConfig = Field(default_factory=WechatConfig)
    data_dir: str = "data"
    max_image_mb: int = 5
    max_image_px: int = 1600
    grade_concurrency: int = 4
    system_prompt_file: str = "prompt_system.txt"  # 批改指令纯文本文件（相对 config.yaml 所在目录）
    system_prompt: str = ""  # load_settings() 会从 system_prompt_file 读入
    user_prompt_template: str = "请批改这张{subject}作业照片（{grade_level}）。严格按系统指令要求的 JSON 格式输出。"

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "app.db")

    @property
    def upload_dir(self) -> str:
        return str(Path(self.data_dir) / "uploads")


def load_settings(path: str | None = None) -> Settings:
    path = path or os.environ.get("CONFIG_PATH", "config.yaml")
    cfg_path = Path(path)
    text = _sub_env(cfg_path.read_text(encoding="utf-8"))
    data = yaml.safe_load(text)
    settings = Settings.model_validate(data)
    # 批改指令从纯文本文件读入（相对 config.yaml 所在目录）
    if settings.system_prompt_file:
        prompt_path = cfg_path.parent / settings.system_prompt_file
        if prompt_path.exists():
            settings.system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not settings.system_prompt:
        raise ValueError("system_prompt 为空：请检查 system_prompt_file 指向的文件是否存在")
    return settings


def provider_chain(settings: Settings) -> list[str]:
    """计算实际调用顺序：默认 provider 在前，fallback 去重补齐。

    跳过未启用、没配 key 的 provider。"""
    order = [settings.llm.default_provider] + list(settings.llm.fallback_order)
    seen: set[str] = set()
    out: list[str] = []
    for name in order:
        cfg = settings.llm.providers.get(name)
        if name not in seen and cfg is not None and cfg.enabled and cfg.api_key:
            seen.add(name)
            out.append(name)
    return out
