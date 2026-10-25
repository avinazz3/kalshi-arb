"""Small GET-only client for public market data. No account credentials."""
from datetime import datetime, timezone
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from . import __version__

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


class RequestError(RuntimeError):
    def __init__(self, message, *, status=None, retryable=False, retry_after=0):
        super().__init__(message)
        self.status, self.retryable = status, retryable
        self.retry_after = retry_after


class RequestDeadline(RequestError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Client:
    def __init__(self, store, timeout=15, retries=2, deadline=None, stop_event=None):
        self.store, self.timeout, self.retries = store, timeout, retries
        self.deadline = deadline
        self.stop_event = stop_event
        self.session = uuid4().hex

    def get(self, path, params=None, *, kind="metadata", ticker=None, sweep=None):
        url = BASE_URL + path
        if params:
            url += "?" + urlencode(params, doseq=True)
        for attempt in range(self.retries + 1):
            if self.stop_event is not None and self.stop_event.is_set():
                raise RequestDeadline("Metadata discovery cancelled")
            remaining = None if self.deadline is None else self.deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise RequestDeadline("Metadata request budget exhausted")
            requested_at, start = utc_now(), time.monotonic()
            status, body, headers, error = None, b"", {}, None
            try:
                req = Request(url, headers={"Accept": "application/json", "User-Agent": "kalshi-crypto-flow/" + __version__}, method="GET")
                with urlopen(req, timeout=self.timeout if remaining is None else min(self.timeout, remaining)) as response:
                    status, headers = response.status, dict(response.headers)
                    body = response.read()
            except HTTPError as exc:
                status, headers, body = exc.code, dict(exc.headers), exc.read()
                error = f"HTTP {status}"
            except (URLError, TimeoutError, OSError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            received_at = utc_now()
            duration_ms = (time.monotonic() - start) * 1000
            self.store.append(
                session=self.session, sweep=sweep, kind=kind, ticker=ticker, url=url,
                requested_at=requested_at, received_at=received_at,
                duration_ms=duration_ms, status=status, headers=headers,
                body=body, error=error,
            )
            if status is not None and 200 <= status < 300 and error is None:
                # Preserve all numeric JSON decimals as strings. Raw bytes are retained.
                parsed = json.loads(body, parse_float=str)
                if not isinstance(parsed, dict):
                    raise ValueError("Expected JSON object; raw response retained")
                return parsed
            retryable = status is None or status == 429 or status >= 500
            try:
                retry_after = float(next((v for k, v in headers.items() if k.lower() == "retry-after"), 0))
            except (ValueError, TypeError):
                retry_after = 0
            if not math.isfinite(retry_after) or retry_after < 0:
                retry_after = 0
            if not retryable or attempt == self.retries:
                raise RequestError(f"GET {path} failed: {error}; raw attempt retained",
                                   status=status, retryable=retryable, retry_after=retry_after)
            delay = min(30, max(2 ** attempt, retry_after))
            if self.deadline is not None:
                delay = max(0, min(delay, self.deadline - time.monotonic()))
            time.sleep(delay)
