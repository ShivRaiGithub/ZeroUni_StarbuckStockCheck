"""All customer-facing copy in one place (Product owns the wording; CLAUDE.md pins rules 4-8)."""
from __future__ import annotations

FIELD_LABEL = {"drink": "drink", "size": "size", "milk": "milk", "add_ins": "add-in"}

STORAGE_UNAVAILABLE = "We couldn't take your order just now. Please try again shortly."
STOCK_UNAVAILABLE = "We couldn't check stock just now. Please try again shortly."
PAYMENT_UNKNOWN = "We're still confirming your payment. Please try again shortly."
PAYMENT_DECLINED = "Your payment was declined."
INTERNAL = "Something went wrong with this order. Please try again."


def store_closed(store_name: str) -> str:  # CLAUDE.md rule 4
    return f"{store_name} is closed right now and can't take mobile orders."


def component_unavailable(line_item: int, size: str, drink: str, component: str) -> str:  # rules 5-8
    return f"Line {line_item}, {size} {drink}: {component} is unavailable at this store right now."


def unknown_store(store_id: str) -> str:
    return f"Store {store_id} does not exist."


def unrecognised(problems: list[dict]) -> str:
    return " ".join(f"Unrecognised {FIELD_LABEL.get(p['field'], p['field'])}: {p['value']}." for p in problems)


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# Refunds after a charge: never "unavailable" (ServiceBoundary, Mobile App Checkout).
def refund_ran_out(components: list[str]) -> str:
    return (f"We've refunded your payment in full. {join_names(components)} ran out before we could make "
            "your order. Please try again with a different option.")


def refund_store_closed(store_name: str) -> str:
    return f"We've refunded your payment in full. {store_name} closed before we could make your order."


def order_not_found(order_id: str) -> str:
    return f"Order {order_id} was not found."


def order_id_conflict(order_id: str) -> str:
    return f"Order {order_id} was already submitted with different contents."
