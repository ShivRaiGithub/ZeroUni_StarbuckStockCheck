# Mobile order gateway: one container (Constitution VII). Build from the repo root:
#   docker build -f services/gateway/Dockerfile -t gateway .
#   docker run --rm -p 8080:8000 -e ORDER_URL=http://order:8000 gateway
# Stateless: no database, no stock data. Its only downstream is the order service at ORDER_URL,
# over HTTP. No secrets.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && useradd --system --uid 10003 --home-dir /srv gateway

COPY services/gateway/app /srv/app

USER gateway
EXPOSE 8000

# The contract has no health path, so the check is "the gateway's own port accepts connections";
# it does not depend on the order service being up.
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=2).close()"

CMD ["uvicorn", "app.main:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
