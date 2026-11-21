import json
import sqlite3

import litellm
import pytest

from app.main import parse_failure_header
from fastapi import HTTPException


def chat(client, session="s-1", message="What is the capital of France?", headers=None, **extra):
    return client.post("/chat", json={"session_id": session, "message": message, **extra}, headers=headers or {})


# ------------------------------------------------------------------ health
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


def test_health_without_keys_is_503(settings_factory):
    with settings_factory(api_keys=()) as c:
        assert c.get("/health").status_code == 503


# -------------------------------------------------------------------- chat
def test_chat_live_contract(client, provider):
    r = chat(client)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s-1"
    assert body["reply"]
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "gemini/gemini-2.0-flash"
    assert isinstance(meta["latency_ms"], int) and meta["latency_ms"] >= 1
    assert meta["input_tokens"] > 0 and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0


def test_same_first_message_in_new_session_is_cache_hit(client, provider):
    first = chat(client, session="a").json()
    second = chat(client, session="b").json()
    assert second["metadata"]["resolution_source"] == "LLM_CACHE"
    assert second["reply"] == first["reply"]
    assert second["metadata"]["key_id"] is None
    assert second["metadata"]["fallback_attempts"] == 0
    assert second["metadata"]["input_tokens"] == first["metadata"]["input_tokens"]
    assert second["metadata"]["output_tokens"] == first["metadata"]["output_tokens"]
    assert len(provider.calls) == 1  # the provider was only called once


def test_different_message_never_comes_from_cache(client, provider):
    chat(client, session="a", message="What is the capital of France?")
    r = chat(client, session="b", message="What is the capital of Spain?").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert len(provider.calls) == 2


def test_different_system_prompt_is_not_cached(client, provider):
    chat(client, session="a")
    r = chat(client, session="b", system_prompt="Answer like a pirate.").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_tokens_are_per_request_not_cumulative(client):
    first = chat(client, session="a", message="aaaa bbbb cccc").json()["metadata"]["input_tokens"]
    again = chat(client, session="a", message="dddd eeee ffff").json()["metadata"]["input_tokens"]
    other = chat(client, session="b", message="gggg hhhh iiii").json()["metadata"]["input_tokens"]
    assert first == other                     # equal-length first messages -> same tokens
    assert again > first                      # history is part of THIS request's prompt...
    third = chat(client, session="c", message="jjjj kkkk llll").json()["metadata"]["input_tokens"]
    assert third == first                     # ...but nothing carries across sessions


# -------------------------------------------------------------- multi-turn
def test_history_is_sent_to_the_model_and_sessions_are_separate(client, provider):
    chat(client, session="a", message="My name is Ada.")
    chat(client, session="a", message="What is my name?")
    last = provider.calls[-1]["messages"]
    assert [m["role"] for m in last] == ["system", "user", "assistant", "user"]
    assert last[1]["content"] == "My name is Ada."
    assert last[-1]["content"] == "What is my name?"

    chat(client, session="b", message="Hello")
    assert [m["role"] for m in provider.calls[-1]["messages"]] == ["system", "user"]


def test_history_endpoint(client):
    chat(client, session="h", message="First")
    chat(client, session="h", message="Second")
    r = client.get("/sessions/h/messages")
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == "h"
    msgs = data["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "First" and "metadata" not in msgs[0]
    assert msgs[1]["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert {"input_tokens", "output_tokens"} <= set(msgs[1]["metadata"])
    assert all(m["role"] != "system" for m in msgs)


def test_unknown_session_is_404(client):
    r = client.get("/sessions/nope/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_system_prompt_override_is_remembered_for_the_session(client, provider):
    chat(client, session="p", message="hi", system_prompt="Answer like a pirate.")
    chat(client, session="p", message="again")
    assert provider.calls[-1]["messages"][0]["content"] == "Answer like a pirate."
    chat(client, session="q", message="hi")
    assert provider.calls[-1]["messages"][0]["content"] == "You are a test assistant."


def test_history_survives_a_restart(tmp_path, provider, settings_factory):
    with settings_factory() as c1:
        chat(c1, session="persist", message="remember me")
    with settings_factory() as c2:  # new app instance, same sqlite file
        assert len(c2.get("/sessions/persist/messages").json()["messages"]) == 2


def test_rows_are_stored_in_sqlite_with_iso_timestamps(tmp_path, client):
    chat(client, session="db")
    conn = sqlite3.connect(tmp_path / "app.db")
    rows = conn.execute("SELECT role, metadata, created_at FROM messages WHERE session_id='db' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["user", "assistant"]
    assert rows[0][1] is None and json.loads(rows[1][1])["resolution_source"] == "LIVE_INFERENCE"
    assert "T" in rows[0][2]


# ------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "s", "message": ""},
        {"session_id": "s", "message": "    "},
        {"session_id": "s"},
        {"message": "hi"},
        {"session_id": "", "message": "hi"},
        {"session_id": "s", "message": "x" * 4001},
        {"session_id": "x" * 129, "message": "hi"},
    ],
)
def test_validation_errors_are_422_json(client, payload):
    r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()
    assert client.get("/sessions/s/messages").status_code == 404  # nothing stored


def test_message_of_exactly_4000_chars_is_accepted(client):
    assert chat(client, message="x" * 4000).status_code == 200


def test_malformed_json_is_422(client):
    r = client.post("/chat", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "detail" in r.json()


# ---------------------------------------------------------------- failover
def test_simulated_429_on_key_1_falls_back_to_key_2(client):
    r = chat(client, headers={"X-Simulate-Failure": "key_1=429"})
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2" and meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"


def test_simulated_500_on_key_1_falls_back_to_key_2(client):
    r = chat(client, headers={"X-Simulate-Failure": "key_1=500"})
    assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_2"


def test_simulation_bypasses_cache_so_failover_is_always_visible(client):
    chat(client, session="a")  # warms the cache
    r = chat(client, session="b", headers={"X-Simulate-Failure": "key_1=429"})
    assert r.json()["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["metadata"]["key_id"] == "key_2"


def test_all_keys_failing_is_clean_503_and_nothing_is_stored(client):
    r = chat(client, session="x", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/x/messages").status_code == 404


def test_both_keys_listed_explicitly_is_503(client):
    r = chat(client, headers={"X-Simulate-Failure": "key_1=429,key_2=500"})
    assert r.status_code == 503


def test_simulation_header_ignored_when_switch_is_off(settings_factory):
    with settings_factory(enable_failure_simulation=False) as c:
        r = chat(c, headers={"X-Simulate-Failure": "*=429"})
        assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_1"


def test_invalid_simulation_header_is_422(client):
    assert chat(client, headers={"X-Simulate-Failure": "key_1=418"}).status_code == 422


def test_real_provider_429_falls_back_to_next_key(client, provider):
    provider.failing["test-key-1"] = litellm.RateLimitError("quota", "gemini", "m")
    meta = chat(client).json()["metadata"]
    assert meta["key_id"] == "key_2" and meta["fallback_attempts"] == 1


def test_real_provider_5xx_falls_back_to_next_key(client, provider):
    provider.failing["test-key-1"] = litellm.ServiceUnavailableError("down", "gemini", "m")
    assert chat(client).json()["metadata"]["key_id"] == "key_2"


def test_every_real_key_failing_is_503(client, provider):
    for k in ("test-key-1", "test-key-2"):
        provider.failing[k] = litellm.InternalServerError("boom", "gemini", "m")
    r = chat(client)
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}


def test_failed_key_is_cooled_down_for_later_requests(settings_factory, provider):
    with settings_factory(key_cooldown_seconds=60) as c:
        provider.failing["test-key-1"] = litellm.RateLimitError("quota", "gemini", "m")
        chat(c, message="one")
        provider.calls.clear()
        chat(c, message="two")
        assert provider.calls[0]["api_key"] == "test-key-2"  # key_1 is skipped while cooling


def test_parse_failure_header():
    assert parse_failure_header("key_1=429, key_2=503") == {"key_1": 429, "key_2": 503}
    assert parse_failure_header("*=502") == {"*": 502}
    assert parse_failure_header(None) == {}
    with pytest.raises(HTTPException):
        parse_failure_header("bogus")


# ------------------------------------------------------------------ misc
def test_unexpected_errors_are_json_without_stack_traces(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(client.app.state.gateway, "complete", boom)
    r = chat(client)
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    assert "secret" not in r.text and "Traceback" not in r.text


def test_cors_preflight_allows_frontend_origin_and_failure_header(client):
    r = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-simulate-failure",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_only_the_three_contract_endpoints_are_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
