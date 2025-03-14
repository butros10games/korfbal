# Dockerfile-uwsgi
FROM python:3.13-slim

# Install system dependencies for PostgreSQL and psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libexpat1 \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /kwt_uwsgi

# Copy requirements file and install dependencies
COPY ../requirements/uwsgi.txt /kwt_uwsgi/
RUN pip install --no-cache-dir -r uwsgi.txt

# Copy application files
COPY ../configs/uwsgi/uwsgi.ini /kwt_uwsgi/
COPY ../manage.py /kwt_uwsgi/
COPY ../korfbal/ /kwt_uwsgi/korfbal/
COPY ../apps/ /kwt_uwsgi/apps/
COPY ../templates/ /kwt_uwsgi/templates/

# Create a directory for logs and change ownership
RUN mkdir -p /kwt_uwsgi/logs && chown -R appuser:appuser /kwt_uwsgi

# Switch to the non-root user
USER appuser

# Expose the uwsgi port
EXPOSE 1664

ENV RUNNER="uwsgi"

# Run uwsgi server
CMD ["uwsgi", "--ini", "/kwt_uwsgi/uwsgi.ini"]