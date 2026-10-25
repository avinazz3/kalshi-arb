"""Bounded, read-only collection across metadata-discovered contract windows."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import time
from uuid import uuid4

from . import __version__
from .capture import CaptureIncomplete, load_kalshi_auth, run_capture
from .client import Client, RequestDeadline, RequestError
from .discovery import ASSETS, NoActiveMarket, discover, parse_time, validate_universe
from .storage import Store
from .stream_store import inspect_stream


class CollectionFailed(RuntimeError):
    pass


class WindowNotReady(ValueError):
    def __init__(self, reason, close=None):
        super().__init__(reason)
        self.close = close


class Clock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def atomic_json(path, value):
    """Replace only an artifact belonging to this newly created run directory."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _discover_archived(directory, assets, deadline, stop_event):
    store = Store(directory / "metadata.sqlite")
    try:
        # The coordinator owns retry timing. Bound each in-flight request and
        # check the run's monotonic deadline before starting another request.
        return discover(Client(store, timeout=10, retries=0, deadline=deadline, stop_event=stop_event), assets)
    finally:
        store.close()


async def archived_discovery(directory, assets, deadline):
    stop_event = Event()
    task = asyncio.create_task(asyncio.to_thread(_discover_archived, directory, assets, deadline, stop_event))
    try:
        # wait() does not cancel its input task when this caller is cancelled.
        # Unlike shield(), it also has no detached exception-logging callback.
        await asyncio.wait({task})
        return task.result()
    except asyncio.CancelledError:
        stop_event.set()
        # Do not leave a worker mutating the archive after the final report.
        # An in-flight HTTP request is allowed to finish its bounded timeout.
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                # Repeated cancellation must not detach a still-writing thread.
                continue
        if not task.cancelled():
            task.exception()  # Retrieve the worker result; preserve caller cancellation.
        raise


def window_plan(universe, assets, now, seen_markets, minimum_seconds):
    records = validate_universe(universe)
    if set(r["asset"] for r in records) != set(assets):
        raise ValueError("Discovery did not return exactly the requested assets")
    opens = {parse_time(r["market"]["open_time"]) for r in records}
    closes = {parse_time(r["market"]["close_time"]) for r in records}
    if len(opens) != 1 or len(closes) != 1:
        raise WindowNotReady("asset_window_mismatch")
    opened, closed = next(iter(opens)), next(iter(closes))
    if (closed - opened).total_seconds() != 900:
        raise ValueError("Rollover currently requires synchronized 900-second crypto windows")
    if now < opened or now >= closed:
        raise WindowNotReady("stale_or_not_open")
    if any(r["market"].get("status") not in ("active", "open") for r in records):
        raise WindowNotReady("inactive_market")
    if any(r["market"]["ticker"] in seen_markets for r in records):
        raise WindowNotReady("already_attempted_window", closed)
    if (closed - now).total_seconds() - 2 < minimum_seconds:
        raise WindowNotReady("near_expiry", closed)
    return records, closed


def coverage_summary(quality, session, previous_last):
    info = quality["sessions"].get(session, {})
    result = {}
    for venue in ("kalshi", "coinbase"):
        first = info.get("venue_first_frame_mono_ns", {}).get(venue)
        last = info.get("venue_last_frame_mono_ns", {}).get(venue)
        previous = previous_last.get(venue)
        result[venue] = {"frames": info.get("venues", {}).get(venue, 0),
            "first_frame_mono_ns": first, "last_frame_mono_ns": last,
            "first_frame_wall_ns": info.get("venue_first_frame_wall_ns", {}).get(venue),
            "last_frame_wall_ns": info.get("venue_last_frame_wall_ns", {}).get(venue),
            "observed_span_seconds": None if first is None or last is None else (last - first) / 1e9,
            "gap_since_previous_window_seconds": None if first is None or previous is None else (first - previous) / 1e9}
        if last is not None:
            previous_last[venue] = last
    return result


def positive_integer(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(name + " must be a positive integer")


async def run_collection(directory, seconds=3600, *, assets=None, retry_seconds=5,
                         minimum_window_seconds=30, max_discovery_failures=5,
                         clock=None, discover_window=None, capture_window=None, preflight=None):
    """New sessions per window; injectable clock/transports support offline tests."""
    for name, value in (("seconds", seconds), ("retry_seconds", retry_seconds),
                        ("minimum_window_seconds", minimum_window_seconds),
                        ("max_discovery_failures", max_discovery_failures)):
        positive_integer(value, name)
    if seconds > 86400 or minimum_window_seconds > 3600 or seconds < minimum_window_seconds:
        raise ValueError("Run seconds must be <=86400 and >= minimum window seconds (<=3600)")
    assets = list(dict.fromkeys(["BTC", "ETH"] if assets is None else assets))
    if not assets or any(asset not in ASSETS for asset in assets):
        raise ValueError("Choose BTC, ETH or SOL")
    directory = Path(directory)
    if directory.exists():
        raise ValueError("Run directory already exists; choose a new path")
    (preflight or load_kalshi_auth)()  # Fail before creating output or making HTTP calls.
    clock = clock or Clock()
    discover_window = discover_window or archived_discovery
    capture_window = capture_window or run_capture
    started = clock.monotonic()
    deadline = started + seconds
    report = {"schema_version": 1, "package_version": __version__, "run_id": uuid4().hex,
        "status": "running", "synthetic": False, "started_at": clock.now().isoformat(),
        "settings": {"assets": assets, "seconds": seconds, "retry_seconds": retry_seconds,
            "minimum_window_seconds": minimum_window_seconds, "max_discovery_failures": max_discovery_failures},
        "attempts": [], "captured_windows": 0, "incomplete_windows": 0,
        "interpretation": "Frame coverage and rollover audit, not continuous-book certification or trading results."}
    directory.mkdir(parents=True, exist_ok=False)
    report_path = directory / "run.json"
    seen_markets, previous_last, failures = set(), {}, 0
    current, current_dir = None, None

    def checkpoint():
        report["elapsed_seconds"] = max(0, clock.monotonic() - started)
        atomic_json(report_path, report)

    async def pause(delay):
        end = min(deadline, clock.monotonic() + max(0, delay))
        while clock.monotonic() < end:
            await clock.sleep(min(30, end - clock.monotonic()))

    def retain_quality(quality, session):
        atomic_json(current_dir / "quality.json", quality)
        current["session"] = session
        current["quality_file"] = str((current_dir / "quality.json").relative_to(directory))
        current["coverage"] = coverage_summary(quality, session, previous_last)

    checkpoint()
    try:
        while deadline - clock.monotonic() >= minimum_window_seconds:
            current_dir = directory / f"attempt-{len(report['attempts']) + 1:05d}"
            current_dir.mkdir()
            current = {"directory": current_dir.name, "status": "discovering",
                       "started_at": clock.now().isoformat()}
            report["attempts"].append(current)
            checkpoint()
            try:
                universe = await discover_window(current_dir, assets, deadline)
            except NoActiveMarket:
                failures = 0
                current.update(status="waiting", reason="no_active_market")
                checkpoint()
                await pause(retry_seconds)
                continue
            except RequestDeadline:
                current.update(status="skipped", reason="run_budget_exhausted")
                break
            except RequestError as exc:
                current.update(status="discovery_failed", error_type=type(exc).__name__, http_status=exc.status)
                if not exc.retryable:
                    raise
                failures += 1
                checkpoint()
                if failures >= max_discovery_failures:
                    raise CollectionFailed("Metadata retry limit reached") from None
                delay = max(retry_seconds * 2 ** min(failures - 1, 5), exc.retry_after)
                current["retry_delay_seconds"] = delay
                checkpoint()
                await pause(delay)
                continue
            failures = 0
            report["synthetic"] = report["synthetic"] or bool(universe.get("synthetic"))
            atomic_json(current_dir / "universe.json", universe)
            try:
                records, closed = window_plan(universe, assets, clock.now(), seen_markets, minimum_window_seconds)
            except WindowNotReady as exc:
                current.update(status="skipped", reason=str(exc))
                checkpoint()
                await pause(retry_seconds if exc.close is None else max(retry_seconds,
                    (exc.close - clock.now()).total_seconds() + 0.25))
                continue
            remaining = deadline - clock.monotonic()
            duration = min(3600, remaining, (closed - clock.now()).total_seconds() - 2)
            if duration < minimum_window_seconds:
                current.update(status="skipped", reason="insufficient_remaining_budget")
                break
            current.update(status="capturing", requested_seconds=duration,
                planned_stop_reason="run_budget" if remaining <= (closed - clock.now()).total_seconds() - 2 else "contract_close_guard",
                markets={r["asset"]: r["market"]["ticker"] for r in records},
                contract_open=records[0]["market"]["open_time"], contract_close=closed.isoformat())
            # One attempt per ticker in a run, including a capture lacking snapshots.
            seen_markets.update(current["markets"].values())
            checkpoint()
            try:
                result = await capture_window(current_dir / "capture.sqlite", duration, universe=universe)
            except CaptureIncomplete as exc:
                retain_quality(exc.quality, exc.session)
                current.update(status="incomplete", reason="missing_required_snapshots")
                report["incomplete_windows"] += 1
            else:
                retain_quality(result["quality"], result["session"])
                current["status"] = "captured"
                report["captured_windows"] += 1
            current["ended_at"] = clock.now().isoformat()
            checkpoint()
            # The old recorder stops 2s before close. Rediscover after actual close;
            # never manufacture a next ticker or repeatedly capture the old one.
            await pause(max(0, (closed - clock.now()).total_seconds() + 0.25))
        report["status"] = "completed" if report["captured_windows"] else "no_complete_windows"
        report["stop_reason"] = "run_budget_exhausted" if clock.monotonic() >= deadline else "insufficient_remaining_budget"
    except asyncio.CancelledError:
        report["status"] = "interrupted"
        if current and current["status"] in ("discovering", "capturing"):
            current["status"] = "interrupted"
        raise
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if current and current["status"] in ("discovering", "capturing"):
            current.update(status="failed", error_type=type(exc).__name__)
        # Persist types/status codes only; exception strings may include secrets.
        raise CollectionFailed(f"Collection stopped ({type(exc).__name__}); inspect {report_path}") from None
    finally:
        if current_dir is not None and (current_dir / "capture.sqlite").exists() and "quality_file" not in current:
            try:
                quality = inspect_stream(current_dir / "capture.sqlite")
                if len(quality["sessions"]) == 1:
                    retain_quality(quality, next(iter(quality["sessions"])))
            except Exception as exc:
                current["quality_error_type"] = type(exc).__name__
        report["ended_at"] = clock.now().isoformat()
        checkpoint()
    if report["status"] == "no_complete_windows":
        raise CollectionFailed(f"No window captured with all required snapshots; inspect {report_path}")
    return {"report": str(report_path), **report}
