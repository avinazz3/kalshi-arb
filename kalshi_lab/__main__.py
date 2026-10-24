import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from urllib.parse import quote

from .books import asks, bids, threshold_pair_quote
from .client import Client, utc_now
from .discovery import candidates, choose_watchlist
from .storage import Store


def dump(value):
    return json.dumps(value, indent=2, default=str, allow_nan=False)


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dump(value) + "\n", encoding="utf-8")


def discover(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    store = Store(out / "requests.sqlite")
    try:
        client = Client(store)
        series = client.get(f"/series/{quote(args.series, safe='')}", ticker=args.series)
        pages, markets, cursor = [], {}, ""
        for _ in range(args.max_pages):
            params = {"series_ticker": args.series, "status": "open", "limit": 200, "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            page = client.get("/markets", params)
            pages.append(page)
            for market in page["markets"]:
                markets[market["ticker"]] = market
            cursor = page.get("cursor", "")
            if not cursor:
                break
            time.sleep(0.25)
        manifest = {"retrieved_at": utc_now(), "series": series, "markets": list(markets.values()),
                    "pagination_complete": not bool(cursor), "remaining_cursor": cursor,
                    "pages_retrieved": len(pages), "session": client.session}
        write_json(out / "markets.json", manifest)
        proposals = candidates(manifest["markets"])
        write_json(out / "relation_candidates.json", proposals)
        watchlist = choose_watchlist(manifest["markets"], args.max_markets, args.event)
        watchlist["series_ticker"] = args.series
        event = client.get(f"/events/{quote(watchlist['event_ticker'], safe='')}", ticker=watchlist["event_ticker"])
        write_json(out / "selected_event.json", event)
        write_json(out / "watchlist.json", watchlist)
        print(dump({"markets_found": len(markets), "pagination_complete": not bool(cursor),
                    "unreviewed_adjacent_pairs": len(proposals), "watchlist": watchlist,
                    "output_directory": str(out)}))
    finally:
        store.close()


def collect(args):
    watch = json.loads(Path(args.watchlist).read_text())
    tickers = list(dict.fromkeys(watch["tickers"]))
    if not tickers or len(tickers) > 12 or not all(isinstance(t, str) and t for t in tickers):
        raise ValueError("Use a watchlist of 1-12 nonempty tickers")
    store = Store(args.db)
    successful, failed = 0, 0
    try:
        client = Client(store)
        # Every run archives its own rule/fee metadata, not just the old watchlist.
        if watch.get("series_ticker"):
            client.get(f"/series/{quote(watch['series_ticker'], safe='')}", ticker=watch["series_ticker"])
        if watch.get("event_ticker"):
            client.get(f"/events/{quote(watch['event_ticker'], safe='')}", ticker=watch["event_ticker"])
        for ticker in tickers:
            client.get(f"/markets/{quote(ticker, safe='')}", ticker=ticker)
        sweep = 0
        while args.sweeps == 0 or sweep < args.sweeps:
            sweep += 1
            started = time.monotonic()
            for ticker in tickers:
                try:
                    payload = client.get(f"/markets/{quote(ticker, safe='')}/orderbook",
                                         kind="orderbook", ticker=ticker, sweep=sweep)
                    bids(payload, "yes")
                    bids(payload, "no")
                    successful += 1
                except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                    failed += 1
                    print(f"{utc_now()} sweep={sweep} ticker={ticker} ERROR {exc}", file=sys.stderr)
                time.sleep(0.25)
            print(dump({"session": client.session, "sweep": sweep,
                        "valid_books_total": successful, "failed_books_total": failed,
                        "sweep_duration_seconds": round(time.monotonic() - started, 3)}), flush=True)
            if args.sweeps == 0 or sweep < args.sweeps:
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("Stopped; committed responses remain on disk.")
    finally:
        store.close()
    if successful == 0:
        raise RuntimeError("No valid orderbooks collected")


def inspect(args):
    # mode=ro avoids creating an empty database on a typo.
    db = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    result, durations, spans = {}, [], {}
    try:
        for row in db.execute("SELECT * FROM responses ORDER BY id"):
            if row["kind"] != "orderbook":
                continue
            ticker = row["ticker"]
            info = result.setdefault(ticker, {"http_attempts": 0, "valid_books": 0, "invalid_or_failed": 0,
                "empty_yes_bid_books": 0, "empty_no_bid_books": 0, "first_received_at": None,
                "last_received_at": None, "latest_yes_ask": None, "latest_no_ask": None})
            info["http_attempts"] += 1
            if row["status"] != 200 or row["error"]:
                info["invalid_or_failed"] += 1
                continue
            try:
                payload = json.loads(row["body"], parse_float=str)
                yes, no = bids(payload, "yes"), bids(payload, "no")
            except (ValueError, TypeError, KeyError):
                info["invalid_or_failed"] += 1
                continue
            info["valid_books"] += 1
            info["empty_yes_bid_books"] += int(not yes)
            info["empty_no_bid_books"] += int(not no)
            info["first_received_at"] = info["first_received_at"] or row["received_at"]
            info["last_received_at"] = row["received_at"]
            info["latest_yes_ask"] = str(asks(payload, "yes")[0][0]) if no else None
            info["latest_no_ask"] = str(asks(payload, "no")[0][0]) if yes else None
            durations.append(row["duration_ms"])
            key = (row["session"], row["sweep"])
            spans.setdefault(key, []).append(datetime.fromisoformat(row["received_at"]))
        max_span = max(((max(v) - min(v)).total_seconds() for v in spans.values()), default=None)
        report = {"generated_at": utc_now(), "markets": result,
                  "median_successful_request_ms": statistics.median(durations) if durations else None,
                  "maximum_observed_sweep_receipt_span_seconds": max_span,
                  "interpretation": "REST samples are asynchronous; receipt spans are not exchange-clock latency or opportunity duration. No fees or net opportunities estimated."}
        if args.out:
            write_json(args.out, report)
        print(dump(report))
    finally:
        db.close()


def demo(_args):
    lower = {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.5600", "20.00"]]}}
    higher = {"orderbook_fp": {"yes_dollars": [["0.4800", "8.00"], ["0.4300", "30.00"]], "no_dollars": []}}
    print("SYNTHETIC EXAMPLE. Assumes strict nested thresholds and ordinary binary settlement.")
    print(dump({"eight_pairs": threshold_pair_quote(lower, higher, "8", "0.20"),
                "twenty_pairs": threshold_pair_quote(lower, higher, "20"),
                "fee_reserve_note": "$0.20 is an invented total reserve for the 8-pair example; it is not a Kalshi fee estimate."}))


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be >=1")
    return number


def main():
    parser = argparse.ArgumentParser(description="Kalshi research starter: public GET-only collection and exact payoff examples")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Offline synthetic depth/payoff example").set_defaults(run=demo)
    p = sub.add_parser("discover", help="Retrieve series, markets and unreviewed threshold proposals")
    p.add_argument("--series", default="KXCPI")
    p.add_argument("--event", help="Optional exact event ticker; otherwise choose by retrieved volume")
    p.add_argument("--max-pages", type=positive_int, default=10)
    p.add_argument("--max-markets", type=positive_int, default=4)
    p.add_argument("--out", default="data/discovery")
    p.set_defaults(run=discover)
    p = sub.add_parser("collect", help="Sample public REST books; --sweeps 0 runs until Ctrl+C")
    p.add_argument("--watchlist", default="data/discovery/watchlist.json")
    p.add_argument("--db", default="data/orderbooks.sqlite")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--sweeps", type=int, default=12)
    p.set_defaults(run=collect)
    p = sub.add_parser("inspect", help="Report capture quality and latest implied asks")
    p.add_argument("--db", default="data/orderbooks.sqlite")
    p.add_argument("--out")
    p.set_defaults(run=inspect)
    args = parser.parse_args()
    if args.command == "collect" and (args.sweeps < 0 or not 1 <= args.interval <= 3600):
        parser.error("Use --sweeps >=0 and --interval between 1 and 3600 seconds")
    if args.command == "discover" and args.max_markets > 12:
        parser.error("Use --max-markets <=12 for this starter")
    try:
        args.run(args)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
