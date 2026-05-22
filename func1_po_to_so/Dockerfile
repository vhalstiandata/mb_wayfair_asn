FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY func1_po_to_so/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY shared/             /app/shared/
COPY func1_po_to_so/     /app/func1_po_to_so/

EXPOSE 8080

CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "4", \
     "-t", "3600", "-b", "0.0.0.0:8080", \
     "--chdir", "/app", \
     "func1_po_to_so.main:app"]
