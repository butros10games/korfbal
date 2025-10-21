## ------------------------------- Dependency Stage ------------------------------ ##
FROM python:3.13-trixie AS deps

RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /install.sh
RUN chmod -R 655 /install.sh && /install.sh && rm /install.sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /build/apps/django_projects/korfbal/deps

COPY apps/django_projects/korfbal/deps/pyproject.toml ./pyproject.toml
COPY apps/django_projects/korfbal/deps/uv.lock ./uv.lock
COPY libs/django_packages/bg_auth/ /build/libs/django_packages/bg_auth/
COPY libs/django_packages/bg_django_caching_paginator/ /build/libs/django_packages/bg_django_caching_paginator/
COPY libs/django_packages/bg_django_mobile_detector/ /build/libs/django_packages/bg_django_mobile_detector/
COPY libs/shared_python_packages/bg_uuidv7/ /build/libs/shared_python_packages/bg_uuidv7/

ENV UV_PROJECT_ENVIRONMENT=/build/.venv

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --group celery --no-editable

## ------------------------------- Builder Stage ------------------------------ ##
FROM deps AS builder

WORKDIR /app

COPY --from=deps /build/.venv .venv

# Copy application sources after dependencies are cached
COPY apps/django_projects/korfbal/pyproject.toml /app/pyproject.toml
COPY apps/django_projects/korfbal/uv.lock /app/uv.lock
COPY --chmod=0555 apps/django_projects/korfbal/manage.py /app/
COPY --chmod=0555 apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY --chmod=0555 apps/django_projects/korfbal/apps/ /app/apps/
RUN --mount=type=cache,target=/root/.cache/pip \
    (.venv/bin/python -m ensurepip --upgrade || true) && \
    .venv/bin/python -m pip install --upgrade pip hatchling && \
    .venv/bin/python -m pip install --no-deps --no-build-isolation . && \
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
