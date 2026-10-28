"""Offline settlement reconstruction under explicit, unapproved conventions.

Compare fixed hypotheses; never choose a rule by maximizing historical matches.
No interpolation, forward filling, intra-second averaging or model fitting.
"""
import csv
from datetime import timedelta
from decimal import Decimal, localcontext
import gzip
import io
import json
from pathlib import Path
import re

from . import __version__
from .digital import SettlementRule
from .history import INDEX_FIELDS, digest, expected_opens, window
from .markets import parse_time
from .numbers import decimal

LIMIT = 8 * 1024 * 1024


def read_json(path):
    if path.stat().st_size > LIMIT:
        raise ValueError('Dataset JSON exceeds size budget')
    return json.loads(path.read_text(encoding='utf-8'), parse_float=str)


def minute(points, end_ms, boundary, rounding):
    """Exactly 60 *whole-second* ticks, not every tick in a 5Hz minute."""
    if boundary not in ('left_closed', 'right_closed'):
        raise ValueError('Unknown window boundary')
    if isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms % 1000:
        raise ValueError('Averaging endpoint must be a whole second')
    start = end_ms - (60000 if boundary == 'left_closed' else 59000)
    stamps = range(start, start + 60000, 1000)
    selected = [points[t] for t in stamps if t in points]
    result = {'samples': len(selected), 'missing': 60-len(selected),
              'amended_samples': sum(p['amend_time_ms'] is not None for p in selected),
              'repeated_samples': sum(p['repeat_of_previous_value'] for p in selected)}
    if len(selected) != 60:
        return result
    with localcontext() as context:
        context.prec = 50
        average = sum((decimal(p['value']) for p in selected), Decimal(0)) / 60
        rounded = SettlementRule(Decimal('1.00'), round_digits=2, rounding=rounding).rounded(average)
    result.update(mean=str(average), rounded=str(rounded))
    return result


def load_dataset(run_dir):
    directory = Path(run_dir)
    manifest = read_json(directory / 'manifest.json')
    if manifest.get('schema_version') != 1 or manifest.get('asset') != 'BTC' or manifest.get('index_id') != 'BRTI':
        raise ValueError('Unsupported history dataset')
    settings = manifest['settings']
    start = parse_time(settings['start'])
    end = parse_time(settings['end'])
    validated_start, validated_end, first = window(settings['start'], settings['hours'], settings['warmup_hours'], end + timedelta(days=2))
    if start != validated_start or end != validated_end:
        raise ValueError('Inconsistent dataset interval')
    # Verify frozen artifacts before using any numbers. Only known filenames are
    # accepted, and paths may not escape the supplied dataset directory.
    files = manifest['files']
    if not isinstance(files, list) or len(files) > 51:
        raise ValueError('Dataset file budget exceeded')
    paths = {}
    for entry in files:
        name = entry['path']
        if name not in ('contracts.json', 'quotes.json') and not re.fullmatch(r'index/[0-9]{8}T[0-9]{2}\.csv\.gz', name):
            raise ValueError('Unexpected dataset filename')
        path = directory / name
        if name in paths or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Invalid or duplicate dataset path')
        if path.stat().st_size > LIMIT or path.stat().st_size != entry['bytes'] or digest(path) != entry['sha256']:
            raise ValueError('Dataset file failed integrity verification: ' + name)
        paths[name] = path
    if not {'contracts.json', 'quotes.json'} <= paths.keys():
        raise ValueError('Incomplete dataset: contracts and quotes are required')
    contracts = read_json(paths['contracts.json'])
    if not isinstance(contracts, list) or len(contracts) > settings['hours'] * 4:
        raise ValueError('Invalid contract count')
    points, index_quality = {}, []
    expected_files = {'index/' + (first + timedelta(hours=i)).strftime('%Y%m%dT%H') + '.csv.gz': first + timedelta(hours=i)
                      for i in range(settings['hours'] + settings['warmup_hours'] + 1)}
    if any(name.startswith('index/') and name not in expected_files for name in paths):
        raise ValueError('Index file outside dataset interval')
    for name, hour in expected_files.items():
        if name not in paths:
            index_quality.append({'file': name, 'missing_file': True})
            continue
        with gzip.open(paths[name], 'rb') as handle:
            raw = handle.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('Expanded index file exceeds size budget')
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8'), newline=''))
        if reader.fieldnames != INDEX_FIELDS:
            raise ValueError('Unexpected normalized index columns')
        previous, first_stamp, count, whole, amended, repeated = None, None, 0, 0, 0, 0
        max_gap = None
        lower = int(hour.timestamp() * 1000)
        for row in reader:
            count += 1
            stamp, value = int(row['time_ms']), decimal(row['value'])
            amend = int(row['amend_time_ms']) if row['amend_time_ms'] else None
            repeat = row['repeat_of_previous_value']
            if count > 18000 or not lower <= stamp < lower+3600000 or (previous is not None and stamp <= previous) or value <= 0:
                raise ValueError('Invalid normalized index sample or ordering')
            if repeat not in ('True', 'False') or (amend is not None and amend <= 0):
                raise ValueError('Invalid normalized revision flags')
            if previous is not None:
                gap = (stamp-previous)/1000
                max_gap = gap if max_gap is None else max(max_gap, gap)
            if first_stamp is None:
                first_stamp = stamp
            previous = stamp
            amended += amend is not None
            repeated += repeat == 'True'
            if stamp % 1000 == 0:
                whole += 1
                points[stamp] = {'value': str(value), 'amend_time_ms': amend, 'repeat_of_previous_value': repeat == 'True'}
        index_quality.append({'file': name, 'samples': count, 'whole_second_samples': whole,
                              'first_time_ms': first_stamp, 'last_time_ms': previous,
                              'missing_whole_seconds': 3600-whole, 'amended_samples': amended,
                              'repeated_samples': repeated, 'max_gap_seconds': max_gap})
    by_open, seen = {}, set()
    for market in contracts:
        opened, closed = parse_time(market['open_time']), parse_time(market['close_time'])
        stamp = int(opened.timestamp())
        if (not re.fullmatch(r'KXBTC15M-[A-Za-z0-9-]+', market['ticker']) or market['ticker'] in seen
                or stamp in by_open or not start <= opened < end or stamp % 900 or opened.microsecond
                or (closed-opened).total_seconds() != 900 or market['result'] not in ('yes', 'no')):
            raise ValueError('Invalid frozen contract schedule or outcome')
        # Also validates positive, cent-rounded K and supported comparison.
        SettlementRule(decimal(market['reference']), market['comparison'], 2)
        seen.add(market['ticker'])
        by_open[stamp] = market
    missing = [t for t in expected_opens(start, end) if t not in by_open]
    return dict(directory=directory, manifest=manifest, contracts=contracts, points=points,
                index_quality=index_quality, missing=missing, settings=settings)


def audit_dataset(run_dir):
    return audit_loaded(load_dataset(run_dir))


def audit_loaded(data):
    directory, manifest, contracts, points, index_quality, missing, settings = (
        data[k] for k in ('directory', 'manifest', 'contracts', 'points', 'index_quality', 'missing', 'settings'))
    by_open = {int(parse_time(m['open_time']).timestamp()): m for m in contracts}
    candidates = []
    for boundary in ('right_closed', 'left_closed'):
        for rounding in ('half_up', 'half_even'):
            rows = []
            for stamp, market in sorted(by_open.items()):
                rule = SettlementRule(decimal(market['reference']), market['comparison'], 2, rounding)
                opening = minute(points, stamp*1000, boundary, rounding)
                closing = minute(points, (stamp+900)*1000, boundary, rounding)
                row = {'ticker': market['ticker'], 'opening': opening, 'closing': closing,
                       'published_reference': market['reference'], 'published_result': market['result'],
                       'opening_matches_reference': None, 'result_matches': None, 'expiration_value_matches': None}
                if not opening['missing']:
                    row['opening_matches_reference'] = decimal(opening['rounded']) == rule.reference
                if not closing['missing']:
                    predicted = 'yes' if rule.yes(closing['rounded']) else 'no'
                    row.update(reconstructed_result=predicted, result_matches=predicted == market['result'])
                    if 'expiration_value' in market:
                        row['published_expiration_value'] = market['expiration_value']
                        row['expiration_value_matches'] = decimal(closing['rounded']) == decimal(market['expiration_value'])
                rows.append(row)
            keys = ('opening_matches_reference', 'result_matches', 'expiration_value_matches')
            counts = {key: {'matches': sum(r[key] is True for r in rows),
                            'mismatches': sum(r[key] is False for r in rows),
                            'unavailable': sum(r[key] is None for r in rows)} for key in keys}
            candidates.append({'boundary': boundary, 'rounding': rounding, 'counts': counts, 'contracts': rows})
    complete = (manifest['status'] == 'downloaded' and not missing and len(contracts) == settings['hours']*4
                and all(not q.get('missing_file') and q['whole_second_samples'] == 3600 for q in index_quality))
    return {'schema_version': 1, 'package_version': __version__, 'status': 'computed' if complete else 'incomplete',
            'manifest_sha256': digest(directory / 'manifest.json'), 'contracts': len(contracts),
            'missing_contract_opens_ts': missing, 'index_quality': index_quality,
            'rule_review_required': True, 'point_in_time_certified': False, 'selected_candidate': None,
            'sampling_assumption': 'Exact timestamps divisible by 1000 ms, 60 ticks per window; not verified settlement sampling.',
            'boundaries': {'right_closed': '(T-60s,T]: T-59s,...,T', 'left_closed': '[T-60s,T): T-60s,...,T-1s'},
            'rounding_assumption': 'Arithmetic mean then round once to 2 decimals; half_up and half_even are sensitivity cases.',
            'interpretation': 'Computed means only. Matches do not certify rules, historical availability or profitable forecasts.',
            'candidates': candidates}
