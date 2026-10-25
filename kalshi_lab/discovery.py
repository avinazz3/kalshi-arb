"""Explicit crypto series selection; no semantic relation mining."""
from datetime import datetime, timezone
from urllib.parse import quote

from .client import utc_now
from .numbers import decimal

ASSETS = {"BTC": ("KXBTC15M", "BTC-USD"), "ETH": ("KXETH15M", "ETH-USD"),
          "SOL": ("KXSOL15M", "SOL-USD")}


class NoActiveMarket(ValueError):
    pass


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
        raise NoActiveMarket(f"No active supported $1 market in {series}; rerun discovery in the next window")
    return min(eligible, key=lambda m: parse_time(m["close_time"]))


def discover(client, assets):
    records = []
    for asset in dict.fromkeys(assets):
        if asset not in ASSETS:
            raise ValueError(f"Unsupported asset: {asset}")
        series, product = ASSETS[asset]
        series_payload = client.get(f"/series/{quote(series, safe='')}", ticker=series)
        markets, cursor = {}, ""
        for _ in range(10):
            params = {"series_ticker": series, "status": "open", "limit": 200, "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            page = client.get("/markets", params)
            for market in page["markets"]:
                markets[market["ticker"]] = market
            cursor = page.get("cursor", "")
            if not cursor:
                break
        if cursor:
            raise ValueError("Market discovery pagination incomplete")
        market = select_active(list(markets.values()), series, datetime.now(timezone.utc))
        event = client.get(f"/events/{quote(market['event_ticker'], safe='')}", ticker=market["event_ticker"])
        records.append({"asset": asset, "coinbase_product": product,
                        "series": series_payload["series"], "event": event["event"], "market": market})
    if not records:
        raise ValueError("Select at least one asset")
    return {"created_at": utc_now(), "purpose": "crypto_order_flow_research",
            "metadata_session": client.session, "assets": records}


def validate_universe(document):
    if not isinstance(document, dict) or document.get("purpose") != "crypto_order_flow_research":
        raise ValueError("Expected a crypto manifest; run discover first")
    records = document.get("assets")
    if not isinstance(records, list) or not records:
        raise ValueError("Manifest must contain assets")
    seen = set()
    for record in records:
        asset = record["asset"]
        if asset in seen or asset not in ASSETS:
            raise ValueError("Assets must be supported and unique")
        seen.add(asset)
        series, product = ASSETS[asset]
        if (record["coinbase_product"] != product or record["series"]["ticker"] != series
                or not record["market"]["ticker"].startswith(series + "-")):
            raise ValueError("Manifest asset, product and market mapping disagree")
        market = record["market"]
        if market.get("market_type") != "binary" or decimal(market.get("notional_value_dollars", "0")) != 1:
            raise ValueError("Manifest market must be a supported $1 binary")
        index = market.get("exchange_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("Manifest must retain the market's nonnegative exchange index")
        if parse_time(market["open_time"]) >= parse_time(market["close_time"]):
            raise ValueError("Market open time must precede close")
        parse_time(record["market"]["close_time"])
    return records
