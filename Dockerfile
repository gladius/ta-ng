# One image, one service. Node only builds the web UI (web/ -> web/dist); the running service is Python, and the
# FastAPI app serves that build under /app next to the API. On Cloud Run: VALIDATION_INPROCESS_WORKERS=1 (set below),
# DATABASE_URL set, CPU always allocated with min instances 1.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.14-slim
WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=web /web/dist web/dist
ENV VALIDATION_INPROCESS_WORKERS=1 PYTHONUNBUFFERED=1
CMD exec uvicorn app.web.server:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 5
