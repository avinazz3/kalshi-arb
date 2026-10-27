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
    p = sub.add_parser("history", help="Bounded retrospective BTC contracts, quotes and BRTI hours")
    p.add_argument("--start", required=True, help="Timezone-aware start, aligned to a whole UTC hour")
    p.add_argument("--hours", type=int, default=24, help="Contract interval, 1 to 24 hours")
    p.add_argument("--warmup-hours", type=int, default=1, help="Prior index history, 1 to 24 hours")
    p.add_argument("--run-dir", required=True, help="New dataset directory")
    p.add_argument("--timeout", type=float, default=10)
    p = sub.add_parser("audit", help="Offline settlement reconstruction under four explicit sampling/rounding hypotheses")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--out", help="New audit JSON file; defaults to run-dir/settlement_audit.json")
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
        elif args.command == "history":
            from .history import fetch_history
            result = fetch_history(args.run_dir, start=args.start, hours=args.hours,
                                   warmup_hours=args.warmup_hours, timeout=args.timeout)
        elif args.command == "audit":
            from .settlement import audit_dataset
            args.out = args.out or str(Path(args.run_dir) / "settlement_audit.json")
            if Path(args.out).exists():
                raise ValueError("Output already exists; choose a new path")
            result = audit_dataset(args.run_dir)
        else:
            document = json.loads(Path(args.input).read_text(), parse_float=str)
            result = calculate_orders(document, args.balance_precision)
        if getattr(args, "out", None):
            write_json(args.out, result, exclusive=args.command in ("digital", "audit"))
        if args.command == "history":
            print(dump({key: result[key] for key in ("status", "settings", "issues")} | {
                "contracts": result.get("contracts"), "requests": len(result["requests"]),
                "index_hours": len(result["index_hours"]), "failure": result.get("failure"),
                "manifest": str(Path(args.run_dir) / "manifest.json")}))
        elif args.command == "audit":
            print(dump({key: result[key] for key in ("status", "contracts", "rule_review_required", "selected_candidate")} | {
                "candidates": [{key: c[key] for key in ("boundary", "rounding", "counts")} for c in result["candidates"]],
                "report": args.out}))
        else:
            print(dump(result))
        expected = {"diagnose": "access_checks_passed", "history": "downloaded", "audit": "computed"}
        return 2 if args.command in expected and result["status"] != expected[args.command] else 0
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, DecimalException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
