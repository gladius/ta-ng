# The API and the validation workers. Build from backend/: podman build -t token-auditor-backend backend
# DATABASE_URL must be set when it runs, and the app stops at startup without it.
FROM docker.io/library/python:3.14-slim
WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV VALIDATION_INPROCESS_WORKERS=1 PYTHONUNBUFFERED=1
CMD exec uvicorn app.web.server:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 5
