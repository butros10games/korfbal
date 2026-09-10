FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS audio-js

## ------------------------------- Dependency Stage ------------------------------ ##
# Install third-party dependencies before local source so library changes retain that cache.
FROM python:3.13-slim-trixie@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS deps

COPY --from=ghcr.io/astral-sh/uv:0.9.18@sha256:5713fa8217f92b80223bc83aac7db36ec80a84437dbc0d04bbc659cae030d8c9 /uv /bin/uv

WORKDIR /build/apps/django_projects/korfbal/deps

# Copy ONLY lock files first (changes less frequently than code)
COPY apps/django_projects/korfbal/deps/pyproject.toml ./pyproject.toml
COPY apps/django_projects/korfbal/deps/uv.lock ./uv.lock

# Copy local package metadata before source so third-party dependencies stay cached.
COPY libs/shared_python_packages/bg_audit_events/pyproject.toml libs/shared_python_packages/bg_audit_events/README.md /build/libs/shared_python_packages/bg_audit_events/
COPY libs/django_packages/bg_auth/pyproject.toml libs/django_packages/bg_auth/LICENSE libs/django_packages/bg_auth/README.md /build/libs/django_packages/bg_auth/
COPY libs/django_packages/bg_django_caching_paginator/pyproject.toml libs/django_packages/bg_django_caching_paginator/LICENSE libs/django_packages/bg_django_caching_paginator/README.md /build/libs/django_packages/bg_django_caching_paginator/
COPY libs/django_packages/bg_django_mobile_detector/pyproject.toml /build/libs/django_packages/bg_django_mobile_detector/
COPY libs/shared_python_packages/bg_uuidv7/pyproject.toml libs/shared_python_packages/bg_uuidv7/LICENSE libs/shared_python_packages/bg_uuidv7/README.md /build/libs/shared_python_packages/bg_uuidv7/

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-local

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --group worker --no-dev --no-editable --no-install-local

## ------------------------------- Venv Optimizer Stage ------------------------------ ##
# Mount local sources only for wheel assembly; retain only the installed, pruned venv.
FROM deps AS venv-optimizer

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=libs/shared_python_packages/bg_audit_events/src,target=/build/libs/shared_python_packages/bg_audit_events/src \
    --mount=type=bind,source=libs/django_packages/bg_auth/src,target=/build/libs/django_packages/bg_auth/src \
    --mount=type=bind,source=libs/django_packages/bg_django_caching_paginator/src,target=/build/libs/django_packages/bg_django_caching_paginator/src \
    --mount=type=bind,source=libs/django_packages/bg_django_mobile_detector/src,target=/build/libs/django_packages/bg_django_mobile_detector/src \
    --mount=type=bind,source=libs/shared_python_packages/bg_uuidv7/src,target=/build/libs/shared_python_packages/bg_uuidv7/src \
    uv sync --frozen --group worker --no-dev --no-editable && \
    find /app/.venv -type d -name "tests" ! -path "*/django/*" -prune -exec rm -rf {} + && \
    find /app/.venv -type d -name "test" ! -path "*/django/*" -prune -exec rm -rf {} + && \
    find /app/.venv -type d -name "examples" -prune -exec rm -rf {} + && \
    rm -rf /app/.venv/lib/python3.13/site-packages/pip \
    /app/.venv/lib/python3.13/site-packages/setuptools \
    /app/.venv/lib/python3.13/site-packages/wheel

## ------------------------------- Production Stage ------------------------------ ##
FROM python:3.13-slim-trixie@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS production

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
    ffmpeg

RUN groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid appuser --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser \
    && chown appuser:appuser /app \
    && install -d -o appuser -g appuser -m 0755 /app/logs

COPY --link --from=audio-js /usr/local/bin/node /usr/local/bin/node

COPY --link --from=venv-optimizer /app/.venv .venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1

COPY --link --chmod=u=rwX,go=rX apps/django_projects/korfbal/manage.py /app/
COPY --link --chmod=u=rwX,go=rX apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY --link --chmod=u=rwX,go=rX apps/django_projects/korfbal/apps/ /app/apps/

USER appuser

EXPOSE 1664

CMD ["celery", "-A", "korfbal", "worker", "--loglevel", "info"]
