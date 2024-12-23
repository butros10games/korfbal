FROM python:3.13-slim

# Install dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    locales \
    && echo "nl_NL.UTF-8 UTF-8" >> /etc/locale.gen \
    && locale-gen \
    && /usr/sbin/update-locale LANG=nl_NL.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables for the locale
ENV LANG=nl_NL.utf8
ENV LANGUAGE=nl_NL:en
ENV LC_ALL=nl_NL.utf8

# Create a non-root user and group
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /kwt_daphne

# Copy requirements file and install dependencies
COPY ../requirements/daphne.txt /kwt_daphne/
RUN pip install --no-cache-dir -r daphne.txt

# Copy application files
COPY ../manage.py /kwt_daphne/
COPY ../korfbal/ /kwt_daphne/korfbal/
COPY ../apps/ /kwt_daphne/apps/

# Change ownership of the application files
RUN chown -R appuser:appuser /kwt_daphne

# Switch to the non-root user
USER appuser

# Expose the Daphne port
EXPOSE 8001

# Run Daphne server
CMD ["daphne", "-p", "8001", "-b", "0.0.0.0", "korfbal.asgi:application"]