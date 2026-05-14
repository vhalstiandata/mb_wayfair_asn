FROM python:3.12-slim

# OS-level deps for pandas/bigquery
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (cached layer)
COPY func1_po_to_so/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared code + function code
COPY shared/             /app/shared/
COPY func1_po_to_so/     /app/func1_po_to_so/

ENV PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Cloud Run honors $PORT. --timeout matches Cloud Run max request duration (3600s).
CMD exec gunicorn \
    --bind :$PORT \
    --workers 1 \
    --threads 1 \
    --timeout 3600 \
    --chdir /app/func1_po_to_so \
    main:app
