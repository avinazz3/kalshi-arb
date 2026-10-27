"""Binary-contract probability research, read-only data and fee accounting."""
import argparse
from decimal import DecimalException
import json
from pathlib import Path
import sys

from .fees import calculate_orders


def dump(value):
    return json.dumps(value, indent=2, default=str, allow_nan=False)


def write_json(path, value, *, exclusive=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("x" if exclusive else "w", encoding="utf-8") as handle:
        handle.write(dump(value) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Lab: settlement probability research and read-only data")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("digital", help="Offline physical-GBM terminal and averaged-payoff probability benchmark")
    p.add_argument("--input", required=True)
    p.add_argument("--out", help="New JSON output file; existing files are preserved")
    p = sub.add_parser("diagnose", help="Bounded BTC metadata, quotes and CF index data-access checks")
    p.add_argument("--run-dir", required=True, help="New directory for the report and compact index samples")
    p.add_argument("--timeout", type=float, default=10, help="Socket timeout per HTTP request, 1 to 30 seconds; no retries")
    p = sub.add_parser("fees", help="Calculate fees from explicit policy and ordered fills")
    p.add_argument("--input", required=True)
    p.add_argument("--balance-precision", required=True, choices=("0.0001", "0.01"))
    p.add_argument("--out")
    args = parser.parse_args()
    try:
        if args.command == "digital":
            from .digital import benchmark
            if args.out and Path(args.out).exists():
                raise ValueError("Output already exists; choose a new path")
            document = json.loads(Path(args.input).read_text(), parse_float=str)
            result = benchmark(document)
        elif args.command == "diagnose":
            from .diagnostics import run_diagnostic
            result = run_diagnostic(args.run_dir, timeout=args.timeout)
        else:
            document = json.loads(Path(args.input).read_text(), parse_float=str)
            result = calculate_orders(document, args.balance_precision)
        if getattr(args, "out", None):
            write_json(args.out, result, exclusive=args.command == "digital")
        print(dump(result))
        return 2 if args.command == "diagnose" and result["status"] != "access_checks_passed" else 0
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, DecimalException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
