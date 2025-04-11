## ------------------------------- Builder Stage ------------------------------ ##
FROM python:3.13-bookworm AS builder

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Download the latest installer, install it and then remove it
ADD https://astral.sh/uv/install.sh /install.sh
RUN chmod -R 655 /install.sh && /install.sh && rm /install.sh

# Set up the UV environment path correctly
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

COPY ./pyproject.toml .

RUN uv sync --group uwsgi

## ------------------------------- Webpack Stage ------------------------------ ##
FROM node:22-alpine AS rspack

WORKDIR /app

COPY ../package.json ../package-lock.json /app/
RUN npm install --ignore-scripts

COPY ../configs/rspack/rspack.config.js /app/configs/rspack/
COPY ../static_workfile/ /app/static_workfile/

RUN npm run build \
    && rm -rf /app/static_workfile/js

## ------------------------------- Production Stage ------------------------------ ##
FROM python:3.13-slim AS production

# Install MinIO client (mc)
ADD https://dl.min.io/client/mc/release/linux-amd64/mc /usr/local/bin/mc

# Copy entrypoint script and adjust permissions
COPY ../configs/collectstatic/entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh \
    && chmod +x /usr/local/bin/mc \
    && useradd --create-home appuser

USER appuser

WORKDIR /app

COPY --from=builder /app/.venv .venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application files
COPY ../manage.py /app/
COPY ../korfbal/ /app/korfbal/
COPY ../apps/ /app/apps/
COPY --from=webpack /app/static_workfile/ /app/static_workfile/

ENTRYPOINT ["/app/entrypoint.sh"]