FROM python:3.13-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    wget \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Download and install Node.js
ADD https://deb.nodesource.com/setup_18.x /tmp/nodesource_setup.sh
RUN bash /tmp/nodesource_setup.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/nodesource_setup.sh

ENV NO_UPDATE_NOTIFIER=1
ENV NPM_CONFIG_UPDATE_NOTIFIER=false

WORKDIR /app

# Install Python packages
COPY ../requirements/uwsgi.txt /app/
RUN pip install --no-cache-dir -r uwsgi.txt

# Copy project files
COPY ../manage.py /app/
COPY ../package.json /app/
COPY ../configs/webpack/webpack.config.js /app/

# Install Node.js packages
RUN npm install --ignore-scripts

# Install MinIO client (mc)
ADD https://dl.min.io/client/mc/release/linux-amd64/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

# Copy entrypoint script
COPY ../configs/collectstatic/entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh \
    && groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser appuser \
    && chown -R appuser:appuser /home/appuser /app

# Ensure HOME is set so mc knows where to store config
ENV HOME=/home/appuser

COPY ../korfbal/ /app/korfbal/
COPY ../apps/ /app/apps/
COPY ../static_workfile/ /app/static_workfile/

# Switch to the non-root user
USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]