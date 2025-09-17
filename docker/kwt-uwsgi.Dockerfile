## ------------------------------- Builder Stage ------------------------------ ##
FROM python:3.13-trixie AS builder

RUN apt-get update && apt-get install --no-install-recommends -y         build-essential &&     apt-get clean && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /install.sh
RUN chmod -R 655 /install.sh && /install.sh && rm /install.sh

ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY libs/ ./libs/
COPY apps/django_projects/korfbal/ ./apps/django_projects/korfbal/

RUN uv sync --all-groups --all-packages --no-editable

## ------------------------------- Production Stage ------------------------------ ##
FROM python:3.13-slim-trixie AS production

ARG APP_UID=1000
ARG APP_GID=1000

WORKDIR /app

RUN apt-get update && apt-get install --no-install-recommends -y         libexpat1 libpq5 libxml2 &&     apt-get clean && rm -rf /var/lib/apt/lists/* &&     groupadd --gid "${APP_GID}" appuser     && useradd --uid "${APP_UID}" --gid appuser --create-home --home-dir /home/appuser --shell /usr/sbin/nologin appuser     && install -d -o appuser -g appuser /app     && install -d -o appuser -g appuser /app/logs     && install -o appuser -g appuser -m 644 /dev/null /app/logs/uwsgi.log

COPY --from=builder --chmod=0555 /app/.venv .venv
ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1

COPY --chmod=0444 apps/django_projects/korfbal/configs/uwsgi/uwsgi.ini /app/
COPY --chmod=0555 apps/django_projects/korfbal/manage.py /app/
COPY --chmod=0555 apps/django_projects/korfbal/korfbal/ /app/korfbal/
COPY --chmod=0555 apps/django_projects/korfbal/templates/ /app/templates/
COPY --chmod=0555 apps/django_projects/korfbal/apps/ /app/apps/

USER appuser

EXPOSE 1664

CMD ["uwsgi", "--ini", "/app/uwsgi.ini"]
