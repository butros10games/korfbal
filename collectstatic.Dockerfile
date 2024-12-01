FROM python:3.13-slim

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    locales \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen nl_NL.utf8 \
    && update-locale LANG=nl_NL.utf8 \
    && curl -fsSL https://deb.nodesource.com/setup_16.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g npx \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for the locale
ENV LANG nl_NL.utf8
ENV LANGUAGE nl_NL:en
ENV LC_ALL nl_NL.utf8

WORKDIR /kwt_uwsgi

# Install Python dependencies
COPY /requirements/daphne.txt /kwt_uwsgi/
RUN pip install --no-cache-dir -r daphne.txt

# Copy application files
COPY /apps/ /kwt_uwsgi/apps/
COPY /korfbal/ /kwt_uwsgi/korfbal/
COPY /templates/ /kwt_uwsgi/templates/
COPY /manage.py /kwt_uwsgi/
COPY /.env /kwt_uwsgi/.env

# Expose the Daphne port
EXPOSE 8001

# Run collectstatic command
CMD ["python", "manage.py", "collectstatic", "--noinput"]