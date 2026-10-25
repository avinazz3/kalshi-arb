"""Read-only WebSocket capture. No order/account endpoints are called."""
import asyncio
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from .discovery import ASSETS, parse_time, validate_universe
from .stream_store import StreamWriter, inspect_stream

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
KALSHI_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


def kalshi_headers(key_id, private_key, timestamp_ms=None):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519
    timestamp = str(int(time.time() * 1000) if timestamp_ms is None else timestamp_ms)
    message = (timestamp + "GET/trade-api/ws/v2").encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(message)
    elif isinstance(private_key, rsa.RSAPrivateKey):
        signature = private_key.sign(message, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        raise ValueError("Kalshi key must be RSA or Ed25519")
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}


def specifications(assets, markets):
    specs = [{"venue": "coinbase", "url": COINBASE_WS,
              "subscriptions": [{"type": "subscribe", "product_ids": [ASSETS[a][1] for a in assets],
                                 "channel": channel} for channel in ("heartbeats", "level2", "market_trades")]}]
    if markets:
        specs.append({"venue": "kalshi", "url": KALSHI_WS, "subscriptions": [
            {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": markets}}]})
    return specs


async def collect_feed(spec, writer, auth=None):
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    retry = 0
    while True:
        connection = uuid4().hex
        headers = auth() if spec["venue"] == "kalshi" else None
        try:
            async with connect(spec["url"], additional_headers=headers, open_timeout=10,
                               close_timeout=3, max_size=32 * 1024 * 1024, max_queue=16,
                               ping_interval=20, ping_timeout=20) as websocket:
                writer.record(spec["venue"], connection, "connected", "{}")
                for subscription in spec["subscriptions"]:
                    await websocket.send(json.dumps(subscription))
                retry = 0
                async for raw in websocket:
                    writer.record(spec["venue"], connection, "frame", raw)
        except (OSError, TimeoutError, ConnectionClosed, InvalidStatus) as exc:
            # Never persist exception reprs/handshake headers; they may contain auth data.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            writer.record(spec["venue"], connection, "disconnected", json.dumps(
                {"error_type": type(exc).__name__, "http_status": status}))
            if status in (401, 403):
                raise RuntimeError(f"{spec['venue']} authentication/access rejected (HTTP {status})") from None
        else:
            writer.record(spec["venue"], connection, "disconnected", '{"reason":"remote_close"}')
        retry += 1
        await asyncio.sleep(min(10, 0.5 * 2 ** min(retry - 1, 5)))


async def run_capture(path, seconds, *, universe=None, reference_only=False, assets=None):
    try:
        import websockets  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Install streaming dependencies: python3 -m pip install -e '.[stream]'") from exc
    if not 0 < seconds <= 3600:
        raise ValueError("Capture seconds must be between 1 and 3600")
    auth = None
    if reference_only:
        assets = list(dict.fromkeys(assets or ["BTC", "ETH"]))
        if not assets or any(a not in ASSETS for a in assets):
            raise ValueError("Select BTC, ETH or SOL")
        tickers, duration = [], seconds
    else:
        if assets:
            raise ValueError("For joint capture select assets with discover, then pass its manifest")
        records = validate_universe(universe)
        now = datetime.now(timezone.utc)
        duration = min(seconds, min((parse_time(r["market"]["close_time"]) - now).total_seconds() - 2 for r in records))
        if duration < 1:
            raise ValueError("Manifest expired or too close to expiry; rerun discover")
        assets, tickers = [r["asset"] for r in records], [r["market"]["ticker"] for r in records]
        key_id, key_path = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_id or not key_path:
            raise ValueError("Joint streaming requires local KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH; reference-only needs neither")
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
        except ImportError as exc:
            raise RuntimeError("Install the [stream] optional dependencies") from exc
        private_key = load_pem_private_key(Path(key_path).read_bytes(), password=None)
        auth = lambda: kalshi_headers(key_id, private_key)
        auth()  # Validate the key before connecting either feed. Do not store headers.
    session = uuid4().hex
    configuration = {"assets": assets, "reference_only": reference_only,
                     "requested_seconds": seconds, "planned_seconds": duration, "universe": universe}
    writer = StreamWriter(path, session, configuration)
    tasks = [asyncio.create_task(collect_feed(spec, writer, auth)) for spec in specifications(assets, tickers)]
    completed = False
    try:
        done, _ = await asyncio.wait(tasks, timeout=duration, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.exception():
                raise task.exception()
        completed = True
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            writer.record("collector", session, "finished", json.dumps({"status": "completed" if completed else "interrupted_or_failed"}))
        except RuntimeError:
            pass  # A failed/full writer already aborts capture; close still needs to run.
        await asyncio.to_thread(writer.close)
    report = inspect_stream(path, session)
    info = report["sessions"].get(session, {})
    missing_products = set(ASSETS[a][1] for a in assets) - set(info.get("coinbase_product_snapshots", {}))
    missing_markets = set(tickers) - set(info.get("kalshi_market_snapshots", {}))
    if missing_products or missing_markets:
        raise RuntimeError("Missing required book snapshots; inspect the retained capture for connection/subscription failures")
    return {"session": session, "quality": report}
