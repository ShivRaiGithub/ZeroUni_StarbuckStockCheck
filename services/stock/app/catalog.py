"""Menu catalog and recipes: what a line item needs (research.md R4).

v1 recipe: 1 unit of the chosen milk per drink (none for "None") and 1 unit of each add-in per
drink, for every size. A line needs quantity x units.
"""
from __future__ import annotations

DRINKS = frozenset({
    "Pike Place Roast", "Caffè Latte", "Iced Caramel Macchiato",
    "Iced Brown Sugar Oatmilk Shaken Espresso", "Chai Tea Latte", "Cold Brew", "Flat White",
    "Matcha Tea Latte", "Caramel Frappuccino", "Vanilla Sweet Cream Cold Brew",
})
SIZES = frozenset({"Tall", "Grande", "Venti"})
NO_MILK = "None"
MILKS = frozenset({"2% Milk", "Whole Milk", "Nonfat Milk", "Almondmilk", "Oatmilk", "Soymilk", "Coconutmilk"})
ADD_INS = frozenset({
    "Vanilla Syrup", "Caramel Drizzle", "Brown Sugar Syrup", "Extra Shot",
    "Vanilla Sweet Cream Cold Foam", "Cinnamon Dolce Topping",
})
UNITS_PER_DRINK = 1

FIELD_LABEL = {"drink": "drink", "size": "size", "milk": "milk", "add_ins": "add-in"}


def catalog_type(component_name: str) -> str | None:
    if component_name in MILKS:
        return "drink_base"
    if component_name in ADD_INS:
        return "add_in"
    return None


def unrecognised(lines, extra_add_ins: frozenset[str] = frozenset()) -> list[dict]:
    """Every unknown drink, size, milk or add-in, in line order. The whole order is rejected."""
    problems = []
    for ln in sorted(lines, key=lambda x: x.line_item):
        if ln.drink not in DRINKS:
            problems.append({"reason": "unrecognised", "field": "drink", "value": ln.drink, "line_item": ln.line_item})
        if ln.size not in SIZES:
            problems.append({"reason": "unrecognised", "field": "size", "value": ln.size, "line_item": ln.line_item})
        if ln.milk != NO_MILK and ln.milk not in MILKS:
            problems.append({"reason": "unrecognised", "field": "milk", "value": ln.milk, "line_item": ln.line_item})
        for add_in in ln.add_ins:
            if add_in not in ADD_INS and add_in not in extra_add_ins:
                problems.append({"reason": "unrecognised", "field": "add_ins", "value": add_in,
                                 "line_item": ln.line_item})
    return problems


def unrecognised_message(problems: list[dict]) -> str:
    return " ".join(f"Unrecognised {FIELD_LABEL[p['field']]}: {p['value']}." for p in problems)


def needs(lines) -> dict[str, dict]:
    """component -> {need, line_item, sort_key}, summed over all lines.

    sort_key is the fixed order used for items and shortfalls: by line item, then the milk
    before add-ins, then add-ins in the order the customer listed them (first use wins).
    """
    out: dict[str, dict] = {}
    for ln in sorted(lines, key=lambda x: x.line_item):
        used = ([(ln.milk, (ln.line_item, 0, 0))] if ln.milk != NO_MILK else []) + [
            (a, (ln.line_item, 1, i)) for i, a in enumerate(ln.add_ins)]
        for comp, key in used:
            entry = out.setdefault(comp, {"need": 0, "line_item": ln.line_item, "sort_key": key})
            entry["need"] += ln.quantity * UNITS_PER_DRINK
    return dict(sorted(out.items(), key=lambda kv: kv[1]["sort_key"]))
