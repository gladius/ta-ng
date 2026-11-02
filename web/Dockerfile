# The UI. Build from web/: podman build -t token-auditor-web web
# BACKEND_URL (e.g. http://token-auditor-backend:8080) must be set when it runs. It listens on 8080.
# To serve everything under a path, add --build-arg PATH_PREFIX=/token-auditor and set the same PATH_PREFIX on the backend.
FROM docker.io/library/node:24-slim AS build
ARG PATH_PREFIX=""
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN PATH_PREFIX=$PATH_PREFIX npm run build

FROM docker.io/nginxinc/nginx-unprivileged:1.29-alpine
ARG PATH_PREFIX=""
ENV PATH_PREFIX=$PATH_PREFIX
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /web/dist /usr/share/nginx/html$PATH_PREFIX/app
