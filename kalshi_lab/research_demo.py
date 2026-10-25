"""Deterministic synthetic research fixture with hand-checkable prices and flow."""
import json
from pathlib import Path

from .dataset import unix_ns
from .stream_store import StreamWriter


def create_research_demo(path):
    if Path(path).exists():
        raise ValueError("Demo output already exists; choose another path")
    market = {"ticker": "KXBTC15M-SYNTHETIC", "market_type": "binary",
              "notional_value_dollars": "1.0000", "exchange_index": 2,
              "open_time": "2026-01-01T00:00:00Z", "close_time": "2026-01-01T00:15:00Z"}
    universe = {"purpose": "crypto_order_flow_research", "assets": [{"asset": "BTC",
        "coinbase_product": "BTC-USD", "series": {"ticker": "KXBTC15M"}, "market": market}]}
    writer = StreamWriter(path, "synthetic-research", {"synthetic": True,
        "reference_only": False, "universe": universe, "purpose": "hand-checkable research plumbing"})
    frames = [(0, "kalshi", {"type": "orderbook_snapshot", "sid": 1, "msg": {
        "market_ticker": market["ticker"], "yes_dollars_fp": [["0.4900", "10.00"]],
        "no_dollars_fp": [["0.5000", "12.00"], ["0.4900", "8.00"]]}}),
        (0, "coinbase", {"channel": "l2_data", "events": [{"type": "snapshot", "product_id": "BTC-USD", "updates": [
            {"side": "bid", "price_level": "100.00", "new_quantity": "1.00"},
            {"side": "offer", "price_level": "100.04", "new_quantity": "2.00"}]}]}),
        (0, "coinbase", {"channel": "market_trades", "events": [{"type": "snapshot", "trades": [
            {"trade_id": "historical", "product_id": "BTC-USD", "price": "900", "size": "100", "side": "BUY"}]}]}),
        (0, "coinbase", {"channel": "subscriptions", "events": [{"subscriptions": {
            "level2": ["BTC-USD"], "market_trades": ["BTC-USD"]}}]}),
        (1500, "coinbase", {"channel": "market_trades", "events": [{"type": "update", "trades": [
            {"trade_id": "new-1", "product_id": "BTC-USD", "price": "100.04", "size": "0.25", "side": "SELL"}]}]}),
        (1500, "coinbase", {"channel": "l2_data", "events": [{"type": "update", "product_id": "BTC-USD", "updates": [
            {"side": "bid", "price_level": "100.00", "new_quantity": "3.00"}]}]}),
        (2500, "kalshi", {"type": "orderbook_delta", "sid": 1, "msg": {
            "market_ticker": market["ticker"], "side": "no", "price_dollars": "0.5000", "delta_fp": "-12.00"}})]
    for second in range(1, 13):
        frames.extend([(second * 1000, "coinbase", {"channel": "heartbeats", "events": []}),
            (second * 1000, "kalshi", {"type": "orderbook_delta", "sid": 1, "msg": {
                "market_ticker": market["ticker"], "side": "yes", "price_dollars": "0.4900", "delta_fp": "1.00"}})])
    wall, mono = unix_ns(market["open_time"]), 1_000_000_000
    sequence = {"kalshi": 1, "coinbase": 0}
    try:
        for milliseconds, venue, frame in sorted(frames, key=lambda item: item[0]):
            frame["seq" if venue == "kalshi" else "sequence_num"] = sequence[venue]
            sequence[venue] += 1
            writer.record(venue, venue + "-synthetic", "frame", json.dumps(frame),
                          wall_ns=wall + milliseconds * 1_000_000, mono_ns=mono + milliseconds * 1_000_000)
        writer.record("collector", "synthetic", "finished", '{"status":"synthetic_completed"}',
                      wall_ns=wall + 12_000_000_001, mono_ns=mono + 12_000_000_001)
    finally:
        writer.close()
    return {"database": str(path), "session": "synthetic-research", "synthetic": True,
        "expected": {"dataset_rows_default": 7,
            "elapsed_1s_signed_notional": "0", "elapsed_2s_signed_notional": "25.0100",
            "elapsed_2s_message_ofi": "2.00", "elapsed_2s_1s_midpoint_change": "0.0050"}}
