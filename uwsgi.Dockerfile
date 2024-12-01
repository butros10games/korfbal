# Dockerfile-uwsgi
FROM python:3.13-slim

# Install system dependencies for PostgreSQL and psycopg2-binary
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /kwt_uwsgi

# Copy requirements file and install dependencies
COPY /requirements/uwsgi.txt /kwt_uwsgi/
RUN pip install --no-cache-dir -r uwsgi.txt

# Copy application files
COPY /apps/ /kwt_uwsgi/apps/
COPY /korfbal/ /kwt_uwsgi/korfbal/
COPY /templates/ /kwt_uwsgi/templates/
COPY /manage.py /kwt_uwsgi/
COPY /.env /kwt_uwsgi/.env

# Create a directory for logs
RUN mkdir -p /kwt_uwsgi/logs

# Expose the uwsgi port
EXPOSE 1664

CMD ["sh", "-c", "set -a && . /kwt_uwsgi/.env && exec uwsgi"]
