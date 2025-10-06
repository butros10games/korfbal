## ------------------------------- Builder Stage ------------------------------ ##
FROM python:3.13-trixie AS builder

RUN apt-get update && \
    apt-get install --no-install-recommends -y build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

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

RUN uv sync --frozen --group daphne --no-editable

## ------------------------------- Production Stage ------------------------------ ##
FROM python:3.13-slim-trixie AS production

ARG APP_UID=1000
ARG APP_GID=1000

RUN groupadd --gid "${APP_GID}" appuser \
    && useradd --uid "${APP_UID}" --gid appuser --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser \
    && install -d -o appuser -g appuser /app

ENV LANG=nl_NL.utf8
ENV LANGUAGE=nl_NL:en
ENV LC_ALL=nl_NL.utf8
WORKDIR /app

COPY --from=builder --chmod=0555 /app/.venv .venv
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1

COPY --chmod=0555 apps/django_projects/korfbal/manage.py /app/
COPY --chmod=0555 apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY --chmod=0555 apps/django_projects/korfbal/apps/ /app/apps/

USER appuser

EXPOSE 8001

CMD ["daphne", "-p", "8001", "-b", "0.0.0.0", "korfbal.asgi:application"]
