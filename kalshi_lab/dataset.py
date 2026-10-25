"""Receipt-time as-of features and separately constructed future quote labels."""
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from . import __version__
from .books import ZERO
from .discovery import parse_time, validate_universe
from .replay import Timeline, load_replay

NS_MS, NS_SECOND = 1_000_000, 1_000_000_000


class Rejected(ValueError):
    pass


def unix_ns(value):
    from datetime import datetime, timezone
    delta = parse_time(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * NS_SECOND + delta.microseconds * 1000


class Health:
    """Frame activity is a conservative liveness proxy, distinct from book age."""
    def __init__(self, activity, max_silence_ns):
        self.max_silence_ns = max_silence_ns
        self.series = Timeline()
        previous, epoch = None, 0
        for timestamp in activity.times:
            if previous is not None and timestamp - previous > max_silence_ns:
                epoch += 1
            self.series.add(timestamp, (timestamp, epoch))
            previous = timestamp

    def check(self, left, right):
        a, b = self.series.at(left), self.series.at(right)
        if (a is None or b is None or a[1] != b[1]
                or left - a[0] > self.max_silence_ns or right - b[0] > self.max_silence_ns):
            raise Rejected("feed_silence")


def continuous(series, left, right):
    a, b = series.at(left), series.at(right)
    if a is None or b is None or a.quote is None or b.quote is None:
        raise Rejected("invalid_book")
    if a.epoch != b.epoch:
        raise Rejected("book_reset")
    return a, b


def flow_available(series, left, right):
    a, b = series.at(left), series.at(right)
    if a is None or b is None or not a[1] or not b[1] or a[0] != b[0]:
        raise Rejected("trade_flow_unavailable")


def positive_integer(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(name + " must be a positive integer")
    return value


def build_dataset(path, *, session=None, grid_ms=1000, lookback_ms=1000,
                  horizons_ms=(1000, 5000), max_silence_ms=5000):
    for name, value in (("grid_ms", grid_ms), ("lookback_ms", lookback_ms),
                        ("max_silence_ms", max_silence_ms)):
        positive_integer(value, name)
    if not horizons_ms or len(set(horizons_ms)) != len(horizons_ms):
        raise ValueError("Choose nonempty, unique label horizons")
    horizons_ms = sorted(positive_integer(h, "horizon") for h in horizons_ms)
    grid, lookback = grid_ms * NS_MS, lookback_ms * NS_MS
    horizons = [h * NS_MS for h in horizons_ms]
    replay = load_replay(path, session)
    if replay.configuration.get("reference_only"):
        raise ValueError("A joint Kalshi/Coinbase capture is required")
    records = validate_universe(replay.configuration.get("universe"))
    if replay.status not in ("completed", "synthetic_completed"):
        raise ValueError("Dataset requires a completed capture with a finish marker")
    if replay.max_clock_deviation_ns > 250 * NS_MS:
        raise ValueError("Wall/monotonic clocks deviate by over 250ms; expiry alignment is uncertain")
    if any(not series.times for series in replay.activity.values()):
        raise ValueError("Both venues must have frames")
    health = {v: Health(s, max_silence_ms * NS_MS) for v, s in replay.activity.items()}
    # Never extend the sample beyond the last observed frame of either venue.
    start = ((max(s.times[0] for s in replay.activity.values()) + grid - 1) // grid) * grid
    end = min(s.times[-1] for s in replay.activity.values())
    grid_count = max(0, (end - max(horizons) - start) // grid + 1)
    if grid_count * len(records) > 1_000_000:
        raise ValueError("Over one million candidate rows; choose a coarser grid")
    rows, rejected = [], Counter()
    anchor_mono, anchor_wall = replay.first_mono_ns, replay.first_wall_ns
    for record in records:
        asset, product = record["asset"], record["coinbase_product"]
        ticker = record["market"]["ticker"]
        opened, closed = (unix_ns(record["market"][key]) for key in ("open_time", "close_time"))
        kalshi = replay.points.get(("kalshi", ticker), Timeline())
        coinbase = replay.points.get(("coinbase", product), Timeline())
        flow = replay.flow.get(product, Timeline())
        for t in range(start, start + grid_count * grid, grid):
            wall = anchor_wall + t - anchor_mono
            try:
                if wall - lookback < opened or wall + max(horizons) >= closed:
                    raise Rejected("outside_contract_window")
                # Features are built first, with no access to any future state.
                for series in health.values():
                    series.check(t - lookback, t)
                k0, k = continuous(kalshi, t - lookback, t)
                c0, c = continuous(coinbase, t - lookback, t)
                flow_available(flow, t - lookback, t)
                trades = replay.trades.get(product, Timeline()).between(t - lookback, t)
                ofi = replay.ofi.get(product, Timeline()).between(t - lookback, t)
                features = {
                    "kalshi_midpoint": k.quote.midpoint, "kalshi_spread": k.quote.spread,
                    "kalshi_bid_size": k.quote.bid_size, "kalshi_ask_size": k.quote.ask_size,
                    "kalshi_queue_imbalance": k.quote.imbalance,
                    "kalshi_midpoint_change": k.quote.midpoint - k0.quote.midpoint,
                    "coinbase_midpoint": c.quote.midpoint, "coinbase_spread": c.quote.spread,
                    "coinbase_return": c.quote.midpoint / c0.quote.midpoint - 1,
                    "coinbase_queue_imbalance": c.quote.imbalance,
                    "coinbase_message_ofi": sum(ofi, ZERO),
                    "signed_aggressive_notional_usd": sum((v[0] for v in trades), ZERO),
                    "total_trade_notional_usd": sum((v[1] for v in trades), ZERO),
                    "trade_count": len(trades),
                    "seconds_to_close": Decimal(closed - wall) / NS_SECOND,
                }
                targets = {}
                for horizon_ms, h in zip(horizons_ms, horizons):
                    for series in health.values():
                        series.check(t, t + h)
                    _, future = continuous(kalshi, t, t + h)
                    continuous(coinbase, t, t + h)
                    targets[f"midpoint_change_{horizon_ms}ms"] = future.quote.midpoint - k.quote.midpoint
                rows.append({"session": replay.session, "asset": asset, "market_ticker": ticker,
                    "coinbase_product": product, "decision_mono_ns": t, "decision_wall_ns": wall,
                    "feature_window_start_ns": t - lookback,
                    "features_available_through_ns": max(replay.activity[v].at(t)[0] for v in health),
                    "source_event_id_through_decision": max(replay.activity[v].at(t)[1] for v in health),
                    "label_end_mono_ns": {str(ms): t + h for ms, h in zip(horizons_ms, horizons)},
                    "features": {key: str(value) if isinstance(value, Decimal) else value for key, value in features.items()},
                    "targets": {key: str(value) for key, value in targets.items()}})
            except Rejected as exc:
                rejected[str(exc)] += 1
    rows.sort(key=lambda row: (row["decision_mono_ns"], row["asset"]))
    manifest = {"schema_version": 1, "package_version": __version__,
        "session": replay.session, "source_digest": replay.source_digest,
        "synthetic": bool(replay.configuration.get("synthetic")),
        "settings": {"grid_ms": grid_ms, "lookback_ms": lookback_ms,
                     "horizons_ms": horizons_ms, "max_silence_ms": max_silence_ms},
        "candidate_rows": grid_count * len(records), "accepted_rows": len(rows),
        "rejected_rows": dict(rejected), "replay_diagnostics": dict(replay.diagnostics),
        "clock_anchor": {"mono_ns": anchor_mono, "wall_ns": anchor_wall,
                         "max_deviation_ns": replay.max_clock_deviation_ns},
        "interpretation": "Research features and quote-change labels; no estimator, fills or P&L. See docs/DATASET.md."}
    return rows, manifest


def export_dataset(path, output, **settings):
    output = Path(output)
    manifest_path = Path(str(output) + ".manifest.json")
    if output.resolve() == Path(path).resolve():
        raise ValueError("Output must differ from raw database")
    if output.exists() or manifest_path.exists():
        raise ValueError("Dataset output already exists; choose a new path")
    rows, manifest = build_dataset(path, **settings)
    payload = "".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in rows).encode()
    manifest["dataset_sha256"] = hashlib.sha256(payload).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves any earlier research artifact.
    created = []
    try:
        with output.open("xb") as handle:
            created.append(output)
            handle.write(payload)
        with manifest_path.open("x", encoding="utf-8") as handle:
            created.append(manifest_path)
            json.dump(manifest, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
    except OSError:
        for artifact in created:
            artifact.unlink(missing_ok=True)
        raise
    return {"dataset": str(output), "manifest": str(manifest_path), **manifest}
