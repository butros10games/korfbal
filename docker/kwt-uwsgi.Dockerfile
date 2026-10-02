## ------------------------------- Python Stage ------------------------------ ##
# uv's python-build-standalone CPython served Korfbal requests ~24% faster than the
# official python image's build of the same release. Keep only runtime files.
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS python

COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /bin/uv

ENV UV_PYTHON_INSTALL_DIR=/opt/python

RUN uv python install --no-bin 3.14.7 && \
    cd /opt/python/cpython-3.14.7-linux-x86_64-gnu && \
    rm -rf include lib/libpython3.14.so* lib/libtcl* lib/itcl* lib/tcl* lib/tk* lib/thread* \
    lib/python3.14/idlelib lib/python3.14/tkinter lib/python3.14/turtledemo \
    lib/python3.14/lib-dynload/_tkinter.* && \
    ln -s /opt/python/cpython-3.14.7-linux-x86_64-gnu/bin/python3.14 /usr/local/bin/python3.14 && \
    ln -s python3.14 /usr/local/bin/python3 && \
    ln -s python3.14 /usr/local/bin/python

## ------------------------------- Dependency Stage ------------------------------ ##
# Install third-party dependencies before local source so library changes retain that cache.
FROM python AS deps

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
ENV UV_PYTHON=3.14.7 UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_DOWNLOADS=never

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-local

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --group uwsgi --no-dev --no-editable --no-install-local

## ------------------------------- Venv Optimizer Stage ------------------------------ ##
# Mount local sources only for wheel assembly; retain only the installed, pruned venv.
FROM deps AS venv-optimizer

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=libs/shared_python_packages/bg_audit_events/src,target=/build/libs/shared_python_packages/bg_audit_events/src \
    --mount=type=bind,source=libs/django_packages/bg_auth/src,target=/build/libs/django_packages/bg_auth/src \
    --mount=type=bind,source=libs/django_packages/bg_django_caching_paginator/src,target=/build/libs/django_packages/bg_django_caching_paginator/src \
    --mount=type=bind,source=libs/django_packages/bg_django_mobile_detector/src,target=/build/libs/django_packages/bg_django_mobile_detector/src \
    --mount=type=bind,source=libs/shared_python_packages/bg_uuidv7/src,target=/build/libs/shared_python_packages/bg_uuidv7/src \
    uv sync --frozen --group uwsgi --no-dev --no-editable && \
    find /app/.venv -type d -name "tests" ! -path "*/django/*" -prune -exec rm -rf {} + && \
    find /app/.venv -type d -name "test" ! -path "*/django/*" -prune -exec rm -rf {} + && \
    find /app/.venv -type d -name "examples" -prune -exec rm -rf {} + && \
    rm -rf /app/.venv/lib/python3.14/site-packages/pip \
    /app/.venv/lib/python3.14/site-packages/setuptools \
    /app/.venv/lib/python3.14/site-packages/wheel

## ------------------------------- Production Stage ------------------------------ ##
FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a AS production

ENV LANG=C.UTF-8

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

# Refresh perl-base from Debian to apply security fixes missing from the pinned base.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
    ca-certificates libjemalloc2 netbase perl-base && \
    groupadd --gid "${APP_GID}" appuser && \
    useradd --uid "${APP_UID}" --gid appuser --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser && \
    install -d -o appuser -g appuser /app && \
    install -d -o appuser -g appuser /app/logs && \
    install -o appuser -g appuser -m 644 /dev/null /app/logs/uwsgi.log

COPY --link --from=python /opt/python /opt/python
COPY --link --from=python /usr/local/bin/ /usr/local/bin/
COPY --link --from=venv-optimizer /app/.venv .venv
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1

COPY --link --chmod=0555 apps/django_projects/korfbal/configs/uwsgi/generic_entrypoint.sh /app/entrypoint.sh
COPY --link --chmod=u=rwX,go=rX apps/django_projects/korfbal/manage.py /app/
COPY --link --chmod=u=rwX,go=rX apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY --link --chmod=u=rwX,go=rX apps/django_projects/korfbal/apps/ /app/apps/

ENV GRANIAN_WORKERS=4 KORFBAL_BIND_HOST=0.0.0.0

USER appuser

# Catch missing cache backend dependencies before publishing any runtime image.
RUN python -c "from apps.kwt_common.adapters.outbound.redis_cache import SharedRedisCache, PrometheusRedisCache; SharedRedisCache('redis://localhost:6379/0', {}); PrometheusRedisCache('redis://localhost:6379/0', {})"

EXPOSE 1664 1665 1666 1667

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "korfbal.serve"]
