"""Bounded GET-only BTC data-access sample. No account or order endpoints.

Only allowlisted public market fields and typed CF index samples are persisted.
HTTP response bodies, headers, credential identifiers and exception text are not.
Historical index values are retrospective and cannot certify availability as of
an old decision time. This is a feasibility probe, not a backtest dataset.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .auth import load_kalshi_auth
from .markets import BASE_URL, parse_time, select_active
from .numbers import decimal

MAX_BYTES = 8 * 1024 * 1024
SERIES = "KXBTC15M"
INDEX = "BRTI"
PUBLIC_PATHS = {"/markets", "/historical/markets"}
INDEX_PATHS = {"/cfbenchmarks/latest_values", "/cfbenchmarks/history/values"}
CANDLE_PATH = re.compile(r"/(?:series/KXBTC15M/markets|historical/markets)/KXBTC15M-[A-Za-z0-9-]+/candlesticks\Z")


class ProbeFailure(RuntimeError):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason, self.status = reason, status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib can forward custom authentication headers on a redirect.
        return None


@dataclass
class Response:
    data: dict
    sha256: str
    received_at: str


class ProbeHTTP:
    def __init__(self, signer=None, timeout=10):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
            raise ValueError("timeout must be from 1 to 30 seconds")
        self.signer, self.timeout = signer, timeout
        self.opener = build_opener(NoRedirect())

    def allowed_path(self, path):
        return path in PUBLIC_PATHS | INDEX_PATHS or bool(CANDLE_PATH.fullmatch(path))

    def get(self, path, params=None):
        if not self.allowed_path(path):
            raise ValueError("Endpoint is outside the read-only diagnostic allowlist")
        headers = {"Accept": "application/json", "User-Agent": "kalshi-lab/" + __version__}
        if path in INDEX_PATHS:
            if self.signer is None:
                raise ProbeFailure("credentials_unavailable")
            headers.update(self.signer("/trade-api/v2" + path))
        url = BASE_URL + path + ("?" + urlencode(params) if params else "")
        try:
            with self.opener.open(Request(url, headers=headers, method="GET"), timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise ProbeFailure("http_error", response.status)
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            # Never expose response text: upstream errors may echo request data.
            raise ProbeFailure("http_error", status) from None
        except (URLError, TimeoutError, OSError):
            raise ProbeFailure("network_error") from None
        if len(raw) > MAX_BYTES:
            raise ProbeFailure("response_size_limit")
        try:
            data = json.loads(raw, parse_float=str)
        except (ValueError, UnicodeError):
            raise ProbeFailure("invalid_json") from None
        if not isinstance(data, dict):
            raise ProbeFailure("unexpected_schema")
        return Response(data, hashlib.sha256(raw).hexdigest(), datetime.now(timezone.utc).isoformat())


def market_summary(market):
    ticker = market["ticker"]
    if not re.fullmatch(r"KXBTC15M-[A-Za-z0-9-]+", ticker) or market.get("market_type") != "binary":
        raise ProbeFailure("unexpected_market")
    if decimal(market.get("notional_value_dollars", "0")) != 1:
        raise ProbeFailure("unexpected_payout")
    opened, closed = parse_time(market["open_time"]), parse_time(market["close_time"])
    if (closed - opened).total_seconds() != 900:
        raise ProbeFailure("unexpected_contract_duration")
    reference = decimal(market["floor_strike"])
    if reference <= 0 or market.get("strike_type") not in ("greater", "greater_or_equal"):
        raise ProbeFailure("missing_or_unsupported_reference")
    if any(not isinstance(market.get(k), str) or not market[k].strip() for k in ("rules_primary", "rules_secondary")):
        raise ProbeFailure("missing_rules")
    result = {"ticker": ticker, "open_time": opened.isoformat(), "close_time": closed.isoformat(),
              "reference": str(reference), "comparison": market["strike_type"],
              "rules_primary": market["rules_primary"], "rules_secondary": market["rules_secondary"],
              "rule_review_required": True}
    # A missing, zero-sized or crossed quote is not an executable observation.
    try:
        bid, ask = decimal(market["yes_bid_dollars"]), decimal(market["yes_ask_dollars"])
        bid_size, ask_size = decimal(market["yes_bid_size_fp"]), decimal(market["yes_ask_size_fp"])
        valid = 0 <= bid <= ask <= 1 and bid_size > 0 and ask_size > 0
    except (KeyError, ValueError):
        valid = False
    result["two_sided_quote_present"] = valid
    if valid:
        result.update(yes_bid=str(bid), yes_ask=str(ask), bid_size=str(bid_size), ask_size=str(ask_size))
    if market.get("result") in ("yes", "no"):
        result["result"] = market["result"]
    return result


def index_points(document, *, latest=False):
    upstream = document.get("data")
    if not isinstance(upstream, dict):
        raise ProbeFailure("unexpected_index_schema")
    if upstream.get("error"):
        raise ProbeFailure("upstream_error")
    payload = upstream.get("payload")
    if latest:
        if not isinstance(payload, dict) or not isinstance(payload.get("latest_values"), dict):
            raise ProbeFailure("unexpected_index_schema")
        point = payload["latest_values"].get(INDEX)
        payload = [] if point is None else [point]
    if not isinstance(payload, list) or not payload:
        raise ProbeFailure("empty_or_unrecognised_index_payload")
    points = []
    for point in payload:
        if not isinstance(point, dict):
            raise ProbeFailure("unexpected_index_schema")
        timestamp, value = point.get("time"), decimal(point.get("value"))
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or not 0 < timestamp < 2**63 or value <= 0:
            raise ProbeFailure("invalid_index_sample")
        amend = point.get("amendTime")
        repeat = point.get("repeatOfPreviousValue", False)
        if (amend is not None and (isinstance(amend, bool) or not isinstance(amend, int) or not 0 < amend < 2**63)) or not isinstance(repeat, bool):
            raise ProbeFailure("invalid_index_flags")
        points.append({"time_ms": timestamp, "value": str(value), "amend_time_ms": amend,
                       "repeat_of_previous_value": repeat})
    if any(a["time_ms"] >= b["time_ms"] for a, b in zip(points, points[1:])):
        raise ProbeFailure("unordered_or_duplicate_index_samples")
    return points


def candle_summary(document):
    candles = document.get("candlesticks")
    if not isinstance(candles, list) or not candles:
        raise ProbeFailure("empty_candles")
    quoted = 0
    for candle in candles:
        bid = candle.get("yes_bid", {}).get("close_dollars")
        ask = candle.get("yes_ask", {}).get("close_dollars")
        if bid is not None and ask is not None and 0 <= decimal(bid) <= decimal(ask) <= 1:
            quoted += 1
    if not quoted:
        raise ProbeFailure("no_two_sided_candle_prices")
    return {"candles": len(candles), "with_two_sided_closing_prices": quoted,
            "interpretation": "Aggregated historical prices, not historical executable depth."}


def run_diagnostic(run_dir, *, timeout=10, http=None, now=None):
    """At most seven bounded requests, no pagination, retry loops or live orders."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
        raise ValueError("timeout must be from 1 to 30 seconds")
    directory = Path(run_dir)
    if directory.exists():
        raise ValueError("Diagnostic run directory already exists; choose a new path")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    directory.mkdir(parents=True, exist_ok=False)
    if http is None:
        try:
            signer = load_kalshi_auth()
        except (ValueError, RuntimeError):
            signer = None
        http = ProbeHTTP(signer, timeout)
    report = {"schema_version": 1, "package_version": __version__, "started_at": now.isoformat(),
              "status": "running", "asset": "BTC", "index_id": INDEX, "checks": {},
              "interpretation": "Small access sample only; not data completeness, rule certification or trading results.",
              "historical_availability": "Retrospective index values may be revised; receipt at past decision times is not established.",
              "rule_review_required": True}

    def checkpoint():
        temporary = directory / "diagnostic.json.tmp"
        temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(directory / "diagnostic.json")

    def check(name, path, params, validate):
        item = {"path": path, "params": params}
        report["checks"][name] = item
        try:
            response = http.get(path, params)
            item.update(received_at=response.received_at, response_sha256=response.sha256)
            summary = validate(response.data)
            item.update(status="passed", summary=summary)
            return summary
        except ProbeFailure as exc:
            item.update(status="blocked", reason=exc.reason, http_status=exc.status)
        except (ValueError, KeyError, TypeError, AttributeError):
            item.update(status="blocked", reason="unexpected_or_invalid_schema")
        finally:
            checkpoint()
        return None

    checkpoint()
    active = check("active_contract", "/markets", {"series_ticker": SERIES, "status": "open", "limit": 20, "mve_filter": "exclude"},
                   lambda d: market_summary(select_active(d["markets"], SERIES, now)))
    if active is not None and not active["two_sided_quote_present"]:
        report["checks"]["active_contract"]["status"] = "blocked"
        report["checks"]["active_contract"]["reason"] = "no_two_sided_quote"

    def settled_sample(document):
        markets = [m for m in document["markets"] if m.get("result") in ("yes", "no") and parse_time(m["close_time"]) <= now]
        if not markets:
            raise ProbeFailure("no_settled_contract_in_sample")
        return market_summary(max(markets, key=lambda m: parse_time(m["close_time"])))

    settled = check("settled_contract", "/markets", {"series_ticker": SERIES, "status": "settled", "limit": 5, "mve_filter": "exclude"}, settled_sample)
    historical = False
    if settled is None:
        settled = check("archived_contract", "/historical/markets", {"series_ticker": SERIES, "limit": 5, "mve_filter": "exclude"}, settled_sample)
        historical = settled is not None
    if settled:
        prefix = "/historical/markets" if historical else "/series/KXBTC15M/markets"
        path = prefix + "/" + settled["ticker"] + "/candlesticks"
        check("historical_quotes", path,
              {"start_ts": int(parse_time(settled["open_time"]).timestamp()),
               "end_ts": int(parse_time(settled["close_time"]).timestamp()), "period_interval": 1}, candle_summary)
    else:
        report["checks"]["historical_quotes"] = {"status": "blocked", "reason": "no_settled_contract"}

    def save_points(document, filename, latest=False, bounds=None):
        points = index_points(document, latest=latest)
        if bounds and any(not bounds[0] <= p["time_ms"] < bounds[1] for p in points):
            raise ProbeFailure("index_samples_outside_requested_hour")
        observed_at = parse_time(report["checks"]["current_index"]["received_at"]) if latest else now
        age = observed_at.timestamp() - points[-1]["time_ms"] / 1000
        if latest and not -5 <= age <= 30:
            raise ProbeFailure("stale_or_future_index_value")
        with (directory / filename).open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=points[0].keys())
            writer.writeheader()
            writer.writerows(points)
        gaps = [(b["time_ms"] - a["time_ms"]) / 1000 for a, b in zip(points, points[1:])]
        return {"samples": len(points), "first_time_ms": points[0]["time_ms"], "last_time_ms": points[-1]["time_ms"],
                "max_gap_seconds": max(gaps, default=None), "last_sample_age_seconds": age,
                "amended_samples": sum(p["amend_time_ms"] is not None for p in points),
                "repeated_samples": sum(p["repeat_of_previous_value"] for p in points), "file": filename}

    check("current_index", "/cfbenchmarks/latest_values", {"id": INDEX, "maxResolution": "PER_SECOND"},
          lambda d: save_points(d, "index_current.csv", latest=True))
    hour = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    lower = int(hour.timestamp() * 1000)
    check("historical_index", "/cfbenchmarks/history/values",
          {"id": INDEX, "timespan": "HOUR", "timestamp": hour.isoformat(timespec="milliseconds").replace("+00:00", "Z")},
          lambda d: save_points(d, "index_history.csv", bounds=(lower, lower + 3600000)))
    # A successful fallback supersedes a failed recent-settled sample only.
    required = ["active_contract", "historical_quotes", "current_index", "historical_index"]
    settled_ok = settled is not None
    report["status"] = "access_checks_passed" if settled_ok and all(report["checks"][k]["status"] == "passed" for k in required) else "blocked"
    report["ended_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    return report
