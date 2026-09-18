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

# Refresh perl-base from Debian to apply security fixes missing from the pinned base.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
    ffmpeg perl-base

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

# Catch missing cache backend dependencies before publishing any runtime image.
RUN python -c "from apps.kwt_common.adapters.outbound.redis_cache import SharedRedisCache, PrometheusRedisCache; SharedRedisCache('redis://localhost:6379/0', {}); PrometheusRedisCache('redis://localhost:6379/0', {})"

EXPOSE 1664

ENTRYPOINT ["python", "-m", "korfbal.worker"]
CMD ["worker"]

# Shared image tools; only the dedicated vision queue executes model inference.
FROM production AS vision-tools
USER root
COPY --from=ghcr.io/astral-sh/uv:0.9.18@sha256:5713fa8217f92b80223bc83aac7db36ec80a84437dbc0d04bbc659cae030d8c9 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
COPY scripts/python/korfbal_vision_environment.py /tmp/create_runtime.py
RUN --mount=type=cache,target=/root/.cache/uv python /tmp/create_runtime.py /opt/vision --cpu
ADD https://github.com/openai/codex/releases/download/rust-v0.154.0/codex-x86_64-unknown-linux-musl.tar.gz /tmp/codex.tar.gz
RUN echo "d7e18b2597ae8f242f5f31ee9e90deef48dbc9edd634d9868fb6435d08c07f02  /tmp/codex.tar.gz" > /tmp/codex.sha256 && \
    sha256sum -c /tmp/codex.sha256 && \
    tar -xzf /tmp/codex.tar.gz -C /tmp && install -m 755 /tmp/codex-x86_64-unknown-linux-musl /usr/local/bin/codex && rm /tmp/codex.sha256 /tmp/codex.tar.gz /tmp/codex-x86_64-unknown-linux-musl
COPY scripts/python/korfbal_vision.py scripts/python/korfbal_vision_environment.py scripts/python/korfbal_autotrack_dataset.py /app/scripts/python/
COPY scripts/python/korfbal_review /app/scripts/python/korfbal_review
# Kits preserve the repository layout, while Django imports the canonical /app/apps tree.
RUN mkdir -p /app/apps/django_projects/korfbal/apps/video_analysis && ln -s /app/apps/video_analysis/engine /app/apps/django_projects/korfbal/apps/video_analysis/engine && install -d -o appuser -g appuser /models/config /var/lib/korfbal/video-analysis && install -d -m 700 -o appuser -g appuser /home/appuser/.codex
ENV PYTHONPATH=/app
USER appuser
