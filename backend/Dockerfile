# API Revenant backend. Build from the repository root:
#   docker build -f backend/Dockerfile .
# NOT YET BUILT OR RUN: Docker is not installed on the development machine.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REVENANT_DEMO_DIR=/app/demo WEB_CONCURRENCY=1

WORKDIR /app/backend
COPY backend/requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY backend/ ./
RUN pip install --no-cache-dir --no-deps .
# The bundled demonstration evidence (fictional services, sample data).
COPY demo/gravepay /app/demo/gravepay
COPY demo/novapay/openapi.yaml demo/novapay/recordings.json /app/demo/novapay/

RUN useradd --create-home --uid 10001 revenant
USER revenant
EXPOSE 8000
# Exactly one worker (docs/architecture.md, section 4.5). The server's own access
# log is off: it would record query strings.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn revenant.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-access-log"]
