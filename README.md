# Relay Chat: multi-turn AI chat with key failover, caching and per-request metadata

React + FastAPI + SQLite + LiteLLM. Every reply shows where it came from (`LLM_CACHE` or `LIVE_INFERENCE`), the model, latency and the tokens used by that one request.

## 1. Live URLs

| What | URL |
| --- | --- |
| Frontend | `https://ai-chat-app-zeta-pearl.vercel.app` |
| Backend base URL | `https://ai-chat-app-7zvg.onrender.com` |
| Health check | `https://ai-chat-app-7zvg.onrender.com/health` |
| GitHub repo | `https://github.com/Shubham07004/ai-chat-app` |
| Demo video | `https://youtu.be/Vdrdliqbnxg` |


## 2. Run locally

### With Docker (what the assignment asks for)

```bash
cp .env.example .env        # then edit LLM_API_KEYS with at least 2 real keys
make up                     # backend http://localhost:8000, frontend http://localhost:3000
make down                   # stop (the SQLite volume is kept)
```

`make up` creates `.env` from `.env.example` if it is missing. Check the stack with `curl localhost:8000/health`.

### Without Docker

```bash
# backend (Python 3.11+)
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export LLM_API_KEYS="key-one,key-two" ENABLE_FAILURE_SIMULATION=true \
       DATABASE_URL="sqlite:///./data/app.db" ALLOWED_ORIGINS="http://localhost:3000"
uvicorn app.main:app --reload --port 8000

# frontend (Node 20+), in a second terminal
cd frontend
npm install
VITE_API_URL=http://localhost:8000 npm run dev         # http://localhost:3000
```

### Tests

```bash
make test        # or: cd backend && pytest
```

The tests use the real FastAPI app, the real SQLite file and LiteLLM's real cache. Only the network call to the provider is replaced (LiteLLM's `mock_response`), so no keys are needed.

### Environment variables

See `.env.example`. The important ones: `LLM_API_KEYS` (comma-separated pool, ids `key_1`, `key_2`, ...), `LLM_MODEL` (any LiteLLM model name that matches your keys), `DATABASE_URL`, `ALLOWED_ORIGINS`, `ENABLE_FAILURE_SIMULATION`.

>> **Model note:** the code default is `gemini/gemini-2.5-flash`, but new Google AI Studio projects no longer get access to 2.5 models (404). This deployment sets `LLM_MODEL=gemini/gemini-3.5-flash-lite`. If your keys are for another provider, set `LLM_MODEL` accordingly (for example `gpt-4o-mini` or `groq/llama-3.3-70b-versatile`).

## 3. Design decisions

### Key fallback
* `LLM_API_KEYS` becomes an ordered pool: `key_1`, `key_2`, ...
* For each request the gateway calls `litellm.acompletion` with one key at a time. Any failure (HTTP 429, exhausted quota, 5xx, timeout, rejected key, or an empty completion) moves on to the next key. LiteLLM's own retries are off (`num_retries=0`) so failover is the retry mechanism.
* The response reports `key_id` (never the key) and `fallback_attempts` (how many keys failed first).
* If every key fails the API returns `503 {"detail": "all providers unavailable"}` and **nothing is written to history**.
* A key that just failed goes to the back of the line for `KEY_COOLDOWN_SECONDS` (default 30), so one exhausted key does not slow down every later request. Requests that use `X-Simulate-Failure` ignore the cooldown so the demo is deterministic.
* Provider error text is never logged, because it can contain request URLs. Only the exception class and status code are logged, and keys are never echoed to clients.

### Caching
* The cache is LiteLLM's `Cache` object (in-memory by default, `CACHE_TYPE=disk` for a disk cache under `/data`).
* The cache key is LiteLLM's key function over **model + the full message list** (system prompt + history + new message). API keys are not part of it, so a reply produced through `key_2` still serves later hits.
* On a hit the stored reply is returned unchanged with the tokens stored alongside it, `resolution_source = LLM_CACHE`, `key_id = null`, `fallback_attempts = 0`. Because history is part of the key, the same first message in a new session is a hit, while a different message, a different system prompt, or a different history is always a live call.
* I use explicit read/write calls on the LiteLLM cache instead of its automatic wrapper, because the automatic wrapper writes entries in a background task and an immediate repeat request could race it. Explicit calls make "second identical request is a hit" deterministic.
* Entries expire after `CACHE_TTL_SECONDS`.

### SQLite session storage
* Stdlib `sqlite3`; the file path comes from `DATABASE_URL` (`sqlite:////data/app.db` in Docker, a named volume), so history survives container restarts. Schema is created on startup.
* `messages` follows the required schema (plus an index on `(session_id, id)`). A small `sessions` table stores the optional per-session system prompt.
* Each successful turn (user message + assistant reply + metadata JSON) is written in **one transaction**. Failed requests (422, 503) write nothing, and an unknown session stays an unknown session (404).
* The client only sends the new message. The server loads the session's history, sends the last `MAX_HISTORY_MESSAGES` (default 40) to the model, and a per-session lock keeps two quick messages from interleaving.
* Token counts are the provider's usage for that single request, never a running total.

### Other choices
* Validation errors, 404s and unexpected errors are all JSON with a `detail` field; stack traces never reach the client.
* `/docs` and `/openapi.json` are off by default (`ENABLE_DOCS=true` to enable) so only the three contract endpoints are exposed.
* Both Dockerfiles are multi-stage. The backend runs as a non-root user; the frontend is static files behind nginx. The frontend reads its backend URL at container start (`API_URL`), so one image works anywhere.

## 4. Verification

Replace `$API` with `http://localhost:8000` or your deployed backend URL.

```bash
API=http://localhost:8000

# Health
curl -s $API/health

# Live inference, then a cache hit (same first message, NEW session)
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-1","message":"What is the capital of France?"}'
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-2","message":"What is the capital of France?"}'
#   -> second response: "resolution_source":"LLM_CACHE", "key_id":null, same reply, lower latency_ms

# Failover: key_1 fails, key_2 answers
curl -s -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"demo-3","message":"Tell me a fun fact about Delhi."}'
#   -> "key_id":"key_2", "fallback_attempts":1

# Every key fails: clean 503, nothing stored
curl -s -i -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"demo-4","message":"hello"}'
curl -s -i $API/sessions/demo-4/messages        # 404 {"detail":"session not found"}

# Multi-turn memory + stored history
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"demo-5","message":"My name is Ada."}'
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"demo-5","message":"What is my name?"}'
curl -s $API/sessions/demo-5/messages

# Validation
curl -s -i -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"x","message":"   "}'   # 422
```

In the UI: every assistant reply shows source, model, latency and that request's input/output tokens. Open **Session options** to type `key_1=429` into the failure simulator, send a new message, and the reply footer will say it was answered by `key_2` after 1 failed key. Click **New chat** and resend an earlier first message to see `LLM_CACHE`.

* Editor: VS Code
* AI tools: Claude (generated the first version of the code and tests; reviewed and run locally by me)
* Stack: React 18 + Vite, FastAPI, LiteLLM, SQLite, Docker Compose, pytest
* Deployment: Render (backend, Docker, free plan), Vercel (frontend)
* Model: `gemini/gemini-3.5-flash-lite`
 with 2 Gemini API keys

## 6. Bonus features

* Optional **system prompt** per session (stored with the session).
* **Failure-simulator field** in the UI so failover can be shown without curl.
* **Key cooldown** so a key that just hit a quota error does not keep slowing requests down.
* Optional **disk cache** (`CACHE_TYPE=disk`) that survives restarts.
* UI also shows which key answered and how many failed first.
* Per-session locking and a cap on history sent to the model, to bound cost.

## Deploying

**Backend (Render, Railway, Fly.io or Hugging Face Spaces, Docker)**
1. Create a web service from this repo with root directory `backend` (it uses `backend/Dockerfile` and honours `$PORT`).
2. Set env vars: `LLM_API_KEYS`, `LLM_MODEL`, `ALLOWED_ORIGINS=https://<your-frontend>`, `ENABLE_FAILURE_SIMULATION=true`, `DATABASE_URL=sqlite:////data/app.db`.
3. To keep history across restarts, attach a persistent volume mounted at `/data` (Render disk, Fly volume, or HF persistent storage). Without one the file lives on the container's own disk and resets when the instance is replaced.
4. Open `/health` to confirm.

**Frontend (Vercel, Netlify, Cloudflare Pages, or Render static site)**
1. Root directory `frontend`, build command `npm run build`, output directory `dist`.
2. Set the build env var `VITE_API_URL=https://<your-backend>`.
3. Add the resulting frontend URL to the backend's `ALLOWED_ORIGINS` and redeploy the backend.

## Repository layout

```
backend/    FastAPI app (app/), tests (tests/), Dockerfile, requirements
frontend/   React + Vite app, Dockerfile (nginx), runtime config script
docker-compose.yml   Makefile   .env.example   README.md
```
