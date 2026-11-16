# Store stock service: one container (Constitution VII). Build from the repo root:
#   docker build -f services/stock/Dockerfile -t stock-service .
#   docker run --rm -p 8002:8000 stock-service
# The SQLite file is private to this container (/data). Each fresh container seeds it from the
# StockSnapshot copied in at build time; the snapshot itself is never written. No secrets.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STOCK_DB_PATH=/data/stock.db \
    STOCK_SNAPSHOT_PATH=/srv/StockSnapshot

WORKDIR /srv

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && useradd --system --uid 10001 --home-dir /srv stock \
 && mkdir -p /data && chown stock /data

COPY StockSnapshot /srv/StockSnapshot
COPY services/stock/app /srv/app

USER stock
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/v1/stores/SB-10482/availability', timeout=2); sys.exit(r.status != 200)"

CMD ["uvicorn", "app.main:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
