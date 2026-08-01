# syntax=docker/dockerfile:1.7

# Multi-stage, mirroring the API image: the builder holds the toolchain and the full
# dependency tree, the runtime holds only what serves requests.
FROM node:22-alpine AS deps

WORKDIR /build

# Dependencies install from the lockfile alone first, so editing a component does not
# invalidate the slow layer. `npm ci` rather than `install` — the lockfile is the
# specification, and a build that silently resolves a different tree is not reproducible.
COPY package.json package-lock.json ./
RUN npm ci


FROM node:22-alpine AS builder

WORKDIR /build
COPY --from=deps /build/node_modules ./node_modules
COPY . .

# **Baked in at build time, not read at runtime.** Next inlines every `NEXT_PUBLIC_*`
# value into the client bundle during the build, so setting these in `docker run` has no
# effect — a footgun worth naming here rather than leaving someone to discover it against
# a page that silently calls the wrong host.
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000/api/v1
ARG NEXT_PUBLIC_DEV_USER
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE \
    NEXT_PUBLIC_DEV_USER=$NEXT_PUBLIC_DEV_USER \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build


FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /srv

# Unprivileged: nothing here needs to write to its own filesystem.
RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001

# `output: "standalone"` produces a server bundle carrying only the modules actually
# imported. Static assets are not in it and are copied separately.
COPY --from=builder --chown=nextjs:nodejs /build/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /build/.next/static ./.next/static
# No `public/` directory exists yet. `COPY` of a missing path fails the build, so this is
# deliberately absent rather than optimistically present — add it here when static assets
# arrive.

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:3000/ || exit 1

CMD ["node", "server.js"]
