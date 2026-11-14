"""Held test 1 of 2 for plan.md line 75 ("fail_saves never ships"): the image test.

Runs the shipped order image. Requires ORDER_IMAGE and STOCK_IMAGE (CI job `order-image`); it is
not in the default test paths because it needs Docker. Without them it fails, it never skips.

1. With FAIL_SAVES or ORDER_FAIL_SAVES set, the image refuses to start: the container exits with a
   non-zero code and its log says why.
2. With a clean environment, on a private Docker network with the real stock image (committed
   StockSnapshot) and the fake payment provider, the image accepts an order. Hand-worked check:
   First Hill 2% Milk is 96 on hand, 3 reserved -> available 93; one Grande Caffè Latte with
   2% Milk is accepted and leaves 95 on hand, 3 reserved -> available 92.
"""
import os
import subprocess
import time
import uuid

import httpx
import pytest

from tests.contract.harness import ROOT
from tests.contract.openapi_check import ORDER

ORDER_IMAGE = os.environ.get("ORDER_IMAGE")
STOCK_IMAGE = os.environ.get("STOCK_IMAGE")
FAULT_VARIABLES = ["FAIL_SAVES=order", "ORDER_FAIL_SAVES=attempt"]


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def wait_healthy(cid, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = docker("inspect", "-f", "{{.State.Status}} {{.State.Health.Status}}", cid).stdout.split()
        if state[-1] == "healthy":
            return
        if state[0] == "exited":
            break
        time.sleep(0.5)
    logs = docker("logs", cid, check=False)
    pytest.fail(f"container {cid[:12]} did not become healthy:\n{logs.stdout}{logs.stderr}", pytrace=False)


def published(cid):
    return "http://" + docker("port", cid, "8000/tcp").stdout.split()[0]


def test_shipped_order_image_refuses_fail_saves_and_accepts_an_order_when_clean():
    assert ORDER_IMAGE and STOCK_IMAGE, "set ORDER_IMAGE and STOCK_IMAGE (see the order-image CI job)"
    started = []
    net = f"order-image-test-{uuid.uuid4().hex[:8]}"
    try:
        # 1. Refuses to start with a fault variable set.
        for variable in FAULT_VARIABLES:
            cid = docker("run", "-d", "-e", variable, "-e", "STOCK_URL=http://stock:8000",
                         "-e", "PAYMENT_URL=http://payment:8000", ORDER_IMAGE).stdout.strip()
            started.append(cid)
            exit_code = docker("wait", cid).stdout.strip()
            logs = docker("logs", cid)
            assert exit_code != "0", f"{variable}: the image started"
            assert "refusing to start: test-only fault variables are set" in logs.stdout + logs.stderr
            assert variable.split("=")[0] in logs.stdout + logs.stderr

        # 2. Starts clean and accepts an order.
        docker("network", "create", net)
        stock = docker("run", "-d", "--network", net, "--network-alias", "stock",
                       "-p", "127.0.0.1::8000", STOCK_IMAGE).stdout.strip()
        payment = docker("run", "-d", "--network", net, "--network-alias", "payment",
                         "-v", f"{ROOT / 'tests' / 'fakes'}:/fakes:ro", ORDER_IMAGE,
                         "uvicorn", "payment:app", "--app-dir", "/fakes", "--host", "0.0.0.0", "--port", "8000"
                         ).stdout.strip()
        order = docker("run", "-d", "--network", net, "-p", "127.0.0.1::8000",
                       "-e", "STOCK_URL=http://stock:8000", "-e", "PAYMENT_URL=http://payment:8000",
                       ORDER_IMAGE).stdout.strip()
        started += [stock, payment, order]
        wait_healthy(stock)
        wait_healthy(order)

        with httpx.Client(base_url=published(order), timeout=10) as client:
            r = client.post("/v1/orders", json={
                "order_id": "MO-IMAGE-0001", "store_id": "SB-10482", "submitted_at": "2025-03-07T07:00:00-08:00",
                "drinks": [{"drink": "Caffè Latte", "size": "Grande", "milk": "2% Milk", "add_ins": [], "quantity": 1}]})
            body = ORDER.check(r)
            assert r.status_code == 201, r.text
            assert (body["order_id"], body["status"]) == ("MO-IMAGE-0001", "accepted")
            got = client.get("/v1/orders/MO-IMAGE-0001")
            assert ORDER.check(got)["status"] == "accepted"

        with httpx.Client(base_url=published(stock), timeout=10) as stock_client:
            levels = stock_client.post("/v1/stock-adjustments", json={
                "store_id": "SB-10482", "component_name": "2% Milk", "delta": 0}).json()
            assert (levels["quantity_on_hand"], levels["reserved_quantity"], levels["available"]) == (95, 3, 92)
    finally:
        for cid in started:
            docker("rm", "-f", cid, check=False)
        docker("network", "rm", net, check=False)
