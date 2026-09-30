# Build directly on the production host from its current worker image.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
# Numerical tools are confined to this batch image, with fixed versions matching
# the audited offline environment. Existing worker and web images are untouched.
COPY apps/django_projects/korfbal/deps/forecast-requirements.txt /tmp/forecast-requirements.txt
RUN /usr/local/bin/python -m pip install --no-cache-dir --no-deps --require-hashes --target /opt/forecast-deps -r /tmp/forecast-requirements.txt
# The launcher builds a minimal context containing only the explicit forecast files.
COPY apps/django_projects/korfbal/apps/competition/ /app/apps/competition/
ENV PYTHONPATH=/opt/forecast-deps:/app OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONUNBUFFERED=1
USER appuser
ENTRYPOINT ["timeout", "--signal=TERM", "--kill-after=30s", "30m", "python", "manage.py", "refresh_score_forecasts"]
