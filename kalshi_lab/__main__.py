"""Crypto capture, reconstruction, research datasets and fee accounting."""
import argparse
import asyncio
from decimal import DecimalException
import json
from pathlib import Path
import sqlite3
import sys

from .client import Client
from .demo import create_demo
from .discovery import ASSETS, discover
from .fees import calculate_orders
from .storage import Store
from .stream_store import inspect_stream


def dump(value):
    return json.dumps(value, indent=2, default=str, allow_nan=False)


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dump(value) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Crypto Flow Lab: read-only research data and fee accounting")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("discover", help="Archive current crypto series/event/market metadata")
    p.add_argument("--assets", nargs="+", choices=tuple(ASSETS), default=["BTC", "ETH"])
    p.add_argument("--out", default="data/universe.json")
    p.add_argument("--db", default="data/metadata.sqlite")
    p = sub.add_parser("capture", help="Record shared-clock WebSocket frames for one market window")
    p.add_argument("--universe", default="data/universe.json")
    p.add_argument("--db", default="data/capture.sqlite")
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--reference-only", action="store_true", help="Public Coinbase-only setup test; cannot evaluate cross-venue signals")
    p.add_argument("--assets", nargs="+", choices=tuple(ASSETS), help="Asset override for reference-only mode")
    p = sub.add_parser("inspect", help="Verify raw stream hashes, counts and sequence continuity")
    p.add_argument("--db", default="data/capture.sqlite")
    p.add_argument("--out")
    p = sub.add_parser("demo", help="Create and inspect a small synthetic stream fixture")
    p.add_argument("--db", default="data/demo.sqlite")
    p = sub.add_parser("research-demo", help="Create a synthetic, hand-checkable research capture")
    p.add_argument("--db", default="data/research_demo.sqlite")
    p = sub.add_parser("replay", help="Reconstruct books and report invalid states")
    p.add_argument("--db", default="data/capture.sqlite")
    p.add_argument("--session")
    p.add_argument("--out")
    p = sub.add_parser("dataset", help="Export as-of features, future quote labels and an audit manifest")
    p.add_argument("--db", default="data/capture.sqlite")
    p.add_argument("--session")
    p.add_argument("--out", default="data/research.jsonl")
    p.add_argument("--grid-ms", type=int, default=1000)
    p.add_argument("--lookback-ms", type=int, default=1000)
    p.add_argument("--horizons-ms", nargs="+", type=int, default=[1000, 5000])
    p.add_argument("--max-silence-ms", type=int, default=5000)
    p = sub.add_parser("fees", help="Calculate fees from explicit policy and ordered fills")
    p.add_argument("--input", required=True)
    p.add_argument("--balance-precision", required=True, choices=("0.0001", "0.01"))
    p.add_argument("--out")
    args = parser.parse_args()
    try:
        if (getattr(args, "out", None) and getattr(args, "db", None)
                and Path(args.out).resolve() == Path(args.db).resolve()):
            raise ValueError("Output must differ from raw database")
        if args.command == "discover":
            store = Store(args.db)
            try:
                result = discover(Client(store), args.assets)
            finally:
                store.close()
            write_json(args.out, result)
            result = {"manifest": args.out, "assets": [
                {"asset": r["asset"], "market": r["market"]["ticker"], "close_time": r["market"]["close_time"]}
                for r in result["assets"]]}
        elif args.command == "capture":
            from .capture import run_capture
            universe = None if args.reference_only else json.loads(Path(args.universe).read_text())
            result = asyncio.run(run_capture(args.db, args.seconds, universe=universe,
                                 reference_only=args.reference_only, assets=args.assets))
        elif args.command == "inspect":
            result = inspect_stream(args.db)
        elif args.command == "demo":
            result = create_demo(args.db)
        elif args.command == "research-demo":
            from .research_demo import create_research_demo
            result = create_research_demo(args.db)
        elif args.command == "replay":
            from .replay import load_replay
            result = load_replay(args.db, args.session).report()
        elif args.command == "dataset":
            from .dataset import export_dataset
            result = export_dataset(args.db, args.out, session=args.session,
                grid_ms=args.grid_ms, lookback_ms=args.lookback_ms,
                horizons_ms=args.horizons_ms, max_silence_ms=args.max_silence_ms)
        else:
            document = json.loads(Path(args.input).read_text(), parse_float=str)
            result = calculate_orders(document, args.balance_precision)
        if getattr(args, "out", None) and args.command not in ("discover", "dataset"):
            write_json(args.out, result)
        print(dump(result))
        return 0
    except KeyboardInterrupt:
        print("Stopped; committed raw data remains available.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error, DecimalException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
