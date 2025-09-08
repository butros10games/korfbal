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

## ------------------------------- Production Stage ------------------------------ ##
FROM python:3.13-slim-bookworm AS production

WORKDIR /app

# Install the Expat runtime library
RUN apt-get update && apt-get install --no-install-recommends -y \
        libexpat1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    mkdir logs && touch logs/uwsgi.log && \
    groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser appuser && \
    chown -R appuser:appuser /app/logs

USER appuser

COPY --from=builder /app/.venv .venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application files
COPY ../configs/uwsgi/uwsgi.ini /app/
COPY ../manage.py /app/
COPY ../korfbal/ /app/korfbal/
COPY ../templates/ /app/templates/
COPY ../apps/ /app/apps/

# Expose the uwsgi port
EXPOSE 1664

ENV RUNNER="uwsgi"

# Run uwsgi server
CMD ["uwsgi", "--ini", "/app/uwsgi.ini"]
