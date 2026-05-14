FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY func2_if_to_asn/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/             /app/shared/
COPY func2_if_to_asn/    /app/func2_if_to_asn/

ENV PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD exec gunicorn \
    --bind :$PORT \
    --workers 1 \
    --threads 1 \
    --timeout 3600 \
    --chdir /app/func2_if_to_asn \
    main:app
