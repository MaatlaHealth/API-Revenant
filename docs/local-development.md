# Local Development

> Stage 1 status: the repository contains specifications and scaffold only. The commands below describe the intended workflow and become runnable as Stages 2–4 land. Each section states the stage that enables it.

## Prerequisites

- Python 3.12+
- Node.js 20+ and npm
- Git
- Optional: Docker Desktop (required only for the hostname-routed demonstration)

No PostgreSQL installation is needed locally; SQLite is used by default.

## Configuration

```powershell
Copy-Item .env.example .env
```

Generate the two required values and paste them into `.env`:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env` is ignored by Git. Never commit it.

## Backend (Stage 2)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
alembic upgrade head
uvicorn revenant.main:app --reload --port 8000 --no-access-log
```

- API documentation: http://localhost:8000/api/v1/docs
- Liveness: http://localhost:8000/healthz

Tests:

```powershell
pytest
```

Run against PostgreSQL instead of SQLite by setting `TEST_DATABASE_URL`.

## Frontend (Stage 4)

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:3000. The app asks for the admin token from your `.env` the first time.

## Demonstration (Stage 3)

Without Docker — runs the NovaPay simulator, the backend and the unchanged legacy client on localhost:

```powershell
python demo/run_demo.py
```

With Docker — demonstrates the original hostname being preserved:

```powershell
docker compose --profile before run --rm legacy-client-before
```

```powershell
docker compose --profile after run --rm legacy-client-after
```

## Database

- Local database file: `backend/revenant.db` (ignored by Git). Delete it to reset.
- New migration: `alembic revision --autogenerate -m "message"`, then review the generated file by hand. Migrations must run on both SQLite and PostgreSQL.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Backend refuses to start: "example secret" | `.env` still contains placeholder values |
| `unsafe_url` when setting a localhost replacement URL | Set `REVENANT_ALLOW_PRIVATE_UPSTREAMS=true` for local work only |
| Frontend shows "Backend unreachable" | Backend not running, or `NEXT_PUBLIC_API_URL` wrong, or origin missing from `REVENANT_CORS_ORIGINS` |
