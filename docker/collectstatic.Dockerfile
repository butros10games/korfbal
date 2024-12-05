FROM python:3.13-slim

# Install system dependencies for PostgreSQL, psycopg2-binary, and Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    wget \
    && curl -fsSL https://deb.nodesource.com/setup_18.x -o nodesource_setup.sh \
    && bash nodesource_setup.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* nodesource_setup.sh

WORKDIR /app

# Install required Python packages
COPY ../requirements/uwsgi.txt /app/
RUN pip install --no-cache-dir -r uwsgi.txt

# Add the project files
COPY ../apps/ /app/apps/
COPY ../korfbal/ /app/korfbal/
COPY ../static_workfile/ /app/static_workfile/
COPY ../manage.py /app/
COPY ../package.json /app/
COPY ../configs/webpack/webpack.config.js /app/

# Install Node.js packages
RUN npm install --ignore-scripts

# Install MinIO client (mc)
ADD https://dl.min.io/client/mc/release/linux-amd64/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc \
    && groupadd -r appuser && useradd -r -g appuser appuser \
    && chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

# Entrypoint script
COPY ../configs/collectstatic/entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]