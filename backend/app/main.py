"""FastAPI app: GET /health, POST /chat, GET /sessions/{id}/messages."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database
from .llm import AllProvidersUnavailable, LLMGateway
from .schemas import ChatMetadata, ChatRequest, ChatResponse

log = logging.getLogger("app")

SIMULATED_STATUSES = {429, 500, 502, 503}
_KEY_ID = re.compile(r"^(key_\d+|\*)$")


def parse_failure_header(raw: str | None) -> dict[str, int]:
    """`key_1=429,key_2=500` or `*=429` -> {"key_1": 429, ...}."""
    if not raw or not raw.strip():
        return {}
    parsed: dict[str, int] = {}
    for pair in raw.split(","):
        key_id, sep, status = pair.strip().partition("=")
        key_id, status = key_id.strip(), status.strip()
        if not sep or not _KEY_ID.match(key_id) or not status.isdigit() or int(status) not in SIMULATED_STATUSES:
            raise HTTPException(
                status_code=422,
                detail="invalid X-Simulate-Failure header; use key_1=429,key_2=500 or *=429 "
                "(statuses: 429, 500, 502, 503)",
            )
        parsed[key_id] = int(status)
    return parsed


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url)
        db.init()  # schema is created automatically on startup
        gateway = LLMGateway(settings)
        if len(gateway.keys) < 2:
            log.warning("only %d API key(s) configured; fallback needs at least 2", len(gateway.keys))
        app.state.db = db
        app.state.gateway = gateway
        yield

    docs = {} if settings.enable_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="AI Chat Backend", lifespan=lifespan, **docs)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # One lock per session so two quick messages cannot interleave their history.
    locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()

    def session_lock(session_id: str) -> asyncio.Lock:
        lock = locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[session_id] = lock
        return lock

    # ---------------------------------------------------------------- errors
    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", "invalid"), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error: %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    # ----------------------------------------------------------------- routes
    @app.get("/health")
    async def health(request: Request) -> dict:
        try:
            await asyncio.to_thread(request.app.state.db.ping)
        except Exception:
            raise HTTPException(status_code=503, detail="database unavailable")
        gateway: LLMGateway = request.app.state.gateway
        if not gateway.keys:
            raise HTTPException(status_code=503, detail="no API keys configured")
        return {"status": "ok", "keys_configured": len(gateway.keys), "cache_enabled": gateway.cache_enabled}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        started = time.perf_counter()
        db: Database = request.app.state.db
        gateway: LLMGateway = request.app.state.gateway

        simulate: dict[str, int] = {}
        if settings.enable_failure_simulation:
            simulate = parse_failure_header(request.headers.get("x-simulate-failure"))

        async with session_lock(body.session_id):
            session = await asyncio.to_thread(db.get_session, body.session_id)
            history = await asyncio.to_thread(db.get_history, body.session_id, settings.max_history_messages)

            override = (body.system_prompt or "").strip() or None
            system_prompt = override or (session or {}).get("system_prompt") or settings.default_system_prompt
            messages = [
                {"role": "system", "content": system_prompt},
                *history,
                {"role": "user", "content": body.message},
            ]

            try:
                result = await gateway.complete(messages, simulate)
            except AllProvidersUnavailable:
                raise HTTPException(status_code=503, detail="all providers unavailable")

            latency_ms = max(1, round((time.perf_counter() - started) * 1000))
            metadata = ChatMetadata(
                resolution_source=result.resolution_source,
                model=result.model,
                latency_ms=latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                key_id=result.key_id,
                fallback_attempts=result.fallback_attempts,
            )
            await asyncio.to_thread(
                db.save_exchange, body.session_id, override, body.message, result.reply, metadata.model_dump()
            )

        return ChatResponse(session_id=body.session_id, reply=result.reply, metadata=metadata)

    @app.get("/sessions/{session_id}/messages", response_model=None)
    async def session_messages(session_id: str, request: Request) -> dict:
        messages = await asyncio.to_thread(request.app.state.db.list_messages, session_id)
        if messages is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "messages": messages}

    return app


app = create_app()
