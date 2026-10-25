"""Exact level books. Kalshi stores two bid ladders; Coinbase stores bids/asks."""
from dataclasses import dataclass
from decimal import Decimal

from .numbers import decimal

ZERO, ONE = Decimal(0), Decimal(1)


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal

    @property
    def midpoint(self):
        return (self.bid + self.ask) / 2

    @property
    def spread(self):
        return self.ask - self.bid

    @property
    def imbalance(self):
        return (self.bid_size - self.ask_size) / (self.bid_size + self.ask_size)


def number(value, *, nonnegative=False):
    value = decimal(value)
    if nonnegative and value < 0:
        raise ValueError("Negative price or quantity")
    return value


class Book:
    def __init__(self, kalshi=False):
        self.kalshi = kalshi
        self.bids, self.asks = {}, {}  # asks contains NO bids for Kalshi

    def _price(self, value):
        price = number(value, nonnegative=True)
        if self.kalshi:
            if price > 1 or price * 10000 != (price * 10000).to_integral_value():
                raise ValueError("Invalid Kalshi fixed-point price")
        elif price <= 0:
            raise ValueError("Coinbase price must be positive")
        return price

    def _size(self, value):
        size = number(value, nonnegative=True)
        if self.kalshi and size * 100 != (size * 100).to_integral_value():
            raise ValueError("Invalid Kalshi fixed-point quantity")
        return size

    def snapshot_kalshi(self, message):
        bids, asks = {}, {}
        for key, ladder in (("yes_dollars_fp", bids), ("no_dollars_fp", asks)):
            for price, size in message[key] or []:
                price, size = self._price(price), self._size(size)
                if price in ladder:
                    raise ValueError("Duplicate snapshot price level")
                ladder[price] = size
        self.bids = {p: q for p, q in bids.items() if q}
        self.asks = {p: q for p, q in asks.items() if q}

    def delta_kalshi(self, message):
        side = message["side"]
        if side not in ("yes", "no"):
            raise ValueError("Unknown Kalshi side")
        price = self._price(message["price_dollars"])
        ladder = self.bids if side == "yes" else self.asks
        size = self._size(ladder.get(price, ZERO) + decimal(message["delta_fp"]))
        if size:
            ladder[price] = size
        else:
            ladder.pop(price, None)

    def update_coinbase(self, updates):
        # Validate the complete batch before changing state. Quantities are absolute.
        parsed = []
        for update in updates:
            side = update["side"]
            if side not in ("bid", "offer"):
                raise ValueError("Unknown Coinbase side")
            parsed.append((side, self._price(update["price_level"]), self._size(update["new_quantity"])))
        for side, price, size in parsed:
            ladder = self.bids if side == "bid" else self.asks
            if size:
                ladder[price] = size
            else:
                ladder.pop(price, None)

    def quote(self):
        if not self.bids or not self.asks:
            return None
        bid = max(self.bids)
        opposite = max(self.asks) if self.kalshi else min(self.asks)
        ask = ONE - opposite if self.kalshi else opposite
        if bid > ask:
            return None
        return Quote(bid, ask, self.bids[bid], self.asks[opposite])


def top_of_book_ofi(previous, current):
    """Cont-style best-level OFI between two complete Coinbase L2 event batches.

    This is message-level sampling, not a claim to recover individual engine events.
    Positive values indicate net buying pressure, in base-asset units.
    """
    return (int(current.bid >= previous.bid) * current.bid_size
            - int(current.bid <= previous.bid) * previous.bid_size
            - int(current.ask <= previous.ask) * current.ask_size
            + int(current.ask >= previous.ask) * previous.ask_size)
