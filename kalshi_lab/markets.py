"""Small metadata helpers used by the BTC settlement-data diagnostic."""
from datetime import datetime

from .numbers import decimal

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Market times must include a timezone")
    return parsed


def select_active(markets, series, now):
    eligible = []
    for market in markets:
        if (not market.get("ticker", "").startswith(series + "-")
                or market.get("market_type") != "binary"
                or market.get("status") not in ("active", "open")):
            continue
        if decimal(market.get("notional_value_dollars", "0")) != 1:
            continue
        if parse_time(market["open_time"]) <= now < parse_time(market["close_time"]):
            eligible.append(market)
    if not eligible:
        raise ValueError(f"No active supported $1 market in {series}")
    return min(eligible, key=lambda m: parse_time(m["close_time"]))
