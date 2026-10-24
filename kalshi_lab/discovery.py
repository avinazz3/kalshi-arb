"""Suggest threshold relations for manual review; never approve from metadata."""
from collections import defaultdict
from .books import D, decimal


def candidates(markets):
    groups = defaultdict(list)
    for market in markets:
        if (market.get("market_type") == "binary"
                and market.get("strike_type") == "greater"
                and market.get("floor_strike") is not None):
            groups[market["event_ticker"]].append(market)
    result = []
    for event, group in sorted(groups.items()):
        ordered = sorted(group, key=lambda m: decimal(m["floor_strike"]))
        for lower, higher in zip(ordered, ordered[1:]):
            if decimal(lower["floor_strike"]) >= decimal(higher["floor_strike"]):
                continue
            result.append({
                "event_ticker": event,
                "lower_ticker": lower["ticker"], "higher_ticker": higher["ticker"],
                "lower_strike": str(lower["floor_strike"]),
                "higher_strike": str(higher["floor_strike"]),
                "proposed_relation": "YES_higher_implies_YES_lower",
                "review_status": "unreviewed",
                "rule_checks": {k: False for k in (
                    "same_underlying_and_reference_period", "same_source_and_revision_policy",
                    "threshold_boundaries_and_rounding", "delay_void_and_nonbinary_settlement",
                    "contract_terms_and_rulebook_reviewed", "effective_fees_reviewed")},
                "review_notes": "Metadata proposal only; not a certified relationship.",
            })
    return result


def choose_watchlist(markets, max_markets=4, event=None):
    eligible = [m for m in markets if m.get("market_type") == "binary"
                and m.get("strike_type") == "greater" and m.get("floor_strike") is not None]
    groups = defaultdict(list)
    for market in eligible:
        if event is None or market["event_ticker"] == event:
            groups[market["event_ticker"]].append(market)
    groups = {k: v for k, v in groups.items() if len(v) >= 2}
    if not groups:
        raise ValueError("No event with >=2 greater-than markets in the retrieved pages")
    chosen = max(groups, key=lambda k: sum(decimal(m.get("volume_fp") or "0") for m in groups[k]))
    def distance(m):
        bid = decimal(m.get("yes_bid_dollars") or "0")
        ask = decimal(m.get("yes_ask_dollars") or "1")
        return abs((bid + ask) / 2 - D("0.5")), m["ticker"]
    selected = sorted(groups[chosen], key=distance)[:max_markets]
    selected.sort(key=lambda m: decimal(m["floor_strike"]))
    return {"series_ticker": None, "event_ticker": chosen,
            "purpose": "data_collection_only", "tickers": [m["ticker"] for m in selected],
            "selection": "highest retrieved event volume; strikes closest to 50-cent midpoint",
            "relations_reviewed": False}
