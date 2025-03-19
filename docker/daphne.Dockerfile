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

RUN uv sync --group daphne

## ------------------------------- Production Stage ------------------------------ ## 
FROM python:3.13-slim AS production

RUN useradd --create-home appuser
USER appuser

# Set locale variables for runtime
ENV LANG=nl_NL.utf8
ENV LANGUAGE=nl_NL:en
ENV LC_ALL=nl_NL.utf8
WORKDIR /app

COPY --from=builder /app/.venv .venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application files
COPY ../manage.py /app/
COPY ../korfbal/ /app/korfbal/
COPY ../apps/ /app/apps/

EXPOSE 8001

CMD ["daphne", "-p", "8001", "-b", "0.0.0.0", "korfbal.asgi:application"]
