# Korfbal (Django)

Web app to track korfbal matches, players, and stats in real time.

## Requirements

- Python 3.13 and uv
- PostgreSQL and Valkey/Redis (local or remote)

## Setup (Windows PowerShell)

- Install deps: uv sync
- Create your environment file based on the provided .env template and update POSTGRES*\*, REDIS*\_, MINIO\_\_ values
- Migrate: uv run python manage.py migrate
- Create admin: uv run python manage.py createsuperuser

## Run

- Dev: uv run python manage.py runserver 0.0.0.0:8000
- Docker (shared dev stack from repo root):
    1. docker network create monorepo_test-net (one-time)
    2. docker compose -f docker-compose.base.yaml -f docker-compose.kwt-dev.yaml --profile korfbal up --build
- ASGI/WSGI via Docker: see the docker/ folder for production-style images

## Features

- Match tracker with live updates
- Player and team management
- Simple dashboards and stats

## Tests and quality

- Tests: uv run pytest -q
- Lint: uv run ruff check .; uv run ruff check --fix .

## Notes

- Static assets are served via Django in dev; use collectstatic for prod.
- The dev compose stack mounts the local source folders for live reload.
