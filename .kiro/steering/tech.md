---
inclusion: always
---

# Technology

## Backend
- Python 3.12, FastAPI, Pydantic v2, pydantic-settings
- SQLAlchemy 2.x (typed declarative), Alembic
- HTTPX (async) on an httpcore pool with a custom network backend for all outbound HTTP (see `spikes/outbound_transport`); dependency ranges pinned
- structlog for JSON logging
- cryptography (Fernet) for secrets
- PyYAML (`safe_load` only)
- jsonschema (independent validator for verification oracles)
- Pytest, pytest-asyncio, Hypothesis, ruff, mypy

## Frontend
- Next.js (App Router), TypeScript strict, Tailwind CSS
- Typed API client generated from or checked against the backend OpenAPI document
- Vitest; Playwright for one end-to-end journey

## Data
- SQLite locally (WAL mode, foreign keys on); PostgreSQL in production
- One model set, one migration history; use only portable column types

## Hosting
- Frontend: Netlify. Backend and PostgreSQL: Render. No other backend target is supported or configured.
- Exactly one Uvicorn worker (`--workers 1`, `WEB_CONCURRENCY=1`), started with `--no-access-log`. The app refuses to start with more than one worker.

## Commands
```
backend:   pip install -e ".[dev]" · alembic upgrade head · uvicorn revenant.main:app --reload · pytest · ruff check . · mypy revenant
frontend:  npm install · npm run dev · npm run build · npm run lint · npm test
demo:      python demo/run_demo.py · docker compose --profile after run --rm legacy-client-after
```

## Constraints
- Do not add a dependency without a stated need; prefer the standard library.
- No LLM calls in this release.
- No `eval`, `exec`, `subprocess` with user data, `pickle`, or template engines on imported content.
- All outbound HTTP goes through `revenant.security.outbound`.
- Configuration comes from environment variables; `.env` is never committed.
