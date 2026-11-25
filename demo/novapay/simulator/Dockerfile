# NovaPay simulator: a FICTIONAL replacement API for the demonstration.
# It contains no API Revenant code.
# NOT YET BUILT OR RUN: Docker is not installed on the development machine.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN pip install --no-cache-dir "fastapi==0.165.0" "uvicorn==0.54.0"
COPY app.py ./
RUN useradd --create-home --uid 10001 novapay
USER novapay
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
