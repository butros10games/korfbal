# Korfbal (Django)

Web app to track korfbal matches, players, and stats in real time.

## Requirements

- Python 3.13 and uv
- PostgreSQL and Redis (local or remote)

## Setup (Windows PowerShell)

- Install deps: uv sync
- Env vars: DJANGO_SECRET_KEY, DEBUG, DATABASE_URL, REDIS_URL
- Migrate: uv run python manage.py migrate
- Create admin: uv run python manage.py createsuperuser

## Run

- Dev: uv run python manage.py runserver 0.0.0.0:8000
- ASGI/WSGI via Docker: see app docker files and infrastructure/ host compose

## Features

- Match tracker with live updates
- Player and team management
- Simple dashboards and stats

## Tests and quality

- Tests: uv run pytest -q
- Lint: uv run ruff check .; uv run ruff check --fix .

## Notes

- Static assets are served via Django in dev; use collectstatic for prod.
