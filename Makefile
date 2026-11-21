.PHONY: up down logs test dev-backend dev-frontend

# Start the whole stack: backend on :8000, frontend on :3000
up:
	@test -f .env || (cp .env.example .env && echo ">> Created .env from .env.example. Put your real keys in LLM_API_KEYS, then run 'make up' again to apply them.")
	docker compose up --build -d
	@echo "Frontend: http://localhost:3000   Backend: http://localhost:8000/health"

# Stop the stack (the SQLite volume is kept; use 'docker compose down -v' to wipe it)
down:
	docker compose down

logs:
	docker compose logs -f

# Backend tests (no network or real keys needed)
test:
	cd backend && python -m pip install -q -r requirements-dev.txt && python -m pytest -q

# Run without Docker
dev-backend:
	cd backend && set -a && . ../.env && set +a && \
	DATABASE_URL=sqlite:///./data/app.db CACHE_DIR=./data/llm_cache uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm install && VITE_API_URL=http://localhost:8000 npm run dev
