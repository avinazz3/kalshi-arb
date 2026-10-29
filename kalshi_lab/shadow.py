"""Bounded REST observation recorder; forecasts are logged, never submitted."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import time

from . import __version__
from .auth import load_kalshi_auth
from .diagnostics import ProbeFailure, ProbeHTTP, index_points, market_summary
from .forecast import HORIZONS, Unavailable, predict_at
from .history import digest, save_json
from .markets import parse_time, select_active
from .numbers import decimal

BOOK_PATH = re.compile(r'/markets/KXBTC15M-[A-Za-z0-9-]+/orderbook\Z')
MAX_BYTES = 256 * 1024 * 1024


class ShadowHTTP(ProbeHTTP):
    def allowed_path(self, path):
        return path in ('/markets', '/cfbenchmarks/latest_values', '/cfbenchmarks/history/values') or bool(BOOK_PATH.fullmatch(path))


def normalize_book(document, depth=10):
    book, result = document['orderbook_fp'], {}
    for side in ('yes', 'no'):
        levels = book.get(side + '_dollars') or []
        if not isinstance(levels, list) or len(levels) > 100:
            raise ValueError('Invalid book depth')
        parsed, seen = [], set()
        for entry in levels:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError('Invalid book level')
            price, size = map(decimal, entry)
            if not 0 <= price <= 1 or size <= 0 or price != price.quantize(Decimal('.0001')) or size != size.quantize(Decimal('.01')) or price in seen:
                raise ValueError('Invalid book price, size or duplicate level')
            seen.add(price)
            parsed.append((price, size))
        parsed.sort(reverse=True)
        result[side + '_bids'] = [[str(p), str(q)] for p, q in parsed[:depth]]
    yes, no = result['yes_bids'], result['no_bids']
    if yes and no and decimal(yes[0][0]) + decimal(no[0][0]) > 1:
        raise ValueError('Crossed book')
    result['two_sided'] = bool(yes and no)
    result['yes_ask'] = str(1 - decimal(no[0][0])) if no else None
    result['no_ask'] = str(1 - decimal(yes[0][0])) if yes else None
    return result


def rule_hash(market):
    fields = {k: market[k] for k in ('ticker', 'open_time', 'close_time', 'reference', 'comparison', 'rules_primary', 'rules_secondary')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def active_market(document, now):
    result = market_summary(select_active(document['markets'], 'KXBTC15M', now))
    result.pop('result', None)
    return result


def live_prediction(points, market, horizon, nominal, freeze, *, paths=2000):
    """Freeze received inputs first; post-freeze observations cannot enter."""
    decision = int(freeze.timestamp())
    eligible = [t for t, p in points.items() if t % 1000 == 0 and t // 1000 < decision
                and parse_time(p['received_at']) <= freeze]
    if not eligible:
        raise Unavailable('no_received_index_sample')
    source = max(eligible) // 1000
    if freeze.timestamp() - source > 3:
        raise Unavailable('stale_index_sample')
    stamps = [(source - 60 * i) * 1000 for i in range(60, -1, -1)]
    if any(t not in points or parse_time(points[t]['received_at']) > freeze for t in stamps):
        raise Unavailable('missing_received_minute_endpoint')
    prediction = predict_at(points, ticker=market['ticker'], reference=market['reference'], comparison=market['comparison'],
                            close_ts=int(parse_time(market['close_time']).timestamp()), horizon=horizon,
                            decision=decision, source=source, paths=paths)
    inputs = [{**points[t], 'time_ms': t} for t in stamps]
    prediction.update(nominal_decision_ts=nominal, calculation_started_at=freeze.isoformat(),
                      input_receipts_max=max(p['received_at'] for p in inputs),
                      input_observations=inputs, input_sha256=hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest())
    return prediction


def run_shadow(run_dir, *, minutes=60, interval=0.5, timeout=10, paths=2000, bootstrap_history=True,
               http=None, clock=None, monotonic=None, pause=None, progress=None):
    if (type(minutes) is not int or not 1 <= minutes <= 1440 or isinstance(interval, bool)
            or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not .5 <= interval <= 10
            or type(paths) is not int or not 100 <= paths <= 20000):
        raise ValueError('minutes must be 1..1440, interval 0.5..10 seconds, paths 100..20000')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
        raise ValueError('timeout must be 1..30 seconds')
    directory = Path(run_dir)
    if directory.exists():
        raise ValueError('Shadow directory already exists; choose a new directory')
    clock = clock or (lambda: datetime.now(timezone.utc))
    monotonic, pause = monotonic or time.monotonic, pause or time.sleep
    if http is None:
        http = ShadowHTTP(load_kalshi_auth(), timeout)  # Validate local access before creating output.
    started = clock()
    if started.tzinfo is None:
        raise ValueError('Recorder clock must be timezone-aware')
    directory.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'kind': 'shadow_recording', 'package_version': __version__, 'status': 'running',
              'started_at': started.isoformat(), 'config': {'minutes': minutes, 'interval_seconds': interval,
              'paths': paths, 'lookback_minutes': 60, 'ewma_half_life_minutes': 15, 'seed': 1729,
              'depth': 10, 'max_lateness_seconds': 10, 'max_index_age_seconds': 3, 'max_book_age_seconds': 5,
              'bootstrap_history': bootstrap_history}, 'requests': 0, 'observations': 0, 'predictions': 0,
              'decisions': 0, 'errors': {}, 'unavailable': {}, 'rule_review_required': True,
              'point_in_time_certified': False,
              'interpretation': 'Receipt-logged REST observations and hypothetical decisions. No orders, guaranteed fills or independently certified exchange timestamps.'}
    errors, unavailable, points, seen_tasks = Counter(), Counter(), {}, set()
    deadline, last_metadata, last_progress = monotonic() + minutes * 60, -math.inf, monotonic()
    market, metadata_id, current_rule_hash = None, None, None
    byte_count, next_id = 0, 0
    save_json(directory / 'report.json', report)
    try:
        with (directory / 'observations.jsonl').open('x', encoding='utf-8') as observations, (directory / 'decisions.jsonl').open('x', encoding='utf-8') as decisions:
            def append(handle, item):
                nonlocal byte_count
                line = json.dumps(item, allow_nan=False) + '\n'
                byte_count += len(line.encode())
                if byte_count > MAX_BYTES:
                    raise RuntimeError('Shadow output size budget exceeded')
                handle.write(line)
                handle.flush()

            def request(kind, path, params, normalize):
                nonlocal next_id
                if monotonic() >= deadline:
                    return None
                if report['requests'] >= 400000:
                    raise RuntimeError('Shadow request budget exceeded')
                sent = clock()
                report['requests'] += 1
                try:
                    response = http.get(path, params)
                    received = clock()
                    if received < sent:
                        raise ProbeFailure('clock_moved_backwards')
                    value = normalize(response.data)
                    item = {'id': next_id, 'kind': kind, 'request_started_at': sent.isoformat(),
                            'received_at': received.isoformat(), 'response_sha256': response.sha256, 'value': value}
                    next_id += 1
                    append(observations, item)
                    report['observations'] += 1
                    return item
                except ProbeFailure as exc:
                    reason = exc.reason
                    status = exc.status
                except (ValueError, KeyError, TypeError, AttributeError):
                    reason, status = 'unexpected_or_invalid_schema', None
                errors[kind + ':' + reason] += 1
                append(observations, {'id': next_id, 'kind': 'error', 'source': kind, 'reason': reason,
                                      'http_status': status, 'request_started_at': sent.isoformat(), 'received_at': clock().isoformat()})
                next_id += 1
                return None

            def add_points(item):
                if item:
                    for point in item['value']:
                        # Update only after receipt; already-written predictions retain their full frozen inputs.
                        points[point['time_ms']] = {**point, 'received_at': item['received_at'], 'observation_id': item['id']}

            if bootstrap_history:
                hour = started.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                for offset in (-2, -1, 0):
                    lower = int((hour + timedelta(hours=offset)).timestamp() * 1000)
                    def normalize_history(document, lower=lower):
                        selected = index_points(document)
                        if len(selected) > 18000 or any(not lower <= p['time_ms'] < lower + 3600000 for p in selected):
                            raise ValueError('History outside requested hour')
                        return [p for p in selected if p['time_ms'] % 1000 == 0 and p['time_ms'] <= int(clock().timestamp() * 1000)]
                    item = request('bootstrap_index', '/cfbenchmarks/history/values', {'id': 'BRTI', 'timespan': 'HOUR',
                                   'timestamp': (hour + timedelta(hours=offset)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')}, normalize_history)
                    add_points(item)
                    pause(interval)
            while monotonic() < deadline:
                cycle_started = monotonic()
                now = clock()
                if market is None or parse_time(market['close_time']) <= now or monotonic() - last_metadata >= 30:
                    item = request('market', '/markets', {'series_ticker': 'KXBTC15M', 'status': 'open', 'limit': 20, 'mve_filter': 'exclude'},
                                   lambda d: active_market(d, clock()))
                    last_metadata = monotonic()
                    if item:
                        new_market = item['value']
                        if market and new_market['ticker'] == market['ticker'] and rule_hash(new_market) != current_rule_hash:
                            errors['market:reference_or_rule_changed'] += 1
                            # A changed reference is retained as evidence; suppress all future forecasts for this contract.
                            seen_tasks.update((new_market['ticker'], h) for h in HORIZONS)
                        market, metadata_id, current_rule_hash = new_market, item['id'], rule_hash(new_market)
                    else:
                        market = None
                index = request('index', '/cfbenchmarks/latest_values', {'id': 'BRTI', 'maxResolution': 'PER_SECOND'},
                                lambda d: index_points(d, latest=True))
                if index:
                    observed = parse_time(index['received_at']).timestamp()
                    if any(p['time_ms'] % 1000 or p['time_ms'] / 1000 > observed for p in index['value']):
                        errors['index:future_or_nonwhole_sample'] += 1
                    else:
                        add_points(index)
                book = None
                if market and parse_time(market['close_time']) > clock():
                    book = request('book', f"/markets/{market['ticker']}/orderbook", {'depth': 10},
                                   lambda d: {**normalize_book(d), 'ticker': market['ticker']})
                freeze = clock()
                if market:
                    close = int(parse_time(market['close_time']).timestamp())
                    for horizon in HORIZONS:
                        task, nominal = (market['ticker'], horizon), close - horizon * 60
                        if task in seen_tasks or freeze.timestamp() < nominal:
                            continue
                        seen_tasks.add(task)
                        decision = {'ticker': market['ticker'], 'horizon_minutes': horizon, 'nominal_decision_ts': nominal,
                                    'metadata_observation_id': metadata_id, 'rule_sha256': current_rule_hash,
                                    'market': market, 'book_observation_id': book['id'] if book else None,
                                    'book': book['value'] if book else None, 'status': 'unavailable'}
                        try:
                            if freeze.timestamp() - nominal > 10:
                                raise Unavailable('missed_nominal_time')
                            if book is None or not book['value']['two_sided']:
                                raise Unavailable('missing_two_sided_book')
                            prediction = live_prediction(points, market, horizon, nominal, freeze, paths=paths)
                            ready = clock()
                            if ready.timestamp() - parse_time(book['received_at']).timestamp() > 5:
                                raise Unavailable('book_stale_after_calculation')
                            if ready.timestamp() >= close - 60:
                                raise Unavailable('calculation_reached_averaging_window')
                            decision.update(status='forecast', prediction=prediction)
                            report['predictions'] += 1
                        except Unavailable as exc:
                            decision['reason'] = str(exc)
                            unavailable[str(exc)] += 1
                        decision['available_at'] = clock().isoformat()
                        append(decisions, decision)
                        report['decisions'] += 1
                cutoff = int((freeze.timestamp() - 5400) * 1000)
                points = {t: p for t, p in points.items() if t >= cutoff}
                report.update(errors=dict(errors), unavailable=dict(unavailable))
                save_json(directory / 'report.json', report)
                if progress and monotonic() - last_progress >= 60:
                    progress(report['observations'], report['predictions'])
                    last_progress = monotonic()
                pause(min(max(0, interval - (monotonic() - cycle_started)), max(0, deadline - monotonic())))
        report['status'] = 'completed_with_gaps' if errors else 'completed'
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        raise
    except Exception:
        report['status'] = 'failed'
        raise
    finally:
        report.update(ended_at=clock().isoformat(), errors=dict(errors), unavailable=dict(unavailable))
        report['artifacts'] = {n: digest(directory / n) for n in ('observations.jsonl', 'decisions.jsonl') if (directory / n).exists()}
        save_json(directory / 'report.json', report)
    return report
