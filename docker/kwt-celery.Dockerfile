## ------------------------------- Dependency Stage ------------------------------ ##
FROM python:3.13-slim-trixie AS deps

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install --no-install-recommends -y \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /build/apps/django_projects/korfbal/deps

COPY apps/django_projects/korfbal/deps/pyproject.toml ./pyproject.toml
COPY apps/django_projects/korfbal/deps/uv.lock ./uv.lock
COPY libs/django_packages/bg_auth/ /build/libs/django_packages/bg_auth/
COPY libs/django_packages/bg_django_caching_paginator/ /build/libs/django_packages/bg_django_caching_paginator/
COPY libs/django_packages/bg_django_mobile_detector/ /build/libs/django_packages/bg_django_mobile_detector/
COPY libs/shared_python_packages/bg_uuidv7/ /build/libs/shared_python_packages/bg_uuidv7/

ENV UV_PROJECT_ENVIRONMENT=/build/.venv

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --group celery --no-dev --no-editable --compile-bytecode

## ------------------------------- Builder Stage ------------------------------ ##
FROM deps AS builder

WORKDIR /build

# Copy workspace config and libs for resolution
COPY pyproject.toml .
COPY libs/django_packages/bg_auth/ libs/django_packages/bg_auth/
COPY libs/django_packages/bg_django_caching_paginator/ libs/django_packages/bg_django_caching_paginator/
COPY libs/django_packages/bg_django_mobile_detector/ libs/django_packages/bg_django_mobile_detector/
COPY libs/shared_python_packages/bg_uuidv7/ libs/shared_python_packages/bg_uuidv7/

# Copy app source
COPY apps/django_projects/korfbal/ apps/django_projects/korfbal/

# Bring in the venv from deps
COPY --from=deps /build/.venv /build/.venv

# Install the app into the venv
WORKDIR /build/apps/django_projects/korfbal
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /build/.venv --no-deps . --compile-bytecode

# Optimize venv size
RUN find /build/.venv -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null || true && \
    find /build/.venv -type d -name "tests" -exec rm -rf {} + && \
    find /build/.venv -type d -name "examples" -exec rm -rf {} + && \
    find /build/.venv -name "__pycache__" -type d -exec rm -rf {} + && \
    find /build/.venv -name "*.pyc" -delete && \
    rm -rf /build/.venv/lib/python3.13/site-packages/pip \
    /build/.venv/lib/python3.13/site-packages/setuptools \
    /build/.venv/lib/python3.13/site-packages/wheel

# Prepare final app directory
WORKDIR /app
RUN cp -r /build/.venv .venv && \
    cp /build/apps/django_projects/korfbal/manage.py . && \
    cp -r /build/apps/django_projects/korfbal/korfbal . && \
    cp -r /build/apps/django_projects/korfbal/apps . && \
    find .venv/bin -maxdepth 1 -type f -exec sed -i '1s|^#!/build/.venv/bin/python|#!/app/.venv/bin/python|' {} +

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
