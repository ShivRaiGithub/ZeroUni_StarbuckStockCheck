"""In-process wiring for the three services. No sockets, no network.

How each service must be buildable (this is what the implementation has to provide):

  services/stock/app/main.py
      create_app(*, db_path: str, seed_rows: list[dict]) -> FastAPI
      seed_rows use the StockSnapshot CSV columns (all values strings, as csv.DictReader
      gives them): store_id, store_name, store_open, component_id, component_name,
      component_type, quantity_on_hand, reserved_quantity, last_updated_at.
      Public store IDs are "SB-" + store_id. The app must be ready to serve without its
      lifespan having run (it is also called through httpx.ASGITransport, which skips lifespan).

  services/order/app/main.py
      create_app(*, db_path: str, stock_client: httpx.AsyncClient,
                 payment_client: httpx.AsyncClient) -> FastAPI
      Calls the stock service at relative paths (/v1/holds, ...) through stock_client and the
      payment provider through payment_client (see tests/fakes/payment.py for that interface).

  services/gateway/app/main.py
      create_app(*, order_client: httpx.AsyncClient) -> FastAPI

Each app/ directory is a package (has __init__.py) and uses relative imports internally.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = ROOT / "services"


def load_service(name: str):
    """Import services/<name>/app/main.py under a unique package name; fail the test if absent."""
    pkg_dir = SERVICES_DIR / name / "app"
    main_py = pkg_dir / "main.py"
    init_py = pkg_dir / "__init__.py"
    if not main_py.is_file() or not init_py.is_file():
        pytest.fail(
            f"{name} service is not implemented yet: expected package "
            f"{pkg_dir.relative_to(ROOT)}/ with __init__.py and main.py defining create_app(...)",
            pytrace=False,
        )
    pkg_name = f"_svc_{name}"
    if pkg_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg_name, init_py, submodule_search_locations=[str(pkg_dir)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = module
        spec.loader.exec_module(module)
    main = importlib.import_module(f"{pkg_name}.main")
    if not callable(getattr(main, "create_app", None)):
        pytest.fail(f"{main_py.relative_to(ROOT)} must define create_app(...)", pytrace=False)
    return main


def asgi_client(app, base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def mock_client(handler, base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


# ---------------------------------------------------------------- stock fixtures (hand-built)

COMPONENTS = {
    # name: (component_id, component_type)
    "2% Milk": ("two_pct_milk", "drink_base"),
    "Whole Milk": ("whole_milk", "drink_base"),
    "Almondmilk": ("almond_milk", "drink_base"),
    "Oatmilk": ("oat_milk", "drink_base"),
    "Vanilla Syrup": ("vanilla_syrup", "add_in"),
    "Caramel Drizzle": ("caramel_drizzle", "add_in"),
    "Brown Sugar Syrup": ("brown_sugar_syrup", "add_in"),
    "Extra Shot": ("extra_shot", "add_in"),
    "Vanilla Sweet Cream Cold Foam": ("vanilla_sweet_cream_cold_foam", "add_in"),
}

STORES = {
    # snapshot store_id: (store_name, store_open)
    "10482": ("First Hill - Madison & Boylston", True),
    "11937": ("University Village", True),
    "20516": ("Bellevue Square", False),
    "09815": ("South Lake Union - Westlake & Mercer", False),
}

DEFAULT_STOCK = (50, 0)  # (quantity_on_hand, reserved_quantity) -> available 50


def seed(stock: dict[str, dict[str, tuple[int, int]]] | None = None,
         open_: dict[str, bool] | None = None) -> list[dict]:
    """Every store x component at DEFAULT_STOCK, with per-store overrides.

    seed({"10482": {"Oatmilk": (1, 0)}}) -> First Hill Oatmilk on hand 1, reserved 0, available 1.
    """
    stock = stock or {}
    open_ = open_ or {}
    rows = []
    for store_id, (store_name, is_open) in STORES.items():
        is_open = open_.get(store_id, is_open)
        for comp_name, (comp_id, comp_type) in COMPONENTS.items():
            on_hand, reserved = stock.get(store_id, {}).get(comp_name, DEFAULT_STOCK)
            rows.append({
                "store_id": store_id,
                "store_name": store_name,
                "store_open": "true" if is_open else "false",
                "component_id": comp_id,
                "component_name": comp_name,
                "component_type": comp_type,
                "quantity_on_hand": str(on_hand),
                "reserved_quantity": str(reserved),
                "last_updated_at": "2025-03-07T06:40:00-08:00",
            })
    return rows


# ---------------------------------------------------------------- request builders

def line(drink: str, size: str, milk: str, add_ins: list[str] | None = None, quantity: int = 1,
         line_item: int | None = None) -> dict:
    d = {"drink": drink, "size": size, "milk": milk, "add_ins": add_ins or [], "quantity": quantity}
    if line_item is not None:
        d["line_item"] = line_item
    return d


def hold_body(store_id: str, *lines: dict, ttl_seconds: float = 120) -> dict:
    numbered = [{**ln, "line_item": ln.get("line_item", i)} for i, ln in enumerate(lines, start=1)]
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    return {"store_id": store_id, "lines": numbered, "expires_at": expires.isoformat()}


def order_body(order_id: str, store_id: str, *drinks: dict) -> dict:
    return {
        "order_id": order_id,
        "store_id": store_id,
        "submitted_at": "2025-03-07T07:00:00-08:00",
        "drinks": list(drinks),
    }


# ---------------------------------------------------------------- environments

@contextmanager
def stock_service(tmp_path: Path, rows: list[dict]):
    app = load_service("stock").create_app(db_path=str(tmp_path / "stock.db"), seed_rows=rows)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def stock_levels(stock_client: TestClient, store_id: str, component: str) -> dict:
    """Read on_hand/reserved/available for one row via a zero adjustment (owner API)."""
    r = stock_client.post("/v1/stock-adjustments",
                          json={"store_id": store_id, "component_name": component, "delta": 0})
    assert r.status_code == 200, r.text
    return r.json()


class OrderEnv:
    def __init__(self, order: TestClient, stock: TestClient | None, payment):
        self.order = order
        self.stock = stock
        self.payment = payment


@contextmanager
def order_service(tmp_path: Path, rows: list[dict] | None = None, *, payment=None,
                  stock_client: httpx.AsyncClient | None = None,
                  payment_client: httpx.AsyncClient | None = None):
    """Order service wired to the real stock service (or a stub) and the fake payment app."""
    from tests.fakes.payment import FakePayment

    stock_tc = None
    if stock_client is None:
        stock_app = load_service("stock").create_app(
            db_path=str(tmp_path / "stock.db"), seed_rows=rows if rows is not None else seed())
        stock_client = asgi_client(stock_app, "http://stock")
        stock_tc = TestClient(stock_app, raise_server_exceptions=False)
    payment = payment or FakePayment()
    if payment_client is None:
        payment_client = asgi_client(payment.app, "http://payment")
    order_app = load_service("order").create_app(
        db_path=str(tmp_path / "order.db"), stock_client=stock_client, payment_client=payment_client)
    with TestClient(order_app, raise_server_exceptions=False) as client:
        yield OrderEnv(client, stock_tc, payment)


@contextmanager
def gateway_service(order_client: httpx.AsyncClient):
    app = load_service("gateway").create_app(order_client=order_client)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
