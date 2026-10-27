"""Bounded retrospective BTC dataset. GET-only, sequential, no retries or orders."""
from datetime import datetime, timedelta, timezone
import csv
import gzip
import hashlib
import json
from pathlib import Path
import time

from . import __version__
from .auth import load_kalshi_auth
from .diagnostics import INDEX, SERIES, ProbeFailure, ProbeHTTP, index_points, market_summary
from .markets import parse_time
from .numbers import decimal

MAX_PAGES = 4
INDEX_FIELDS = ['time_ms', 'value', 'amend_time_ms', 'repeat_of_previous_value']


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def window(start, hours, warmup_hours, now):
    start = parse_time(start).astimezone(timezone.utc)
    for name, value, upper in [('hours', hours, 24), ('warmup_hours', warmup_hours, 24)]:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValueError(f'{name} must be an integer from 1 to {upper}')
    if start.minute or start.second or start.microsecond or start.year < 2020:
        raise ValueError('start must be a whole UTC hour in 2020 or later')
    end = start + timedelta(hours=hours)
    # Fetch the hour containing the final close tick; wait until that hour is
    # finished plus CF's documented potential publication delay (15 minutes).
    if end + timedelta(hours=1, minutes=15) > now:
        raise ValueError('Choose an older interval: end must be at least 75 minutes ago')
    return start, end, start - timedelta(hours=warmup_hours)


def expected_opens(start, end):
    return [int(start.timestamp()) + 900 * i for i in range(int((end-start).total_seconds()) // 900)]


def normalized_market(raw, start, end):
    opened, closed = parse_time(raw['open_time']), parse_time(raw['close_time'])
    if not start <= opened < end:
        return None
    result = market_summary(raw)
    if closed > end or int(opened.timestamp()) % 900 or opened.microsecond:
        raise ProbeFailure('unexpected_contract_schedule')
    if raw.get('result') not in ('yes', 'no'):
        raise ProbeFailure('unsettled_contract')
    # Snapshot live quotes are not historical observations. Freeze only rules,
    # references and outcome fields needed for the audit.
    keep = ['ticker', 'open_time', 'close_time', 'reference', 'comparison',
            'rules_primary', 'rules_secondary', 'result', 'rule_review_required']
    result = {key: result[key] for key in keep}
    result.update(open_time=opened.astimezone(timezone.utc).isoformat(), close_time=closed.astimezone(timezone.utc).isoformat())
    expiry = raw.get('expiration_value')
    if expiry not in (None, ''):
        value = decimal(expiry)
        if value <= 0:
            raise ProbeFailure('invalid_expiration_value')
        result['expiration_value'] = str(value)
    if raw.get('is_provisional') is True:
        raise ProbeFailure('provisional_outcome')
    return result


def quote_rows(document, market):
    if document.get('ticker', market['ticker']) != market['ticker']:
        raise ProbeFailure('wrong_candle_market')
    candles = document.get('candlesticks')
    if not isinstance(candles, list) or len(candles) > 16:
        raise ProbeFailure('unexpected_candle_schema')
    lower = int(parse_time(market['open_time']).timestamp())
    upper = int(parse_time(market['close_time']).timestamp())
    rows, seen = [], set()
    for candle in candles:
        stamp = candle['end_period_ts']
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp % 60 or not lower <= stamp <= upper or stamp in seen:
            raise ProbeFailure('invalid_candle_timestamp')
        seen.add(stamp)
        # API start_ts is inclusive; exclude the candle ending at open (it
        # describes the preceding minute). Null/crossed prices stay flagged.
        if stamp == lower:
            continue
        bid = candle.get('yes_bid', {}).get('close_dollars')
        ask = candle.get('yes_ask', {}).get('close_dollars')
        bid = None if bid is None else decimal(bid)
        ask = None if ask is None else decimal(ask)
        if any(x is not None and not 0 <= x <= 1 for x in (bid, ask)):
            raise ProbeFailure('invalid_candle_price')
        valid = bid is not None and ask is not None and bid <= ask
        rows.append({'ticker': market['ticker'], 'end_period_ts': stamp,
                     'yes_bid_close': None if bid is None else str(bid),
                     'yes_ask_close': None if ask is None else str(ask),
                     'two_sided': valid})
    return sorted(rows, key=lambda row: row['end_period_ts'])


def fetch_history(run_dir, *, start, hours=24, warmup_hours=1, timeout=10, http=None, now=None, pause=time.sleep):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    start, end, first = window(start, hours, warmup_hours, now)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
        raise ValueError('timeout must be from 1 to 30 seconds')
    directory = Path(run_dir)
    if directory.exists():
        raise ValueError('History run directory already exists; choose a new path')
    if http is None:
        http = ProbeHTTP(load_kalshi_auth(), timeout)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'index').mkdir()
    report = {'schema_version': 1, 'package_version': __version__, 'asset': 'BTC', 'index_id': INDEX,
              'started_at': now.isoformat(), 'status': 'running', 'synthetic': False,
              'settings': {'start': start.isoformat(), 'end': end.isoformat(), 'hours': hours,
                           'warmup_hours': warmup_hours, 'timeout': timeout},
              'requests': [], 'files': [], 'issues': [], 'index_hours': [],
              'rule_review_required': True, 'point_in_time_certified': False,
              'interpretation': 'Retrospective data and sampling audit only; not a backtest or executable quotes.'}
    markets, sources, quotes = {}, {}, []
    target_opens = expected_opens(start, end)

    def checkpoint():
        save_json(directory / 'manifest.json', report)

    def register(path):
        report['files'].append({'path': path.relative_to(directory).as_posix(), 'sha256': digest(path), 'bytes': path.stat().st_size})

    def get(path, params):
        item = {'path': path, 'params': params, 'status': 'running'}
        report['requests'].append(item)
        checkpoint()
        try:
            if len(report['requests']) > 1:
                pause(0.3)  # <= 3.34 requests/s even with zero network latency
            response = http.get(path, params)
            item.update(status='received', received_at=response.received_at, response_sha256=response.sha256)
            return response.data
        except ProbeFailure as exc:
            item.update(status='blocked', reason=exc.reason, http_status=exc.status)
            raise
        finally:
            checkpoint()

    def listing(path, params):
        cursor, seen = '', set()
        for page in range(MAX_PAGES):
            document = get(path, {**params, **({'cursor': cursor} if cursor else {})})
            entries, next_cursor = document['markets'], document.get('cursor', '')
            if not isinstance(entries, list) or len(entries) > 1000 or not isinstance(next_cursor, str):
                raise ProbeFailure('invalid_market_page')
            for raw in entries:
                market = normalized_market(raw, start, end)
                if market is None:
                    continue
                ticker = market['ticker']
                if ticker in markets and markets[ticker] != market:
                    raise ProbeFailure('conflicting_market_snapshots')
                markets[ticker] = market
                sources[ticker] = path
                if len(markets) > hours * 4:
                    raise ProbeFailure('too_many_contracts')
            if path == '/historical/markets' and {int(parse_time(m['open_time']).timestamp()) for m in markets.values()} == set(target_opens):
                return
            if not next_cursor:
                return
            if next_cursor in seen:
                raise ProbeFailure('repeated_pagination_cursor')
            seen.add(next_cursor)
            cursor = next_cursor
        raise ProbeFailure('market_page_limit')

    checkpoint()
    try:
        # Close filters cannot be combined with status=settled. Locally validate
        # results, schedules and the requested opening interval instead.
        listing('/markets', {'series_ticker': SERIES, 'mve_filter': 'exclude', 'limit': 1000,
                            'min_close_ts': int(start.timestamp()), 'max_close_ts': int(end.timestamp()) + 1})
        observed = {int(parse_time(m['open_time']).timestamp()) for m in markets.values()}
        if observed != set(target_opens):
            # Historical listing has no time filters. Bound pagination explicitly;
            # an exhausted page budget is a failure, never silent completeness.
            listing('/historical/markets', {'series_ticker': SERIES, 'limit': 1000})
        by_open = {}
        for market in markets.values():
            opened = int(parse_time(market['open_time']).timestamp())
            if opened in by_open:
                raise ProbeFailure('duplicate_contract_window')
            by_open[opened] = market['ticker']
        report['missing_contract_opens'] = [datetime.fromtimestamp(t, timezone.utc).isoformat() for t in target_opens if t not in by_open]
        if report['missing_contract_opens']:
            report['issues'].append('missing_contracts')
        ordered = sorted(markets.values(), key=lambda m: m['open_time'])
        save_json(directory / 'contracts.json', ordered)
        register(directory / 'contracts.json')
        for market in ordered:
            prefix = '/historical/markets' if sources[market['ticker']] == '/historical/markets' else '/series/KXBTC15M/markets'
            data = get(prefix + '/' + market['ticker'] + '/candlesticks',
                       {'start_ts': int(parse_time(market['open_time']).timestamp()),
                        'end_ts': int(parse_time(market['close_time']).timestamp()), 'period_interval': 1})
            rows = quote_rows(data, market)
            quotes.extend(rows)
            if len(rows) != 15 or not all(row['two_sided'] for row in rows):
                report['issues'].append('incomplete_quotes:' + market['ticker'])
        save_json(directory / 'quotes.json', quotes)
        register(directory / 'quotes.json')
        # Include the entire hour containing the final close, whose :00 tick is
        # needed by the right-closed candidate; never extrapolate it.
        for offset in range(hours + warmup_hours + 1):
            hour = first + timedelta(hours=offset)
            data = get('/cfbenchmarks/history/values', {'id': INDEX, 'timespan': 'HOUR',
                       'timestamp': hour.isoformat(timespec='milliseconds').replace('+00:00', 'Z')})
            points = index_points(data)
            lower = int(hour.timestamp() * 1000)
            if len(points) > 18000 or any(not lower <= p['time_ms'] < lower + 3600000 for p in points):
                raise ProbeFailure('index_samples_outside_requested_hour_or_budget')
            path = directory / 'index' / (hour.strftime('%Y%m%dT%H') + '.csv.gz')
            with gzip.open(path, 'xt', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
                writer.writeheader()
                writer.writerows(points)
            register(path)
            whole_seconds = sum(p['time_ms'] % 1000 == 0 for p in points)
            summary = {'hour': hour.isoformat(), 'file': path.relative_to(directory).as_posix(),
                       'samples': len(points), 'whole_second_samples': whole_seconds,
                       'first_time_ms': points[0]['time_ms'], 'last_time_ms': points[-1]['time_ms'],
                       'max_gap_seconds': max(((b['time_ms']-a['time_ms'])/1000 for a, b in zip(points, points[1:])), default=None),
                       'amended_samples': sum(p['amend_time_ms'] is not None for p in points),
                       'repeated_samples': sum(p['repeat_of_previous_value'] for p in points)}
            report['index_hours'].append(summary)
            if whole_seconds != 3600:
                report['issues'].append('missing_whole_second_samples:' + summary['hour'])
            checkpoint()
        report['contracts'] = len(markets)
        report['quote_rows'] = len(quotes)
        report['status'] = 'downloaded' if not report['issues'] else 'incomplete'
    except ProbeFailure as exc:
        report.update(status='blocked', failure={'reason': exc.reason, 'http_status': exc.status})
    except (ValueError, KeyError, TypeError, AttributeError):
        report.update(status='blocked', failure={'reason': 'unexpected_or_invalid_schema'})
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        raise
    except OSError:
        report['status'] = 'local_error'
        raise
    finally:
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        checkpoint()
    return report
