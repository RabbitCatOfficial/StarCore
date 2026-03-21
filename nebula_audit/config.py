from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict
from urllib.parse import urljoin
import json


APP_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = APP_ROOT / "runtime_data"
# Keep the extraction workspace path very short to avoid Windows MAX_PATH failures
# on deep repositories (for example large .NET demo projects with long nested paths).
WORKSPACE_ROOT = APP_ROOT / "_w"
REPORTS_ROOT = DATA_ROOT / "reports"
CONFIG_PATH = DATA_ROOT / "config.json"
REPORT_INDEX_PATH = REPORTS_ROOT / "index.json"

TRANSPORT_LABELS: Dict[str, str] = {
    "openai": "OpenAI Chat Completions",
    "responses": "OpenAI Responses API",
    "ollama": "Ollama Chat API",
}


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    transport: str
    base_url: str
    chat_path: str
    default_model: str
    requires_api_key: bool = True


PROVIDER_PRESETS: Dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        key="openai",
        label="OpenAI",
        transport="openai",
        base_url="https://api.openai.com/v1",
        chat_path="chat/completions",
        default_model="gpt-4.1",
    ),
    "deepseek": ProviderPreset(
        key="deepseek",
        label="DeepSeek",
        transport="openai",
        base_url="https://api.deepseek.com/v1",
        chat_path="chat/completions",
        default_model="deepseek-chat",
    ),
    "qwen": ProviderPreset(
        key="qwen",
        label="Qwen / DashScope",
        transport="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        chat_path="chat/completions",
        default_model="qwen-max",
    ),
    "moonshot": ProviderPreset(
        key="moonshot",
        label="Moonshot",
        transport="openai",
        base_url="https://api.moonshot.cn/v1",
        chat_path="chat/completions",
        default_model="moonshot-v1-8k",
    ),
    "siliconflow": ProviderPreset(
        key="siliconflow",
        label="SiliconFlow",
        transport="openai",
        base_url="https://api.siliconflow.cn/v1",
        chat_path="chat/completions",
        default_model="deepseek-ai/DeepSeek-V3",
    ),
    "openrouter": ProviderPreset(
        key="openrouter",
        label="OpenRouter",
        transport="openai",
        base_url="https://openrouter.ai/api/v1",
        chat_path="chat/completions",
        default_model="openai/gpt-4o-mini",
    ),
    "ai-db-kj": ProviderPreset(
        key="ai-db-kj",
        label="AI-DB-KJ",
        transport="responses",
        base_url="https://ai-api.db-kj.com/v1",
        chat_path="responses",
        default_model="gpt-5.4-xhigh",
    ),
    "ollama": ProviderPreset(
        key="ollama",
        label="Ollama",
        transport="ollama",
        base_url="http://127.0.0.1:11434",
        chat_path="api/chat",
        default_model="qwen2.5-coder:7b",
        requires_api_key=False,
    ),
    "custom": ProviderPreset(
        key="custom",
        label="Custom",
        transport="openai",
        base_url="https://your-endpoint.example/v1",
        chat_path="chat/completions",
        default_model="your-model-name",
    ),
}


def ensure_data_dirs() -> None:
    for path in (DATA_ROOT, WORKSPACE_ROOT, REPORTS_ROOT):
        path.mkdir(parents=True, exist_ok=True)


@dataclass
class AppConfig:
    provider: str = "deepseek"
    transport: str = "openai"
    base_url: str = PROVIDER_PRESETS["deepseek"].base_url
    chat_path: str = PROVIDER_PRESETS["deepseek"].chat_path
    model: str = PROVIDER_PRESETS["deepseek"].default_model
    api_key: str = ""
    temperature: float = 0.1
    top_p: float = 0.95
    max_output_tokens: int = 4096
    timeout_seconds: int = 600
    audit_max_files: int = 24
    audit_max_chars: int = 65000
    per_file_char_limit: int = 6000
    extra_headers_json: str = ""
    system_prompt_suffix: str = ""
    verify_ssl: bool = True
    enable_codex_mode: bool = True

    def resolve_chat_url(self) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", self.chat_path.lstrip("/"))

    def preset(self) -> ProviderPreset:
        return PROVIDER_PRESETS.get(self.provider, PROVIDER_PRESETS["custom"])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "AppConfig":
        config = cls()
        for field_name in config.to_dict():
            if field_name in payload:
                setattr(config, field_name, payload[field_name])

        if not config.provider:
            config.provider = "custom"
        if not config.transport or config.transport not in TRANSPORT_LABELS:
            config.transport = config.preset().transport
        return config

    def apply_provider_defaults(self, provider_key: str) -> None:
        preset = PROVIDER_PRESETS[provider_key]
        self.provider = preset.key
        self.transport = preset.transport
        self.base_url = preset.base_url
        self.chat_path = preset.chat_path
        self.model = preset.default_model


def load_config() -> AppConfig:
    ensure_data_dirs()
    if not CONFIG_PATH.exists():
        return AppConfig()

    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()

    return AppConfig.from_dict(payload)


def save_config(config: AppConfig) -> None:
    ensure_data_dirs()
    CONFIG_PATH.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
