"""Deterministic, read-only reconstruction of one raw capture session."""
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from decimal import DecimalException
import hashlib
import json
from pathlib import Path
import sqlite3

from .books import Book, number, top_of_book_ofi


class Timeline:
    """Right-continuous receipt-time states, including all frames tied at a time."""
    def __init__(self):
        self.times, self.values = [], []

    def add(self, timestamp, value):
        self.times.append(timestamp)
        self.values.append(value)

    def at(self, timestamp):
        index = bisect_right(self.times, timestamp) - 1
        return None if index < 0 else self.values[index]

    def between(self, left, right):
        return self.values[bisect_right(self.times, left):bisect_right(self.times, right)]


@dataclass(frozen=True)
class BookPoint:
    epoch: int
    quote: object
    event_id: int
    received_ns: int
    reason: str


class Replay:
    def __init__(self, session, configuration):
        self.session, self.configuration = session, configuration
        self.books, self.points, self.epochs, self.scopes = {}, {}, Counter(), {}
        self.connections, self.sequences = {}, {}
        self.activity = {v: Timeline() for v in ("kalshi", "coinbase")}
        self.trades, self.ofi, self.flow = {}, {}, {}
        self.flow_epochs, self.trade_ids = Counter(), {}
        self.diagnostics = Counter()
        self.first_mono_ns = self.first_wall_ns = self.last_mono_ns = None
        self.max_clock_deviation_ns = 0
        self.status, self.source_digest = "no_finish_record", None

    def _point(self, key, row, reason="ok"):
        book = self.books.get(key)
        self.points.setdefault(key, Timeline()).add(row["received_mono_ns"], BookPoint(
            self.epochs[key], book.quote() if book else None, row["id"], row["received_mono_ns"], reason))

    def _invalidate(self, row, *, venue=None, scope=None, key=None, reason):
        keys = set(self.books) | set(self.points)
        if key is not None:
            keys.add(key)
        for candidate in keys:
            if ((key is not None and key != candidate)
                    or (venue is not None and candidate[0] != venue)
                    or (scope is not None and self.scopes.get(candidate) != scope)):
                continue
            self.books.pop(candidate, None)
            self.epochs[candidate] += 1
            self._point(candidate, row, reason)
        if venue == "coinbase":
            for product in list(self.flow):
                self.flow_epochs[product] += 1
                self.flow[product].add(row["received_mono_ns"], (self.flow_epochs[product], False))

    def _ready(self, product, row):
        timeline = self.flow.setdefault(product, Timeline())
        # A replacement snapshot is a new flow-coverage boundary too.
        self.flow_epochs[product] += 1
        timeline.add(row["received_mono_ns"], (self.flow_epochs[product], True))

    def _sequence(self, row, data):
        venue, connection = row["venue"], row["connection"]
        if venue == "coinbase":
            scope, seq = "connection", data["sequence_num"]
        else:
            scope, seq = data.get("sid"), data.get("seq")
            if scope is None or seq is None:
                if data.get("type") in ("orderbook_snapshot", "orderbook_delta"):
                    raise ValueError("Book message missing sequence/subscription")
                return True
            if not isinstance(scope, int) or isinstance(scope, bool) or scope < 0:
                raise ValueError("Invalid subscription ID")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("Invalid sequence")
        key = (venue, connection, scope)
        previous = self.sequences.get(key)
        if previous is not None and seq != previous + 1:
            self.diagnostics["sequence_discontinuities"] += 1
            if venue == "kalshi":
                self._invalidate(row, scope=key, reason="sequence_gap")
            else:
                self._invalidate(row, venue=venue, reason="sequence_gap")
            if seq <= previous:
                return False  # Never apply duplicate/out-of-order deltas twice.
        self.sequences[key] = seq
        return True

    def _kalshi(self, row, data):
        kind = data.get("type")
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            if kind in ("error", "unsubscribed"):
                self.diagnostics["server_errors"] += 1
                self._invalidate(row, venue="kalshi", reason=kind)
            return
        message = data["msg"]
        ticker = message["market_ticker"]
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("Invalid market ticker")
        key, scope = ("kalshi", ticker), ("kalshi", row["connection"], data["sid"])
        if kind == "orderbook_snapshot":
            book = Book(kalshi=True)
            book.snapshot_kalshi(message)
            self.epochs[key] += 1
            self.books[key], self.scopes[key] = book, scope
            self.diagnostics["kalshi_snapshots"] += 1
        elif key not in self.books or self.scopes.get(key) != scope:
            self.diagnostics["deltas_without_snapshot"] += 1
            self._invalidate(row, key=key, reason="snapshot_required")
            return
        else:
            self.books[key].delta_kalshi(message)
        self._check_quote(key, row)

    def _check_quote(self, key, row):
        if self.books[key].quote() is None:
            self.diagnostics["empty_or_crossed_books"] += 1
            self._invalidate(row, key=key, reason="empty_or_crossed")
        else:
            self._point(key, row)

    def _coinbase(self, row, data):
        channel = data.get("channel")
        if data.get("type") == "error" or channel == "error":
            self.diagnostics["server_errors"] += 1
            self._invalidate(row, venue="coinbase", reason="server_error")
        elif channel == "subscriptions":
            for event in data["events"]:
                subscriptions = event["subscriptions"]
                for key in list(self.books):
                    if key[0] == "coinbase" and key[1] not in subscriptions.get("level2", []):
                        self._invalidate(row, key=key, reason="unsubscribed")
                for product, timeline in self.flow.items():
                    if product not in subscriptions.get("market_trades", []):
                        self.flow_epochs[product] += 1
                        timeline.add(row["received_mono_ns"], (self.flow_epochs[product], False))
                # An ack after the historical trade snapshot establishes coverage
                # even if an illiquid product had an empty snapshot.
                for product in subscriptions.get("market_trades", []):
                    self._ready(product, row)
        elif channel == "l2_data":
            for event in data["events"]:
                product, kind = event["product_id"], event["type"]
                if kind not in ("snapshot", "update") or not isinstance(product, str):
                    raise ValueError("Unknown Coinbase book event")
                key = ("coinbase", product)
                old = self.books[key].quote() if key in self.books else None
                if kind == "snapshot":
                    book = Book()
                    book.update_coinbase(event["updates"])
                    self.epochs[key] += 1
                    self.books[key] = book
                    self.diagnostics["coinbase_snapshots"] += 1
                elif key not in self.books:
                    self.diagnostics["deltas_without_snapshot"] += 1
                    self._invalidate(row, key=key, reason="snapshot_required")
                    continue
                else:
                    self.books[key].update_coinbase(event["updates"])
                self._check_quote(key, row)
                current = self.books[key].quote() if key in self.books else None
                if kind == "update" and old and current:
                    self.ofi.setdefault(product, Timeline()).add(row["received_mono_ns"], top_of_book_ofi(old, current))
        elif channel == "market_trades":
            self._market_trades(row, data["events"])

    def _market_trades(self, row, events):
        # Parse the entire frame before recording any trades.
        parsed, snapshots = [], set()
        for event in events:
            kind = event["type"]
            if kind not in ("snapshot", "update"):
                raise ValueError("Unknown trade event")
            for trade in event["trades"]:
                product, trade_id = trade["product_id"], trade["trade_id"]
                if not isinstance(product, str) or not isinstance(trade_id, str) or not trade_id:
                    raise ValueError("Invalid trade identity")
                price, size = number(trade["price"]), number(trade["size"])
                side = trade["side"]
                if price <= 0 or size <= 0 or side not in ("BUY", "SELL"):
                    raise ValueError("Invalid trade")
                if kind == "snapshot":
                    snapshots.add(product)
                parsed.append((kind, product, trade_id, price, size, side))
        staged = {}
        for kind, product, trade_id, price, size, side in parsed:
            identity, signature = (product, trade_id), (price, size, side)
            previous = staged.get(identity, self.trade_ids.get(identity))
            if previous is not None and previous != signature:
                raise ValueError("Conflicting duplicate trade ID")
            if previous is not None:
                self.diagnostics["duplicate_trades"] += 1
            elif kind == "update":
                notional = price * size
                # API side is MAKER side: SELL maker implies aggressive BUY.
                self.trades.setdefault(product, Timeline()).add(row["received_mono_ns"],
                    (notional if side == "SELL" else -notional, notional))
            staged[identity] = signature
        self.trade_ids.update(staged)
        for product in snapshots:
            self._ready(product, row)

    def consume(self, row):
        timestamp, wall = row["received_mono_ns"], row["received_wall_ns"]
        if self.first_mono_ns is None:
            self.first_mono_ns, self.first_wall_ns = timestamp, wall
        if self.last_mono_ns is not None and timestamp < self.last_mono_ns:
            raise ValueError("Receipt clock moved backwards in capture order")
        self.last_mono_ns = timestamp
        self.max_clock_deviation_ns = max(self.max_clock_deviation_ns,
            abs((wall - self.first_wall_ns) - (timestamp - self.first_mono_ns)))
        venue, kind, connection = row["venue"], row["kind"], row["connection"]
        if kind == "finished":
            self.status = json.loads(row["body"])["status"]
            return
        if venue not in self.activity:
            return
        if self.connections.get(venue) != connection:
            self._invalidate(row, venue=venue, reason="new_connection")
            self.connections[venue] = connection
        if kind in ("connected", "disconnected"):
            self._invalidate(row, venue=venue, reason=kind)
            return
        if kind != "frame":
            return
        self.activity[venue].add(timestamp, (timestamp, row["id"]))
        try:
            data = json.loads(row["body"], parse_float=str)
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            if not self._sequence(row, data):
                return
            if venue == "kalshi":
                self._kalshi(row, data)
            else:
                self._coinbase(row, data)
        except (ValueError, KeyError, TypeError, AttributeError, DecimalException):
            self.diagnostics["malformed_frames"] += 1
            self._invalidate(row, venue=venue, reason="malformed_frame")

    def report(self):
        quotes = {}
        for (venue, instrument), series in self.points.items():
            point = series.values[-1]
            q = point.quote
            quotes[venue + ":" + instrument] = {
                "epoch": point.epoch, "reason": point.reason, "source_event_id": point.event_id,
                "quote": None if q is None else {"bid": str(q.bid), "ask": str(q.ask),
                    "bid_size": str(q.bid_size), "ask_size": str(q.ask_size),
                    "midpoint": str(q.midpoint), "imbalance": str(q.imbalance)}}
        return {"session": self.session, "synthetic": bool(self.configuration.get("synthetic")),
                "source_digest": self.source_digest, "status": self.status,
                "diagnostics": dict(self.diagnostics), "final_books": quotes,
                "interpretation": "Reconstructed quotes only; no forecasts or trading results."}


def load_replay(path, session=None):
    """Hold a SQLite read snapshot; verify bytes and capture order before use."""
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        sessions = list(db.execute("SELECT session,configuration_json FROM sessions"))
        if session is None:
            if len(sessions) != 1:
                raise ValueError("Select --session explicitly when a database has zero or multiple sessions")
            session = sessions[0]["session"]
        config = next((r["configuration_json"] for r in sessions if r["session"] == session), None)
        if config is None:
            raise ValueError("Unknown capture session")
        configuration = json.loads(config)
        if not isinstance(configuration, dict):
            raise ValueError("Capture session configuration must be an object")
        result = Replay(session, configuration)
        digest = hashlib.sha256()
        digest.update(json.dumps(["crypto-flow-replay-v1", session, json.loads(config)], sort_keys=True).encode())
        for row in db.execute("SELECT * FROM stream_events WHERE session=? ORDER BY id", (session,)):
            if hashlib.sha256(row["body"]).hexdigest() != row["sha256"]:
                raise ValueError(f"Body hash failure at event {row['id']}; refusing replay")
            digest.update(json.dumps([row[k] for k in ("id", "venue", "connection", "kind",
                "received_wall_ns", "received_mono_ns", "sha256")], separators=(",", ":")).encode())
            result.consume(row)
        result.source_digest = digest.hexdigest()
        return result
    finally:
        db.close()
