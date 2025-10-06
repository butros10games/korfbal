## ------------------------------- Builder Stage ------------------------------ ##
FROM python:3.13-trixie AS builder

RUN apt-get update && \
    apt-get install --no-install-recommends -y build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /install.sh
RUN chmod -R 655 /install.sh && /install.sh && rm /install.sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY libs/django_packages/bg_auth/ ./libs/django_packages/bg_auth/
COPY libs/django_packages/bg_django_caching_paginator/ ./libs/django_packages/bg_django_caching_paginator/
COPY libs/django_packages/bg_django_mobile_detector/ ./libs/django_packages/bg_django_mobile_detector/
COPY libs/shared_python_packages/bg_uuidv7/ ./libs/shared_python_packages/bg_uuidv7/

# Bring in the Django project sources that are required to build the wheel inside the workspace.
COPY apps/django_projects/korfbal/pyproject.toml ./apps/django_projects/korfbal/pyproject.toml
COPY apps/django_projects/korfbal/uv.lock ./apps/django_projects/korfbal/uv.lock
COPY apps/django_projects/korfbal/korfbal/ ./apps/django_projects/korfbal/korfbal/
COPY apps/django_projects/korfbal/apps/ ./apps/django_projects/korfbal/apps/

WORKDIR /app/apps/django_projects/korfbal/
ENV UV_PROJECT_ENVIRONMENT=/app/.venv

RUN uv sync --frozen --no-editable

## ------------------------------- Webpack Stage ------------------------------ ##
FROM node:22-alpine AS rspack

WORKDIR /app

COPY apps/django_projects/korfbal/package.json apps/django_projects/korfbal/package-lock.json ./
RUN npm install --ignore-scripts

COPY apps/django_projects/korfbal/configs/rspack/rspack.config.js ./configs/rspack/
COPY apps/django_projects/korfbal/static_workfile/ ./static_workfile/

RUN npm run build     && rm -rf /app/static_workfile/js

## ------------------------------- Production Stage ------------------------------ ##
FROM python:3.13-slim-trixie AS production

ARG APP_UID=1000
ARG APP_GID=1000
ARG TARGETOS=linux
ARG TARGETARCH
ARG TARGETVARIANT=""

RUN set -euo \
    && ARCH_GUESS="${TARGETARCH:-}" \
    && if [ -z "$ARCH_GUESS" ]; then ARCH_GUESS="$(dpkg --print-architecture 2>/dev/null || uname -m)"; fi \
    && if [ "$ARCH_GUESS" = "arm" ] && [ -n "${TARGETVARIANT:-}" ]; then ARCH_GUESS="arm${TARGETVARIANT}"; fi \
    && case "$ARCH_GUESS" in \
        amd64|x86_64) MC_ARCH="amd64" ;; \
        arm64|aarch64) MC_ARCH="arm64" ;; \
        armv7|armv7l|armhf|armv6|armv6l|armel|arm) MC_ARCH="arm" ;; \
        *) echo "Unsupported architecture: ${ARCH_GUESS}" >&2 ; exit 1 ;; \
    esac \
    && MC_URL="https://dl.min.io/client/mc/release/${TARGETOS}-${MC_ARCH}/mc" \
    && python -c "import sys, urllib.request; url = sys.argv[1]; open('/usr/local/bin/mc', 'wb').write(urllib.request.urlopen(url).read())" "${MC_URL}" \
    && chmod +x /usr/local/bin/mc \
    && groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid appuser --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser \
    && install -d -o appuser -g appuser /app \
    && install -d -o appuser -g appuser /app/logs

COPY --chmod=0555 apps/django_projects/korfbal/configs/collectstatic/entrypoint.sh /app/entrypoint.sh

WORKDIR /app

COPY --from=builder --chmod=0555 /app/.venv .venv
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1

COPY --chmod=0555 apps/django_projects/korfbal/manage.py /app/
COPY --chmod=0555 apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY --chmod=0555 apps/django_projects/korfbal/apps/ /app/apps/
COPY --from=rspack /app/static_workfile/ /app/static_workfile/

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
