FROM python:3.13-slim

# Install system dependencies for PostgreSQL and psycopg2-binary
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    libpq-dev && \
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install required packages
COPY ../requirements/uwsgi.txt /app/
RUN pip install --no-cache-dir -r uwsgi.txt

# Add the project files
COPY ../apps/ /app/apps/
COPY ../korfbal/ /app/korfbal/
COPY ../static_workfile/ /app/static_workfile/
COPY ../manage.py /app/
COPY ../package.json /app/
COPY ../webpack.config.js /app/

RUN npm install

# Install MinIO client (mc)
RUN apt-get update && apt-get install -y wget && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
ADD https://dl.min.io/client/mc/release/linux-amd64/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

# Entrypoint script
COPY ../collectstatic_entrypoint.sh /app/
RUN chmod +x /app/collectstatic_entrypoint.sh

ENTRYPOINT ["/app/collectstatic_entrypoint.sh"]
