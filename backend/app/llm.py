"""LiteLLM gateway: key pool with automatic fallback + LiteLLM response cache.

Design notes
------------
* Every call goes through `litellm.acompletion`, one API key at a time.
* On any provider failure (429, quota, 5xx, timeout, bad key, empty reply) the
  next key is tried. If all keys fail we raise `AllProvidersUnavailable`.
* The cache is LiteLLM's own `Cache` object (in-memory or disk backend). The
  cache key is LiteLLM's key function over *model + the full message list*,
  so API keys never influence it, and a hit is reported as LLM_CACHE.
* Provider exceptions are never logged with their message (they can contain
  request URLs); only the exception class and status code are.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import litellm
from litellm.caching.caching import Cache

from .config import Settings

log = logging.getLogger("app.llm")

litellm.suppress_debug_info = True
litellm.drop_params = True


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed for this request."""


class SimulatedProviderError(Exception):
    """Raised for keys listed in X-Simulate-Failure (affects one request only)."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"simulated provider status {status_code}")
        self.status_code = status_code


@dataclass
class LLMResult:
    reply: str
    resolution_source: str  # LIVE_INFERENCE | LLM_CACHE
    model: str
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.model
        self.keys: list[tuple[str, str]] = [
            (f"key_{i}", key) for i, key in enumerate(settings.api_keys, start=1)
        ]
        self._cooldown_until: dict[str, float] = {}
        self.cache = self._build_cache(settings)

    # ------------------------------------------------------------------ cache
    @staticmethod
    def _build_cache(settings: Settings) -> Cache:
        if settings.cache_type == "disk":
            return Cache(
                type="disk",
                disk_cache_dir=settings.cache_dir,
                ttl=settings.cache_ttl_seconds,
            )
        return Cache(type="local", ttl=settings.cache_ttl_seconds)

    @property
    def cache_enabled(self) -> bool:
        return self.cache is not None

    def _cache_key(self, messages: list[dict[str, str]]) -> str:
        # model + full message list (system prompt + history + new message)
        return self.cache.get_cache_key(model=self.model, messages=messages)

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        try:
            value = self.cache.cache.get_cache(key)
        except Exception:  # a broken cache must never break chat
            log.warning("cache read failed", exc_info=False)
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        return value if isinstance(value, dict) else None

    def _cache_set(self, key: str, entry: dict[str, Any]) -> None:
        try:
            self.cache.cache.set_cache(
                key, json.dumps(entry), ttl=self.settings.cache_ttl_seconds
            )
        except Exception:
            log.warning("cache write failed", exc_info=False)

    # ------------------------------------------------------------- key order
    def _ordered_keys(self, honour_cooldown: bool) -> list[tuple[str, str]]:
        """key_1, key_2, ... but keys that recently failed are tried last."""
        if not honour_cooldown or self.settings.key_cooldown_seconds <= 0:
            return list(self.keys)
        now = time.monotonic()
        healthy = [k for k in self.keys if self._cooldown_until.get(k[0], 0) <= now]
        cooling = [k for k in self.keys if k not in healthy]
        return healthy + cooling

    def _mark_failed(self, key_id: str) -> None:
        if self.settings.key_cooldown_seconds > 0:
            self._cooldown_until[key_id] = time.monotonic() + self.settings.key_cooldown_seconds

    # --------------------------------------------------------------- the call
    async def complete(
        self,
        messages: list[dict[str, str]],
        simulate: dict[str, int] | None = None,
    ) -> LLMResult:
        simulate = simulate or {}
        cache_key = self._cache_key(messages)

        # A simulated-failure request must exercise the live path + failover.
        if not simulate:
            hit = self._cache_get(cache_key)
            if hit and hit.get("reply"):
                return LLMResult(
                    reply=hit["reply"],
                    resolution_source="LLM_CACHE",
                    model=hit.get("model", self.model),
                    input_tokens=int(hit.get("input_tokens", 0)),
                    output_tokens=int(hit.get("output_tokens", 0)),
                    key_id=None,
                    fallback_attempts=0,
                )

        failures = 0
        for key_id, api_key in self._ordered_keys(honour_cooldown=not simulate):
            try:
                sim_status = simulate.get(key_id) or simulate.get("*")
                if sim_status:
                    raise SimulatedProviderError(sim_status)
                response = await litellm.acompletion(
                    model=self.model,
                    messages=messages,
                    api_key=api_key,
                    temperature=self.settings.temperature,
                    timeout=self.settings.request_timeout_seconds,
                    num_retries=0,  # failover to the next key replaces retries
                )
                reply = (response.choices[0].message.content or "").strip()
                if not reply:
                    raise ValueError("empty completion")
                usage = response.usage
                result = LLMResult(
                    reply=reply,
                    resolution_source="LIVE_INFERENCE",
                    model=self.model,
                    input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                    key_id=key_id,
                    fallback_attempts=failures,
                )
            except Exception as exc:  # noqa: BLE001 - any provider failure => next key
                failures += 1
                status = getattr(exc, "status_code", None)
                log.warning(
                    "key %s failed (%s, status=%s); %d failed so far",
                    key_id, type(exc).__name__, status, failures,
                )
                if not isinstance(exc, SimulatedProviderError):
                    self._mark_failed(key_id)
                continue

            self._cache_set(
                cache_key,
                {
                    "reply": result.reply,
                    "model": result.model,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                },
            )
            return result

        raise AllProvidersUnavailable()
