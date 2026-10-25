"""Small, explicitly synthetic stream fixture for offline plumbing checks."""
import json
from pathlib import Path

from .stream_store import StreamWriter, inspect_stream


def create_demo(path):
    if Path(path).exists():
        raise ValueError("Demo output already exists; choose another path")
    writer = StreamWriter(path, "synthetic-demo", {"synthetic": True, "purpose": "offline plumbing only"})
    frames = [
        ("kalshi", {"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
            "market_ticker": "SYNTHETIC-BTC", "yes_dollars_fp": [["0.4900", "10.00"]],
            "no_dollars_fp": [["0.5000", "12.00"]]}}),
        ("coinbase", {"channel": "l2_data", "sequence_num": 0, "events": [{"type": "snapshot",
            "product_id": "BTC-USD", "updates": [{"side": "bid", "price_level": "100.00", "new_quantity": "1.00"},
            {"side": "offer", "price_level": "100.01", "new_quantity": "1.50"}]}]}),
        ("coinbase", {"channel": "market_trades", "sequence_num": 1, "events": [{"type": "update", "trades": [
            {"trade_id": "synthetic-1", "product_id": "BTC-USD", "price": "100.01", "size": "0.25", "side": "SELL"}]}]}),
        ("kalshi", {"type": "orderbook_delta", "sid": 1, "seq": 2, "msg": {
            "market_ticker": "SYNTHETIC-BTC", "price_dollars": "0.4900", "delta_fp": "1.00", "side": "yes"}}),
    ]
    try:
        for index, (venue, frame) in enumerate(frames):
            writer.record(venue, venue + "-demo", "frame", json.dumps(frame),
                          wall_ns=1_000_000_000 + index * 1_000_000, mono_ns=100 + index)
        writer.record("collector", "demo", "finished", '{"status":"synthetic_completed"}',
                      wall_ns=1_004_000_000, mono_ns=104)
    finally:
        writer.close()
    return {"synthetic": True, "quality": inspect_stream(path)}
