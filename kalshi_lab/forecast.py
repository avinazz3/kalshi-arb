"""Offline retrospective forecasts. No HTTP, tuning, execution or profit claims."""
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from . import __version__
from .digital import SettlementRule, simulate, terminal_probability
from .history import digest, save_json
from .markets import parse_time
from .numbers import decimal
from .settlement import audit_loaded, load_dataset, read_json
from .volatility import estimate

HORIZONS = (10, 5, 2)
MODELS = ('rolling', 'ewma', 'market')
LOG_EPSILON = 1e-6


class Unavailable(ValueError):
    pass


def predict(points, *, ticker, reference, comparison, close_ts, horizon, lookback=60,
            half_life=15, paths=2000, seed=1729):
    """Inputs only; neither labels nor post-decision prices enter calculations."""
    decision = close_ts - horizon*60
    source = decision - 1  # exact tick strictly preceding nominal decision
    return predict_at(points, ticker=ticker, reference=reference, comparison=comparison,
                      close_ts=close_ts, horizon=horizon, decision=decision, source=source,
                      lookback=lookback, half_life=half_life, paths=paths, seed=seed)


def predict_at(points, *, ticker, reference, comparison, close_ts, horizon, decision,
               source, lookback=60, half_life=15, paths=2000, seed=1729):
    """Same fixed model with explicit actual decision/source times for live records."""
    if any(type(t) is not int for t in (close_ts, decision, source)) or not source < decision < close_ts - 60:
        raise ValueError('Require source < decision before the final averaging minute')
    stamps = [source-60*i for i in range(lookback, -1, -1)]
    if any(t*1000 not in points for t in stamps):
        raise Unavailable('missing_input_sample')
    samples = [points[t*1000] for t in stamps]
    if any(p['amend_time_ms'] is not None for p in samples):
        raise Unavailable('amended_input_sample')
    if any(p['repeat_of_previous_value'] for p in samples):
        raise Unavailable('repeated_input_sample')
    vol = estimate([p['value'] for p in samples], half_life)
    rule = SettlementRule(reference, comparison, 2, 'half_up')
    offsets = list(range(close_ts-60-source, close_ts-source))
    row_seed = int.from_bytes(hashlib.sha256(f'{seed}|{ticker}|{decision}'.encode()).digest()[:8], 'big')
    models = {}
    for model in ('rolling', 'ewma'):
        sigma = vol[model+'_annual_volatility']
        result = simulate(samples[-1]['value'], rule, close_ts-source, sigma, offsets,
                          paths=paths, seed=row_seed)['arithmetic_average']
        models[model] = {**result, 'annual_volatility': sigma,
                         'terminal_analytic_probability': terminal_probability(samples[-1]['value'], rule, close_ts-source, sigma)}
    return {'ticker': ticker, 'horizon_minutes': horizon, 'decision_ts': decision,
            'source_ts': source, 'input_start_ts': stamps[0], 'input_end_ts': source,
            'spot': samples[-1]['value'], 'reference': str(reference), 'comparison': comparison,
            'sample_offsets_seconds': offsets, 'seconds_to_close_from_source': close_ts-source,
            'seed': row_seed, 'models': models}


def losses(probability, outcome):
    if not math.isfinite(probability) or not 0 <= probability <= 1 or outcome not in (0, 1):
        raise ValueError('Invalid scoring observation')
    clipped = min(1-LOG_EPSILON, max(LOG_EPSILON, probability))
    return {'brier': (probability-outcome)**2,
            'log_loss': -math.log(clipped if outcome else 1-clipped),
            'log_clipped': clipped != probability}


def summarize(rows):
    result = {}
    for name, group in [('overall', rows)] + [(f'{h}min', [r for r in rows if r['horizon_minutes']==h]) for h in HORIZONS]:
        models = {}
        for model in MODELS:
            scores = [losses(r[model], r['outcome']) for r in group]
            models[model] = {'brier': math.fsum(s['brier'] for s in scores)/len(scores) if scores else None,
                             'log_loss': math.fsum(s['log_loss'] for s in scores)/len(scores) if scores else None,
                             'log_clipped_rows': sum(s['log_clipped'] for s in scores)}
        result[name] = {'rows': len(group), 'contracts': len({r['ticker'] for r in group}), 'models': models}
    return result


def quotes_by_key(quotes, contracts):
    markets = {m['ticker']: m for m in contracts}
    result = {}
    for row in quotes:
        ticker, stamp = row['ticker'], row['end_period_ts']
        if ticker not in markets or isinstance(stamp, bool) or not isinstance(stamp, int) or stamp % 60:
            raise ValueError('Invalid quote identity or timestamp')
        opened = int(parse_time(markets[ticker]['open_time']).timestamp())
        closed = int(parse_time(markets[ticker]['close_time']).timestamp())
        if not opened < stamp <= closed or (ticker, stamp) in result:
            raise ValueError('Duplicate or out-of-window quote')
        bid, ask = row.get('yes_bid_close'), row.get('yes_ask_close')
        if bid is None or ask is None or row.get('two_sided') is not True:
            result[ticker, stamp] = None
        else:
            bid, ask = decimal(bid), decimal(ask)
            result[ticker, stamp] = float((bid+ask)/2) if 0 <= bid <= ask <= 1 else None
    return result


def run_forecast(run_dir, out_dir, *, lookback=60, half_life=15, paths=2000, seed=1729, progress=None):
    for name, value, lower, upper in [('lookback', lookback, 2, 1440), ('paths', paths, 100, 20000)]:
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f'{name} must be an integer from {lower} to {upper}')
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError('seed must be an integer')
    if isinstance(half_life, bool) or not isinstance(half_life, (int, float)) or not math.isfinite(half_life) or half_life <= 0:
        raise ValueError('half-life must be positive and finite')
    output = Path(out_dir)
    if output.exists():
        raise ValueError('Forecast output directory already exists; choose a new path')
    data = load_dataset(run_dir)
    audit = audit_loaded(data)
    if audit['status'] != 'computed':
        raise ValueError('Complete and audit the historical dataset before forecasting')
    selected = next(c for c in audit['candidates'] if c['boundary']=='left_closed' and c['rounding']=='half_up')
    if any(c['mismatches'] or (key != 'expiration_value_matches' and c['unavailable']) for key, c in selected['counts'].items()):
        raise ValueError('Left-closed/half-up settlement reconstruction fails; investigate before forecasting')
    contracts = sorted(data['contracts'], key=lambda m: parse_time(m['open_time']))
    transitions = len(contracts)*len(HORIZONS)*2*paths*61
    if transitions > 250000000:
        raise ValueError('Simulation budget exceeds 250 million transitions; reduce paths or interval')
    quotes = quotes_by_key(read_json(Path(run_dir)/'quotes.json'), contracts)
    output.mkdir(parents=True, exist_ok=False)
    config = {'lookback_minutes': lookback, 'ewma_half_life_minutes': half_life, 'paths': paths, 'seed': seed,
              'horizons_minutes': list(HORIZONS), 'annual_price_drift': 0, 'seconds_per_year': 31536000,
              'settlement_boundary': 'left_closed', 'rounding': 'half_up', 'round_digits': 2,
              'spot_lag_seconds': 1, 'log_loss_clip_epsilon': LOG_EPSILON}
    report = {'schema_version': 1, 'package_version': __version__, 'status': 'running',
              'started_at': datetime.now(timezone.utc).isoformat(), 'config': config,
              'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
              'input_manifest_sha256': audit['manifest_sha256'], 'input_files': data['manifest']['files'],
              'point_in_time_certified': False, 'rule_review_required': True,
              'interpretation': 'Retrospective engineering experiment; no held-out performance or trading claim.',
              'forecast_rows': 0, 'paired_rows': 0, 'planned_rows': len(contracts)*3, 'exclusions': []}
    paired = []
    save_json(output/'report.json', report)
    try:
        with (output/'predictions.jsonl').open('x', encoding='utf-8') as handle:
            for n, market in enumerate(contracts, 1):
                close = int(parse_time(market['close_time']).timestamp())
                for horizon in HORIZONS:
                    try:
                        prediction = predict(data['points'], ticker=market['ticker'], reference=market['reference'],
                                             comparison=market['comparison'], close_ts=close, horizon=horizon,
                                             lookback=lookback, half_life=half_life, paths=paths, seed=seed)
                    except Unavailable as exc:
                        report['exclusions'].append({'ticker': market['ticker'], 'horizon_minutes': horizon, 'reason': str(exc)})
                        continue
                    handle.write(json.dumps(prediction, allow_nan=False)+'\n')
                    handle.flush()
                    report['forecast_rows'] += 1
                    midpoint = quotes.get((market['ticker'], prediction['decision_ts']))
                    if midpoint is None:
                        report['exclusions'].append({'ticker': market['ticker'], 'horizon_minutes': horizon, 'reason': 'missing_or_invalid_matching_quote'})
                        continue
                    # Labels and market benchmark are attached only after prediction.
                    paired.append({'ticker': market['ticker'], 'horizon_minutes': horizon,
                                   'decision_ts': prediction['decision_ts'], 'outcome': int(market['result']=='yes'),
                                   'rolling': prediction['models']['rolling']['probability'],
                                   'ewma': prediction['models']['ewma']['probability'], 'market': midpoint})
                report['paired_rows'] = len(paired)
                save_json(output/'report.json', report)
                if progress:
                    progress(n, len(contracts))
        with (output/'evaluation.csv').open('x', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['ticker','horizon_minutes','decision_ts','outcome',*MODELS])
            writer.writeheader()
            writer.writerows(paired)
        report['scores'] = summarize(paired)
        report['exclusion_counts'] = dict(Counter(r['reason'] for r in report['exclusions']))
        report['status'] = 'completed' if len(paired)==report['planned_rows'] else 'incomplete'
        report['artifacts'] = {name: digest(output/name) for name in ('predictions.jsonl','evaluation.csv')}
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        raise
    except Exception:
        report['status'] = 'failed'
        raise
    finally:
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        save_json(output/'report.json', report)
    return report
