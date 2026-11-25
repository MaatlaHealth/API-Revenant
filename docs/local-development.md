# Local Development

> Status: slice 1 is implemented. The backend section below is runnable today; the frontend, demonstration and database sections describe the intended workflow and become runnable as later slices land.

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

## Backend

Slices 1 to 4 are implemented: the backend starts, checks its configuration, serves `/healthz` and `/readyz`, authenticates the management API, stores credentials, manages projects, imports evidence, runs reconstruction and records mapping review. SQLite is used locally; no PostgreSQL installation is needed.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.lock.txt
pip install --no-deps -e .
alembic upgrade head
uvicorn revenant.main:app --reload --port 8000 --no-access-log
```

- Liveness: http://localhost:8000/healthz
- Readiness: http://localhost:8000/readyz (reports `not_ready` until `alembic upgrade head` has been run)
- API documentation: http://localhost:8000/api/v1/docs (projects, evidence, reconstruction, mappings and credentials)

Checks:

```powershell
ruff check .
mypy
pytest
python tests/mutation/outbound_mutation_check.py
python tests/mutation/foundation_mutation_check.py
python tests/mutation/evidence_mutation_check.py
python tests/mutation/reconstruction_mutation_check.py
```

Tests create a fresh SQLite database per test from the real migrations. To run them against PostgreSQL instead, set `TEST_DATABASE_URL`; the database it names is emptied for every test, so never point it at one you care about.

Rotating the master key: set `REVENANT_SECRET_KEY` to the new key and `REVENANT_SECRET_KEY_PREVIOUS` to the old one, then run `python -m revenant.maintenance rotate-master-key`.

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
