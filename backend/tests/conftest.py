"""Test fixtures: real FastAPI app + real LiteLLM cache, but no network.

`litellm.acompletion` is wrapped so that it (a) records the keys / messages it
was called with, (b) raises provider errors for chosen keys, and (c) otherwise
returns LiteLLM's built-in `mock_response` answer.
"""
from __future__ import annotations

import litellm
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeProvider:
    def __init__(self, real_acompletion):
        self._real = real_acompletion
        self.calls: list[dict] = []          # every provider call
        self.failing: dict[str, Exception] = {}  # api_key -> exception to raise
        self.reply = "Paris is the capital of France."

    async def __call__(self, **kwargs):
        self.calls.append({"api_key": kwargs["api_key"], "messages": kwargs["messages"]})
        exc = self.failing.get(kwargs["api_key"])
        if exc is not None:
            raise exc
        kwargs = {k: v for k, v in kwargs.items() if k not in {"timeout", "num_retries"}}
        resp = await self._real(mock_response=self.reply, **kwargs)
        # make usage depend on the prompt so per-request token tests are meaningful
        resp.usage.prompt_tokens = sum(len(m["content"].split()) for m in kwargs["messages"])
        resp.usage.completion_tokens = len(self.reply.split())
        return resp


@pytest.fixture()
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(litellm.acompletion)
    monkeypatch.setattr(litellm, "acompletion", fake)
    return fake


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        api_keys=("test-key-1", "test-key-2"),
        database_url=f"sqlite:///{tmp_path}/app.db",
        allowed_origins=("http://localhost:3000",),
        enable_failure_simulation=True,
        model="gemini/gemini-2.0-flash",
        default_system_prompt="You are a test assistant.",
        cache_type="local",
        cache_dir=str(tmp_path / "cache"),
        cache_ttl_seconds=600,
        request_timeout_seconds=5,
        temperature=0.7,
        max_history_messages=40,
        key_cooldown_seconds=0,
        enable_docs=False,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def client(tmp_path, provider):
    app = create_app(make_settings(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def settings_factory(tmp_path, provider):
    def build(**overrides):
        return TestClient(create_app(make_settings(tmp_path, **overrides)), raise_server_exceptions=False)
    return build
