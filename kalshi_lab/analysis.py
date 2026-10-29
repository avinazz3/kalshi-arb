"""Offline diagnostics of frozen forecasts. No fitting or new simulations."""
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from . import __version__
from .digital import SECONDS_PER_YEAR, SettlementRule
from .forecast import HORIZONS, MODELS, losses, quotes_by_key, summarize
from .history import digest, save_json
from .markets import parse_time
from .numbers import decimal
from .settlement import audit_loaded, load_dataset, read_json
from .volatility import estimate

MAX_ROWS = 96 * len(HORIZONS)


def mean(values):
    return math.fsum(values) / len(values) if values else None


def past_features(points, source, spot):
    """Exact endpoints ending at source; never reads a later observation."""
    result = {}
    for minutes in (1, 2, 5):
        point = points.get((source - 60 * minutes) * 1000)
        reason = ('missing_sample' if point is None else
                  'amended_sample' if point['amend_time_ms'] is not None else
                  'repeated_sample' if point['repeat_of_previous_value'] else None)
        result[f'return_{minutes}m_log'] = None if reason else math.log(spot / float(point['value']))
        result[f'return_{minutes}m_status'] = reason or 'available'
    return result


def uncertainty(spot, sigma, offsets, seconds_to_close):
    """Exact unrounded arithmetic-average and terminal SD under fixed zero-drift GBM."""
    rate = sigma * sigma / SECONDS_PER_YEAR
    # Cov(S_a,S_b) = S_0^2 * (exp(sigma^2 * min(a,b)/C) - 1).
    # Sorted offsets: the j-th time is the minimum in 2*(n-j)-1 ordered pairs.
    count = len(offsets)
    variance = math.fsum((2 * (count - j) - 1) * math.expm1(rate * t)
                         for j, t in enumerate(offsets)) / count**2
    return spot * math.sqrt(variance), spot * math.sqrt(math.expm1(rate * seconds_to_close))


def _key(row):
    horizon, stamp = row['horizon_minutes'], row['decision_ts']
    if (isinstance(horizon, bool) or not isinstance(horizon, int) or horizon not in HORIZONS
            or isinstance(stamp, bool) or not isinstance(stamp, int)):
        raise ValueError('Invalid forecast identity')
    return row['ticker'], horizon, stamp


def _validate_prediction(prediction, market, points, config):
    _, horizon, decision = _key(prediction)
    close = int(parse_time(market['close_time']).timestamp())
    source = decision - 1
    offsets = list(range(close - 60 - source, close - source))
    if (decision != close - horizon * 60 or prediction['source_ts'] != source
            or prediction['input_end_ts'] != source
            or prediction['input_start_ts'] != source - 60 * config['lookback_minutes']
            or prediction['seconds_to_close_from_source'] != close - source
            or prediction['sample_offsets_seconds'] != offsets
            or decimal(prediction['reference']) != decimal(market['reference'])
            or prediction['comparison'] != market['comparison']):
        raise ValueError('Forecast timestamps or contract reference do not match history')
    stamps = range(prediction['input_start_ts'], source + 1, 60)
    samples = [points.get(t * 1000) for t in stamps]
    if any(p is None or p['amend_time_ms'] is not None or p['repeat_of_previous_value'] for p in samples):
        raise ValueError('Saved prediction uses missing or flagged input samples')
    if decimal(prediction['spot']) != decimal(samples[-1]['value']):
        raise ValueError('Forecast spot does not match history')
    vols = estimate([p['value'] for p in samples], config['ewma_half_life_minutes'])
    for model in ('rolling', 'ewma'):
        saved = prediction['models'][model]
        probability, sigma = float(saved['probability']), float(saved['annual_volatility'])
        losses(probability, 0)  # finite, in [0,1]
        if not math.isfinite(sigma) or not math.isclose(sigma, vols[model + '_annual_volatility'], rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError('Forecast volatility does not match historical inputs')
        paths, yes = saved['paths'], saved['yes_paths']
        if (type(paths) is not int or type(yes) is not int or paths != config['paths']
                or not 0 <= yes <= paths or probability != yes / paths):
            raise ValueError('Invalid saved Monte Carlo counts')
        expected_se = math.sqrt(probability * (1 - probability) / paths)
        if not math.isclose(float(saved['standard_error']), expected_se, rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError('Invalid saved Monte Carlo standard error')


def load_rows(run_dir, forecast_dir):
    """Verify provenance and exact joins before producing any output."""
    data = load_dataset(run_dir)
    audit = audit_loaded(data)
    candidate = next(c for c in audit['candidates'] if c['boundary'] == 'left_closed' and c['rounding'] == 'half_up')
    if audit['status'] != 'computed' or any(
            c['mismatches'] or (k != 'expiration_value_matches' and c['unavailable'])
            for k, c in candidate['counts'].items()):
        raise ValueError('Complete and reconcile the historical settlement audit first')
    directory = Path(forecast_dir)
    if (directory / 'report.json').stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Forecast report exceeds size budget')
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    if report['schema_version'] != 1 or report['status'] not in ('completed', 'incomplete'):
        raise ValueError('Forecast run must have a finished evaluation')
    if report['input_manifest_sha256'] != audit['manifest_sha256']:
        raise ValueError('Forecast run belongs to a different historical manifest')
    config = report['config']
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if config_hash != report['config_sha256']:
        raise ValueError('Forecast configuration failed integrity verification')
    expected = {'annual_price_drift': 0, 'seconds_per_year': SECONDS_PER_YEAR,
                'settlement_boundary': 'left_closed', 'rounding': 'half_up', 'round_digits': 2,
                'spot_lag_seconds': 1, 'horizons_minutes': list(HORIZONS), 'log_loss_clip_epsilon': 1e-6}
    if any(config.get(k) != v for k, v in expected.items()):
        raise ValueError('Unsupported forecast model configuration')
    if (type(config['lookback_minutes']) is not int or not 2 <= config['lookback_minutes'] <= 1440
            or type(config['paths']) is not int or not 100 <= config['paths'] <= 20000):
        raise ValueError('Invalid forecast configuration')
    for name in ('predictions.jsonl', 'evaluation.csv'):
        path = directory / name
        if path.stat().st_size > 8 * 1024 * 1024 or digest(path) != report['artifacts'][name]:
            raise ValueError('Forecast artifact failed integrity verification: ' + name)
    markets = {m['ticker']: m for m in data['contracts']}
    predictions = {}
    with (directory / 'predictions.jsonl').open(encoding='utf-8') as handle:
        for line in handle:
            row = json.loads(line)
            key = _key(row)
            if key in predictions or row['ticker'] not in markets or len(predictions) >= MAX_ROWS:
                raise ValueError('Duplicate, unknown or excessive prediction rows')
            _validate_prediction(row, markets[row['ticker']], data['points'], config)
            predictions[key] = row
    quote_rows = read_json(Path(run_dir) / 'quotes.json')
    quotes = quotes_by_key(quote_rows, data['contracts'])
    raw_quotes = {(q['ticker'], q['end_period_ts']): q for q in quote_rows}
    rows, seen = [], set()
    with (directory / 'evaluation.csv').open(newline='', encoding='utf-8') as handle:
        for raw in csv.DictReader(handle):
            row = {k: raw[k] for k in ('ticker',)}
            row.update({k: int(raw[k]) for k in ('horizon_minutes', 'decision_ts', 'outcome')})
            row.update({m: float(raw[m]) for m in MODELS})
            key = _key(row)
            if key in seen or key not in predictions or len(rows) >= MAX_ROWS:
                raise ValueError('Duplicate or unmatched evaluation row')
            seen.add(key)
            prediction, market = predictions[key], markets[row['ticker']]
            if row['outcome'] != int(market['result'] == 'yes'):
                raise ValueError('Evaluation outcome does not match historical contract')
            for model in MODELS:
                losses(row[model], row['outcome'])
                expected_p = (quotes.get((row['ticker'], row['decision_ts'])) if model == 'market'
                              else prediction['models'][model]['probability'])
                if row[model] != expected_p:
                    raise ValueError('Evaluation probability does not match its source')
            quote = raw_quotes[row['ticker'], row['decision_ts']]
            spot = float(prediction['spot'])
            rule = SettlementRule(prediction['reference'], prediction['comparison'], 2, 'half_up')
            row.update(source_ts=prediction['source_ts'], spot=prediction['spot'], reference=prediction['reference'],
                       comparison=prediction['comparison'], gap_dollars=spot - float(rule.reference),
                       effective_cutoff=rule.continuous_cutoff(),
                       yes_bid_close=quote['yes_bid_close'], yes_ask_close=quote['yes_ask_close'])
            # Outcomes and future settlement values are attached only as diagnostic labels.
            settlement = next(c for c in candidate['contracts'] if c['ticker'] == row['ticker'])
            row['closing_average'] = settlement['closing']['rounded']
            row.update(past_features(data['points'], prediction['source_ts'], spot))
            market_loss = losses(row['market'], row['outcome'])
            for model in MODELS:
                score = losses(row[model], row['outcome'])
                row.update({f'{model}_{k}': v for k, v in score.items()})
                if model == 'market':
                    continue
                saved = prediction['models'][model]
                avg_sd, terminal_sd = uncertainty(spot, saved['annual_volatility'],
                                                  prediction['sample_offsets_seconds'], prediction['seconds_to_close_from_source'])
                row.update({f'{model}_annual_volatility': saved['annual_volatility'],
                            f'{model}_mc_standard_error': saved['standard_error'],
                            f'{model}_average_sd_dollars': avg_sd, f'{model}_terminal_sd_dollars': terminal_sd,
                            f'{model}_gap_z': (spot - rule.continuous_cutoff()) / avg_sd if avg_sd else None,
                            f'{model}_excess_brier': score['brier'] - market_loss['brier'],
                            f'{model}_excess_log_loss': score['log_loss'] - market_loss['log_loss']})
            rows.append(row)
    if (len(rows) != report['paired_rows'] or len(predictions) != report['forecast_rows']
            or report['planned_rows'] != len(markets) * len(HORIZONS)):
        raise ValueError('Forecast row counts do not reconcile')
    expected_pairs = {k for k, p in predictions.items() if quotes.get((p['ticker'], p['decision_ts'])) is not None}
    if seen != expected_pairs:
        raise ValueError('Evaluation omits a prediction with a valid matching quote')
    return sorted(rows, key=lambda r: (r['decision_ts'], r['ticker'])), report, audit


def calibration(rows):
    result = []
    for horizon in HORIZONS:
        group = [r for r in rows if r['horizon_minutes'] == horizon]
        for model in MODELS:
            for index in range(10):
                selected = [r for r in group if min(9, int(r[model] * 10)) == index]
                result.append({'horizon_minutes': horizon, 'model': model, 'bin_lower': index / 10,
                               'bin_upper': (index + 1) / 10, 'includes_upper': index == 9,
                               'rows': len(selected), 'mean_probability': mean([r[model] for r in selected]),
                               'yes_rate': mean([r['outcome'] for r in selected])})
    return result


def paired_summary(group, model):
    values = [r[f'{model}_excess_brier'] for r in group]
    return {'rows': len(group), 'contracts': len({r['ticker'] for r in group}),
            'mean_excess_brier': mean(values), 'sum_excess_brier': math.fsum(values),
            'mean_excess_log_loss': mean([r[f'{model}_excess_log_loss'] for r in group]),
            'model_better': sum(v < 0 for v in values), 'market_better': sum(v > 0 for v in values),
            'ties': sum(v == 0 for v in values)}


def panel_summary(rows):
    result = {}
    for horizon in HORIZONS:
        group = [r for r in rows if r['horizon_minutes'] == horizon]
        models = {}
        for model in ('rolling', 'ewma'):
            ordered = sorted(group, key=lambda r: (-r[f'{model}_excess_brier'], r['ticker']))
            worst = ordered[:3]
            extremes = [r for r in group if r[model] in (0, 1)]
            z_groups = {label: [r for r in group if r[f'{model}_gap_z'] is not None and predicate(r[f'{model}_gap_z'])]
                        for label, predicate in [('<-2', lambda z: z < -2), ('[-2,-1)', lambda z: -2 <= z < -1),
                                                 ('[-1,1]', lambda z: -1 <= z <= 1), ('(1,2]', lambda z: 1 < z <= 2),
                                                 ('>2', lambda z: z > 2)]}
            z_groups['zero_variance'] = [r for r in group if r[f'{model}_gap_z'] is None]
            momentum = {}
            for minutes in (1, 2, 5):
                key = f'return_{minutes}m_log'
                momentum[f'{minutes}min'] = {
                    name: paired_summary([r for r in group if predicate(r[key])], model)
                    for name, predicate in [('negative', lambda v: v is not None and v < 0),
                                            ('zero', lambda v: v == 0), ('positive', lambda v: v is not None and v > 0),
                                            ('unavailable', lambda v: v is None)]}
            models[model] = {**paired_summary(group, model),
                'extremes': {'rows': len(extremes), 'wrong': sum(r[model] != r['outcome'] for r in extremes)},
                'worst_three': [{k: r[k] for k in ('ticker', 'decision_ts', 'outcome', model, 'market', f'{model}_excess_brier')}
                                for r in worst],
                'worst_three_sum_excess_brier': math.fsum(r[f'{model}_excess_brier'] for r in worst),
                'remaining_rows': paired_summary(ordered[3:], model),
                'by_gap_z': {k: paired_summary(v, model) for k, v in z_groups.items()},
                'by_past_return_sign': momentum}
        result[f'{horizon}min'] = models
    return result


def markdown_report(report, bins):
    lines = ['# Forecast diagnostics', '',
             f"Paired forecasts: {report['rows']}; distinct contracts: {report['contracts']}.", '',
             'Positive excess loss means the model scored worse than the market midpoint.', '',
             '| Horizon | Model | Brier | Market Brier | Mean excess Brier | Worst 3 sum | Remaining sum |',
             '|---|---|---:|---:|---:|---:|---:|']
    fmt = lambda v: '—' if v is None else f'{v:.6f}'
    for horizon in HORIZONS:
        key = f'{horizon}min'
        scores = report['scores'][key]['models']
        for model in ('rolling', 'ewma'):
            panel = report['panels'][key][model]
            lines.append(f"| {key} | {model} | {fmt(scores[model]['brier'])} | {fmt(scores['market']['brier'])} | "
                         f"{fmt(panel['mean_excess_brier'])} | {fmt(panel['worst_three_sum_excess_brier'])} | "
                         f"{fmt(panel['remaining_rows']['sum_excess_brier'])} |")
    lines += ['', '## Largest excess Brier losses', '',
              '| Horizon | Model | Ticker | Outcome | Model p | Market p | Excess Brier |',
              '|---|---|---|---:|---:|---:|---:|']
    for horizon, models in report['panels'].items():
        for model, panel in models.items():
            for row in panel['worst_three']:
                lines.append(f"| {horizon} | {model} | {row['ticker']} | {row['outcome']} | {row[model]:.4f} | "
                             f"{row['market']:.4f} | {row[model + '_excess_brier']:+.6f} |")
    lines += ['', '## Calibration (nonempty bins)', '',
              '| Horizon | Model | Probability bin | Count | Mean p | YES rate |',
              '|---|---|---|---:|---:|---:|']
    for row in bins:
        if row['rows']:
            bracket = ']' if row['includes_upper'] else ')'
            lines.append(f"| {row['horizon_minutes']}min | {row['model']} | [{row['bin_lower']:.1f},{row['bin_upper']:.1f}{bracket} | "
                         f"{row['rows']} | {row['mean_probability']:.4f} | {row['yes_rate']:.4f} |")
    lines += ['', 'Past-return and standardized-gap panels are in report.json; individual cases are in rows.csv.', '',
              'The worst cases were selected after observing outcomes and remain in every full score. Removing them is only a concentration diagnostic.', '',
              'Bins and horizon panels can be small. Horizons share outcomes; adjacent contracts may also be dependent. '
              'This report supplies descriptive results, with no significance, calibration or profit conclusion.', '',
              'The standardized gap uses the exact variance of the unrounded GBM arithmetic average. '
              'It is a scale diagnostic, not an assumption that the average is normal.', '',
              'Historical timestamps do not establish live availability. The inspected day is development data. '
              'Keep model settings fixed and evaluate additional untouched days.', '']
    return '\n'.join(lines)


def _csv(path, rows):
    with path.open('x', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run_analysis(run_dir, forecast_dir, out_dir):
    output = Path(out_dir)
    if output.exists():
        raise ValueError('Analysis output directory already exists; choose a new path')
    rows, source_report, audit = load_rows(run_dir, forecast_dir)
    if not rows:
        raise ValueError('No paired forecasts to analyze')
    bins = calibration(rows)
    report = {'schema_version': 1, 'package_version': __version__, 'status': 'completed',
              'created_at': datetime.now(timezone.utc).isoformat(), 'rows': len(rows),
              'contracts': len({r['ticker'] for r in rows}), 'source_status': source_report['status'],
              'unpaired_predictions': source_report['forecast_rows'] - len(rows),
              'source_exclusion_counts': source_report.get('exclusion_counts', {}),
              'history_manifest_sha256': audit['manifest_sha256'],
              'forecast_report_sha256': digest(Path(forecast_dir) / 'report.json'),
              'forecast_artifact_hashes': source_report['artifacts'], 'config': source_report['config'],
              'config_sha256': source_report['config_sha256'],
              'point_in_time_certified': False, 'rule_review_required': True,
              'interpretation': 'Descriptive retrospective diagnostics of a fixed model; no tuning or inference of an edge.',
              'scores': summarize(rows), 'panels': panel_summary(rows)}
    output.mkdir(parents=True, exist_ok=False)
    _csv(output / 'rows.csv', rows)
    _csv(output / 'calibration.csv', bins)
    (output / 'summary.md').write_text(markdown_report(report, bins), encoding='utf-8')
    report['artifacts'] = {name: digest(output / name) for name in ('rows.csv', 'calibration.csv', 'summary.md')}
    save_json(output / 'report.json', report)
    return report
