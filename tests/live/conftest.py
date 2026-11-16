"""Tests against the running compose stack (not in-process), started with:

    STOCK_SEED=./tests/live/SampleStock.csv docker compose --profile payment up -d --build --wait
    pytest -v -rA tests/live

The payment profile is needed because an order is only accepted after the fake provider charges it.
STOCK_SEED seeds every store the sample orders use (SB-10482, SB-11937, closed SB-20516) with every
menu component; the default StoreStock has neither SB-11937 nor SB-20516.
They are not in pytest.ini's testpaths: the default run (the contract-tests check) has no stack, and
a skip there counts as a failure. If the stack is not reachable these tests fail, they never skip.
"""
from __future__ import annotations

import os

import httpx
import pytest

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8088")
STOCK_URL = os.environ.get("STOCK_URL", "http://127.0.0.1:8002")  # for reading/setting stock only


@pytest.fixture(scope="session")
def gateway():
    with httpx.Client(base_url=GATEWAY_URL, timeout=10) as c:
        try:
            c.get("/v1/orders")
        except httpx.HTTPError as e:
            pytest.fail(f"gateway not reachable at {GATEWAY_URL} ({e}); "
                        "run: docker compose --profile payment up -d --build --wait")
        yield c


@pytest.fixture(scope="session")
def stock():
    with httpx.Client(base_url=STOCK_URL, timeout=10) as c:
        try:
            c.get("/v1/stores/SB-10482/availability")
        except httpx.HTTPError as e:
            pytest.fail(f"stock service not reachable at {STOCK_URL} ({e})")
        yield c


def available(stock: httpx.Client, store_id: str, component: str) -> int:
    r = stock.get(f"/v1/stores/{store_id}/availability")
    assert r.status_code == 200, r.text
    return next(c["available"] for c in r.json()["components"] if c["component_name"] == component)
