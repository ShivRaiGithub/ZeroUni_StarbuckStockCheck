# Order service: one container (Constitution VII). Build from the repo root:
#   docker build -f services/order/Dockerfile -t order-service .
#   docker run --rm -e STOCK_URL=http://stock:8000 -e PAYMENT_URL=http://payment:8000 order-service
# Its SQLite file is private to this container (/data). It never reads stock data; it calls the
# stock service at STOCK_URL. The test-only fail_saves hook is never reachable: the entry point
# always passes None and refuses to start if a FAIL_SAVES / ORDER_FAIL* variable is set. No secrets.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORDER_DB_PATH=/data/order.db

WORKDIR /srv

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && useradd --system --uid 10002 --home-dir /srv order \
 && mkdir -p /data && chown order /data

COPY services/order/app /srv/app

USER order
EXPOSE 8000

# Any HTTP answer means the service is up (an unknown order is a 404 by contract).
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,urllib.error,sys;\
exec('try:\n urllib.request.urlopen(\"http://127.0.0.1:8000/v1/orders/healthcheck\", timeout=2)\nexcept urllib.error.HTTPError as e:\n sys.exit(e.code != 404)')"

CMD ["uvicorn", "app.main:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
