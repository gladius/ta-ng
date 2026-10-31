# The UI. Build from web/: podman build -t token-auditor-web web
# BACKEND_URL (e.g. http://token-auditor-backend:8080) must be set when it runs. It listens on 8080.
FROM docker.io/library/node:24-slim AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM docker.io/nginxinc/nginx-unprivileged:1.29-alpine
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /web/dist /usr/share/nginx/html/app
