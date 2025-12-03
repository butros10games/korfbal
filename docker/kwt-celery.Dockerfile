## ------------------------------- Dependency Stage ------------------------------ ##
# This stage only installs third-party deps. Libs code changes won't invalidate this cache.
FROM python:3.13-slim-trixie AS deps

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install --no-install-recommends -y \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /build/apps/django_projects/korfbal/deps

# Copy ONLY lock files first (changes less frequently than code)
COPY apps/django_projects/korfbal/deps/pyproject.toml ./pyproject.toml
COPY apps/django_projects/korfbal/deps/uv.lock ./uv.lock

# Copy ONLY build-required files from libs (pyproject.toml, src/, LICENSE, README where needed)
COPY libs/django_packages/bg_auth/pyproject.toml libs/django_packages/bg_auth/LICENSE libs/django_packages/bg_auth/README.md /build/libs/django_packages/bg_auth/
COPY libs/django_packages/bg_auth/src/ /build/libs/django_packages/bg_auth/src/
COPY libs/django_packages/bg_django_caching_paginator/pyproject.toml libs/django_packages/bg_django_caching_paginator/LICENSE libs/django_packages/bg_django_caching_paginator/README.md /build/libs/django_packages/bg_django_caching_paginator/
COPY libs/django_packages/bg_django_caching_paginator/src/ /build/libs/django_packages/bg_django_caching_paginator/src/
COPY libs/django_packages/bg_django_mobile_detector/pyproject.toml /build/libs/django_packages/bg_django_mobile_detector/
COPY libs/django_packages/bg_django_mobile_detector/src/ /build/libs/django_packages/bg_django_mobile_detector/src/
COPY libs/shared_python_packages/bg_uuidv7/pyproject.toml libs/shared_python_packages/bg_uuidv7/LICENSE libs/shared_python_packages/bg_uuidv7/README.md /build/libs/shared_python_packages/bg_uuidv7/
COPY libs/shared_python_packages/bg_uuidv7/src/ /build/libs/shared_python_packages/bg_uuidv7/src/

ENV UV_PROJECT_ENVIRONMENT=/build/.venv
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --group celery --no-dev --no-editable --compile-bytecode

## ------------------------------- Venv Optimizer Stage ------------------------------ ##
# Separate stage so app source changes don't re-run optimization
FROM deps AS venv-optimizer

RUN find /build/.venv -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null || true && \
    find /build/.venv -type d -name "tests" ! -path "*/django/*" -exec rm -rf {} + 2>/dev/null || true && \
    find /build/.venv -type d -name "test" ! -path "*/django/*" -exec rm -rf {} + 2>/dev/null || true && \
    find /build/.venv -type d -name "examples" -exec rm -rf {} + 2>/dev/null || true && \
    find /build/.venv -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true && \
    find /build/.venv -name "*.pyc" -delete 2>/dev/null || true && \
    rm -rf /build/.venv/lib/python3.13/site-packages/pip \
    /build/.venv/lib/python3.13/site-packages/setuptools \
    /build/.venv/lib/python3.13/site-packages/wheel && \
    find /build/.venv/bin -maxdepth 1 -type f -exec sed -i '1s|^#!/build/.venv/bin/python|#!/app/.venv/bin/python|' {} +

## ------------------------------- Builder Stage ------------------------------ ##
FROM venv-optimizer AS builder

WORKDIR /app

# Copy venv and app source directly to final location
RUN cp -r /build/.venv .venv

# Copy app source (this layer changes most often - keep it late)
COPY apps/django_projects/korfbal/manage.py /app/
COPY apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY apps/django_projects/korfbal/apps/ /app/apps/

## ------------------------------- Production Stage ------------------------------ ##
FROM python:3.13-slim-trixie AS production

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

RUN groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid appuser --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser \
    && chown appuser:appuser /app \
    && install -d -o appuser -g appuser -m 0755 /app/logs

COPY --from=builder --chmod=0555 /app/.venv .venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1

COPY --chmod=0555 apps/django_projects/korfbal/manage.py /app/
COPY --chmod=0555 apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY --chmod=0555 apps/django_projects/korfbal/apps/ /app/apps/

USER appuser

EXPOSE 1664

CMD ["celery", "-A", "korfbal", "worker", "--loglevel", "info"]
