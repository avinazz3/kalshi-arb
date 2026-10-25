"""Documented quadratic fee arithmetic for explicitly configured $1 binaries.

This module does not discover effective exchange policies or certify profitability.
Create one accumulator per order and feed actual fills in chronological order.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .numbers import decimal

D = Decimal
MICRO = D("0.000001")
ZERO = D("0")


@dataclass(frozen=True)
class FeePolicy:
    """Multipliers for each role must be supplied, including an explicit zero.

    `model` names our calculation family, not the exchange's raw fee_type field.
    Resolve and verify series/event policy externally before constructing this.
    """
    model: str
    taker_multiplier: Decimal
    maker_multiplier: Decimal

    def __post_init__(self):
        if self.model != "quadratic":
            raise ValueError(f"Unsupported fee model: {self.model!r}")
        for name in ("taker_multiplier", "maker_multiplier"):
            value = decimal(getattr(self, name))
            if value < 0:
                raise ValueError("Fee multipliers cannot be negative")
            object.__setattr__(self, name, value)

    def model_fee(self, fill):
        coefficient = D("0.07") if fill.liquidity == "taker" else D("0.0175")
        multiplier = getattr(self, fill.liquidity + "_multiplier")
        p = fill.price_dollars
        return multiplier * coefficient * fill.quantity * p * (1 - p)


@dataclass(frozen=True)
class Fill:
    price_dollars: Decimal
    quantity: Decimal
    liquidity: str
    direction: str

    def __post_init__(self):
        p, q = decimal(self.price_dollars), decimal(self.quantity)
        if not ZERO <= p <= 1 or p != p.quantize(D("0.0001")):
            raise ValueError("Price must be in [0,1] with at most four decimal places")
        if q <= 0 or q != q.quantize(D("0.01")):
            raise ValueError("Quantity must be positive with at most two decimal places")
        if self.liquidity not in ("maker", "taker"):
            raise ValueError("Liquidity must be maker or taker")
        if self.direction not in ("buy", "sell"):
            raise ValueError("Direction must be buy or sell")
        object.__setattr__(self, "price_dollars", p)
        object.__setattr__(self, "quantity", q)

    @property
    def revenue(self):
        amount = self.price_dollars * self.quantity
        return -amount if self.direction == "buy" else amount


class OrderFeeAccumulator:
    """One order's carry, under the documented balance-alignment mechanics."""
    def __init__(self, balance_precision):
        precision = decimal(balance_precision)
        if precision not in (D("0.0001"), D("0.01")):
            raise ValueError("Balance precision must be 0.0001 or 0.01 dollars")
        # Decimal.quantize uses the exponent, so canonicalise equivalent strings.
        self.precision = D("0.01") if precision == D("0.01") else D("0.0001")
        self.rounding_carry = ZERO

    def apply(self, revenue, model_fee):
        revenue, model_fee = decimal(revenue), decimal(model_fee)
        if revenue != revenue.quantize(MICRO) or model_fee < 0:
            raise ValueError("Revenue must have <=6 decimal places and model fee must be nonnegative")
        trade_fee = model_fee.quantize(MICRO, rounding=ROUND_CEILING)
        aligned_change = (revenue - trade_fee).quantize(self.precision, rounding=ROUND_FLOOR)
        rounding_fee = revenue - trade_fee - aligned_change
        carry = self.rounding_carry + rounding_fee
        available_rebate = carry.quantize(self.precision, rounding=ROUND_FLOOR)
        # Rebates use whole precision increments and cannot make this fill's fee negative.
        rebate_cap = (trade_fee + rounding_fee).quantize(self.precision, rounding=ROUND_FLOOR)
        rebate = min(available_rebate, rebate_cap)
        net_fee = trade_fee + rounding_fee - rebate
        self.rounding_carry = carry - rebate
        return {
            "revenue_dollars": revenue, "model_fee_dollars": model_fee,
            "trade_fee_dollars": trade_fee, "rounding_fee_dollars": rounding_fee,
            "rebate_dollars": rebate, "net_fee_dollars": net_fee,
            "cash_change_dollars": aligned_change + rebate,
            "rounding_carry_dollars": self.rounding_carry,
        }


def calculate_order(fills, policy, balance_precision):
    """Model one complete or partial order; never merge unrelated orders here."""
    accumulator = OrderFeeAccumulator(balance_precision)
    records = []
    for fill in fills:
        if not isinstance(fill, Fill):
            raise ValueError("Every order fill must be a Fill instance")
        if records and fill.direction != records[0]["fill"]["direction"]:
            raise ValueError("One order cannot contain both buy and sell fills")
        result = accumulator.apply(fill.revenue, policy.model_fee(fill))
        records.append({"fill": asdict(fill), **result})
    if not records:
        raise ValueError("An order must contain at least one fill")
    return {
        "fills": records,
        "net_fee_dollars": sum((r["net_fee_dollars"] for r in records), ZERO),
        "revenue_dollars": sum((r["revenue_dollars"] for r in records), ZERO),
        "cash_change_dollars": sum((r["cash_change_dollars"] for r in records), ZERO),
        "rounding_carry_dollars": accumulator.rounding_carry,
    }


def calculate_orders(document, balance_precision):
    """Calculate an explicit policy and ordered fill lists from the CLI JSON shape."""
    if not isinstance(document, dict) or set(document) != {"policy", "orders"}:
        raise ValueError("Input must contain exactly policy and orders")
    if not isinstance(document["policy"], dict):
        raise ValueError("policy must be an object")
    policy = FeePolicy(**document["policy"])
    orders = document["orders"]
    if not isinstance(orders, list) or not orders:
        raise ValueError("orders must be a nonempty list")
    seen, results = set(), []
    for order in orders:
        if not isinstance(order, dict) or set(order) != {"order_id", "fills"}:
            raise ValueError("Each order must contain exactly order_id and fills")
        order_id = order["order_id"]
        if not isinstance(order_id, str) or not order_id or order_id in seen:
            raise ValueError("Order IDs must be nonempty and unique; combine fills of one order")
        if not isinstance(order["fills"], list):
            raise ValueError("fills must be a list")
        seen.add(order_id)
        fills = []
        for entry in order["fills"]:
            if not isinstance(entry, dict):
                raise ValueError("Each fill must be an object")
            fills.append(Fill(**entry))
        results.append({"order_id": order_id, **calculate_order(fills, policy, balance_precision)})
    return {
        "classification": "modelled_fees_for_supplied_policy_and_fill_sequence",
        "policy": asdict(policy), "balance_precision_dollars": decimal(balance_precision),
        "orders": results,
        "total_fee_dollars": sum((r["net_fee_dollars"] for r in results), ZERO),
        "total_cash_change_dollars": sum((r["cash_change_dollars"] for r in results), ZERO),
    }


