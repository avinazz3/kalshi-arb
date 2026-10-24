"""Exact dollar/contract arithmetic. No float conversion or implicit cent units."""
from decimal import Decimal, InvalidOperation

D = Decimal
ONE = D("1")


def decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Use exact strings, integers or Decimal, not float/bool")
    try:
        number = D(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("Non-finite value")
    return number


def bids(payload, side):
    if side not in ("yes", "no"):
        raise ValueError("Side must be yes or no")
    if not isinstance(payload, dict):
        raise ValueError("Expected an orderbook JSON object")
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict) or f"{side}_dollars" not in book:
        raise ValueError("Expected orderbook_fp with explicit dollar-string ladders")
    levels = book[f"{side}_dollars"]
    if levels is None:
        return []
    if not isinstance(levels, list):
        raise ValueError("Expected a price-level list")
    merged = {}
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ValueError("Expected [price_dollars, count_fp]")
        price, size = map(decimal, level)
        if not D("0") <= price <= ONE or size < 0:
            raise ValueError("Price outside [0,1] or negative quantity")
        if size:
            merged[price] = merged.get(price, D("0")) + size
    return sorted(merged.items(), reverse=True)


def asks(payload, side):
    """Buying YES consumes NO bids, and buying NO consumes YES bids."""
    if side not in ("yes", "no"):
        raise ValueError("Side must be yes or no")
    opposite = "no" if side == "yes" else "yes"
    return sorted((ONE - price, size) for price, size in bids(payload, opposite))


def buy_cost(payload, side, quantity):
    """Full-depth quoted cost; reject requests exceeding visible liquidity."""
    remaining = decimal(quantity)
    if remaining <= 0:
        raise ValueError("Quantity must be positive")
    cost, fills = D("0"), []
    for price, available in asks(payload, side):
        take = min(remaining, available)
        cost += price * take
        fills.append({"price_dollars": price, "quantity": take})
        remaining -= take
        if remaining == 0:
            return {"cost_dollars": cost, "levels_consumed": fills}
    raise ValueError(f"Insufficient depth: {remaining} contracts unfilled")


def threshold_pair_quote(lower_book, higher_book, quantity, total_fee_reserve=None):
    """Conditional on YES_higher => YES_lower and complete execution of both legs.

    No rules are inferred here. This function is an accounting primitive, not an
    opportunity validator or an order sender. Fee reserve is supplied, not estimated.
    """
    q = decimal(quantity)
    lower = buy_cost(lower_book, "yes", q)
    higher = buy_cost(higher_book, "no", q)
    cost = lower["cost_dollars"] + higher["cost_dollars"]
    # Valid (YES_lower, YES_higher) states: (0,0), (1,0), (1,1).
    payouts = [q, 2 * q, q]
    gross = min(payouts) - cost
    reserve = None if total_fee_reserve is None else decimal(total_fee_reserve)
    if reserve is not None and reserve < 0:
        raise ValueError("Fee reserve cannot be negative")
    return {
        "quantity_each_leg": q,
        "quoted_cost_dollars": cost,
        "minimum_normal_binary_settlement_dollars": min(payouts),
        "gross_surplus_dollars": gross,
        "assumed_total_fee_reserve_dollars": reserve,
        "surplus_after_assumed_reserve_dollars": None if reserve is None else gross - reserve,
        "lower_yes": lower,
        "higher_no": higher,
        "classification": "conditional_snapshot_calculation_not_executable_arbitrage",
    }
