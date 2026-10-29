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
    p.add_argument("--run-dir", required=True, help="Dataset directory; existing runs require --resume")
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--resume", action="store_true", help="Continue missing index hours after verifying frozen contracts, quotes and saved files")
    p.add_argument("--retries", type=int, choices=(0, 1, 2), default=2, help="Extra attempts for transient GET failures, default 2")
    p = sub.add_parser("audit", help="Offline settlement reconstruction under four explicit sampling/rounding hypotheses")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--out", help="New audit JSON file; defaults to run-dir/settlement_audit.json")
    p = sub.add_parser("forecast", help="Offline rolling/EWMA settlement forecasts and matched scoring")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--lookback", type=int, default=60)
    p.add_argument("--half-life", type=float, default=15)
    p.add_argument("--paths", type=int, default=2000)
    p.add_argument("--seed", type=int, default=1729)
    p = sub.add_parser("analyze", help="Offline diagnostics of existing forecasts; no refitting or simulation")
    p.add_argument("--run-dir", required=True, help="Historical dataset used by the forecasts")
    p.add_argument("--forecast-dir", required=True, help="Finished forecast run with saved predictions and evaluation")
    p.add_argument("--out-dir", required=True, help="New directory for diagnostics")
    p = sub.add_parser("compare", help="Pool verified analysis runs with daily scores and day-block uncertainty")
    p.add_argument("--analysis-dirs", nargs="+", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--block-days", type=int, default=2)
    p.add_argument("--bootstrap-samples", type=int, default=2000)
    p.add_argument("--bootstrap-seed", type=int, default=314159)
    p = sub.add_parser("batch", help="Download, audit, forecast and compare consecutive UTC days with verified reuse")
    p.add_argument("--start", required=True)
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--root-dir", default="data")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--paths", type=int, default=2000)
    p = sub.add_parser("shadow", help="Bounded live REST recording and hypothetical forecasts; no orders")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--minutes", type=int, default=60)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--paths", type=int, default=2000)
    p.add_argument("--no-bootstrap-history", action="store_true")
    p = sub.add_parser("paper", help="Offline latency/depth/fee scenarios on a finished shadow recording")
    p.add_argument("--shadow-dir", required=True)
    p.add_argument("--run-dir", action="append", required=True, help="Historical label dataset; repeat for multiple days")
    p.add_argument("--policy", required=True, help="Explicit hypothetical fee and fill assumptions JSON")
    p.add_argument("--out-dir", required=True)
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
                                   warmup_hours=args.warmup_hours, timeout=args.timeout, retries=args.retries, resume=args.resume)
        elif args.command == "audit":
            from .settlement import audit_dataset
            args.out = args.out or str(Path(args.run_dir) / "settlement_audit.json")
            if Path(args.out).exists():
                raise ValueError("Output already exists; choose a new path")
            result = audit_dataset(args.run_dir)
        elif args.command == "forecast":
            from .forecast import run_forecast
            def progress(n, total):
                if n % 4 == 0 or n == total:
                    print(f"Forecasted {n}/{total} contracts", file=sys.stderr)
            result = run_forecast(args.run_dir, args.out_dir, lookback=args.lookback,
                                  half_life=args.half_life, paths=args.paths, seed=args.seed, progress=progress)
        elif args.command == "analyze":
            from .analysis import run_analysis
            result = run_analysis(args.run_dir, args.forecast_dir, args.out_dir)
        elif args.command == "compare":
            from .compare import run_compare
            result = run_compare(args.analysis_dirs, args.out_dir, block_days=args.block_days,
                                 samples=args.bootstrap_samples, seed=args.bootstrap_seed)
        elif args.command == "batch":
            from .batch import run_batch
            result = run_batch(args.start, args.days, args.root_dir, args.out_dir, paths=args.paths,
                               progress=lambda n, total: print(f"Completed {n}/{total} UTC days", file=sys.stderr))
        elif args.command == "shadow":
            from .shadow import run_shadow
            result = run_shadow(args.run_dir, minutes=args.minutes, interval=args.interval, timeout=args.timeout,
                                paths=args.paths, bootstrap_history=not args.no_bootstrap_history,
                                progress=lambda n, p: print(f"Recorded {n} observations; {p} forecasts", file=sys.stderr))
        elif args.command == "paper":
            from .paper import run_paper
            result = run_paper(args.shadow_dir, args.run_dir, args.policy, args.out_dir)
        else:
            document = json.loads(Path(args.input).read_text(), parse_float=str)
            result = calculate_orders(document, args.balance_precision)
        if getattr(args, "out", None):
            write_json(args.out, result, exclusive=args.command in ("digital", "audit"))
        if args.command == "history":
            print(dump({key: result[key] for key in ("status", "settings", "issues")} | {
                "contracts": result.get("contracts"), "requests": len(result["requests"]),
                "index_hours": len(result["index_hours"]), "failure": result.get("failure"),
                "requests_this_run": result["requests_this_run"], "reused_index_hours": result["reused_index_hours"],
                "request_policy": result["request_policy"],
                "manifest": str(Path(args.run_dir) / "manifest.json")}))
        elif args.command == "audit":
            print(dump({key: result[key] for key in ("status", "contracts", "rule_review_required", "selected_candidate")} | {
                "candidates": [{key: c[key] for key in ("boundary", "rounding", "counts")} for c in result["candidates"]],
                "report": args.out}))
        elif args.command == "forecast":
            print(dump({k: result[k] for k in ("status", "forecast_rows", "paired_rows", "planned_rows", "scores", "exclusion_counts")} | {"report": str(Path(args.out_dir)/"report.json")}))
        elif args.command == "analyze":
            print(dump({k: result[k] for k in ("status", "rows", "contracts", "source_status", "unpaired_predictions", "scores")} | {"summary": str(Path(args.out_dir)/"summary.md"), "report": str(Path(args.out_dir)/"report.json")}))
        elif args.command == "compare":
            print(dump({k: result[k] for k in ("status", "rows", "contracts", "utc_days", "scores", "bootstrap")} | {"summary": str(Path(args.out_dir)/"summary.md")}))
        elif args.command == "batch":
            print(dump(result))
        elif args.command == "shadow":
            print(dump(result))
        else:
            print(dump(result))
        expected = {"diagnose": "access_checks_passed", "history": "downloaded", "audit": "computed", "forecast": "completed", "shadow": "completed"}
        return 2 if args.command in expected and result["status"] != expected[args.command] else 0
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, DecimalException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
