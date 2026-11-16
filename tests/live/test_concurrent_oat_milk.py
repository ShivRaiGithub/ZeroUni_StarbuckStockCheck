"""Two oat milk latte orders sent at the same moment, through the live gateway, for the last unit
of Oatmilk at First Hill (SB-10482). Exactly one is accepted; the other is refused naming
Oatmilk (CLAUDE.md rules 5 and 7: stock is re-checked at commit, never accepted from a stale check)
and stock never goes negative (rule 8).

Oatmilk is first set to exactly 1 available through the stock service, so the test can be rerun
against the same stack. Order IDs are unique per run, so neither order is an idempotent replay.
"""
import json
import threading
import uuid

from tests.contract.openapi_check import GATEWAY
from tests.live.conftest import available

STORE = "SB-10482"
OAT = "Oatmilk"
REFUSAL = "Line 1, Grande Caffè Latte: Oatmilk is unavailable at this store right now."


def oat_latte(order_id: str) -> dict:
    return {"order_id": order_id, "store_id": STORE,
            "drinks": [{"drink": "Caffè Latte", "size": "Grande", "milk": OAT, "add_ins": [],
                        "quantity": 1, "total_price": 6.79}]}


def test_two_oat_milk_lattes_at_once_one_accepted_one_refused(gateway, stock):
    # Exactly one Oatmilk left.
    delta = 1 - available(stock, STORE, OAT)
    if delta:
        r = stock.post("/v1/stock-adjustments", json={"store_id": STORE, "component_name": OAT, "delta": delta})
        assert r.status_code == 200, r.text
    assert available(stock, STORE, OAT) == 1

    run = uuid.uuid4().hex[:8]
    ids = [f"MO-RACE-{run}-A", f"MO-RACE-{run}-B"]
    replies: dict[str, object] = {}
    start = threading.Barrier(len(ids))

    def send(order_id):
        start.wait()  # release both requests together
        replies[order_id] = gateway.post("/v1/orders", json=oat_latte(order_id))

    threads = [threading.Thread(target=send, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert set(replies) == set(ids), "both orders must get a reply"
    for order_id in ids:
        r = replies[order_id]
        print(f"\n{order_id} -> HTTP {r.status_code}\n{json.dumps(r.json(), indent=2, ensure_ascii=False)}")

    bodies = {i: GATEWAY.check(replies[i]) for i in ids}  # both replies match the gateway contract
    by_status = {replies[i].status_code: i for i in ids}
    assert sorted(by_status) == [201, 409], f"want one 201 and one 409, got {[replies[i].status_code for i in ids]}"

    accepted = bodies[by_status[201]]
    assert (accepted["order_id"], accepted["status"]) == (by_status[201], "accepted")

    refused = bodies[by_status[409]]["error"]
    assert refused["code"] == "component_unavailable"
    assert refused["message"] == REFUSAL
    assert [(d["line_item"], d["missing_component"]) for d in refused["details"]] == [(1, OAT)]

    assert available(stock, STORE, OAT) == 0, "the accepted order took the last Oatmilk; never negative"
