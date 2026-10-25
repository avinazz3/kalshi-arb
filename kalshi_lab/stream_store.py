"""Raw stream capture with a bounded writer queue and shared receipt clocks."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Empty, Full, Queue
import sqlite3
from threading import Event, Thread
import time


class StreamWriter:
    def __init__(self, path, session, configuration, queue_size=4096):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.session, self.configuration, self.path = session, configuration, str(path)
        self.queue = Queue(maxsize=queue_size)
        self.error, self.ready = None, Event()
        self.thread = Thread(target=self._write, daemon=True)
        self.thread.start()
        if not self.ready.wait(10) or self.error:
            raise RuntimeError("Could not initialise stream database") from self.error

    def record(self, venue, connection, kind, body, *, wall_ns=None, mono_ns=None):
        # Timestamp before encoding, parsing, queueing or database work.
        mono_ns = time.monotonic_ns() if mono_ns is None else mono_ns
        wall_ns = time.time_ns() if wall_ns is None else wall_ns
        if self.error:
            raise RuntimeError("Stream writer failed; capture must stop") from self.error
        payload = body.encode("utf-8") if isinstance(body, str) else body
        item = (self.session, venue, connection, kind, wall_ns, mono_ns, payload)
        try:
            self.queue.put_nowait(item)
        except Full as exc:
            raise RuntimeError("Capture queue full; aborting rather than silently dropping frames") from exc

    def _write(self):
        db = None
        try:
            db = sqlite3.connect(self.path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                session TEXT PRIMARY KEY, created_at TEXT NOT NULL, configuration_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS stream_events (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, venue TEXT NOT NULL,
                connection TEXT NOT NULL, kind TEXT NOT NULL,
                received_wall_ns INTEGER NOT NULL, received_mono_ns INTEGER NOT NULL,
                body BLOB NOT NULL, sha256 TEXT NOT NULL, written_wall_ns INTEGER NOT NULL)""")
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (
                self.session, datetime.now(timezone.utc).isoformat(), json.dumps(self.configuration, sort_keys=True)))
            db.commit()
            self.ready.set()
            batch, last_commit = [], time.monotonic()
            while True:
                try:
                    item = self.queue.get(timeout=0.1)
                except Empty:
                    item = "flush"
                if item is None:
                    if batch:
                        self._insert(db, batch)
                    break
                if item != "flush":
                    batch.append((*item, hashlib.sha256(item[-1]).hexdigest(), time.time_ns()))
                if batch and (len(batch) >= 100 or time.monotonic() - last_commit >= 0.1):
                    self._insert(db, batch)
                    batch, last_commit = [], time.monotonic()
        except Exception as exc:
            self.error = exc
            self.ready.set()
        finally:
            if db:
                db.close()

    @staticmethod
    def _insert(db, batch):
        with db:
            db.executemany("""INSERT INTO stream_events
                (session,venue,connection,kind,received_wall_ns,received_mono_ns,body,sha256,written_wall_ns)
                VALUES (?,?,?,?,?,?,?,?,?)""", batch)

    def close(self):
        deadline = time.monotonic() + 10
        while self.thread.is_alive() and not self.error and time.monotonic() < deadline:
            try:
                self.queue.put(None, timeout=0.2)
                break
            except Full:
                continue
        self.thread.join(timeout=max(0, deadline - time.monotonic()))
        if self.thread.is_alive() or self.error:
            raise RuntimeError("Stream writer did not finish cleanly") from self.error


def inspect_stream(path, session=None):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    report = {"sessions": {}, "body_hash_failures": 0, "malformed_frames": 0,
              "sequence_discontinuities": 0, "database_bytes": Path(path).stat().st_size,
              "interpretation": "Counts and integrity only; no reconstructed books, forecasts or profitability estimates."}
    sequence = {}
    try:
        query, parameters = "SELECT * FROM stream_events", []
        if session:
            query += " WHERE session=?"
            parameters.append(session)
        query += " ORDER BY received_mono_ns,id"
        for row in db.execute(query, parameters):
            info = report["sessions"].setdefault(row["session"], {
                "venues": {}, "first_wall_ns": row["received_wall_ns"], "last_wall_ns": row["received_wall_ns"],
                "kalshi_snapshots": 0, "coinbase_snapshots": 0, "server_errors": 0,
                "kalshi_market_snapshots": {}, "coinbase_product_snapshots": {},
                "connection_events": {}, "message_types": {}, "session_status": "no_finish_record",
                "venue_first_frame_wall_ns": {}, "venue_last_frame_wall_ns": {},
                "venue_first_frame_mono_ns": {}, "venue_last_frame_mono_ns": {}})
            info["last_wall_ns"] = row["received_wall_ns"]
            info["venues"][row["venue"]] = info["venues"].get(row["venue"], 0) + int(row["kind"] == "frame")
            report["body_hash_failures"] += int(hashlib.sha256(row["body"]).hexdigest() != row["sha256"])
            if row["kind"] != "frame":
                events = info["connection_events"]
                events[row["kind"]] = events.get(row["kind"], 0) + 1
                if row["kind"] == "finished":
                    try:
                        info["session_status"] = json.loads(row["body"])["status"]
                    except (ValueError, KeyError, TypeError):
                        info["session_status"] = "invalid_finish_record"
                continue
            info["venue_first_frame_wall_ns"].setdefault(row["venue"], row["received_wall_ns"])
            info["venue_last_frame_wall_ns"][row["venue"]] = row["received_wall_ns"]
            info["venue_first_frame_mono_ns"].setdefault(row["venue"], row["received_mono_ns"])
            info["venue_last_frame_mono_ns"][row["venue"]] = row["received_mono_ns"]
            try:
                data = json.loads(row["body"])
                if not isinstance(data, dict):
                    raise ValueError("Expected object")
                message_type = str(data.get("type") or data.get("channel") or "unknown")
                label = row["venue"] + ":" + message_type
                info["message_types"][label] = info["message_types"].get(label, 0) + 1
                info["server_errors"] += int(data.get("type") == "error")
                if row["venue"] == "kalshi":
                    info["kalshi_snapshots"] += int(data.get("type") == "orderbook_snapshot")
                    if data.get("type") == "orderbook_snapshot":
                        ticker = data.get("msg", {}).get("market_ticker")
                        if isinstance(ticker, str):
                            counts = info["kalshi_market_snapshots"]
                            counts[ticker] = counts.get(ticker, 0) + 1
                    seq, channel = data.get("seq"), data.get("sid")
                else:
                    seq, message_channel = data.get("sequence_num"), data.get("channel")
                    # Coinbase sequence numbers cover the whole connection, across channels.
                    channel = "connection"
                    if message_channel == "l2_data":
                        info["coinbase_snapshots"] += sum(e.get("type") == "snapshot" for e in data.get("events", []))
                        for event in data.get("events", []):
                            product = event.get("product_id")
                            if event.get("type") == "snapshot" and isinstance(product, str):
                                counts = info["coinbase_product_snapshots"]
                                counts[product] = counts.get(product, 0) + 1
                if isinstance(seq, int) and not isinstance(seq, bool) and channel is not None:
                    key = (row["session"], row["venue"], row["connection"], channel)
                    if key in sequence and seq != sequence[key] + 1:
                        report["sequence_discontinuities"] += 1
                    sequence[key] = seq
            except (ValueError, TypeError, KeyError, AttributeError):
                report["malformed_frames"] += 1
        for info in report["sessions"].values():
            info["both_book_snapshot_types_present"] = bool(info["kalshi_snapshots"] and info["coinbase_snapshots"])
        return report
    except sqlite3.OperationalError as exc:
        raise ValueError("Not a stream capture database; use the new capture or demo command") from exc
    finally:
        db.close()
