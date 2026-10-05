# SupportFlow frontend — built static assets, served by nginx.
#
# Multi-stage, and the split is the whole point: the build stage carries Node, the module
# cache, and the toolchain, none of which belongs in a container that only ever hands back
# pre-built files. The final image is nginx plus a directory.
#
# Deployed behind Caddy (`docker/Caddyfile`), which owns TLS and path routing. **This image
# serves static files and nothing else** — it deliberately does not proxy `/api`, because two
# places that know how to reach the backend is two places to keep in sync, and Caddy's routing
# is what the development Vite proxy already mirrors.

# --- build -----------------------------------------------------------------
FROM node:22-slim AS build

WORKDIR /app

# Dependency manifests alone, so the install layer caches independently of source changes —
# the same ordering `backend/Dockerfile` uses for `pyproject.toml`.
COPY package.json package-lock.json ./
# `npm ci` rather than `npm install`: it installs exactly the lockfile and fails if the two
# disagree, which is what makes a build reproducible rather than merely usually-the-same.
RUN npm ci

COPY . .

# Same-origin relative URLs. In the deployed stack Caddy has already put the API, the socket,
# and the SPA on one origin, so a build-time hostname would be a second source of truth for
# something the proxy already answers — and a hardcoded one is wrong the moment the domain
# changes. `vite.config.ts` reads these through `import.meta.env`.
ARG VITE_API_BASE_URL=""
ARG VITE_WS_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_WS_URL=$VITE_WS_URL

RUN npm run build

# --- runtime ---------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# The stock config listens on 80 and serves /usr/share/nginx/html with no SPA fallback, no
# cache policy, and a `Server: nginx/1.27` banner. Replaced wholesale rather than extended.
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf

# The security headers are a separate file because `add_header` does not inherit into a
# `location` block that has one of its own, so each block has to include the set explicitly.
# `nginx.conf` includes this path from three places; if it is missing, nginx refuses to start —
# which is the failure that says so, rather than a page quietly served without `nosniff`.
COPY nginx-headers.conf /etc/nginx/snippets/supportflow-headers.conf

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# `wget --spider` rather than curl: the alpine image ships busybox wget and not curl, and
# adding curl to a static file server to satisfy a healthcheck is a package for a probe.
#
# **`127.0.0.1` and not `localhost`, and that is a bug this line already had.** `/etc/hosts` in
# the container maps `localhost` to both `::1` and `127.0.0.1`, busybox wget resolves the IPv6
# address first, and `listen 80;` binds IPv4 only — so the probe was refused and the container
# sat at `unhealthy` while serving every request correctly. The symptom was a healthcheck that
# failed against a server that was demonstrably up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
