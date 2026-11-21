"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, concise assistant. Answer clearly and keep earlier "
    "messages in this conversation in mind."
)


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...]
    database_url: str
    allowed_origins: tuple[str, ...]
    enable_failure_simulation: bool
    model: str
    default_system_prompt: str
    cache_type: str  # "local" (in-memory) or "disk"
    cache_dir: str
    cache_ttl_seconds: int
    request_timeout_seconds: float
    temperature: float
    max_history_messages: int
    key_cooldown_seconds: float
    enable_docs: bool

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            api_keys=_split_csv(env("LLM_API_KEYS")),
            database_url=env("DATABASE_URL", "sqlite:///./data/app.db"),
            allowed_origins=_split_csv(env("ALLOWED_ORIGINS", "http://localhost:3000")),
            enable_failure_simulation=_as_bool(env("ENABLE_FAILURE_SIMULATION")),
            model=env("LLM_MODEL", "gemini/gemini-2.5-flash"),
            default_system_prompt=env("DEFAULT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            cache_type=env("CACHE_TYPE", "local").strip().lower(),
            cache_dir=env("CACHE_DIR", "/data/llm_cache"),
            cache_ttl_seconds=int(env("CACHE_TTL_SECONDS", "3600")),
            request_timeout_seconds=float(env("LLM_TIMEOUT_SECONDS", "30")),
            temperature=float(env("LLM_TEMPERATURE", "0.7")),
            max_history_messages=int(env("MAX_HISTORY_MESSAGES", "40")),
            key_cooldown_seconds=float(env("KEY_COOLDOWN_SECONDS", "30")),
            enable_docs=_as_bool(env("ENABLE_DOCS"), False),
        )
